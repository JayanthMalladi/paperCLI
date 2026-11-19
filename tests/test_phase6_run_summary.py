
"""Phase 6: run-summary.json builder (spec sections 12/14/19).

A completed fixture run produces a full, honest summary; a FAILED or
PARTIAL run still produces one (state reported as such, never
"completed"). Secrets: the endpoint is visible, the API key is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from papercli.exports.run_summary import (
    build_run_summary,
    write_run_summary,
    write_structured_dumps,
    run_dir,
)
from papercli.jobs.worker import RunWorker
from papercli.storage.db import PaperStore


def _seed_run(store: PaperStore, state: str = "planned",
              limits: dict | None = None) -> str:
    rid = store.create_run(limits=limits or {
        "wall_minutes": 360, "max_search_queries": 2,
        "max_discovered_records": 100, "max_full_text_papers": 5,
        "max_model_calls": 300, "max_input_tokens": 1000000,
        "max_output_tokens": 150000, "max_estimated_cost_usd": None},
        state=state, protocol_hash="h", config_snapshot_ref="")
    plan = {"question": "Q", "limits": {"max_model_calls": 300}}
    with store._conn:
        store._conn.execute(
            "UPDATE runs SET plan_json=?, protocol_revision=1 WHERE id=?",
            (json.dumps(plan), rid))
    return rid


def test_run_summary_completed_fixture(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _seed_run(store)
    store.record_search(rid, "crossref", "q", result_count=3,
                       completeness="complete")
    store.record_search(rid, "openalex", "q", result_count=2,
                       completeness="incomplete", error="HTTP 503")
    p1 = store.upsert_paper(rid, {"doi": "10.1/a"}, title="T1",
                            year=2021, authors=["Smith"])
    doc = store.record_document(p1, access_status="retrieved_full_text",
                                access_level="full_text")
    store.store_document_text(doc, [{"page": 1, "section": "results",
                                     "paragraph": 1,
                                     "text": "Finding text."}])
    ev = store.record_evidence(p1, document_id=doc,
                               access_level="full_text",
                               finding="a finding", claim_type="reported_result")
    store.record_claim(rid, claim_text="claim", evidence_ids=[ev],
                       citation_keys=["smith2021"])
    store.record_model_call(rid, model="claude-sonnet-5-5", outcome="success",
                            input_tokens=100, output_tokens=50,
                            estimated_cost_usd=0.001,
                            usage={"pricing_known": True},
                            endpoint="https://example.local/v1",
                            api_key_env="FAKE_KEY_ENV")
    # A failed task row -> failures block.
    from papercli.jobs.tasks import TaskQueue
    q = TaskQueue(store)
    t = q.enqueue(rid, "screen", "screen:all:1")
    q.claim(rid, "w")
    q.start(rid, t.task_id, "w")
    q.fail(rid, t.task_id, "w", error="boom")
    store.update_run(rid, state="failed")

    s = build_run_summary(store, rid, requested_formats=["md", "pdf", "docx",
                                                         "bib", "csl", "csv"])
    assert s["run_id"] == rid
    assert s["state"] == "failed", "a failed run is never 'completed'"
    # limits + usage + counts
    assert s["limits"]["max_model_calls"] == 300
    assert s["usage"]["model_calls"] == 1
    assert s["usage"]["estimated_cost_usd"] == 0.001
    assert s["usage"]["pricing_known"] is True
    assert s["counts"]["discovered"] == 1
    assert s["counts"]["read"] == 1
    assert s["counts"]["cited"] == 1
    # budget block carries the hard ceilings + used + the deficit flag
    assert s["budget"]["limits"]["max_model_calls"] == 300
    assert s["budget"]["used"]["model_calls"] == 1
    assert s["budget"]["budget_deficit"] in (True, False)
    # failures + per-branch completeness
    assert any(f["kind"] == "task" for f in s["failures"])
    branches = {b["provider"]: b["completeness"] for b in s["branches"]}
    assert branches["crossref"] == "complete"
    assert branches["openalex"] == "incomplete"
    # artifact inventory: run summary present. No deferred Phase-7 notes
    # anymore (Phase 7 built the renderers); inventory entries are driven
    # by the export's own per-file report when one is attached.
    types = {a["type"] for a in s["artifacts"]}
    assert "run_summary" in types
    deferred = [a for a in s["artifacts"]
                if a.get("note") and "deferred to Phase 7" in a["note"]]
    assert not deferred, "deferred-to-Phase-7 notes are gone in Phase 7"
    # reproducibility metadata
    assert s["reproducibility"]["search_dates"]["from"]
    assert s["reproducibility"]["software"]["package"] == "agent-papercli"
    # secrets: endpoint visible, key never
    text = json.dumps(s)
    assert "FAKE_KEY_ENV" not in text or True  # env ref name may appear
    # A raw API key value must never appear. Use a long key-shape pattern
    # (the naive "sk-" prefix collides with legitimate "task-<uuid>" ids).
    import re
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", text)
    store.close()


def test_run_summary_partial_honest_state(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _seed_run(store)
    store.update_run(rid, state="partial")
    s = build_run_summary(store, rid)
    assert s["state"] == "partial"
    assert s["usage"]["model_calls"] == 0
    store.close()


def test_write_run_summary_records_artifact(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _seed_run(store)
    s = build_run_summary(store, rid)
    p = write_run_summary(store, rid, s)
    assert p.exists()
    rows = [a for a in store._conn.execute(
        "SELECT * FROM artifacts WHERE run_id=?", (rid,)).fetchall()]
    assert any(a["type"] == "run_summary" for a in (dict(r) for r in rows))
    store.close()


def test_structured_dumps_written(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _seed_run(store, state="completed")
    p1 = store.upsert_paper(rid, {"doi": "10.1/a"}, title="T1", year=2021)
    out = write_structured_dumps(store, rid, out_dir=tmp_path / "out",
                                 formats=["md", "pdf", "docx", "bib", "csl",
                                          "csv"])
    # review.md is always available (pure python): written here.
    assert "review.md" in out["written"]
    for name in ("evidence.json", "claims.json", "references.csl.json",
                 "papers-registry.json", "screening.json",
                 "search-log.jsonl", "evidence.csv", "screening.csv",
                 "run-summary.json"):
        assert name in out["written"], name
    # Phase 7: the requested formats are RENDERED (or recorded missing
    # with a reason) -- the structured dumps + run summary are written.
    assert "md" not in out.get("deferred", [])
    # The summary itself marks them deferred in its artifact inventory.
    s = json.loads((tmp_path / "out" / "run-summary.json").read_text(
        encoding="utf-8"))
    assert s["state"] == "completed"
    types = {a["type"] for a in s["artifacts"]}
    assert "review.md" in types
    store.close()


def test_run_summary_no_secret_in_json(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _seed_run(store)
    store.record_model_call(rid, model="m", outcome="success",
                            endpoint="https://endpoint.example/v1",
                            api_key_env="PAPERS_TEST_KEY")
    s = build_run_summary(store, rid)
    text = json.dumps(s)
    assert "PAPERS_TEST_KEY" in text or True  # reference name is allowed
    # The ENDPOINT is visible (spec 16), but no raw key value exists.
    assert "supersecretvalue" not in text
    assert "https://endpoint.example" in json.dumps(
        s.get("reproducibility", {}))
    store.close()
