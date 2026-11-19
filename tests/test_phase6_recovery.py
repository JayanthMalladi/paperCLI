"""Phase 6 batch A (worker finisher): explicit run recovery.

Offline: resume is driven with fake/in-process child handlers; the
interrupted-run fixture mirrors the Phase-6 worker tests (succeeded +
retry_wait + a dead worker's stale lock/heartbeat).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from papercli.jobs.detach import (
    _write_json_atomic,
    acquire_run_lock,
    heartbeat_path,
    lock_file_path,
)
from papercli.jobs.recovery import BriefChangedError, ResumeResult, resume_run
from papercli.jobs.tasks import TaskQueue
from papercli.storage.db import PaperStore
from tests.test_phase6_worker import _fake_documents


def _store(tmp_path) -> PaperStore:
    return PaperStore(tmp_path / "p6r.db")


def _seed_run(store: PaperStore, state: str = "running") -> str:
    """A run row in the given state (the worker's input protocol is the
    empty default -- these tests exercise the recovery path, not the
    DAG)."""
    return store.create_run(limits={}, state=state,
                            protocol_hash="test-hash")


def _dead_pid() -> int:
    """Run a trivial process and use its now-dead PID as the crashed
    worker's pid."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


#: The brief a run is planned from. The fixture saves it on the run's
#: plan (``brief_ref.sha256`` via the plan module's own digest) so the
#: changed-brief preflight has the SAVED hash to compare against.
SAVED_BRIEF = {
    "schema_version": "1.0",
    "question": "Does aerobic exercise training improve cognition in adults?",
    "purpose": "narrative review",
    "scope": "aerobic exercise training in adults",
    "outputs": ["md"],
    "limits": {},
}


def _interrupted_run(store) -> tuple[str, str]:
    """Seed a mid-DAG interruption: 3 discover tasks succeeded, one
    retry_wait (a failed-but-retriable task), plus a dead worker's stale
    heartbeat + lock marker. Mirrors the Phase-6 forced-interruption
    fixture. Returns (run_id, retry_task_id)."""
    rid = _seed_run(store)
    # Save the brief's ref (same digest the plan module computes) so the
    # resume preflight can detect a CHANGED brief.
    from papercli.jobs.recovery import brief_hash

    with store._conn:
        store._conn.execute(
            "UPDATE runs SET plan_json=? WHERE id=?",
            (json.dumps({
                "brief_ref": {"sha256": brief_hash(SAVED_BRIEF),
                              "path": "tests/fake-brief.yaml"},
            }, sort_keys=True), rid))
    _fake_documents(store, rid)
    q = TaskQueue(store)
    for i in range(4):
        q.enqueue(rid, "discover", f"discover:query-{i}",
                 payload={"input_tag": f"query-{i}", "query_index": i,
                          "provider": "crossref", "query": f"q{i}"})
    tasks = q.list(rid)
    keep, retry = tasks[:3], tasks[3]
    past = "2000-01-01T00:00:00.000+00:00"
    dead = _dead_pid()
    with store._conn:
        for t in keep:
            store._conn.execute(
                "UPDATE tasks SET state='succeeded', result_hash=?, "
                "updated_at=? WHERE id=?",
                (f"sha-{t.task_id}", past, t.task_id))
        store._conn.execute(
            "UPDATE tasks SET state='retry_wait', lease_owner=NULL, "
            "lease_expires_at=NULL, next_attempt_at=?, result_hash=NULL "
            "WHERE id=?", (past, retry.task_id))
    # The crashed worker's durable state: stale heartbeat + its lock.
    _write_json_atomic(heartbeat_path(store, rid), {
        "monotonic": 0.0, "wall": time.time() - 900.0, "wall_iso": "",
        "pid": dead, "ts": past, "source": "test"})
    _write_json_atomic(lock_file_path(store, rid),
                      {"pid": dead, "start_ts": past, "ts": past})
    return rid, retry.task_id


def test_resume_re_dispatches_only_unfinished(tmp_path):
    store = _store(tmp_path)
    rid, retry_id = _interrupted_run(store)
    before = {t.task_id: t.result_hash for t in TaskQueue(store).list(rid)}
    succeeded_before = {tid: h for tid, h in before.items() if h is not None}
    assert len(succeeded_before) == 3

    def child_fn(**kw):
        # The re-launched worker holds the (reclaimed) lock + handshakes.
        g = acquire_run_lock(store, rid, background_heartbeat=False)
        g.release()
        _write_json_atomic(kw["work_dir"] / "handshake.json",
                          {"accepted": True, "pid": os.getpid(),
                           "run_id": rid, "start_ts": ""})
        return None

    res = resume_run(store, rid, child_fn=child_fn)
    assert isinstance(res, ResumeResult)
    assert res.launched is True
    assert res.resumed is True
    assert any("stale" in n for n in res.notes)

    # Succeeded result hashes are UNCHANGED (idempotent, spec 11); the
    # unfinished task is back in a re-dispatchable state.
    after = {t.task_id: t for t in TaskQueue(store).list(rid)}
    for tid, h in succeeded_before.items():
        assert after[tid].result_hash == h,             f"completed task {tid} was mutated by the resume"
    assert after[retry_id].state in ("pending", "retry_wait", "succeeded")
    store.close()


def test_resume_changed_brief_requires_new_revision(tmp_path):
    store = _store(tmp_path)
    rid, _ = _interrupted_run(store)
    launched = {}

    def child_fn(**kw):
        launched["yes"] = True
        _write_json_atomic(kw["work_dir"] / "handshake.json",
                          {"accepted": True, "pid": os.getpid(),
                           "run_id": rid, "start_ts": ""})
        return None

    # The SAVED brief passes the preflight (digests match) and re-launches.
    res0 = resume_run(store, rid, brief=SAVED_BRIEF, child_fn=child_fn)
    assert res0.launched is True
    assert res0.state in ("running", "queued", "planned")

    # A CHANGED brief (different question -> different digest than the
    # saved brief_ref): resume must REFUSE with the typed new-revision
    # error, before any re-launch or task mutation.
    changed = json.loads(json.dumps(SAVED_BRIEF))
    changed["question"] = "A completely different question?"
    launched.clear()  # the refused resume below must add nothing
    with pytest.raises(BriefChangedError) as excinfo:
        resume_run(store, rid, brief=changed, child_fn=child_fn)
    msg = str(excinfo.value).lower()
    assert "new" in msg and "revision" in msg
    assert excinfo.value.details.get("guidance")
    assert "yes" not in launched, "a refused resume must not launch"
    store.close()


def _never_launch(**kw):
    raise AssertionError("a terminal-state resume must not launch a worker")


def test_resume_clean_completed_run_is_noop(tmp_path):
    store = _store(tmp_path)
    rid = _seed_run(store, state="completed")
    res = resume_run(store, rid, child_fn=_never_launch)
    assert res.launched is False
    assert res.resumed is False
    assert res.state == "completed"
    assert res.pid is None
    store.close()


def test_resume_partial_run_reports_state(tmp_path):
    """A partial (terminal) run is not relaunched by resume: it reports
    its state and points at a new revision for a changed brief."""
    store = _store(tmp_path)
    rid = _seed_run(store, state="partial")
    res = resume_run(store, rid, child_fn=_never_launch)
    assert res.launched is False
    assert res.state == "partial"
    store.close()
