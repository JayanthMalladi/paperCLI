"""Phase 6 batch A (worker finisher): detached launch + per-run lock.

Fully offline: the child is a fake/inline worker or a trivial real
process, so the tests exercise the lock/handshake/heartbeat semantics
(spec sections 4/5/12) without a live worker run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from papercli.jobs.detach import (
    LaunchResult,
    RunLockHeld,
    RunNotStartable,
    HandshakeTimeout,
    acquire_run_lock,
    heartbeat_age_seconds,
    heartbeat_path,
    is_lock_stale,
    launch_worker,
    lock_file_path,
    lock_held,
    pid_alive,
    run_child,
    STALE_AFTER_SECONDS,
)
from papercli.storage.db import PaperStore


def _store(tmp_path) -> PaperStore:
    return PaperStore(tmp_path / "p6d.db")


def _seed_run(store: PaperStore, state: str = "running") -> str:
    return store.create_run(limits={}, state=state,
                            protocol_hash="test-hash")


def _write_heartbeat(store, run_id, *, seconds_old: float = 0.0,
                     pid: int | None = None) -> None:
    from papercli.jobs.detach import _write_json_atomic
    _write_json_atomic(heartbeat_path(store, run_id), {
        "monotonic": time.monotonic(),
        "wall": time.time() - seconds_old,
        "wall_iso": "",
        "pid": pid if pid is not None else os.getpid(),
        "ts": "",
        "source": "test",
    })


def _write_lock_marker(store, run_id, pid: int) -> None:
    from papercli.jobs.detach import _write_json_atomic
    _write_json_atomic(lock_file_path(store, run_id),
                      {"pid": pid, "start_ts": "", "ts": ""})


class _FakeWorker:
    """A canned worker: execute() returns a state, never touches the DAG."""

    def __init__(self, state: str = "completed"):
        self.state = state
        self.guard = None

    def execute(self) -> dict:
        return {"state": self.state}


# ---------------------------------------------------------------------------
# Launch: lock file + handshake
# ---------------------------------------------------------------------------

def test_launch_writes_lock_and_handshake(tmp_path):
    store = _store(tmp_path)
    rid = _seed_run(store)
    guard = {}

    def child_fn(**kw):
        g = acquire_run_lock(store, rid, background_heartbeat=False)
        guard["g"] = g
        from papercli.jobs.detach import _write_json_atomic
        _write_json_atomic(kw["work_dir"] / "heartbeat.json",
                          {"wall": time.time(), "pid": os.getpid(),
                           "ts": "", "monotonic": time.monotonic()})
        _write_json_atomic(kw["work_dir"] / "handshake.json",
                          {"accepted": True, "pid": os.getpid(),
                           "run_id": rid, "start_ts": "",
                           "token": kw.get("handshake_token")})
        return None  # inline child: no separate process

    res = launch_worker(store, rid, child_fn=child_fn)
    assert isinstance(res, LaunchResult)
    assert res.handshake is not None and res.handshake["accepted"] is True
    assert res.pid == os.getpid()

    # The per-run lock marker (pid + start ts) is durable in the run dir.
    lock = lock_file_path(store, rid)
    assert lock.exists()
    marker = json.loads(lock.read_text(encoding="utf-8"))
    assert marker["pid"] == os.getpid()
    assert "start_ts" in marker

    # A second launch on the SAME run while the lock is held is rejected.
    with pytest.raises(RunLockHeld):
        launch_worker(store, rid, child_fn=child_fn)

    # Normal close path: the lock is RELEASED by the holder.
    guard["g"].release()
    assert not lock.exists()
    store.close()


def test_launch_rejects_unknown_and_terminal_runs(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(RunNotStartable):
        launch_worker(store, "run-does-not-exist")
    rid = _seed_run(store, state="completed")
    with pytest.raises(RunNotStartable):
        launch_worker(store, rid)
    rid2 = _seed_run(store, state="cancelled")
    with pytest.raises(RunNotStartable):
        launch_worker(store, rid2)
    store.close()


def test_launch_with_real_child_handshake(tmp_path):
    """A real (trivial) child process writes the handshake: launch_worker
    waits for it and reports accepted via the durable HANDSHAKE (keyed on
    the run, not on pid -- spec 12: a PID alone is never the signal)."""
    store = _store(tmp_path)
    rid = _seed_run(store)
    hs_file = str(heartbeat_path(store, rid).parent / "handshake.json")

    def child_fn(**kw):
        import uuid as _uuid
        tok = kw.get("handshake_token") or _uuid.uuid4().hex
        code = (
            "import json, os, sys\n"
            f"json.dump({{'accepted': True, 'pid': os.getpid(), "
            f"'run_id': {rid!r}, 'token': {tok!r}}}, open({hs_file!r}, 'w'))\n"
        )
        return subprocess.Popen([sys.executable, "-c", code])

    res = launch_worker(store, rid, child_fn=child_fn,
                       detach_timeout_seconds=20.0)
    assert res.handshake is not None
    # Accepted is keyed on the RUN, not the launcher's Popen pid (which on
    # some hosts differs from the child's own getpid -- spec 12).
    assert res.handshake["run_id"] == rid
    assert res.handshake["accepted"] is True
    assert res.process is not None
    assert res.process.wait(timeout=10.0) == 0
    store.close()


def test_handshake_timeout_is_typed(tmp_path):
    """A child that never handshakes: the launch is NOT accepted
    (spec section 6/12)."""
    store = _store(tmp_path)
    rid = _seed_run(store)
    code = "import time\ntime.sleep(1.5)\n"

    def child_fn(**kw):
        return subprocess.Popen([sys.executable, "-c", code])

    with pytest.raises(HandshakeTimeout):
        launch_worker(store, rid, child_fn=child_fn,
                     detach_timeout_seconds=0.4)
    store.close()


# ---------------------------------------------------------------------------
# Heartbeat age: RUNNING vs STALE (never from a PID alone, spec 12)
# ---------------------------------------------------------------------------

def test_fresh_heartbeat_reports_running(tmp_path):
    store = _store(tmp_path)
    rid = _seed_run(store)
    g = acquire_run_lock(store, rid, background_heartbeat=False)
    _write_heartbeat(store, rid)
    try:
        # Injected clock (like the worker tests): a heartbeat advanced a
        # little is still RUNNING, not stale.
        t0 = time.time()

        def now_fn():
            return t0 + 5.0

        info = lock_held(store, rid, now_fn=now_fn)
        assert info["held"] is True
        assert info["stale"] is False
        assert info["holder_pid"] == os.getpid()
        assert is_lock_stale(store, rid, now_fn=now_fn) is False
    finally:
        g.release()
    store.close()


def test_stale_heartbeat_flags_possibly_interrupted(tmp_path):
    store = _store(tmp_path)
    rid = _seed_run(store)
    g = acquire_run_lock(store, rid, background_heartbeat=False)
    # Stop the child's heartbeat and age it past the threshold: even
    # though the holder PID is still alive, the run is STALE -- possibly
    # interrupted, not 'running' (spec section 12).
    _write_heartbeat(store, rid, seconds_old=STALE_AFTER_SECONDS + 60.0)
    info = lock_held(store, rid)
    assert info["held"] is True
    assert info["stale"] is True
    assert "heartbeat stale" in info["reason"]
    assert heartbeat_age_seconds(store, rid) > STALE_AFTER_SECONDS
    g.release()
    store.close()


def test_dead_process_lock_is_reclaimable(tmp_path):
    """Kill the process: its marker names a dead PID -> a resume can
    reclaim the lock (spec section 12)."""
    store = _store(tmp_path)
    rid = _seed_run(store)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not pid_alive(proc.pid)

    _write_lock_marker(store, rid, proc.pid)
    _write_heartbeat(store, rid, seconds_old=STALE_AFTER_SECONDS + 30.0,
                    pid=proc.pid)
    assert lock_held(store, rid)["stale"] is True
    assert is_lock_stale(store, rid) is True

    # The reclaim path: stale lock is detected and released so a resume
    # (re-launch) can hold it.
    g = acquire_run_lock(store, rid, background_heartbeat=False)
    try:
        marker = json.loads(lock_file_path(store, rid).read_text(
            encoding="utf-8"))
        assert marker["pid"] == os.getpid()
        assert lock_held(store, rid,
                         now_fn=lambda: time.time() + 5)["stale"] is False
    finally:
        g.release()
    store.close()


# ---------------------------------------------------------------------------
# The child's main: handshake + lock release on normal close (spec 12)
# ---------------------------------------------------------------------------

def test_run_child_writes_handshake_and_releases_lock(tmp_path):
    store = _store(tmp_path)
    rid = _seed_run(store)
    factory = lambda s, r, g: _FakeWorker("completed")
    rc = run_child(store, rid, worker_factory=factory)
    assert rc == 0
    hs = json.loads(
        (heartbeat_path(store, rid).parent / "handshake.json")
        .read_text(encoding="utf-8"))
    assert hs["accepted"] is True
    # The lock was RELEASED by the child on normal close.
    assert not lock_file_path(store, rid).exists()
    assert not is_lock_stale(store, rid)

    # A partial closure exits 6 (documented: run completed partially).
    rid2 = _seed_run(store)
    rc2 = run_child(store, rid2,
                   worker_factory=lambda s, r, g: _FakeWorker("partial"))
    assert rc2 == 6
    assert not lock_file_path(store, rid2).exists()
    store.close()


def test_run_child_internal_failure_rejects_handshake(tmp_path):
    store = _store(tmp_path)
    rid = _seed_run(store)

    class _Broken:
        def __init__(self, *a, **k):
            pass

        def execute(self):
            raise RuntimeError("boom")

    rc = run_child(store, rid, worker_factory=lambda s, r, g: _Broken())
    assert rc == 1
    hs = json.loads(
        (heartbeat_path(store, rid).parent / "handshake.json")
        .read_text(encoding="utf-8"))
    assert hs["accepted"] is False
    assert "boom" in str(hs.get("error"))
    # Even a crashed child releases its lock on the normal-close paths
    # (a hard kill would instead leave a STALE lock, covered above).
    assert not lock_file_path(store, rid).exists()
    store.close()
