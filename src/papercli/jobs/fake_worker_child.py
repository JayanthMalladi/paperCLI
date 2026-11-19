"""The offline worker-child test seam (Phase 6 batch A-3).

The production detached child is ``python -m papercli.jobs.worker``. For
offline CLI tests the launcher's environment carries
``PAPERS_WORKER_MODULE=papercli.jobs.fake_worker_child`` so the child runs
THIS module instead: the same ``run_child`` protocol (acquire lock,
durable handshake stamped with the launch nonce, heartbeat, lock release
on the normal close path, one log line) but a TRIVIAL task DAG instead of
the live worker, so ``papers review run --detach`` is exercisable without
any network or model endpoint.

It is a documented test seam, never the production default (the launcher
only swaps the module when that environment variable is set).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from papercli.jobs.detach import (
    _write_json_atomic,
    run_child,
    utc_now_iso,
)
from papercli.storage.db import PaperStore

__all__ = ["FakeWorker", "main"]


class FakeWorker:
    """A tiny worker: builds a small DAG, runs one task, closes.

    Mirrors the production worker's durable protocol (handshake +
    heartbeat + cancel request + log) at trivial cost."""

    def __init__(self, store: PaperStore, run_id: str, *,
                 lock_guard: Any = None,
                 heartbeat_interval: float = 1.0):
        self.store = store
        self.run_id = run_id
        self.lock_guard = lock_guard
        self.heartbeat_interval = heartbeat_interval
        from papercli.exports.run_summary import run_dir
        self.work_dir = run_dir(store, run_id)
        self._cancel = self.work_dir / "cancel-request"
        self._log_file = self.work_dir / "worker.log.jsonl"
        self._cancel_requested = False

    # -- the tiny DAG ---------------------------------------------------
    def build_dag(self) -> dict[str, str]:
        from papercli.jobs.tasks import TaskQueue
        q = TaskQueue(self.store)
        # A configurable number of tiny tasks keeps the worker busy long
        # enough for `papers review cancel` to catch a live worker.
        n = int(os.environ.get("PAPERS_FAKE_WORKER_TASKS", "2") or 2)
        prev = None
        ids: dict[str, str] = {}
        for i in range(max(1, n)):
            t = q.enqueue(
                self.run_id, "discover", f"discover:fake-{i}",
                payload={"input_tag": f"fake-{i}", "provider": "crossref",
                         "query": "fake"})
            if prev:
                with self.store._conn:
                    self.store._conn.execute(
                        "UPDATE tasks SET dependencies_json=? WHERE id=?",
                        (json.dumps([prev]), t.task_id))
            prev = t.task_id
            ids[f"discover-{i}"] = t.task_id
        t2 = q.enqueue(self.run_id, "screen", "screen:fake-0",
                       payload={"input_tag": "fake-0"},
                       dependencies=[prev] if prev else [])
        ids["screen"] = t2.task_id
        return ids

    def _log(self, event: str, data: dict[str, Any]) -> None:
        envelope = {"schema_version": "1.0", "status": "ok",
                    "data": {"event": event, **data,
                              "run_id": self.run_id,
                              "ts": utc_now_iso()},
                    "warnings": []}
        with self._log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope, default=str) + "\n")
            fh.flush()

    def _heartbeat(self) -> None:
        _write_json_atomic(self.work_dir / "heartbeat.json", {
            "monotonic": time.monotonic(), "wall": time.time(),
            "wall_iso": utc_now_iso(), "pid": os.getpid(),
            "ts": utc_now_iso(),
        })

    def cancel_requested(self) -> bool:
        if self._cancel.exists():
            self._cancel_requested = True
        return self._cancel_requested

    def execute(self) -> dict[str, Any]:
        from papercli.jobs.tasks import TaskQueue
        from papercli.exports.run_summary import (
            build_run_summary, write_run_summary)

        q = TaskQueue(self.store)
        # A test hold (seconds between tasks) keeps the run "running" long
        # enough for `papers review cancel` to catch a live worker.
        hold = float(os.environ.get("PAPERS_FAKE_WORKER_HOLD_SECONDS", "0")
                    or 0)
        self.build_dag()
        self._heartbeat()
        self._log("worker_started", {"fake": True, "hold": hold})
        for _ in range(8):
            if self.cancel_requested():
                self._log("cancel_requested", {})
                break
            task = q.claim(self.run_id, "fake-worker", limit=1)
            if not task:
                break
            task = task[0]
            q.start(self.run_id, task.task_id, "fake-worker")
            q.complete(self.run_id, task.task_id, "fake-worker",
                      result={"task": task.type, "ok": True})
            self._heartbeat()
            self._log("task_completed", {"task": task.task_id})
            if hold > 0:
                # Poll for cancellation while holding (bounded child work,
                # spec 12): the cancel request is honored at the poll.
                end = time.monotonic() + hold
                while time.monotonic() < end:
                    if self.cancel_requested():
                        break
                    time.sleep(0.1)
                if self.cancel_requested():
                    self._log("cancel_requested", {})
                    break
        state = "cancelled" if self._cancel_requested else "completed"
        # Close the run row FIRST (terminal state + ended_at), then build
        # + write the summary (which reads the run row) -- the same order
        # the production worker uses, so the summary's state is terminal.
        self.store.update_run(self.run_id, state=state,
                              ended_at=utc_now_iso())
        summary = build_run_summary(self.store, self.run_id)
        write_run_summary(self.store, self.run_id, summary,
                         self.work_dir / "run-summary.json")
        self._heartbeat()
        self._log("run_closed", {"state": state})
        if self.lock_guard is not None:
            self.lock_guard.release()
        return summary


def _fake_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="papercli.jobs.fake_worker_child")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--heartbeat-interval", type=float, default=1.0)
    parser.add_argument("--handshake-token", default=None)
    ns = parser.parse_args(argv)

    store = PaperStore(Path(ns.db_path))
    try:
        return run_child(
            store, ns.run_id,
            worker_factory=lambda s, r, g: FakeWorker(
                s, r, lock_guard=g,
                heartbeat_interval=ns.heartbeat_interval),
            heartbeat_interval=ns.heartbeat_interval,
            handshake_token=ns.handshake_token,
        )
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    return _fake_main(argv)


if __name__ == "__main__":
    raise SystemExit(_fake_main())
