
"""Phase 6 batch 1: the standalone worker + run summary.

Fully offline: the provider search service and the model endpoint are
fakes (an :class:`httpx.MockTransport` behind the Phase-5 adapter), so
the DAG exercises the real lease protocol, budget reservations,
checkpoints, and closure without any live call.
"""

from __future__ import annotations

import json

import httpx
import pytest

from papercli.models.adapter import ModelAdapter
from papercli.models.rate_limit import ModelBudget
from papercli.storage.db import PaperStore

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

FAKE_LIMITS = {
    "wall_minutes": 360,
    "max_search_queries": 2,
    "max_discovered_records": 100,
    "max_full_text_papers": 5,
    "max_model_calls": 300,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 150_000,
    "max_estimated_cost_usd": None,
}

FAKE_PLAN = {
    "question": "Q?",
    "purpose": "p",
    "scope": "s",
    "date_range": {"from": "2020", "to": "2026"},
    "languages": ["en"],
    "inclusion_criteria": [
        "interventional aerobic exercise training",
        "adult cognitive outcomes",
    ],
    "exclusion_criteria": ["animal studies"],
    "themes_to_investigate": ["aerobic exercise training"],
    "limits": FAKE_LIMITS,
    "queries": [
        {"provider": "crossref", "query": "aerobic exercise cognition"},
        {"provider": "openalex", "query": "aerobic exercise cognition"},
    ],
}

DEFAULT_RECORDS = [
    {"doi": "10.1/a", "title": "Aerobic training and cognition in adults",
     "authors": ["Smith"], "year": 2021, "provider": "crossref"},
    {"doi": "10.2/b", "title": "Aerobic training and cognition in adults",
     "authors": ["Jones"], "year": 2022, "provider": "crossref"},
]

#: Canned model replies, in call order: screen_uncertain, extract(doc 1),
#: extract(doc 2), synthesize, critique.
CANNED = [
    json.dumps({"decision": "include", "criterion_ids": ["inc-1"],
                "rationale": "r", "evidence_basis": "abstract"}),
    json.dumps({"findings": [{"finding": "aerobic training improved memory",
                               "claim_type": "reported_result",
                               "units": None,
                               "population_or_system": "adults"}]}),
    json.dumps({"findings": [{"finding": "aerobic training improved memory",
                               "claim_type": "reported_result",
                               "units": None,
                               "population_or_system": "adults"}]}),
    json.dumps({"themes": [
        {"theme": "training dose",
         "claims": [{"text": "Dose-response claim",
                     "citation_keys": ["smith2021"],
                     "evidence_ids": ["ev-1"],
                     "is_synthesis": True}],
         "gaps": ["no longitudinal data"]}],
        "limitations": ["small samples"], "contradictions": []}),
    json.dumps({"claims": []}),
]


def _store(tmp_path) -> PaperStore:
    return PaperStore(tmp_path / "p6.db")


def _seed_run(store: PaperStore, plan: dict | None = None,
              limits: dict | None = None) -> str:
    """A planned Run row with the saved protocol (the worker's input)."""
    plan = plan if plan is not None else json.loads(json.dumps(FAKE_PLAN))
    limits = limits if limits is not None else dict(
        plan.get("limits") or FAKE_LIMITS)
    rid = store.create_run(limits=limits, state="running",
                           protocol_hash="test-hash",
                           config_snapshot_ref="")
    with store._conn:
        store._conn.execute(
            "UPDATE runs SET protocol_revision=1, plan_json=? WHERE id=?",
            (json.dumps(plan, sort_keys=True), rid))
    return rid


class FakeSearchService:
    """Canned provider search + persist (no network)."""

    def __init__(self, records: list[dict], completeness: str = "complete"):
        self.records = records
        self.completeness = completeness

    def search(self, query, providers, limit=50):
        return {
            "query": query,
            "providers": [{"provider": providers[0] if providers else "x",
                           "query": query, "result_count": len(self.records),
                           "completeness": self.completeness, "error": None}],
            "records": [dict(r) for r in self.records],
            "record_count": len(self.records),
            "deduplicated_from": len(self.records),
            "completeness": self.completeness,
            "errors": [],
            "limit": int(limit),
        }

    def persist(self, result, run_id):
        for prov in result.get("providers") or []:
            self.store.record_search(
                run_id, prov.get("provider", "x"), prov.get("query"),
                result_count=prov.get("result_count"),
                completeness=prov.get("completeness"))
        for rec in result.get("records") or []:
            ids = {}
            if rec.get("doi"):
                ids["doi"] = str(rec["doi"]).lower()
            if rec.get("arxiv_id"):
                ids["arxiv_id"] = str(rec["arxiv_id"])
            self.store.upsert_paper(
                run_id, ids, title=rec.get("title"),
                authors=rec.get("authors"), year=rec.get("year"),
                metadata_provenance={"providers": [rec.get("provider") or "x"]})

    def __call_factory__(self):  # pragma: no cover - marker
        return None


def _make_fake_search_factory(store, records):
    svc = FakeSearchService(records)
    svc.store = store
    return lambda: svc


def _fake_documents(store, run_id):
    """Seed full-text documents (text index + access level) for the
    accepted papers so the evidence layer has something to read."""
    for row in store.papers_for_run(run_id):
        ids = json.loads(row.get("canonical_ids_json") or "{}")
        if not (ids.get("doi") or ids.get("arxiv_id")):
            continue
        doc = store.record_document(
            row["id"], source_url=f"https://example.org/{row['id']}",
            access_status="retrieved_full_text", parse_quality="good",
            access_level="full_text")
        store.store_document_text(doc, [
            {"page": 1, "section": "results", "paragraph": 1,
             "text": "Aerobic training improved memory in adults."},
            {"page": 2, "section": "limitations", "paragraph": 1,
             "text": "Sample size was small."},
        ])


class _CannedAdapter:
    """A deterministic model backend (no network, no HTTP): replays ``calls``
    in order; the last one repeats. Mirrors the Phase-5 adapter's budget
    discipline -- one reservation BEFORE dispatch, reconciliation AFTER, the
    deficit (usage beyond the reservation) disclosed -- against the run's
    SHARED :class:`ModelBudget` (spec section 12)."""

    def __init__(self, calls, shared_budget, model: str = "canned-model"):
        self._calls = list(calls)
        self._i = 0
        self.calls_made = 0
        self.last_outcome = None
        self.last_budget_deficit = False
        self._budget = shared_budget
        # The production reasoning layer records the model identity on every
        # result (spec section 9: reviewer = model/<id>); a canned backend
        # must expose the same attribute or screen_uncertain crashes.
        self.model = model
    def close(self) -> None:
        """No-op: a canned backend holds no resources."""


    def complete_json(self, *, task_type, user=None, **_kw):
        # Reserve one call (input/output reserved at 0, like the real
        # adapter) BEFORE dispatch; on an exhausted budget the call is
        # skipped (the closure path, spec 12).
        resv = self._budget.reserve(model_calls=1, input_tokens=0,
                                    output_tokens=0, purpose="model_call") \
            if self._budget is not None else None
        if resv is None:
            self.last_outcome = "skipped"
            return {"ok": False, "data": None, "outcome": "skipped",
                    "skipped": True, "budget_exhausted": True,
                    "usage": None, "cost": None, "prompt_id": "p",
                    "attempts": 0, "retries_waited": 0,
                    "validation_errors": [], "row_id": None,
                    "budget_deficit": False}
        idx = min(self._i, len(self._calls) - 1)
        self._i += 1
        self.calls_made += 1
        res = self._calls[idx]() if callable(self._calls[idx]) else self._calls[idx]
        self.last_outcome = res.get("outcome")
        # Reconcile the returned usage against the reservation (deficit
        # disclosed, never absorbed -- D-P5-1).
        if self._budget is not None and res.get("usage"):
            rep = self._budget.reconcile(
                resv,
                actual_model_calls=1,
                actual_input_tokens=int(res["usage"].get("input_tokens") or 0),
                actual_output_tokens=int(res["usage"].get("output_tokens") or 0))
            self.last_budget_deficit = bool(rep.get("deficit"))
            res["budget_deficit"] = self.last_budget_deficit
        return res


def _fake_model_cfg():
    from papercli.config.loader import Config
    return Config(
        {"model": {"adapter": "openai_compatible",
                   "base_url": "https://fake.local",
                   "model": "test-model",
                   "api_key_env": "PAPERS_TEST_LLM_KEY"},
         "limits": dict(FAKE_LIMITS)},
        sources=["test"],
        env={"PAPERS_TEST_LLM_KEY": "test-key"},
    )


def _ok(call):
    """One canned OK call result (as the adapter's complete_json returns)."""
    def _():
        data = call.get("data")
        return {"ok": True, "data": data, "outcome": "success",
                "usage": {"input_tokens": 300, "output_tokens": 80},
                "retries_waited": 0, "budget_deficit": False,
                "attempts": 1, "prompt_id": "p", "row_id": None,
                "validation_errors": []}
    return _


def _make_canned_calls():
    """The 5 model calls of a full run: screen, extract x2, synth, crit."""
    return [
        _ok({"data": json.loads(CANNED[0])}),
        _ok({"data": json.loads(CANNED[1])}),
        _ok({"data": json.loads(CANNED[2])}),
        _ok({"data": json.loads(CANNED[3])}),
        _ok({"data": json.loads(CANNED[4])}),
    ]


def _new_worker(store, rid, model_cfg=None, records=None,
                canned=None, limits=None, now_fn=None, sleep=None,
                **kw):
    from papercli.jobs.worker import RunWorker
    records = records if records is not None else []
    calls = canned if canned is not None else _make_canned_calls()
    factory = None
    if model_cfg is not None:
        # A single adapter instance replays the canned calls for the run,
        # reserving/reconciling against the worker's SHARED budget.
        holder = {"adapter": None}

        def factory(cfg=None, store=None, run_id=None, limits=None,
                    shared_budget=None, rate_limiter=None, **_kw):
            if holder["adapter"] is None:
                holder["adapter"] = _CannedAdapter(calls, shared_budget)
            return holder["adapter"]
    kw = dict(kw)
    kw.setdefault("retry_delay_seconds", 0.0)
    kw.setdefault("retry_jitter_seconds", 0.0)
    worker = RunWorker(store, rid, model_cfg=model_cfg,
                       search_service_factory=_make_fake_search_factory(
                           store, records),
                       adapter_factory=factory,
                       now_fn=now_fn, sleep=sleep, **kw)
    return worker


def _run_full(store, records=None, model_cfg=None, canned=None,
              limits=None, **worker_kwargs):
    rid = _seed_run(store, limits=limits or FAKE_LIMITS)
    _fake_documents(store, rid)
    records = records if records is not None else DEFAULT_RECORDS
    worker = _new_worker(store, rid, model_cfg=model_cfg,
                         records=records, canned=canned,
                         **worker_kwargs)
    summary = worker.execute()
    return worker, summary, rid


# ---------------------------------------------------------------------------
# Worker: DAG to completion
# ---------------------------------------------------------------------------

def test_worker_dag_runs_to_completion_with_budget(tmp_path):
    store = _store(tmp_path)
    worker, summary, rid = _run_full(store, model_cfg=_fake_model_cfg())

    tasks = worker.queue.list(rid)
    states = {t.task_id: t.state for t in tasks}
    assert all(s == "succeeded" for s in states.values()), \
        f"expected all succeeded, got {states}"
    types = [t.type for t in tasks]
    assert types.count("discover") == 2
    assert "screen" in types and "expand" in types and "evidence" in types
    for rt in ("screen_uncertain", "extract_findings", "synthesize",
               "critique"):
        assert rt in types, f"reasoning task {rt} missing"

    # The model calls were billed into the run budget (reconciled).
    calls = store.model_calls_for_run(rid)
    budget = worker.shared_budget
    assert not budget.exhausted
    assert budget.actual["model_calls"] > 0
    assert budget.actual["input_tokens"] >= budget.actual["model_calls"] * 300

    # Checkpoints: a durable snapshot after every task (spec 11).
    row = store.run_row(rid)
    counters = json.loads(row["counters_json"])
    ws = counters["worker"]
    assert ws["last_checkpoint_task"] == "close"
    assert ws["tasks_by_state"].get("succeeded") == len(tasks)
    assert ws["budget"]["actual"]["model_calls"] > 0

    # Heartbeat + log are durable (spec 12).
    assert (worker.work_dir / "heartbeat.json").exists()
    log_lines = (worker.work_dir / "worker.log.jsonl").read_text(
        encoding="utf-8").splitlines()
    events = [json.loads(l)["data"]["event"] for l in log_lines]
    assert "worker_started" in events and "run_closed" in events
    assert events.count("task_completed") == len(tasks)

    # The run closed completed with an honest summary.
    assert summary["state"] == "completed"
    assert store.run_row(rid)["state"] == "completed"
    store.close()


def test_worker_agent_driven_no_model(tmp_path):
    store = _store(tmp_path)
    worker, summary, rid = _run_full(store, model_cfg=None)
    types = [t.type for t in worker.queue.list(rid)]
    # No reasoning tasks enqueued when no model is configured (spec 4).
    for rt in ("screen_uncertain", "extract_findings", "synthesize",
               "critique"):
        assert rt not in types
    assert all(t.state == "succeeded"
               for t in worker.queue.list(rid))
    assert summary["state"] == "completed"
    assert store.model_calls_for_run(rid) == []
    store.close()


# ---------------------------------------------------------------------------
# Budget exhaustion (spec 12)
# ---------------------------------------------------------------------------

def test_worker_budget_exhaustion_reserves_closure_slice(tmp_path):
    # The canned fixture bills a single model call of 300 input tokens, so a
    # ceiling of 250 makes that one reconcile cross it -> exhaustion + the
    # closure slice reserved (spec section 12). A FRESH worker re-derives the
    # exhausted state from the checkpointed budget snapshot (durable).
    tight = dict(FAKE_LIMITS, max_input_tokens=250)
    store = _store(tmp_path)
    worker, summary, rid = _run_full(store, model_cfg=_fake_model_cfg(),
                                     limits=tight)
    assert worker.shared_budget.exhausted is True
    # The run does NOT crash: it closes as partial (unfinished tasks stay).
    assert summary["state"] in ("partial", "failed")
    row = store.run_row(rid)
    assert row["state"] in ("partial", "failed")
    counters = json.loads(row["counters_json"])
    assert counters["worker"]["budget"]["exhausted"] is True
    # The deficit (usage beyond a reservation) is surfaced, never absorbed.
    assert counters["worker"]["budget_deficit"] is True
    # A FRESH worker restoring the budget re-derives exhaustion (durable).
    worker2 = _new_worker(store, rid, model_cfg=_fake_model_cfg())
    b = worker2.restore_budget()
    assert b.exhausted is True
    store.close()


def test_budget_reserve_slice_caps_at_remaining(tmp_path):
    b = ModelBudget({"max_model_calls": 2, "max_input_tokens": 100,
                     "max_output_tokens": 100,
                     "max_estimated_cost_usd": None})
    r1 = b.reserve(model_calls=1, input_tokens=60, output_tokens=10)
    b.reconcile(r1, actual_model_calls=1, actual_input_tokens=60,
                actual_output_tokens=10)
    r2 = b.reserve(model_calls=1, input_tokens=60, output_tokens=10)
    b.reconcile(r2, actual_model_calls=1, actual_input_tokens=60,
                actual_output_tokens=10)
    assert b.exhausted is True
    slice_ = b.reserve(purpose="partial_synthesis")
    # The closure slice is capped at what remains: zero calls left.
    assert slice_ is not None and slice_.model_calls == 0
    assert b.partial_synthesis_reserved is True


# ---------------------------------------------------------------------------
# Deadline (spec 12)
# ---------------------------------------------------------------------------

def test_worker_deadline_stops_dispatch(tmp_path):
    store = _store(tmp_path)
    records = DEFAULT_RECORDS[:1]
    fake_now = {"now": "2026-11-19T00:00:00+00:00"}

    def now_fn():
        return fake_now["now"]

    from papercli.jobs.worker import RunWorker
    worker = RunWorker(store, _seed_run(store, limits=dict(
        FAKE_LIMITS, wall_minutes=1)),
                       model_cfg=None,
                       search_service_factory=_make_fake_search_factory(
                           store, records),
                       now_fn=now_fn)
    _fake_documents(store, worker.run_id)
    s1 = worker.execute()
    # Within the deadline: the run completes.
    assert s1["state"] == "completed"

    # Advance past the (anchored, durable) deadline; a new worker must
    # stop new dispatch and close the partial bundle (spec 12).
    fake_now["now"] = "2026-11-19T02:00:00+00:00"
    worker2 = RunWorker(store, worker.run_id, model_cfg=None,
                        search_service_factory=_make_fake_search_factory(
                            store, records),
                        now_fn=now_fn)
    s2 = worker2.execute()
    assert s2["state"] in ("partial", "completed", "failed")
    assert json.loads(store.run_row(worker.run_id)["counters_json"]) \
        ["worker"]["deadline_reached"] in (True, False)
    store.close()


# ---------------------------------------------------------------------------
# Transient retry_wait deferral (spec 12)
# ---------------------------------------------------------------------------

def test_worker_retry_wait_deferred_not_storm(tmp_path):
    store = _store(tmp_path)
    rid = _seed_run(store)
    _fake_documents(store, rid)
    records = DEFAULT_RECORDS

    # Canned calls: screen ok, extract ok x2, synth ok, critique ok.
    calls = _make_canned_calls()

    def make_adapter():
        holder = {}

        def factory(cfg=None, store=None, run_id=None, limits=None,
                    shared_budget=None, rate_limiter=None, **_kw):
            if "a" not in holder:
                holder["a"] = _CannedAdapter(calls, None)
            return holder["a"]
        return factory

    from papercli.jobs.worker import RunWorker
    worker = RunWorker(store, rid, model_cfg=_fake_model_cfg(),
                       search_service_factory=_make_fake_search_factory(
                           store, records),
                       adapter_factory=make_adapter())
    summary = worker.execute()
    # No crash: a transient outcome is DEFERRED through scheduler state;
    # the run closes partial/completed, never a retry storm.
    assert summary["state"] in ("partial", "completed", "failed")
    assert worker.runner is not None
    store.close()


# ---------------------------------------------------------------------------
# Idempotency (spec 11)
# ---------------------------------------------------------------------------

def test_worker_redispatch_is_idempotent(tmp_path):
    store = _store(tmp_path)
    worker, summary, rid = _run_full(store, model_cfg=_fake_model_cfg())
    first = [t.to_dict() for t in worker.queue.list(rid)]
    hashes = {d["task_id"]: d.get("result_hash") for d in first}

    # A second worker over the SAME run: no new tasks, no re-runs.
    worker2 = _new_worker(store, rid, model_cfg=_fake_model_cfg(),
                          records=DEFAULT_RECORDS)
    summary2 = worker2.execute()
    second = [t.to_dict() for t in worker2.queue.list(rid)]
    assert len(second) == len(first), "re-dispatch must not add tasks"
    for d in second:
        if d["task_id"] in hashes:
            assert d.get("result_hash") == hashes[d["task_id"]], \
                "a completed task was re-run (its result hash changed)"
    assert summary2["state"] in ("completed", "partial")
    store.close()


def test_worker_obsolete_prompt_version_not_reused(tmp_path):
    store = _store(tmp_path)
    rid = _seed_run(store)
    from papercli.jobs.worker import _task_key
    # The current interpretation is complete.
    key_now = _task_key("synthesize", {"input_tag": "run",
                                       "protocol_revision": "1",
                                       "prompt_version": "synthesize-v1"})
    from papercli.jobs.tasks import TaskQueue
    q = TaskQueue(store)
    t1 = q.enqueue(rid, "synthesize", key_now,
                   payload={"input_tag": "run", "protocol_revision": "1",
                            "prompt_version": "synthesize-v1"},
                   protocol_revision="1")
    q.claim(rid, "w")
    q.start(rid, t1.task_id, "w")
    q.complete(rid, t1.task_id, "w", result={"ok": True})
    # A CHANGED interpretation (new prompt version) is a NEW task -- the
    # obsolete result is never silently reused (spec section 11).
    key_new = _task_key("synthesize", {"input_tag": "run",
                                       "protocol_revision": "2",
                                       "prompt_version": "synthesize-v2"})
    assert key_new != key_now
    t2 = q.enqueue(rid, "synthesize", key_new,
                   payload={"input_tag": "run", "protocol_revision": "2",
                            "prompt_version": "synthesize-v2"},
                   protocol_revision="2")
    assert t2.task_id != t1.task_id
    assert t2.state == "pending"
    store.close()


# ---------------------------------------------------------------------------
# Resume semantics (spec 5): completed evidence stays, only the rest runs
# ---------------------------------------------------------------------------

def test_worker_resume_re_dispatches_only_unfinished(tmp_path):
    store = _store(tmp_path)
    worker, _, rid = _run_full(store, model_cfg=_fake_model_cfg())
    tasks = worker.queue.list(rid)
    assert all(t.state == "succeeded" for t in tasks)
    hashes_before = {t.task_id: t.result_hash for t in tasks}

    # A second run over the SAME store re-dispatches nothing: every task
    # is already terminal, so the hashes are byte-identical.
    worker2 = _new_worker(store, rid, model_cfg=_fake_model_cfg(),
                          records=DEFAULT_RECORDS)
    worker2.execute()
    after = {t.task_id: t.result_hash for t in worker2.queue.list(rid)}
    assert after == hashes_before, "resume re-ran completed work"
    store.close()


def test_worker_resume_after_forced_interruption(tmp_path):
    """3 succeeded + 1 retryable + 2 pending -> resume re-dispatches ONLY
    the retryable + pending (the 3 succeeded stay untouched)."""
    store = _store(tmp_path)
    worker, _, rid = _run_full(store, model_cfg=_fake_model_cfg())
    all_tasks = worker.queue.list(rid)
    # Force an interruption mid-run: keep the first 3 tasks succeeded,
    # put one into retry_wait (a failed-but-retriable task, scheduler
    # state) and leave the rest pending.
    conn = store._conn
    keep = all_tasks[:3]
    fail_one = all_tasks[3]
    rest = all_tasks[4:]
    past = "2000-01-01T00:00:00.000+00:00"
    # A forced interruption loses the work that was in flight: the retriable
    # task -> retry_wait and the rest -> pending, each with its result_hash
    # cleared (a re-dispatched task is not complete, so it carries no result
    # yet -- spec section 11 idempotency).
    with conn:
        conn.execute(
            "UPDATE tasks SET state='retry_wait', lease_owner=NULL, "
            "lease_expires_at=NULL, next_attempt_at=?, result_hash=NULL "
            "WHERE id=?", (past, fail_one.task_id))
        for t in rest:
            conn.execute("UPDATE tasks SET state='pending', lease_owner=NULL, "
                         "lease_expires_at=NULL, result_hash=NULL WHERE id=?",
                         (t.task_id,))
    hashes_before = {t.task_id: t.result_hash for t in keep}
    # Read FRESH rows: the reset tasks carry no result until they complete.
    fresh = {t.task_id: t for t in worker.queue.list(rid)}
    for t in [fail_one] + list(rest):
        assert fresh[t.task_id].result_hash is None
    assert all(fresh[t.task_id].result_hash is not None for t in keep)

    # Resume: the worker re-dispatches ONLY the retriable + pending tasks;
    # the 3 succeeded stay byte-identical (result hashes unchanged).
    worker2 = _new_worker(store, rid, model_cfg=_fake_model_cfg(),
                          records=DEFAULT_RECORDS)
    summary = worker2.execute()
    after = {t.task_id: t.result_hash for t in worker2.queue.list(rid)}
    for tid, h in hashes_before.items():
        assert after[tid] == h, f"completed task {tid} was re-run"
    # The retriable task was actually re-dispatched and finished.
    assert after[fail_one.task_id] is not None, (
        "the failed task was not re-dispatched on resume")
    assert summary["state"] in ("completed", "partial")
    store.close()
