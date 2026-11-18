"""Phase 5 batch 2: the model-driven reasoning tasks.

Fully offline: a fake model backend (httpx MockTransport) stands in for
the endpoint. Each task is exercised with an ``adapter_factory`` that
returns a :class:`ModelAdapter` over the mock, so no live model call
happens in the offline suite.
"""

from __future__ import annotations

import json

import httpx
import pytest

from papercli.config.loader import Config
from papercli.models.adapter import ModelAdapter
from papercli.reasoning import (
    TASKS,
    critique,
    extract_findings,
    screen_uncertain,
    synthesize,
)
from papercli.storage.db import PaperStore


def _cfg(tmp_path) -> Config:
    """A config with a model block + a .env key, so model_available() is True
    without touching the real environment."""
    import os
    data = {
        "model": {"adapter": "openai_compatible",
                  "base_url": "https://fake.local",
                  "model": "test-model",
                  "api_key_env": "PAPERS_TEST_LLM_KEY"},
        "limits": {"max_model_calls": 50, "max_input_tokens": 100000,
                    "max_output_tokens": 20000},
    }
    return Config(data, sources=["test"],
                  env={"PAPERS_TEST_LLM_KEY": "test-key"})


def _fake_adapter(responses, *, on_record=None):
    """Build a ModelAdapter over an httpx MockTransport that replays
    ``responses`` in order (the last one repeats for extra calls)."""
    state = {"i": 0}
    def next_response() -> httpx.Response:
        idx = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[idx]
    transport = httpx.MockTransport(lambda req: next_response())
    client = httpx.Client(transport=transport)
    return ModelAdapter(base_url="https://fake.local", model="test-model",
                        api_key="test-key", client=client,
                        on_record=on_record or (lambda r: None))


def _ok(content: str, usage=None) -> httpx.Response:
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def _make_run(store: PaperStore, protocol: dict | None = None) -> str:
    rid = store.create_run(state="running")
    protocol = protocol or {
        "question": "Q", "protocol_revision": 1,
        "inclusion_criteria": ["cancer immunotherapy"],
        "exclusion_criteria": ["animals only"],
        "themes_to_investigate": ["theme-A"],
    }
    import json as _json
    with store._conn:
        store._conn.execute("UPDATE runs SET plan_json = ? WHERE id = ?",
                            (_json.dumps(protocol, sort_keys=True), rid))
    return rid


def _add_paper(store: PaperStore, run_id: str, doi: str, title: str,
               year: int, authors=None):
    return store.upsert_paper(run_id, {"doi": doi}, title=title,
                              year=year, authors=authors or ["Smith"])


# ---------------------------------------------------------------------------
# screen_uncertain
# ---------------------------------------------------------------------------

def test_screen_uncertain_model_decision_full_fields(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    pid = _add_paper(store, rid, "10.1/a", "T1", 2021)
    store.record_uncertainty(rid, pid, stage="title_abstract",
                             reason="ambiguous", status="queued")
    adapter = _fake_adapter([_ok(json.dumps(
        {"decision": "include", "criterion_ids": ["inc-1"],
         "rationale": "matches cancer immunotherapy",
         "evidence_basis": "abstract"}),
        usage={"prompt_tokens": 10, "completion_tokens": 5})])
    cfg = _cfg(tmp_path)
    res = screen_uncertain(store, {"run_id": rid}, cfg=cfg,
                          adapter_factory=lambda **kw: adapter)
    assert res["status"] == "succeeded"
    assert res["resolved"] == 1
    scr = [r for r in store.screenings_for_run(rid)
           if r["decision"] == "include"]
    assert scr
    row = scr[0]
    # Full spec section 9 field set: decision, criteria, rationale,
    # evidence basis, reviewer, prompt version, timestamp.
    assert row["criterion_ids"] == ["inc-1"]
    assert row["rationale"]
    assert row["evidence_basis"] == "abstract"
    assert row["reviewer"] == "model/test-model"
    assert row["prompt_version"]
    assert row["decided_at"]
    # The uncertainty row is resolved.
    unc = store.uncertainty_for_run(rid)
    assert unc[0]["status"] == "resolved"
    assert unc[0]["rechecks"] == 1
    store.close()


def test_screen_uncertain_no_model_skips_deterministic(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    pid = _add_paper(store, rid, "10.1/b", "T2", 2020)
    store.record_uncertainty(rid, pid, stage="title_abstract",
                             reason="x", status="queued")
    # cfg=None -> no model -> skipped_no_model, run continues deterministically.
    res = screen_uncertain(store, {"run_id": rid}, cfg=None)
    assert res["status"] == "skipped_no_model"
    assert res["pending_uncertain"] == 1
    # Uncertainty row still queued (nothing force-decided).
    assert store.uncertainty_for_run(rid)[0]["status"] == "queued"
    store.close()


def test_screen_uncertain_leftover_reported_unresolved(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    pid = _add_paper(store, rid, "10.1/c", "T3", 2019)
    store.record_uncertainty(rid, pid, stage="title_abstract",
                             reason="x", status="queued")
    adapter = _fake_adapter([_ok(json.dumps({"decision": "uncertain",
                                             "rationale": "cannot tell"}))])
    res = screen_uncertain(store, {"run_id": rid}, cfg=_cfg(tmp_path),
                          adapter_factory=lambda **kw: adapter)
    assert res["status"] == "succeeded"
    assert res["unresolved"]
    assert store.uncertainty_for_run(rid)[0]["status"] == "unresolved"
    store.close()


# ---------------------------------------------------------------------------
# extract_findings (access-level gate)
# ---------------------------------------------------------------------------

def test_extract_findings_gates_on_access_level(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    pid = _add_paper(store, rid, "10.1/d", "T4", 2022)
    # abstract_only document -> gated out (never-infer rule).
    doc = store.record_document(pid, access_status="retrieved_abstract")
    store.set_paper_access_level(pid, "abstract_only")
    res = extract_findings(store, {"run_id": rid}, cfg=_cfg(tmp_path),
                          adapter_factory=lambda **kw: _fake_adapter(
                              [_ok('{"findings": []}')]))
    assert res["status"] == "succeeded"
    assert res["documents_gated"] == 1
    assert res["findings_total"] == 0
    assert res["evidence_ids"] == []
    assert "abstract" in res["gated_documents"][0]["access_level"] or \
        res["gated_documents"][0]["access_level"] == "abstract_only"
    store.close()


def test_extract_findings_full_text_produces_findings(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    pid = _add_paper(store, rid, "10.1/e", "T5", 2023)
    doc = store.record_document(pid, access_status="retrieved_full_text")
    store.set_paper_access_level(pid, "full_text")
    store.store_document_text(doc, [
        {"page": 1, "section": "Results", "paragraph": 0,
         "text": "The treatment group showed a 24% reduction in risk."}])
    findings = {"findings": [
        {"finding": "24% risk reduction in the treatment group",
         "claim_type": "reported_result", "source_blocks": [1],
         "units": "%", "population_or_system": "treatment group"}]}
    adapter = _fake_adapter([_ok(json.dumps(findings),
                                 usage={"prompt_tokens": 8,
                                        "completion_tokens": 20})])
    res = extract_findings(store, {"run_id": rid}, cfg=_cfg(tmp_path),
                          adapter_factory=lambda **kw: adapter)
    assert res["status"] == "succeeded"
    assert res["findings_total"] == 1
    assert res["documents_gated"] == 0
    ev = store.evidence_for_run(rid)
    assert len(ev) == 1
    assert ev[0]["finding"].startswith("24%")
    assert ev[0]["claim_type"] == "reported_result"
    assert ev[0]["access_level"] == "full_text"
    # Numeric finding carried units + context -> no numeric warnings.
    assert not ev[0]["extraction_warnings"]
    store.close()


def test_extract_findings_numeric_missing_units_flagged(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    pid = _add_paper(store, rid, "10.1/f", "T6", 2021)
    doc = store.record_document(pid, access_status="retrieved_full_text")
    store.set_paper_access_level(pid, "full_text")
    store.store_document_text(doc, [
        {"page": 1, "section": "Results", "paragraph": 0,
         "text": "Effect size was 0.5."}])
    findings = {"findings": [
        {"finding": "Effect size was 0.5", "claim_type": "reported_result",
         "source_blocks": [1], "units": None, "population_or_system": None}]}
    adapter = _fake_adapter([_ok(json.dumps(findings))])
    res = extract_findings(store, {"run_id": rid}, cfg=_cfg(tmp_path),
                          adapter_factory=lambda **kw: adapter)
    ev = store.evidence_for_run(rid)
    # Numeric finding WITHOUT units/context -> flagged.
    assert ev[0]["extraction_warnings"]
    store.close()


def test_extract_findings_no_model_skips(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    res = extract_findings(store, {"run_id": rid}, cfg=None)
    assert res["status"] == "skipped_no_model"
    store.close()


# ---------------------------------------------------------------------------
# synthesize (registry-only citations)
# ---------------------------------------------------------------------------

def test_synthesize_drops_unregistered_citation_keys(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    p1 = _add_paper(store, rid, "10.1/g", "Paper G", 2020,
                    authors=["Garcia"])
    p2 = _add_paper(store, rid, "10.1/h", "Paper H", 2021,
                    authors=["Huang"])
    # A claim citing one registered + one FABRICATED key.
    draft = {"themes": [
        {"theme": "theme-A",
         "claims": [
             {"text": "G shows effect",
              "citation_keys": ["garcia2020", "fakedkey2099"],
              "evidence_ids": [], "is_synthesis": False}],
         "gaps": ["no RCTs"]}],
        "limitations": ["small n"], "contradictions": []}
    adapter = _fake_adapter([_ok(json.dumps(draft))])
    res = synthesize(store, {"run_id": rid}, cfg=_cfg(tmp_path),
                     adapter_factory=lambda **kw: adapter)
    assert res["status"] == "succeeded"
    # The fabricated key is flagged + dropped; the registered one survives.
    dropped = res["dropped_citation_keys"]
    assert any(d["citation_key"] == "fakedkey2099" for d in dropped)
    theme0 = res["draft"]["themes"][0]
    kept_keys = theme0["claims"][0]["citation_keys"]
    assert "fakedkey2099" not in kept_keys
    # Claim ledger persisted with the kept (registry) key.
    claims = store.claims_for_run(rid)
    assert claims
    # Correction/retraction metadata check recorded with limits.
    assert res["correction_check"]["limits"]
    store.close()


def test_synthesize_no_model_skips(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    res = synthesize(store, {"run_id": rid}, cfg=None)
    assert res["status"] == "skipped_no_model"
    store.close()


# ---------------------------------------------------------------------------
# critique (labels applied + unsupported handled)
# ---------------------------------------------------------------------------

def test_critique_applies_labels_and_handles_unsupported(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    pid = _add_paper(store, rid, "10.1/i", "Paper I", 2022,
                     authors=["Ivanov"])
    c1 = store.record_claim(rid, claim_text="grounded claim",
                            location="theme::A",
                            evidence_ids=[], audit_status="uncertain")
    c2 = store.record_claim(rid, claim_text="ungrounded claim",
                            location="theme::A",
                            evidence_ids=[], audit_status="uncertain")
    audit = {"claims": [
        {"claim_id": c1, "label": "supported",
         "action": "keep", "reason": "evidence links"},
        {"claim_id": c2, "label": "unsupported",
         "action": "mark_unresolved", "reason": "no evidence"}]}
    adapter = _fake_adapter([_ok(json.dumps(audit))])
    res = critique(store, {"run_id": rid}, cfg=_cfg(tmp_path),
                   adapter_factory=lambda **kw: adapter)
    assert res["status"] == "succeeded"
    assert res["claims_audited"] == 2
    assert res["labels_applied"][c1] == "supported"
    assert res["labels_applied"][c2] == "unsupported"
    # Unsupported claim was prominently marked unresolved in the revision.
    marked = [k for k in res["revision"]["kept"]
              if k.get("marked_unresolved")]
    assert any(k["claim_id"] == c2 for k in marked)
    # Persisted labels: c1 supported, c2 unsupported.
    claims = {c["id"]: c for c in store.claims_for_run(rid)}
    assert claims[c1]["audit_status"] == "supported"
    assert claims[c2]["audit_status"] == "unsupported"
    store.close()


def test_critique_no_model_keeps_uncertain(tmp_path):
    store = PaperStore(tmp_path / "s.db")
    rid = _make_run(store)
    pid = _add_paper(store, rid, "10.1/j", "Paper J", 2021,
                     authors=["Jones"])
    c1 = store.record_claim(rid, claim_text="x", location="theme::A",
                            evidence_ids=[], audit_status="uncertain")
    res = critique(store, {"run_id": rid}, cfg=None)
    assert res["status"] == "skipped_no_model"
    assert store.claims_for_run(rid)[0]["audit_status"] == "uncertain"
    store.close()


# ---------------------------------------------------------------------------
# Task registry wiring
# ---------------------------------------------------------------------------

def test_task_registry_dispatch_table():
    from papercli.reasoning import TASKS as registry
    assert set(registry) == {"screen_uncertain", "extract_findings",
                             "synthesize", "critique"}
    assert registry is TASKS
    for name, fn in registry.items():
        assert callable(fn)
