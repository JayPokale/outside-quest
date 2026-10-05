#!/usr/bin/env python3
"""Outside Quest: an offline photo scavenger hunt that gets you outdoors.

1. Before the walk, a local Gemma model writes a short seasonal quest card.
2. During the walk you only take photos.
3. Afterwards, Gemma checks which quests each photo completes and builds a
   walk journal (timeline + route from the photos' EXIF data).

Runs entirely on your machine with Ollama. Only dependency: Pillow (for EXIF
and resizing). Nothing is uploaded anywhere.
"""
import base64
import io
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

OLLAMA_URL = os.environ.get("QUEST_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("QUEST_MODEL", "gemma4:e2b")
HOST = os.environ.get("QUEST_HOST", "127.0.0.1")
PORT = int(os.environ.get("QUEST_PORT", "8777"))
STATIC = Path(__file__).parent / "static"
MAX_BODY = 25 * 1024 * 1024

# Quests the model writes are filtered through plain code before anyone sees
# them. Anything that sends people somewhere unsafe, or near wildlife, is
# swapped for one of these.
SAFE_POOL = [
    ("A bird", "🐦", 10),
    ("An insect on a flower", "🐝", 15),
    ("A leaf bigger than your hand", "🍃", 10),
    ("A mushroom or fungus (photo only, don't touch)", "🍄", 20),
    ("A cloud that looks like an animal", "☁️", 15),
    ("Something bright red in nature", "🔴", 10),
    ("Tree bark with an interesting texture", "🌳", 10),
    ("A flower you have never noticed before", "🌼", 15),
    ("Running water: a stream, fountain or river", "💧", 20),
    ("A spider web", "🕸️", 20),
    ("Moss or lichen on a stone or wall", "🪨", 15),
    ("A seed, pod or fallen fruit on the ground", "🌰", 15),
]
UNSAFE = re.compile(
    r"\b(climb|cliff|edge|swim|wade|enter the water|touch|pick|eat|taste|feed|"
    r"pet|chase|nest|snake|trespass|private|night|alone|road|highway|rail|track)\w*",
    re.I,
)
PHOTO_ONLY = re.compile(r"\b(mushroom|fungus|fungi|berry|berries|nest|egg)s?\b", re.I)


def ollama_chat(messages, schema=None, temperature=0.2, timeout=900):
    payload = {
        "model": MODEL,
        "stream": False,
        "options": {"temperature": temperature},
        "messages": messages,
    }
    if schema:
        payload["format"] = schema
    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["message"]["content"]


# ---------------------------------------------------------------- quests ---

QUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "quests": {
            "type": "array",
            "minItems": 6,
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "emoji": {"type": "string"},
                    "points": {"type": "integer"},
                },
                "required": ["text", "emoji", "points"],
            },
        },
    },
    "required": ["title", "quests"],
}


def quick_quests():
    picks = random.sample(SAFE_POOL, 6)
    return {"title": "Quick Quest", "quests": [
        {"id": f"q{i}", "text": t, "emoji": e, "points": p} for i, (t, e, p) in enumerate(picks, 1)]}


def make_quests(place: str, month: str, who: str):
    prompt = (
        f"Write a photo scavenger hunt for a 30-60 minute walk.\n"
        f"Where: {place or 'a local park or neighbourhood'}. Month: {month}. Who: {who or 'anyone'}.\n"
        "Six quests. Each must be something you can find outdoors in that place and season and "
        "prove with ONE photo, and must be visible without touching anything. Mix easy and hard. "
        "Think about the real local climate and season there: tropical places have no autumn colours "
        "or snow, and monsoon, dry and winter seasons look different. Prefer living things and nature "
        "over man-made objects. "
        "Use short, concrete wording (under 10 words), one emoji each (in the emoji field, not the text), "
        "points 10-25 (harder = more). "
        "Also give the hunt a fun title under 6 words."
    )
    try:
        data = json.loads(
            ollama_chat([{"role": "user", "content": prompt}], QUEST_SCHEMA, temperature=0.8, timeout=300)
        )
        raw = data.get("quests", [])
        title = str(data.get("title") or "Outside Quest")[:60]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        raw, title = [], "Outside Quest"

    quests, seen = [], set()
    for q in raw:
        # drop emoji/symbols the model sometimes repeats inside the text, and trailing dots
        text = re.sub(r"[^\w\s,'()/:-]+", "", str(q.get("text", ""))).strip().rstrip(".")[:80]
        # "don't touch" is a safety note, not an instruction to touch
        checked = re.sub(r"\b(don'?t|do not|without|no)\s+touch\w*", "", text, flags=re.I)
        if not text or UNSAFE.search(checked) or text.lower() in seen:
            continue
        if PHOTO_ONLY.search(text) and "photo only" not in text.lower():
            text += " (photo only, don't touch)"
        pts = max(5, min(30, int(q.get("points") or 10)))
        quests.append({"text": text, "emoji": str(q.get("emoji") or "📸")[:4], "points": pts})
        seen.add(text.lower())
    pool = [p for p in SAFE_POOL if p[0].lower() not in seen]
    random.shuffle(pool)
    while len(quests) < 6 and pool:  # model failed or a quest was filtered
        text, emoji, pts = pool.pop()
        quests.append({"text": text, "emoji": emoji, "points": pts})
    for i, q in enumerate(quests[:6], 1):
        q["id"] = f"q{i}"
    return {"title": title, "quests": quests[:6]}


# ---------------------------------------------------------------- photos ---

GPS_TAG = next(k for k, v in ExifTags.TAGS.items() if v == "GPSInfo")


def _to_deg(v, ref):
    d, m, s = (float(x) for x in v)
    deg = d + m / 60 + s / 3600
    return -deg if ref in ("S", "W") else deg


def read_photo(data: bytes):
    """Return (jpeg bytes resized for the model, taken_at ISO or None, (lat, lon) or None)."""
    img = Image.open(io.BytesIO(data))
    exif = img.getexif()
    taken = None
    raw_time = exif.get_ifd(ExifTags.IFD.Exif).get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
    if raw_time:
        try:
            taken = datetime.strptime(str(raw_time), "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    gps = None
    g = exif.get_ifd(GPS_TAG)
    if g and 2 in g and 4 in g:
        try:
            gps = (round(_to_deg(g[2], g.get(1, "N")), 6), round(_to_deg(g[4], g.get(3, "E")), 6))
        except (TypeError, ValueError, ZeroDivisionError):
            gps = None
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((1024, 1024))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue(), taken, gps


def check_schema(ids):
    return {
        "type": "object",
        "properties": {
            "what_i_see": {"type": "string"},
            "main_subject": {"type": "string"},
            "checks": {
                "type": "array",
                "minItems": len(ids),
                "maxItems": len(ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "evidence": {"type": "string"},
                        "is_main_subject": {"type": "boolean"},
                        "completed": {"type": "boolean"},
                    },
                    "required": ["id", "evidence", "is_main_subject", "completed"],
                },
            },
        },
        "required": ["what_i_see", "main_subject", "checks"],
    }


def check_photo(jpeg: bytes, quests):
    ids = [q["id"] for q in quests]
    by_id = {q["id"]: q for q in quests}
    qtext = "\n".join(f'{q["id"]}: {q["text"]}' for q in quests)
    content = ollama_chat(
        [
            {
                "role": "system",
                "content": "You judge an outdoor photo scavenger hunt. Describe the photo in one friendly sentence "
                "and name its main subject. Then go through EVERY quest: describe what in this photo relates to it "
                "(or say what is missing). Set is_main_subject to true only if the quest is about the main subject "
                "of the photo, and completed to true only if the quest is clearly fulfilled. Background greenery does not count for a leaf or plant "
                "quest unless the leaves or plant are the focus. Do not guess exact species.",
            },
            {"role": "user", "content": "Quests:\n" + qtext, "images": [base64.b64encode(jpeg).decode()]},
        ],
        check_schema(ids),
        temperature=0,
    )
    data = json.loads(content)
    done, seen = [], set()
    for c in data.get("checks", []):
        qid, ev = c.get("id"), str(c.get("evidence", "")).strip()
        # Two explicit yes/no answers per quest, both required: is it in the photo, and is it what the
        # photo is mainly of? This stops background leaves or sky from earning points.
        if (c.get("completed") is True and c.get("is_main_subject") is True
                and qid in by_id and qid not in seen and len(ev) > 10):
            done.append({"id": qid, "evidence": ev[:200]})
            seen.add(qid)
    return {"what_i_see": str(data.get("what_i_see", "")).strip()[:300],
            "main_subject": str(data.get("main_subject", "")).strip()[:80], "completed": done}


# ------------------------------------------------------------------ http ---

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("quest: " + fmt % args + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        if self.path == "/api/health":
            try:
                with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3) as r:
                    names = [m["name"] for m in json.load(r).get("models", [])]
                return self._json(200, {"ok": MODEL in names, "model": MODEL})
            except (urllib.error.URLError, OSError):
                return self._json(200, {"ok": False, "model": MODEL, "error": "Ollama is not running"})
        name = "index.html" if self.path in ("/", "/index.html") else self.path.lstrip("/").split("?")[0]
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self._json(404, {"error": "not found"})
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".png": "image/png"}.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self._body()
        if data is None:
            return self._json(400, {"error": "bad request"})
        try:
            if self.path == "/api/quests" and data.get("quick"):
                return self._json(200, quick_quests())
            if self.path == "/api/quests":
                month = data.get("month") or datetime.now().strftime("%B")
                return self._json(200, make_quests(str(data.get("place", ""))[:80], str(month)[:20],
                                                   str(data.get("who", ""))[:60]))
            if self.path == "/api/check":
                quests = [q for q in data.get("quests", []) if isinstance(q, dict) and "id" in q][:8]
                raw = base64.b64decode(str(data.get("image", "")).split(",", 1)[-1], validate=True)
                jpeg, taken, gps = read_photo(raw)
                result = check_photo(jpeg, quests)
                result.update(taken_at=taken, gps=gps,
                              thumb="data:image/jpeg;base64," + base64.b64encode(jpeg).decode())
                return self._json(200, result)
        except (ValueError, OSError) as e:
            return self._json(400, {"error": f"Could not read that photo ({type(e).__name__})"})
        except urllib.error.URLError:
            return self._json(503, {"error": "The local model is not running. Start Ollama."})
        return self._json(404, {"error": "not found"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Outside Quest at http://{HOST}:{PORT}  (model: {MODEL}, offline)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
