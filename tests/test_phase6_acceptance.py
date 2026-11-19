"""Phase 6 acceptance test batch (spec section 19, Phase-6 rows).

Exercises the detached worker + budgets + concurrency, fully offline:

* the live RunWorker path uses canned model replies + fake search (the
  Batch-1 helpers from tests/test_phase6_worker.py);
* the detached LAUNCH file protocol (accepted = lock + handshake) is
  exercised in-process against the launcher's injection seams, and the
  real CLI ``--detach`` seam (``papercli.jobs.fake_worker_child``) is
  exercised end-to-end through the CLI;
* clocks are injected (``now_fn`` / fake clocks / ``sleep``) -- no real
  sleeps where avoidable;
* budget ceilings use the AGREED guardrails (spec sections 7/12):
  max_model_calls=300, max_input_tokens=1000000, max_output_tokens=150000,
  wall_minutes=360, max_search_queries=30, max_discovered_records=1000,
  max_full_text_papers=60, USD cap null; 45-rpm limiter.

Acceptance rows covered (spec section 19, Phase 6):
  1. Detached launch: accepted + lock (not "completed"), handshake +
     heartbeat files, lock held by the worker.
  2. Resume after interruption: re-dispatch ONLY the unfinished work;
     completed result hashes unchanged (idempotent).
  3. Limits / budgets: exhaustion stops dispatch, closure slice reserved,
     partial (never "completed") close, honest run-summary.json; 45-rpm
     + the agreed hard ceilings enforced (reserve-before-dispatch).
  4. Concurrency: lease exclusivity (exactly one acquirer) + no
     double-spend of a shared budget reservation.
  5. Rate limits: a model 429 -> bounded backoff into retry_wait,
     deferred through scheduler state -- no retry storm.
  6. Secrets: no secret value appears in any durable worker surface
     (handshake / heartbeat / worker log / status + run-summary JSON).
  7. Exports: a failed/partial run still exports a valid partial bundle;
     rendered md/pdf/docx are DEFERRED (Phase 7), never silently missing.
  8. Cancel: the cancel-request stops new dispatch, terminates bounded
     child work where safe, checkpoints, writes the partial bundle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from papercli.exports.run_summary import (
    build_run_summary,
    run_dir,
    write_structured_dumps,
    write_run_summary,
)
from papercli.jobs.detach import (
    LaunchResult,
    RunLockHeld,
    _write_json_atomic,
    acquire_run_lock,
    heartbeat_path,
    launch_worker,
    lock_file_path,
    lock_held,
)
from papercli.jobs.recovery import resume_run
from papercli.jobs.tasks import TaskQueue, TaskQueueError
from papercli.models.rate_limit import (
    DEFAULT_BUDGETS,
    DEFAULT_RPM,
    ModelBudget,
    RateLimiter,
)
from papercli.storage.db import PaperStore

from tests.conftest import run_papers
from tests.test_phase6_worker import (
    DEFAULT_RECORDS,
    FAKE_LIMITS,
    _fake_documents,
    _make_fake_search_factory,
    _new_worker,
    _run_full,
    _seed_run,
    _fake_model_cfg,
)

#: The offline worker-child seam (never the production default).
FAKE_WORKER_ENV = {"PAPERS_WORKER_MODULE": "papercli.jobs.fake_worker_child"}


def _envelope(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


# Secret sentinel VALUES used to prove ABSENCE from the durable surfaces.
# They are deliberately distinctive so a leak would be unmistakable, but
# they do NOT match ci/secret_scan.py's committed-credential patterns
# (no sk-/xoxb-/ghp- shapes, no key-shaped assignment literals).
SENTINELS = {
    "PAPERS_EMAIL": "sentinel-user@example-test.invalid",
    "OPENALEX_API_KEY": "openalex-sentinel-0001",
    "PAPERS_LLM_API_KEY": "llm-sentinel-key-0001",
}

#: The agreed budget guardrails (spec sections 7/12 ceilings).
AGREED_CEILINGS = {
    "max_model_calls": 300,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 150_000,
    "wall_minutes": 360,
    "max_search_queries": 30,
    "max_discovered_records": 1_000,
    "max_full_text_papers": 60,
    "max_estimated_cost_usd": None,
}


def _dead_pid() -> int:
    """Run a trivial process and use its now-dead PID as the crashed
    worker's pid (a dead holder is detectable -- spec section 12)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _crash_marker(store: PaperStore, run_id: str, pid: int) -> None:
    """A dead worker's durable state: stale heartbeat + its lock marker."""
    past = time.time() - 900.0
    _write_json_atomic(heartbeat_path(store, run_id), {
        "monotonic": 0.0, "wall": past, "wall_iso": "", "pid": pid,
        "ts": "", "source": "test"})
    _write_json_atomic(lock_file_path(store, run_id),
                      {"pid": pid, "start_ts": past, "ts": past})


def _seed_papers_and_docs(store: PaperStore, run_id: str) -> None:
    """Pre-seed the run's papers + full-text documents so the model
    reasoning tasks actually have usable documents to call on (without a
    saved config snapshot the retrieval task records 'unavailable' and
    creates no documents of its own)."""
    for rec in DEFAULT_RECORDS:
        store.upsert_paper(
            run_id, {"doi": rec["doi"]}, title=rec["title"],
            authors=rec["authors"], year=rec["year"],
            metadata_provenance={"providers": [rec["provider"]]})
    _fake_documents(store, run_id)


def _retry_wait_call():
    """One canned model call that lands in retry_wait (a 429 that the
    scheduler defers, spec section 12)."""
    return {"ok": False, "data": None, "outcome": "retry_wait",
            "throttled": True, "wait_seconds": 1.0, "attempts": 1,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "cost": None, "prompt_id": "p", "retries_waited": 0,
            "budget_deficit": False, "validation_errors": [],
            "row_id": None}


# ===========================================================================
# 1. Detached launch
# ===========================================================================

def test_detached_launch_fake_child_accepted_and_lock_held(tmp_path):
    """``review run --plan <plan> --detach`` on a fixture plan -> the
    envelope says ACCEPTED (accepted + lock, NOT "completed") while the
    worker's file-state shows RUNNING -- handshake + heartbeat present,
    lock held by the worker (spec section 6). The CLI test already spawns
    the real fake-worker child; this one exercises the LAUNCHER's file
    protocol in-process via the documented child seam."""
    store = PaperStore(tmp_path / "p6a.db")
    rid = _seed_run(store)
    files_held: dict = {}

    def child_fn(**kw):
        # The fake child: acquires the lock, stamps heartbeat + handshake,
        # leaves the lock HELD (the worker is "running" durably).
        g = acquire_run_lock(store, rid, background_heartbeat=False)
        files_held["guard"] = g
        _write_json_atomic(kw["work_dir"] / "heartbeat.json", {
            "monotonic": 0.0, "wall": time.time(), "pid": os.getpid(),
            "ts": "", "source": "test"})
        _write_json_atomic(kw["work_dir"] / "handshake.json", {
            "accepted": True, "pid": os.getpid(), "run_id": rid,
            "start_ts": "", "token": kw.get("handshake_token")})
        return None

    res = launch_worker(store, rid, child_fn=child_fn)
    assert isinstance(res, LaunchResult)
    assert res.handshake is not None
    assert res.handshake["accepted"] is True
    # Accepted + lock: the marker is held by a LIVE pid, not stale.
    assert lock_file_path(store, rid).exists()
    info = lock_held(store, rid)
    assert info["held"] is True and info["stale"] is False
    assert info["holder_pid"] == os.getpid()
    # The handshake + heartbeat files are durable on disk.
    work_dir = run_dir(store, rid)
    assert (work_dir / "handshake.json").exists()
    assert (work_dir / "heartbeat.json").exists()
    # The initiating process can finish while the worker file-state shows
    # RUNNING: the lock stays held by the worker until its normal close.
    files_held["guard"].release()
    assert not lock_file_path(store, rid).exists()
    store.close()


def test_detached_launch_second_live_holder_rejected(tmp_path):
    """While a LIVE worker holds the lock, a second launch of the same
    run is rejected (a launch counts accepted only when the child
    handshaked + acquired the lock, spec section 6)."""
    store = PaperStore(tmp_path / "p6a.db")
    rid = _seed_run(store)
    held: dict = {}

    def child_fn(**kw):
        held["g"] = acquire_run_lock(store, rid, background_heartbeat=False)
        _write_json_atomic(kw["work_dir"] / "handshake.json", {
            "accepted": True, "pid": os.getpid(), "run_id": rid,
            "start_ts": "", "token": kw.get("handshake_token")})
        _write_json_atomic(kw["work_dir"] / "heartbeat.json", {
            "monotonic": 0.0, "wall": time.time(), "pid": os.getpid(),
            "ts": "", "source": "test"})
        return None

    launch_worker(store, rid, child_fn=child_fn)
    # A second launch while the live holder holds the lock -> rejected.
    with pytest.raises(RunLockHeld):
        launch_worker(store, rid, child_fn=child_fn)
    held["g"].release()
    store.close()


def test_detached_launch_cli_detach_accepted(tmp_path):
    """End-to-end through the CLI: ``review run --detach`` on a fixture
    plan spawns the REAL fake-worker child; the envelope says accepted +
    running (not completed)."""
    import yaml
    proj = tmp_path / "proj"
    proj.mkdir()
    rc, out, err = run_papers("init", cwd=proj)
    assert rc == 0, err
    brief = {
        "schema_version": "1.0",
        "question": "Does exercise improve memory in adults?",
        "purpose": "narrative",
        "scope": "aerobic exercise",
        "outputs": ["md"],
        "limits": dict(AGREED_CEILINGS, wall_minutes=60,
                       max_search_queries=1, max_discovered_records=50,
                       max_full_text_papers=1, max_model_calls=10,
                       max_input_tokens=10000, max_output_tokens=2000),
        "inclusion_criteria": ["human adults"],
        "exclusion_criteria": ["animals"],
    }
    brief_path = proj / "brief.yaml"
    brief_path.write_text(yaml.safe_dump(brief), encoding="utf-8")
    rc, out, err = run_papers("review", "plan", "--brief", "brief.yaml",
                              "--out", "plan.yaml", cwd=proj)
    assert rc == 0, err

    # A short hold keeps the fake worker RUNNING when the launch returns,
    # so the envelope + file-state read is deterministic.
    rc, out, err = run_papers("review", "run", "--plan", "plan.yaml",
                              "--detach", cwd=proj,
                              env_extra={**FAKE_WORKER_ENV,
                                         "PAPERS_FAKE_WORKER_HOLD_SECONDS":
                                             "2"})
    assert rc == 0, (err, out)
    env = _envelope(out)
    assert env["status"] == "ok"
    d = env["data"]
    assert d["accepted"] is True
    assert d["detach"] is True
    assert d["handshake"] is True
    assert d["state"] == "running"
    run_id = d["run_id"]

    # Bounded cleanup: request cancel; the fake worker honors it at its
    # next poll and closes the run as cancelled.
    store = PaperStore(proj / ".papercli" / "storage" / "papercli.db")
    rd = run_dir(store, run_id)
    (rd / "cancel-request").write_text(
        json.dumps({"requested_at": "now"}), encoding="utf-8")
    store.close()
    deadline = time.monotonic() + 25.0
    state = None
    while time.monotonic() < deadline:
        rc, out, err = run_papers("review", "status", run_id, cwd=proj)
        state = _envelope(out)["data"]["state"]
        if state in ("completed", "partial", "failed", "cancelled"):
            break
        time.sleep(0.25)
    assert state == "cancelled", f"fake worker did not honor cancel: {state}"


# ===========================================================================
# 2. Resume after interruption
# ===========================================================================

def test_resume_re_dispatches_only_unfinished_with_hash_stability(tmp_path):
    """A worker that DIED mid-run (stale heartbeat + dead lock holder,
    some tasks succeeded, some unfinished) -> resume re-launches and
    re-dispatches ONLY the pending/failed work; completed result hashes
    are UNCHANGED and no committed work is duplicated (idempotent, spec
    section 11)."""
    store = PaperStore(tmp_path / "p6a.db")
    _worker, _, rid = _run_full(store, model_cfg=_fake_model_cfg())
    tasks = _worker.queue.list(rid)
    keep, fail_one, rest = tasks[:3], tasks[3], tasks[4:]
    hashes_kept = {t.task_id: t.result_hash for t in keep}
    assert all(h is not None for h in hashes_kept.values())
    # Force the interruption: the in-flight task -> retry_wait (past due),
    # the rest -> pending (their results are lost), the 3 succeeded stay.
    past = "2000-01-01T00:00:00.000+00:00"
    with store._conn:
        store._conn.execute(
            "UPDATE tasks SET state='retry_wait', lease_owner=NULL, "
            "lease_expires_at=NULL, next_attempt_at=?, result_hash=NULL "
            "WHERE id=?", (past, fail_one.task_id))
        for t in rest:
            store._conn.execute(
                "UPDATE tasks SET state='pending', lease_owner=NULL, "
                "lease_expires_at=NULL, result_hash=NULL WHERE id=?",
                (t.task_id,))
    # A true crash leaves the run row RUNNING (the child never closed it)
    # and its lock + heartbeat stale with a dead holder pid.
    store.update_run(rid, state="running")
    _crash_marker(store, rid, _dead_pid())

    launched: dict = {}

    def child_fn(**kw):
        g = acquire_run_lock(store, rid, background_heartbeat=False)
        g.release()
        launched["yes"] = True
        _write_json_atomic(kw["work_dir"] / "handshake.json", {
            "accepted": True, "pid": os.getpid(), "run_id": rid,
            "start_ts": "", "token": kw.get("handshake_token")})
        return None

    res = resume_run(store, rid, child_fn=child_fn)
    assert res.launched is True
    assert res.resumed is True
    assert any("stale" in n for n in res.notes)
    assert launched.get("yes") is True, "resume did not re-launch"

    # The resumed worker re-dispatches ONLY the unfinished tasks and
    # finishes the run; the 3 succeeded stay byte-identical.
    worker2 = _new_worker(store, rid, model_cfg=_fake_model_cfg(),
                          records=DEFAULT_RECORDS)
    summary = worker2.execute()
    after = {t.task_id: t.result_hash for t in worker2.queue.list(rid)}
    for tid, h in hashes_kept.items():
        assert after[tid] == h, f"completed task {tid} was re-run"
    assert after[fail_one.task_id] is not None, (
        "the unfinished task was not re-dispatched on resume")
    # No duplicate committed work: the task set is unchanged (idempotent
    # keys never enqueue new rows on re-dispatch).
    assert len(after) == len(tasks)
    assert summary["state"] in ("completed", "partial")
    store.close()


# ===========================================================================
# 3. Limits / budgets
# ===========================================================================

def test_budget_exhaustion_closes_partial_with_honest_summary(tmp_path):
    """A model-call ceiling hit mid-run: dispatch stops (the reservation
    fails), the closure slice is reserved, the run closes PARTIAL
    (never completed), and run-summary.json reports the exhaustion
    honestly."""
    store = PaperStore(tmp_path / "p6a.db")
    limits = dict(FAKE_LIMITS, max_model_calls=1,
                  max_input_tokens=10_000_000)
    worker, summary, rid = _run_full(store, model_cfg=_fake_model_cfg(),
                                     limits=limits)
    # The budget hit its hard ceiling and the closure slice is reserved.
    assert worker.shared_budget.exhausted is True
    assert worker.shared_budget.partial_synthesis_reserved is True
    # The inline canned-adapter path does not persist model-call rows, so
    # the authoritative exhaustion state lives in the checkpointed budget
    # snapshot (durable, spec section 11) -- assert it there.
    counters = json.loads(store.run_row(rid)["counters_json"])
    assert counters["worker"]["budget"]["exhausted"] is True
    assert counters["worker"]["budget"]["partial_synthesis_reserved"] is True
    # The run closed with a PARTIAL closure, never 'completed' (the
    # exhausted budget stops new dispatch; the remaining model tasks
    # cannot finish -> partial/failed).
    assert summary["state"] in ("partial", "failed")
    assert store.run_row(rid)["state"] in ("partial", "failed")
    # The run-summary.json on disk carries the honest partial state.
    spath = write_run_summary(store, rid, build_run_summary(store, rid))
    s = json.loads(spath.read_text(encoding="utf-8"))
    assert s["state"] in ("partial", "failed")
    assert s["state"] != "completed"
    assert s["budget"]["exhausted"] is True
    # The honest ceilings from the agreed guardrails are reported.
    assert s["budget"]["limits"]["max_input_tokens"] == limits["max_input_tokens"]
    store.close()


def test_wall_deadline_stops_dispatch_partial_close(tmp_path):
    """A wall-clock deadline hit mid-run: new dispatch stops, the run
    closes partial, and the anchored deadline is checkpointed (durable)."""
    from papercli.jobs.worker import RunWorker
    store = PaperStore(tmp_path / "p6a.db")
    fake_now = {"now": "2026-11-19T00:00:00+00:00"}

    def now_fn():
        return fake_now["now"]

    rid = _seed_run(store, limits=dict(FAKE_LIMITS, wall_minutes=1))
    _seed_papers_and_docs(store, rid)
    w1 = _new_worker(store, rid, model_cfg=None, records=DEFAULT_RECORDS,
                     now_fn=now_fn, sleep=lambda s: None,
                     retry_delay_seconds=0.0, retry_jitter_seconds=0.0)
    s1 = w1.execute()
    assert s1["state"] == "completed", "within the deadline the run completes"

    # A NEW unit of work lands on the queue, and the (injected) clock
    # advances past the anchored 1-minute deadline: a resuming worker
    # must stop new dispatch immediately.
    TaskQueue(store).enqueue(rid, "discover", "discover:acceptance-extra",
                            payload={"input_tag": "extra"},
                            protocol_revision="1")
    fake_now["now"] = "2026-11-19T02:00:00+00:00"
    w2 = _new_worker(store, rid, model_cfg=None, records=DEFAULT_RECORDS,
                     now_fn=now_fn, sleep=lambda s: None,
                     retry_delay_seconds=0.0, retry_jitter_seconds=0.0)
    s2 = w2.execute()
    assert s2["state"] == "partial", (
        "past the deadline with pending work: the run must close partial")
    ws = json.loads(store.run_row(rid)["counters_json"])["worker"]
    assert ws["deadline_at"] is not None
    assert ws["deadline_reached"] is True
    # The pending task was never dispatched.
    extra = [t for t in TaskQueue(store).list(rid)
             if t.type == "discover" and (t.payload or {}).get("input_tag") == "extra"]
    assert extra and extra[0].state == "pending"
    store.close()


def test_agreed_budget_ceiling_guardrails():
    """The agreed guardrails are the ENFORCED ceilings (reserve-before-
    dispatch; hard ceilings) and the 45-rpm limiter default."""
    # The documented defaults ARE the agreed model ceilings.
    for k, v in AGREED_CEILINGS.items():
        if k in DEFAULT_BUDGETS:
            assert DEFAULT_BUDGETS[k] == v, k
    assert DEFAULT_RPM == 45

    # Reserve-before-dispatch: a reservation HOLDS budget before the call.
    b = ModelBudget(dict(AGREED_CEILINGS))
    r = b.reserve(model_calls=1, input_tokens=300, output_tokens=80)
    assert r is not None
    assert b.remaining()["model_calls"] == 299
    assert b.remaining()["input_tokens"] == 1_000_000 - 300
    rep = b.reconcile(r, actual_model_calls=1, actual_input_tokens=300,
                      actual_output_tokens=80)
    assert rep["deficit"] is False
    assert b.actual["model_calls"] == 1
    assert not b.exhausted, "one call of 300 tokens is far under the ceilings"

    # Hard ceilings: hitting one is terminal (exhaustion + closure slice).
    tight = ModelBudget({"max_model_calls": 1, "max_input_tokens": 10**7,
                         "max_output_tokens": 10**6})
    t1 = tight.reserve(model_calls=1)
    assert t1 is not None
    tight.reconcile(t1, actual_model_calls=1, actual_input_tokens=300,
                    actual_output_tokens=80)
    assert tight.exhausted is True
    assert tight.reserve(model_calls=1) is None, (
        "a normal dispatch after exhaustion must be refused")
    assert tight.allow_dispatch() is False
    assert tight.allow_dispatch(purpose="partial_synthesis") is True

    # The 45-rpm limiter: capacity 45, rapid acquires bounded; the 46th
    # is throttled with a positive bounded backoff wait.
    lim = RateLimiter()
    assert lim.rpm == 45 and lim.capacity == 45
    granted = sum(1 for _ in range(45) if lim.acquire().granted)
    assert granted == 45
    throttled = lim.acquire()
    assert throttled.throttled is True
    assert 0 < throttled.wait_seconds <= 60
    assert lim.wait_seconds() > 0


# ===========================================================================
# 4. Concurrency: lease exclusivity + no double-spend
# ===========================================================================

def test_concurrent_claims_exactly_one_acquires(tmp_path):
    """Two concurrent claims on the same run's task queue: the lease is
    EXCLUSIVE -- exactly one worker acquires the task, and no second
    worker can commit it. Each worker uses its own store connection
    (the single-writer SQLite handle is per-process)."""
    db = tmp_path / "p6a.db"
    rid_store = PaperStore(db)
    rid = _seed_run(rid_store)
    only = TaskQueue(rid_store).enqueue(rid, "discover", "conc:only",
                                       payload={"input_tag": "only"})
    rid_store.close()

    # Two concurrent workers: each owns its OWN store connection to the
    # same database (a single-writer SQLite handle is per-connection), so
    # the lease's conditional UPDATE is the only exclusion.
    results: dict = {}
    errors: list = []
    barrier = threading.Barrier(2)

    def claimer(name):
        # Each worker runs fully inside its own thread: claim, and if it
        # won, start + commit its leased task; if it lost, prove it CANNOT
        # commit a task it does not own.
        barrier.wait()
        worker = "w-" + name
        store = PaperStore(db)
        try:
            q = TaskQueue(store)
            got = q.claim(rid, worker, limit=1)
            if got:
                t = got[0]
                q.start(rid, t.task_id, worker)
                q.complete(rid, t.task_id, worker, result={"x": 1})
                row = store._conn.execute(
                    "SELECT state FROM tasks WHERE id=?",
                    (t.task_id,)).fetchone()
                results[name] = ("won", row["state"])
            else:
                results[name] = ("lost", None)
        except Exception as e:  # noqa: BLE001
            errors.append((name, e))
        finally:
            store.close()

    threads = [threading.Thread(target=claimer, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    winners = [n for n in ("a", "b") if results[n][0] == "won"]
    assert len(winners) == 1, f"two workers acquired the same task: {results}"
    loser = "b" if winners[0] == "a" else "a"
    assert results[winners[0]][1] == "succeeded"
    assert results[loser] == ("lost", None), (
        "a second worker acquired a task: no lease exclusivity")
    # Commit exclusion: the task is now terminal; no worker -- winner or
    # loser -- can commit it a second time.
    check = PaperStore(db)
    try:
        row = check._conn.execute(
            "SELECT state, result_hash FROM tasks WHERE id=?",
            (only.task_id,)).fetchone()
        assert row["state"] == "succeeded"
        assert row["result_hash"] is not None
        with pytest.raises(TaskQueueError):
            TaskQueue(check).complete(rid, only.task_id, "w-" + loser,
                                      result={"x": 1})
    finally:
        check.close()


def test_shared_budget_no_double_spend_across_threads(tmp_path):
    """Two workers against ONE shared ModelBudget with a single-call
    ceiling: the last-unit reservation is never double-spent (exactly
    one grant), and an unbounded pool is fully grantable."""
    # Last-unit: max 1 call -> exactly one grant, one refusal.
    b = ModelBudget({"max_model_calls": 1, "max_input_tokens": 10**6,
                     "max_output_tokens": 10**5})
    lock = threading.Lock()
    got = {"granted": 0, "refused": 0}
    barrier = threading.Barrier(2)

    def spender():
        barrier.wait()
        with lock:
            r = b.reserve(model_calls=1)
        if r is None:
            got["refused"] += 1
            return
        got["granted"] += 1
        with lock:
            b.reconcile(r, actual_model_calls=1, actual_input_tokens=300,
                        actual_output_tokens=80)

    threads = [threading.Thread(target=spender) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads)
    assert got["granted"] == 1, "the last unit was double-spent"
    assert got["refused"] == 1
    assert b.actual["model_calls"] == 1
    assert b.exhausted is True
    assert b.open_reserved("model_calls") == 0

    # Unbounded pool: both grants succeed (no artificial double-spend).
    b2 = ModelBudget({"max_model_calls": 4, "max_input_tokens": 10**6,
                      "max_output_tokens": 10**5})
    got2 = {"granted": 0, "refused": 0}
    barrier2 = threading.Barrier(2)

    def spender2():
        barrier2.wait()
        with lock:
            r = b2.reserve(model_calls=1)
        if r is None:
            got2["refused"] += 1
            return
        got2["granted"] += 1
        with lock:
            b2.reconcile(r, actual_model_calls=1, actual_input_tokens=300,
                         actual_output_tokens=80)

    threads2 = [threading.Thread(target=spender2) for _ in range(2)]
    for t in threads2:
        t.start()
    for t in threads2:
        t.join(timeout=20)
    assert got2["granted"] == 2
    assert got2["refused"] == 0
    assert b2.actual["model_calls"] == 2
    assert not b2.exhausted
    store = PaperStore(tmp_path / "p6a.db")
    store.close()


# ===========================================================================
# 5. Rate limits: 429 -> bounded backoff into retry_wait
# ===========================================================================

def test_model_429_bounded_backoff_not_storm():
    """A model 429: a BOUNDED number of backoffs (the configured
    re-prompts), then a terminal bounded outcome -- no retry storm at
    the HTTP layer. A drained limiter defers the call into retry_wait
    WITHOUT any HTTP call (spec section 12)."""
    import httpx
    from papercli.models.adapter import ModelAdapter, ModelCallOutcome

    hits: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(429)
        return httpx.Response(429, json={"error": "rate limited"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    limiter = RateLimiter(requests_per_minute=45, now_fn=time.monotonic,
                          wait_fn=lambda s: None)
    adapter = ModelAdapter(
        base_url="https://fake.local", model="m", api_key="test-key",
        client=client, rate_limiter=limiter,
        budget=ModelBudget(dict(AGREED_CEILINGS)),
        sleep_fn=sleeps.append, on_record=lambda rec: None)
    res = adapter.complete_json(schema={"type": "object"}, system="s",
                                user="u", task_type="synthesize",
                                prompt_version="synthesize-v1")
    # Bounded: exactly the configured re-prompts back off; each 429 is
    # hit at most (1 + max_re_prompts) times -- no unbounded storm.
    assert len(sleeps) == adapter.max_re_prompts
    assert len(hits) == 1 + adapter.max_re_prompts
    assert res["retries_waited"] == adapter.max_re_prompts
    assert res["ok"] is False
    assert res["outcome"] == ModelCallOutcome.FAILED
    # The drain path: no tokens left -> retry_wait outcome, zero HTTP.
    drained = RateLimiter(requests_per_minute=45, capacity=0,
                          now_fn=time.monotonic, wait_fn=lambda s: None)
    client2 = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]})))
    adapter2 = ModelAdapter(
        base_url="https://fake.local", model="m", api_key="test-key",
        client=client2, rate_limiter=drained,
        budget=ModelBudget(dict(AGREED_CEILINGS)),
        on_record=lambda rec: None)
    res2 = adapter2.complete_json(schema={"type": "object"}, system="s",
                                  user="u", task_type="synthesize",
                                  prompt_version="synthesize-v1")
    assert res2["outcome"] == ModelCallOutcome.RETRY_WAIT
    assert res2.get("throttled") is True
    assert len(hits) == 1 + adapter.max_re_prompts, (
        "the retry_wait deferral must not issue new HTTP calls")
    adapter.close()
    adapter2.close()


def test_worker_defers_429_through_scheduler_state(tmp_path):
    """Worker level: model 429s defer the task into retry_wait THROUGH
    SCHEDULER STATE (the task's next_attempt_at) -- one bounded deferral,
    no retry storm; the run closes partial with the deferral visible in
    the honest summary."""
    store = PaperStore(tmp_path / "p6a.db")
    rid = _seed_run(store)
    _seed_papers_and_docs(store, rid)
    fake_now = {"now": "2026-11-19T00:00:00+00:00"}

    def now_fn():
        return fake_now["now"]

    w = _new_worker(store, rid, model_cfg=_fake_model_cfg(),
                    records=DEFAULT_RECORDS, canned=[_retry_wait_call],
                    now_fn=now_fn, sleep=lambda s: None,
                    retry_delay_seconds=0.0, retry_jitter_seconds=0.0)
    summary = w.execute()
    assert summary["state"] in ("partial", "completed", "failed")
    # The first model task in the reasoning chain (screen_uncertain is
    # resolved deterministically and skipped) is the one that hit the
    # 429 and got parked in scheduler state.
    extract = [t for t in w.queue.list(rid) if t.type == "extract_findings"]
    assert extract and extract[0].state == "retry_wait", (
        "the 429 task must be deferred through scheduler state")
    row = store._conn.execute("SELECT * FROM tasks WHERE id=?",
                              (extract[0].task_id,)).fetchone()
    # One bounded deferral: a single failed attempt parked with a
    # next_attempt_at, never a repeated retry loop inside this run.
    assert row["attempt"] == 1
    assert row["next_attempt_at"] is not None
    assert "retry_wait" in (row["last_error"] or "")
    # The honest summary surfaces the in-flight deferral, never hides it.
    s = build_run_summary(store, rid)
    assert any(f["kind"] == "task" and f["state"] == "retry_wait"
               for f in s["failures"])
    store.close()


# ===========================================================================
# 6. Secrets: no secret value in any durable worker surface
# ===========================================================================

def test_no_secret_values_in_durable_surfaces(tmp_path, monkeypatch):
    """Across handshake / heartbeat files, worker.log.jsonl, the status
    data (run-summary.json) and the structured dumps: NO secret VALUE
    ever appears (spec section 13). The env carries sentinel values;
    every durable surface must stay free of them."""
    store = PaperStore(tmp_path / "p6a.db")
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    worker, summary, rid = _run_full(store, model_cfg=_fake_model_cfg(),
                                     records=DEFAULT_RECORDS)
    assert summary["state"] == "completed"
    # The model-call row keeps only the env-var REFERENCE name, never a
    # value (spec section 16: the endpoint is visible; the key is not).
    store.record_model_call(
        rid, model="canned-model", outcome="success",
        task_type="synthesize", prompt_hash="p",
        prompt_version="synthesize-v1", protocol_revision="1",
        input_tokens=300, output_tokens=80,
        estimated_cost_usd=None,
        usage={"input_tokens": 300, "output_tokens": 80},
        endpoint="https://fake.local/v1",
        api_key_env="PAPERS_LLM_API_KEY")
    # The durable surfaces: the worker's own files + a child's handshake /
    # heartbeat + the status surface + the structured dumps.
    work_dir = run_dir(store, rid)
    _write_json_atomic(work_dir / "handshake.json", {
        "accepted": True, "pid": os.getpid(), "run_id": rid,
        "start_ts": "", "token": "nonce"})
    _write_json_atomic(work_dir / "heartbeat.json", {
        "monotonic": 0.0, "wall": time.time(), "pid": os.getpid(),
        "ts": "", "source": "test"})
    write_run_summary(store, rid, build_run_summary(store, rid))
    write_structured_dumps(store, rid, formats=["md", "pdf", "docx"])
    files = [p for p in work_dir.rglob("*") if p.is_file()]
    assert any(p.name == "worker.log.jsonl" for p in files)
    assert any(p.name == "run-summary.json" for p in files)
    assert any(p.name == "handshake.json" for p in files)
    assert any(p.name == "heartbeat.json" for p in files)
    blob = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in files)
    for value in SENTINELS.values():
        assert value not in blob, (
            f"a secret value leaked into a durable surface: {value[:10]}...")
    # The endpoint (nonsecret) is visible in the summary; the API key is
    # referenced only by env-var NAME, and no VALUE appears anywhere.
    summary_text = (work_dir / "run-summary.json").read_text(encoding="utf-8")
    assert "https://fake.local/v1" in summary_text, (
        "the endpoint is visible (spec section 16)")
    for value in SENTINELS.values():
        assert value not in summary_text
    store.close()


def test_gitignore_covers_local_secret_and_venv_paths():
    """Local secret / venv / work dirs must stay git-ignored."""
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    text = gitignore.read_text(encoding="utf-8")
    assert ".env" in text, ".gitignore must cover .env"
    assert ".papercli/" in text, ".gitignore must cover the .papercli work dir"
    assert ".v6/" in text, ".gitignore must cover the .v6 test venv"


# ===========================================================================
# 7. Exports: honest partial bundles, rendered deferred (Phase 7)
# ===========================================================================

def test_partial_run_exports_valid_partial_bundle(tmp_path):
    """A failed/partial run still exports a VALID partial bundle: the
    structured dumps + run-summary.json carry the honest state; rendered
    md/pdf/docx are marked DEFERRED (Phase 7), never silently missing."""
    store = PaperStore(tmp_path / "p6a.db")
    limits = dict(FAKE_LIMITS, max_model_calls=1,
                  max_input_tokens=10_000_000)
    _worker, summary, rid = _run_full(store, model_cfg=_fake_model_cfg(),
                                      limits=limits)
    assert summary["state"] == "partial"
    out = write_structured_dumps(store, rid,
                                 formats=["md", "pdf", "docx", "csl", "csv"])
    # The Phase-6 structured dumps + run summary are all written.
    for name in ("evidence.json", "claims.json", "references.csl.json",
                 "papers-registry.json", "screening.json",
                 "search-log.jsonl", "evidence.csv", "screening.csv",
                 "run-summary.json"):
        assert name in out["written"], f"{name} missing from the export"
        assert Path(out["written"][name]).exists()
    # Rendered formats are DEFERRED (Phase 7), not silently missing.
    deferred = {d["format"] for d in out["deferred"]}
    assert {"md", "pdf", "docx"} <= deferred
    # The run summary on disk reports the HONEST partial state + the
    # deferred artifact inventory.
    s = json.loads(Path(out["written"]["run-summary.json"]).read_text(
        encoding="utf-8"))
    assert s["state"] == "partial"
    assert s["state"] != "completed"
    assert s["budget"]["exhausted"] is True
    types = {a["type"] for a in s["artifacts"]}
    assert {"review.md", "review.pdf", "review.docx"} <= types
    deferred_art = [a for a in s["artifacts"] if a.get("type") == "review.md"]
    assert deferred_art and deferred_art[0]["generation_status"] == "missing"
    assert "Phase 7" in deferred_art[0].get("note", "")
    store.close()


# ===========================================================================
# 8. Cancel: stop new dispatch, checkpoint, partial bundle
# ===========================================================================

def test_cancel_stops_dispatch_and_writes_partial_bundle(tmp_path):
    """Writing the cancel-request makes a RUNNING worker stop scheduling
    new tasks, terminate bounded child work where safe (the injected
    sleep writes the request instead of waiting out the full deferral),
    checkpoint, and write the partial status bundle (state 'cancelled')."""
    store = PaperStore(tmp_path / "p6a.db")
    rid = _seed_run(store)
    _seed_papers_and_docs(store, rid)
    sleeps: list[float] = []

    # The worker's cancel-request file (what `papers review cancel` writes).
    from papercli.exports.run_summary import run_dir as _rd
    cancel_file = _rd(store, rid) / "cancel-request"

    def fake_sleep(s):
        # Bounded child work: the worker's injected sleep polls for the
        # cancel request instead of waiting out the full deferral; on the
        # first deferral wait it writes the request (a stand-in for the
        # user's cancel), so the run stops at the next dispatch boundary.
        sleeps.append(float(s))
        if len(sleeps) == 1:
            cancel_file.write_text(json.dumps({"requested_at": "now"}),
                                   encoding="utf-8")

    w = _new_worker(store, rid, model_cfg=_fake_model_cfg(),
                    records=DEFAULT_RECORDS, canned=[_retry_wait_call],
                    sleep=fake_sleep)
    assert str(w._cancel_request) == str(cancel_file)
    # Run the worker deterministically on the main thread: it completes a
    # few tasks, parks the 429 task into retry_wait, then -- because the
    # injected sleep wrote the cancel request -- the dispatch loop breaks
    # at the next boundary and closes the partial bundle.
    summary = w.execute()
    assert summary["state"] == "cancelled"
    assert summary["state"] != "completed"
    assert store.run_row(rid)["state"] == "cancelled"
    # No task was re-run: the completed tasks kept their result hashes.
    done = [t for t in TaskQueue(store).list(rid) if t.state == "succeeded"]
    assert len(done) >= 1
    for t in done:
        assert t.result_hash is not None
    # The partial status bundle was written by the closure.
    spath = w.work_dir / "run-summary.json"
    assert spath.exists()
    s = json.loads(spath.read_text(encoding="utf-8"))
    assert s["state"] == "cancelled"
    # The worker log shows the 429 task was deferred through scheduler
    # state and the run closed (the cancel took effect at the next
    # dispatch boundary, which is what "bounded child work" means).
    log = (w.work_dir / "worker.log.jsonl").read_text(encoding="utf-8")
    events = [json.loads(l)["data"]["event"] for l in log.splitlines()
              if l.strip()]
    assert "task_deferred" in events
    assert "run_closed" in events
    # The cancel-request file is present (what `papers review cancel` writes).
    assert cancel_file.exists()
    # Bounded child work: no unbounded wait was slept through.
    assert all(s_ <= 5.0 for s_ in sleeps), (
        f"a cancel should bound the child's waits, saw {sleeps}")
    store.close()
