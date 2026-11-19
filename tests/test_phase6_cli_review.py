"""Phase 6 batch A-3: the review lifecycle CLI (run/status/logs/resume/
cancel/export).

Fully offline: the detached child is the fake worker module
(``PAPERS_WORKER_MODULE=papercli.jobs.fake_worker_child``), so
``papers review run --detach`` runs a trivial DAG without any network or
model endpoint. The CLI is exercised through a subprocess shim (like
tests/conftest.py) so envelopes + exit codes are asserted on real stdout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from tests.conftest import run_papers

#: The offline worker-child seam (never the production default).
FAKE_WORKER_ENV = {
    "PAPERS_WORKER_MODULE": "papercli.jobs.fake_worker_child",
}

BRIEF = {
    "schema_version": "1.0",
    "question": "Does exercise improve memory in adults?",
    "purpose": "narrative",
    "scope": "aerobic exercise and cognition in adults",
    "outputs": ["md"],
    "limits": {
        "wall_minutes": 60,
        "max_search_queries": 1,
        "max_discovered_records": 50,
        "max_full_text_papers": 1,
        "max_model_calls": 10,
        "max_input_tokens": 10000,
        "max_output_tokens": 2000,
        "max_estimated_cost_usd": None,
    },
    "inclusion_criteria": ["human adults"],
    "exclusion_criteria": ["animals"],
}


def _run_papers_env(*args, cwd, **kw):
    """run_papers with the fake-worker module injected."""
    env_extra = dict(FAKE_WORKER_ENV)
    env_extra.update(kw.pop("env_extra", {}) or {})
    return run_papers(*args, cwd=cwd, env_extra=env_extra, **kw)


def _envelope(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


def _assert_envelope_shape(env: dict) -> None:
    assert env.get("schema_version") == "1.0"
    assert env.get("status") in ("ok", "error")
    assert isinstance(env.get("warnings"), list)


def _project(tmp_path, question=None):
    """A fresh project with a validated plan file. Returns (plan_path,
    plan_env)."""
    rc, out, err = _run_papers_env("init", cwd=tmp_path)
    assert rc == 0, err
    brief = json.loads(json.dumps(BRIEF))
    if question:
        brief["question"] = question
    brief_path = tmp_path / "brief.yaml"
    brief_path.write_text(yaml.safe_dump(brief), encoding="utf-8")
    rc, out, err = _run_papers_env(
        "review", "plan", "--brief", str(brief_path.name),
        "--out", "plan.yaml", cwd=tmp_path)
    assert rc == 0, err
    return tmp_path / "plan.yaml", _envelope(out)


def _launch_and_wait(tmp_path, plan_env, hold_seconds=None):
    """review run --detach on the fake worker; wait for its closure."""
    plan_path = tmp_path / "plan.yaml"
    env_extra = {}
    if hold_seconds is not None:
        env_extra["PAPERS_FAKE_WORKER_HOLD_SECONDS"] = str(hold_seconds)
    rc, out, err = _run_papers_env(
        "review", "run", "--plan", str(plan_path.name), "--detach",
        cwd=tmp_path, env_extra=env_extra)
    assert rc == 0, err
    launch = _envelope(out)
    _assert_envelope_shape(launch)
    assert launch["data"]["accepted"] is True
    run_id = launch["data"]["run_id"]
    # The fake worker closes in ~1s (without a hold); give it a moment.
    deadline = time.monotonic() + (hold_seconds + 6 if hold_seconds else 10)
    while time.monotonic() < deadline:
        rc, out, err = _run_papers_env("review", "status", run_id,
                                        cwd=tmp_path)
        state = _envelope(out)["data"]["state"]
        if state in ("completed", "partial", "failed", "cancelled"):
            return run_id, state
        time.sleep(0.3)
    raise AssertionError(f"fake worker did not close: {state}")


# ---------------------------------------------------------------------------
# review run
# ---------------------------------------------------------------------------

def test_review_run_detach_accepted_envelope(tmp_path):
    plan_path, plan_env = _project(tmp_path)
    # A short hold keeps the worker RUNNING when the launch returns, so
    # the envelope reports the documented post-launch state deterministically.
    rc, out, err = _run_papers_env(
        "review", "run", "--plan", plan_path.name, "--detach",
        cwd=tmp_path,
        env_extra={"PAPERS_FAKE_WORKER_HOLD_SECONDS": "2"})
    assert rc == 0, err
    env = _envelope(out)
    _assert_envelope_shape(env)
    assert env["status"] == "ok"
    d = env["data"]
    assert d["accepted"] is True
    assert d["run_id"] == plan_env["data"]["run_id"]
    # The run row moved planned -> running (a live worker holds it).
    assert d["state"] == "running"
    assert d["detach"] is True
    assert d["handshake"] is True
    # Let the fake worker finish so its lock releases cleanly.
    time.sleep(3.0)


def test_review_run_missing_plan_exits_2(tmp_path):
    _project(tmp_path)
    rc, out, err = _run_papers_env(
        "review", "run", "--plan", "nope.yaml", "--detach", cwd=tmp_path)
    assert rc == 2
    env = _envelope(out)
    assert env["status"] == "error"
    assert env["error"]["code"] == "CONFIG_REQUIRED"
    assert env["error"]["retryable"] is False


def test_review_run_second_launch_rejected_while_locked(tmp_path):
    """A second launch on the same run while the worker holds the lock
    is a preflight failure (exit 2), not corruption."""
    plan_path, _ = _project(tmp_path)
    rc, out, err = _run_papers_env(
        "review", "run", "--plan", plan_path.name, "--detach", cwd=tmp_path,
        env_extra={"PAPERS_FAKE_WORKER_HOLD_SECONDS": "30"})
    assert rc == 0, err
    # While the worker holds the lock: a second launch is REJECTED.
    rc2, out2, err2 = _run_papers_env(
        "review", "run", "--plan", plan_path.name, "--detach", cwd=tmp_path)
    assert rc2 == 2, (rc2, out2, err2)
    env2 = _envelope(out2)
    assert env2["status"] == "error"
    assert env2["error"]["code"] in ("RUN_LOCK_HELD", "RUN_NOT_STARTABLE")
    # Cancel the held run so the test leaves no live worker.
    run_id = _envelope(out)["data"]["run_id"]
    _run_papers_env("review", "cancel", run_id, cwd=tmp_path)
    time.sleep(2.0)


def test_review_run_foreground_blocks_and_reports(tmp_path):
    """Without --detach the command BLOCKS on the worker's closure and
    reports the final state (exit 0 completed; the envelope carries the
    worker's exit code)."""
    plan_path, _ = _project(tmp_path)
    t0 = time.monotonic()
    rc, out, err = _run_papers_env(
        "review", "run", "--plan", plan_path.name, cwd=tmp_path)
    assert rc == 0, (rc, out, err)
    env = _envelope(out)
    d = env["data"]
    assert d["detach"] is False
    assert d["state"] in ("completed", "partial", "cancelled")
    assert d["worker_exit_code"] in (0, 6, None)
    assert time.monotonic() - t0 >= 1.0, "foreground must block on closure"


# ---------------------------------------------------------------------------
# review status
# ---------------------------------------------------------------------------

def test_review_status_describes_completed_run(tmp_path):
    run_id, state = _launch_and_wait(tmp_path, _project(tmp_path)[1])
    rc, out, err = _run_papers_env("review", "status", run_id, cwd=tmp_path)
    assert rc == 0, err
    env = _envelope(out)
    _assert_envelope_shape(env)
    d = env["data"]
    # A successful status call describes a finished run (spec 6).
    assert d["state"] == state in ("completed", "partial", "cancelled")
    assert d["task_counts"]["total"] >= 2
    assert "succeeded" in d["task_counts"]
    assert d["task_progress"], "per-task-type progress expected"
    assert d["budget"]["limits"]["max_model_calls"] is not None
    assert "exhausted" in d["budget"]
    assert isinstance(d["stale"], bool)
    assert "held" in d["lock"]
    # Failures block is present even when empty (never hidden).
    assert "failures" in d


def test_review_status_unknown_run_exits_2(tmp_path):
    _project(tmp_path)
    rc, out, err = _run_papers_env("review", "status", "run-missing",
                                    cwd=tmp_path)
    assert rc == 2
    env = _envelope(out)
    assert env["error"]["code"] == "RUN_NOT_FOUND"


def test_review_status_stale_flag_on_interrupted_run(tmp_path):
    """A run whose heartbeat is old reports STALE (possibly interrupted),
    never 'running' (spec 12)."""
    run_id, _ = _launch_and_wait(tmp_path, _project(tmp_path)[1])
    # Age the heartbeat far past the threshold + drop the lock (a dead
    # holder), i.e. simulate a crash.
    from papercli.jobs.detach import _write_json_atomic, heartbeat_path, lock_file_path
    from papercli.storage.db import PaperStore
    store = PaperStore(tmp_path / ".papercli" / "storage" / "papercli.db")
    try:
        hb = heartbeat_path(store, run_id)
        _write_json_atomic(hb, {
            "monotonic": 0.0, "wall": time.time() - 3600.0, "wall_iso": "",
            "pid": 999999, "ts": "", "source": "test",
        })
        _write_json_atomic(lock_file_path(store, run_id),
                          {"pid": 999999, "start_ts": "", "ts": ""})
    finally:
        store.close()
    rc, out, err = _run_papers_env("review", "status", run_id, cwd=tmp_path)
    assert rc == 0
    d = _envelope(out)["data"]
    assert d["stale"] is True
    assert d["lock"]["stale"] is True
    assert d["heartbeat_age_seconds"] > 3000


# ---------------------------------------------------------------------------
# review logs
# ---------------------------------------------------------------------------

def test_review_logs_streams_envelopes(tmp_path):
    run_id, _ = _launch_and_wait(tmp_path, _project(tmp_path)[1])
    rc, out, err = _run_papers_env("review", "logs", run_id,
                                    cwd=tmp_path)
    assert rc == 0, err
    env = _envelope(out)
    _assert_envelope_shape(env)
    events = [e["data"]["event"] for e in env["data"]["events"]]
    assert "worker_started" in events
    assert "run_closed" in events
    assert env["data"]["lines"] == len(env["data"]["events"])


def test_review_logs_follow_terminal_run_returns_immediately(tmp_path):
    run_id, _ = _launch_and_wait(tmp_path, _project(tmp_path)[1])
    t0 = time.monotonic()
    rc, out, err = _run_papers_env("review", "logs", run_id, "--follow",
                                    cwd=tmp_path)
    assert rc == 0
    assert _envelope(out)["data"]["lines"] >= 3
    assert time.monotonic() - t0 < 30, "follow on a closed run must stop"


def test_review_logs_unknown_run_exits_2(tmp_path):
    _project(tmp_path)
    rc, out, err = _run_papers_env("review", "logs", "run-missing",
                                    cwd=tmp_path)
    assert rc == 2
    assert _envelope(out)["error"]["code"] == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# review resume
# ---------------------------------------------------------------------------

def test_review_resume_completed_run_is_noop(tmp_path):
    run_id, state = _launch_and_wait(tmp_path, _project(tmp_path)[1])
    assert state in ("completed", "partial", "cancelled")
    rc, out, err = _run_papers_env("review", "resume", run_id, "--detach",
                                    cwd=tmp_path)
    assert rc == 0, err
    env = _envelope(out)
    assert env["data"]["launched"] is False
    assert env["data"]["resumed"] is False
    assert env["data"]["state"] == state


def test_review_resume_changed_brief_exits_2(tmp_path):
    """A CHANGED brief on resume -> exit 2 with new-revision guidance
    (spec 5)."""
    plan_path, plan_env = _project(tmp_path)
    run_id = plan_env["data"]["run_id"]
    # A different brief (changed question): its digest no longer matches
    # the run's saved brief_ref.
    changed = json.loads(json.dumps(BRIEF))
    changed["question"] = "A completely different question?"
    changed_path = tmp_path / "changed-brief.yaml"
    changed_path.write_text(yaml.safe_dump(changed), encoding="utf-8")

    rc, out, err = _run_papers_env(
        "review", "resume", run_id, "--detach",
        "--brief", changed_path.name, cwd=tmp_path)
    assert rc == 2, (rc, out, err)
    env = _envelope(out)
    assert env["status"] == "error"
    assert env["error"]["code"] == "BRIEF_CHANGED"
    msg = env["error"]["message"].lower()
    assert "new" in msg and "revision" in msg
    assert "papers review plan" in env["error"]["details"].get("guidance",
                                                               "")


def test_review_resume_same_brief_accepted(tmp_path):
    plan_path, plan_env = _project(tmp_path)
    run_id = plan_env["data"]["run_id"]
    brief_path = tmp_path / "brief.yaml"  # the SAME brief that planned it
    rc, out, err = _run_papers_env(
        "review", "resume", run_id, "--detach",
        "--brief", brief_path.name, cwd=tmp_path)
    # The run is in a non-terminal state; the fake worker re-launches and
    # idempotently re-dispatches only unfinished work.
    assert rc == 0, (rc, out, err)
    d = _envelope(out)["data"]
    assert d["launched"] is True
    time.sleep(2.0)


# ---------------------------------------------------------------------------
# review cancel
# ---------------------------------------------------------------------------

def test_review_cancel_running_worker_writes_partial_bundle(tmp_path):
    """cancel on a RUNNING (fake) worker: the cancel-request file makes
    it stop dispatch + checkpoint + write the partial status bundle
    (spec 12)."""
    plan_path, _ = _project(tmp_path)
    rc, out, err = _run_papers_env(
        "review", "run", "--plan", plan_path.name, "--detach",
        cwd=tmp_path,
        env_extra={"PAPERS_FAKE_WORKER_HOLD_SECONDS": "30"})
    assert rc == 0, err
    run_id = _envelope(out)["data"]["run_id"]
    time.sleep(1.5)  # let the worker grab its lock + start a task

    rc, out, err = _run_papers_env("review", "cancel", run_id, cwd=tmp_path)
    assert rc == 0, (rc, out, err)
    env = _envelope(out)
    assert env["data"]["worker_running"] is True
    # The cancel-request path is relative to the CLI's cwd (tmp_path).
    cancel_path = Path(env["data"]["cancel_request"])
    if not cancel_path.is_absolute():
        cancel_path = tmp_path / cancel_path
    assert cancel_path.exists()

    # The worker honors the request: bounded hold loop, then closure.
    deadline = time.monotonic() + 15
    state = None
    while time.monotonic() < deadline:
        rc, out, err = _run_papers_env("review", "status", run_id,
                                        cwd=tmp_path)
        state = _envelope(out)["data"]["state"]
        if state == "cancelled":
            break
        time.sleep(0.5)
    assert state == "cancelled", f"worker did not honor cancel: {state}"
    # The partial status bundle was written by the worker's closure.
    from papercli.exports.run_summary import run_dir
    from papercli.storage.db import PaperStore
    store = PaperStore(tmp_path / ".papercli" / "storage" / "papercli.db")
    try:
        summary_dir = run_dir(store, run_id)
        if not summary_dir.is_absolute():
            summary_dir = tmp_path / summary_dir
        assert (summary_dir / "run-summary.json").exists()
    finally:
        store.close()


def test_review_cancel_no_live_worker_marks_cancelled(tmp_path):
    """No live worker (stale lock + old heartbeat) -> the run is marked
    cancelled directly, with its unfinished leases released."""
    from papercli.jobs.detach import _write_json_atomic, heartbeat_path, lock_file_path
    from papercli.storage.db import PaperStore

    plan_path, _ = _project(tmp_path)
    store = PaperStore(tmp_path / ".papercli" / "storage" / "papercli.db")
    run_id = None
    for row in store._conn.execute("SELECT id, state FROM runs"):
        if row["state"] in ("planned", "running", "queued"):
            run_id = row["id"]
    assert run_id is not None, "no startable run found"
    # Simulate a crashed worker: stale heartbeat + dead holder pid.
    _write_json_atomic(heartbeat_path(store, run_id), {
        "monotonic": 0.0, "wall": time.time() - 3600.0, "wall_iso": "",
        "pid": 42424242, "ts": "", "source": "test"})
    _write_json_atomic(lock_file_path(store, run_id),
                      {"pid": 42424242, "start_ts": "", "ts": ""})
    # The worker never ran a task: the DAG is not built. Seed one task so
    # the cancel has a lease to release.
    from papercli.jobs.tasks import TaskQueue
    TaskQueue(store).enqueue(run_id, "discover", "discover:seed",
                             payload={"input_tag": "seed"})
    store.close()

    rc, out, err = _run_papers_env("review", "cancel", run_id, cwd=tmp_path)
    assert rc == 0, (rc, out, err)
    env = _envelope(out)
    assert env["data"]["worker_running"] is False
    assert env["data"]["state"] == "cancelled"
    store = PaperStore(tmp_path / ".papercli" / "storage" / "papercli.db")
    try:
        assert store.run_row(run_id)["state"] == "cancelled"
        assert store.run_row(run_id)["ended_at"] is not None
    finally:
        store.close()


def test_review_cancel_closed_run_exits_2(tmp_path):
    run_id, state = _launch_and_wait(tmp_path, _project(tmp_path)[1])
    assert state in ("completed", "partial", "cancelled")
    rc, out, err = _run_papers_env("review", "cancel", run_id, cwd=tmp_path)
    assert rc == 2
    assert _envelope(out)["error"]["code"] == "RUN_ALREADY_CLOSED"


# ---------------------------------------------------------------------------
# review export
# ---------------------------------------------------------------------------

def test_review_export_writes_bundle(tmp_path):
    run_id, _ = _launch_and_wait(tmp_path, _project(tmp_path)[1])
    rc, out, err = _run_papers_env(
        "review", "export", run_id,
        "--formats", "md,pdf,docx,bib,csl,csv", cwd=tmp_path)
    assert rc == 0, (rc, out, err)
    env = _envelope(out)
    _assert_envelope_shape(env)
    d = env["data"]
    written = d["written"]
    # The structured dumps + run summary are written.
    for name in ("evidence.json", "claims.json", "references.csl.json",
                 "papers-registry.json", "screening.json",
                 "search-log.jsonl", "evidence.csv", "screening.csv",
                 "run-summary.json"):
        assert name in written, f"{name} missing from the export"
    # Phase 7: the requested formats are RENDERED (or recorded missing
    # with a reason) -- no 'deferred' list remains.
    assert "deferred" not in d, "Phase 7 built the renderers"
    rendered = d["rendered"]
    for name in ("review.md", "references.bib", "references.csl.json"):
        assert rendered[name]["generation_status"] == "complete", name
    for name in ("review.pdf", "review.docx"):
        st = rendered[name]
        assert st["generation_status"] in ("complete", "missing")
        if st["generation_status"] == "missing":
            assert st["note"], "a missing renderer must carry a reason"
            assert any(name in w for w in env["warnings"]), \
                "missing renderer must surface as an envelope warning"
    # run-summary.json exists on disk with the per-file inventory. The
    # CLI (cwd=tmp_path) reports run-dir-relative paths.
    import json as _json
    summary_path = Path(written["run-summary.json"])
    if not summary_path.is_absolute():
        summary_path = tmp_path / summary_path
    summary = _json.loads(summary_path.read_text(encoding="utf-8"))
    types = {a["type"] for a in summary["artifacts"]}
    assert {"review.md", "references.bib", "references.csl.json",
            "coverage-and-limitations.md", "citation-audit.json"} <= types
    # The disclosure files are in the bundle on disk.
    summary_parent = summary_path.parent
    assert (summary_parent / "coverage-and-limitations.md").exists()
    assert (summary_parent / "citation-audit.json").exists()
    assert (summary_parent / "review.md").exists()


def test_review_export_unknown_run_exits_2(tmp_path):
    _project(tmp_path)
    rc, out, err = _run_papers_env(
        "review", "export", "run-missing", "--formats", "md", cwd=tmp_path)
    assert rc == 2
    assert _envelope(out)["error"]["code"] == "RUN_NOT_FOUND"
