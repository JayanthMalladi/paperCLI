"""Phase 5 defect fixes: D-P5-0 (MAJOR, pre-existing Phase-3/Phase-1).

``PaperStore.evidence_for_run`` and the ``papers evidence list`` CLI must
surface records that conform to the PUBLISHED
``agent_contract/schemas/evidence.json`` contract: every record carries
``evidence_id`` (aliased from the stored primary key) -- the schema is
versioned and consumed by other phases + external agents, so the field
name is KEPT and the accessor aliases the key (D-P5-0 fix).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from papercli.agent_contract import validate
from papercli.storage.db import PaperStore

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"


def _make_evidence_store(db_path: Path) -> tuple[PaperStore, str, str, str]:
    store = PaperStore(db_path)
    rid = store.create_run(state="planned")
    pid = store.upsert_paper(rid, {"doi": "10.5/dp5"})
    doc = store.record_document(pid, access_status="retrieved_full_text")
    store.set_paper_access_level(pid, "full_text")
    store.record_evidence(
        pid, document_id=doc, access_level="full_text",
        location={"page": 1, "section": "Results", "paragraph": 0},
        supporting_excerpt="verbatim slice of the stored text",
        finding="A 24% reduction in the treatment group",
        claim_type="reported_result",
        methods={"n": 42},
        population_or_system="adults",
        limitations=["single center"],
        extraction_warnings=["numeric finding carries units"])
    return store, rid, pid, doc


def test_evidence_for_run_records_validate_against_published_schema(tmp_path):
    store, rid, pid, _ = _make_evidence_store(tmp_path / "e.db")
    try:
        records = store.evidence_for_run(rid)
        assert len(records) == 1
        rec = records[0]
        # The published field name: aliased from the stored primary key.
        assert rec["evidence_id"] == rec["id"]
        # Full draft-2020-12 validation: 0 errors (jsonschema installed).
        assert validate(rec, "evidence") == []
    finally:
        store.close()


def test_evidence_list_cli_records_validate_against_schema(tmp_project, tmp_path):
    """``papers evidence list`` output must also carry the published key."""
    from tests.conftest import run_papers
    from papercli.documents.ingest import ingest_local_document
    from papercli.research.evidence import extract_evidence_for_document

    os.chdir(tmp_project)
    try:
        with PaperStore() as store:
            rid = store.create_run(state="planned")
            rep = ingest_local_document(store, rid, str(FIXTURES / "sample_paper.pdf"))
            ev = extract_evidence_for_document(
                store, run_id=rid, paper_id=rep["paper_id"],
                document_id=rep["document_id"])
        rc, out, _ = run_papers("evidence", "list", "--run", rid,
                                cwd=tmp_project,
                                env_strip={"PAPERS_EMAIL"})
        assert rc == 0, out
        env = json.loads(out)
        assert env["status"] == "ok"
        assert env["data"]["count"] == ev["count"] > 0
        for rec in env["data"]["evidence"]:
            assert rec["evidence_id"] == rec["id"]
            assert validate(rec, "evidence") == []
    finally:
        os.chdir(REPO)


def test_budget_deficit_flag_is_surfaced_not_absorbed():
    """D-P5-1: when a call's actual usage exceeds its reservation, the
    result dict AND the recorded model-call row carry budget_deficit=True
    (a Phase-6 run summary can disclose it). Zero usage -> no deficit."""
    import httpx
    from papercli.models.adapter import ModelAdapter
    from papercli.models.rate_limit import ModelBudget, RateLimiter, DEFAULT_BUDGETS

    rows = []
    def on_record(rec):
        rows.append(rec)
        return {"id": 1}

    def make(prompt_tokens, completion_tokens):
        client = httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"choices": [{"message": {"content": "{}"}}],
                           "usage": {"prompt_tokens": prompt_tokens,
                                     "completion_tokens": completion_tokens}})))
        return ModelAdapter(base_url="https://fake.local", model="m",
                            api_key="probe-key-not-secret",
                            client=client,
                            budget=ModelBudget(DEFAULT_BUDGETS),
                            rate_limiter=RateLimiter(),
                            on_record=on_record)

    # The internal reservation holds input_tokens=0, so any nonzero usage
    # is a deficit that must be disclosed, not absorbed.
    a = make(5, 3)
    res = a.complete_json(schema={"type": "object"}, system="s", user="u",
                          task_type="t")
    assert res["ok"] is True
    assert res["budget_deficit"] is True, "deficit must be surfaced in the result"
    assert rows and rows[-1]["budget_deficit"] is True, "deficit must reach the model-call row"
    a.close()

    # Zero usage: no deficit.
    rows.clear()
    b = make(0, 0)
    res0 = b.complete_json(schema={"type": "object"}, system="s", user="u",
                           task_type="t")
    assert res0["ok"] is True
    assert res0["budget_deficit"] is False
    assert rows and rows[-1]["budget_deficit"] is False
    b.close()


def test_wait_seconds_is_pure_query_no_bucket_side_effect():
    """D-P5-2: wait_seconds() reports the wait WITHOUT decrementing the
    bucket (the old implementation called acquire() as a side effect)."""
    from papercli.models.rate_limit import RateLimiter
    lim = RateLimiter(requests_per_minute=60, capacity=1)
    before = lim.tokens
    assert lim.wait_seconds() == 0.0          # a free token is available
    assert lim.tokens == before, "wait_seconds must not consume a token"
    lim.acquire()                             # consume the token
    assert lim.wait_seconds() > 0.0          # now it reports a positive wait
    assert lim.tokens >= 0.0


def test_fail_result_reports_real_retries_waited_on_429():
    """D-P5-3: a bounded 429 backoff path must report the real retry count,
    not a hard-coded 0 (and must not crash)."""
    import httpx
    from papercli.models.adapter import ModelAdapter, ModelCallOutcome
    from papercli.models.rate_limit import ModelBudget, RateLimiter, DEFAULT_BUDGETS

    n = {"count": 0}
    def handler(req):
        n["count"] += 1
        if n["count"] <= 2:
            return httpx.Response(429, json={"error": "rate limited"},
                                  headers={"Retry-After": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": 0,
                                                    "completion_tokens": 0}})
    adapter = ModelAdapter(
        base_url="https://fake.local", model="m",
        api_key="probe-key-not-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=ModelBudget(DEFAULT_BUDGETS),
        rate_limiter=RateLimiter(requests_per_minute=600, capacity=600),
        on_record=lambda rec: {"id": 1})
    res = adapter.complete_json(schema={"type": "object"}, system="s", user="u",
                                task_type="t")
    # Two 429s then a success: the call succeeded AFTER backing off.
    assert res["ok"] is True, res
    assert res["retries_waited"] >= 2, "real 429 backoff count must be reported"
    adapter.close()
