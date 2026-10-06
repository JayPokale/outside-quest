"""Durable photo checking with Temporal (optional).

Checking a walk is slow: ~30 s per photo on a laptop GPU, so 40 photos is 20 minutes. In the plain mode the
browser loops over the photos, and closing the tab, a laptop going to sleep or Ollama running out of memory loses
the rest of the batch. In durable mode each walk is a Temporal workflow and each photo is an activity:

- the photos are saved to disk first, so the browser can close straight away;
- a failed model call (Ollama down, out of memory, broken JSON) is retried with backoff instead of skipped;
- if the server is killed, the workflow picks up at the next unchecked photo when it comes back.

Everything still runs on your machine: `temporal server start-dev` is the open-source Temporal server.
"""
import asyncio
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.service import RPCError
from temporalio.worker import Worker

TEMPORAL_ADDRESS = os.environ.get("QUEST_TEMPORAL", "127.0.0.1:7233")
TASK_QUEUE = "outside-quest"
WALKS = Path(os.environ.get("QUEST_DATA", Path.home() / ".outside-quest")) / "walks"
WALK_ID = re.compile(r"^[a-z0-9-]{8,40}$")


def walk_dir(walk_id: str) -> Path:
    if not WALK_ID.match(walk_id):
        raise ValueError("bad walk id")
    return WALKS / walk_id


@activity.defn
def check_one(walk_id: str, name: str, quests: list) -> dict:
    """Ask the local model about one saved photo. Idempotent: a finished answer is cached next to the photo."""
    import server  # imported here so the workflow sandbox never loads Pillow or urllib

    folder = walk_dir(walk_id)
    done = folder / f"{name}.result.json"
    if done.exists():
        return json.loads(done.read_text())
    try:
        jpeg = (folder / f"{name}.jpg").read_bytes()
    except FileNotFoundError:
        raise ApplicationError(f"photo {name} is missing", non_retryable=True)
    try:
        result = server.check_photo(jpeg, quests)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"model returned broken JSON: {e}")  # retryable: temperature 0 still varies on retry
    except OSError as e:
        raise RuntimeError(f"local model unreachable: {e}")  # retryable: Ollama restarting or out of memory
    done.write_text(json.dumps(result))
    return result


@workflow.defn(sandboxed=False)  # the workflow body only loops and awaits, so it is deterministic
class CheckWalk:
    def __init__(self):
        self.results, self.failed, self.total = [], [], 0

    @workflow.run
    async def run(self, walk_id: str, names: list, quests: list) -> dict:
        self.total = len(names)
        for name in names:
            try:
                r = await workflow.execute_activity(
                    check_one, args=[walk_id, name, quests],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2,
                                             maximum_interval=timedelta(minutes=2), maximum_attempts=30),
                )
                self.results.append({"name": name, **r})
            except ActivityError as e:
                self.failed.append({"name": name, "error": str(e.cause or e)[:200]})
        return self.progress()

    @workflow.query
    def progress(self) -> dict:
        return {"total": self.total, "results": self.results, "failed": self.failed}


class Durable:
    """Runs a Temporal client and worker on a background asyncio loop, for the threaded HTTP server."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.client = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def _run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def connect(self) -> bool:
        async def go():
            self.client = await Client.connect(TEMPORAL_ADDRESS)
            worker = Worker(self.client, task_queue=TASK_QUEUE, workflows=[CheckWalk], activities=[check_one],
                            activity_executor=ThreadPoolExecutor(1))  # one photo at a time: one GPU
            asyncio.ensure_future(worker.run())
        try:
            self._run(go(), timeout=5)
            return True
        except Exception:
            return False

    def start(self, walk_id: str, names: list, quests: list):
        walk_dir(walk_id)

        async def go():
            try:
                await self.client.start_workflow(CheckWalk.run, args=[walk_id, names, quests],
                                                 id=f"walk-{walk_id}", task_queue=TASK_QUEUE)
            except RPCError as e:
                if "already" not in str(e).lower():  # same walk started twice (page reload): keep the first
                    raise
        self._run(go())

    def progress(self, walk_id: str) -> dict:
        walk_dir(walk_id)

        async def go():
            h = self.client.get_workflow_handle(f"walk-{walk_id}")
            p = await h.query(CheckWalk.progress)
            desc = await h.describe()
            p["running"] = desc.status == WorkflowExecutionStatus.RUNNING
            return p
        return self._run(go())
