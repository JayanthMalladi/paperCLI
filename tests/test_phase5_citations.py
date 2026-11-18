"""Phase 5 batch 3: the claim-ledger citations audit + CLI.

Offline: the store + audit are exercised in-process; the CLI is driven
through the same shim the rest of the suite uses. No live model calls.
"""

from __future__ import annotations

import json

from conftest import run_papers
from papercli.citations.audit import (
    audit_citations,
    claim_audit_rows,
    reference_identity_checks,
)
from papercli.storage.db import PaperStore


def _seed(store: PaperStore) -> str:
    rid = store.create_run(state="running")
    p1 = store.upsert_paper(rid, {"doi": "10.1/p1"}, title="Alpha",
                            year=2021, authors=["Alpha A"])
    p2 = store.upsert_paper(rid, {"doi": "10.1/p2"}, title="Beta",
                            year=2022, authors=["Beta B"])
    doc = store.record_document(p1, access_status="retrieved_full_text",
                                identity_status="matched")
    store.record_evidence(p1, document_id=doc, access_level="full_text",
                          finding="alpha effect", claim_type="reported_result",
                          supporting_excerpt="alpha shows an effect")
    return rid


def test_claim_ledger_persists_with_citation_keys(tmp_path):
    store = PaperStore(tmp_path / "c.db")
    rid = _seed(store)
    c = store.record_claim(rid, claim_text="claim text",
                          location="theme::T1",
                          evidence_ids=["ev-x"],
                          citation_keys=["alpha2021"],
                          audit_status="supported")
    rows = store.claims_for_run(rid)
    assert rows[0]["claim_text"] == "claim text"
    assert rows[0]["evidence_ids"] == ["ev-x"]
    assert rows[0]["citation_keys"] == ["alpha2021"]
    assert rows[0]["location"] == "theme::T1"
    assert rows[0]["audit_status"] == "supported"
    store.close()


def test_supported_claim_without_evidence_downgraded(tmp_path):
    store = PaperStore(tmp_path / "c.db")
    rid = _seed(store)
    store.record_claim(rid, claim_text="unsupported really",
                      location="theme::T2", evidence_ids=[],
                      citation_keys=["alpha2021"],
                      audit_status="supported")
    rows = claim_audit_rows(store, rid)
    assert rows[0]["audit_status"] == "uncertain"
    assert any("DOI resolution" in d for d in rows[0]["disclosures"])
    store.close()


def test_fabricated_citation_key_flagged(tmp_path):
    store = PaperStore(tmp_path / "c.db")
    rid = _seed(store)
    store.record_claim(rid, claim_text="uses fake key",
                      location="theme::T3",
                      evidence_ids=[],
                      citation_keys=["fakenote2099"],
                      audit_status="uncertain")
    checks = reference_identity_checks(store, rid)
    assert "fakenote2099" in checks["unregistered_keys_used"]
    store.close()


def test_audit_envelope_shape_and_coverage_disclosure(tmp_path):
    store = PaperStore(tmp_path / "c.db")
    rid = _seed(store)
    store.record_claim(rid, claim_text="ok claim", location="theme::T1",
                      evidence_ids=[], citation_keys=[],
                      audit_status="supported")
    data = audit_citations(store, rid)
    assert data["claims"]["total"] == 1
    assert data["coverage_disclosure"]["exhaustive_coverage_claimed"] is False
    assert "NOT a ruling" in data["correction_retraction_check"]["limits"][0]
    assert data["references"]["citation_keys_validated"] == 0
    store.close()

def test_citations_audit_cli_envelope(tmp_path):
    """`papers citations audit --run RID` emits one ok JSON envelope."""
    rc, out, err = run_papers("init", cwd=tmp_path)
    assert rc == 0, err
    # The CLI opens the store relative to its CWD (tmp_path); seed the
    # run in that same store.
    store_path = tmp_path / ".papercli" / "storage" / "papercli.db"
    with PaperStore(store_path) as store:
        rid = _seed(store)
    rc, out, err = run_papers("citations", "audit", "--run", rid,
                              cwd=tmp_path)
    assert rc == 0, f"exit {rc}: {err}\n{out}"
    env = json.loads(out)
    assert env["status"] == "ok"
    assert env["data"]["run_id"] == rid
    assert env["data"]["coverage_disclosure"]["exhaustive_coverage_claimed"] is False
    assert "rows" in env["data"]["claims"]


def test_citations_audit_unknown_run_exit_2(tmp_path):
    rc, out, err = run_papers("init", cwd=tmp_path)
    assert rc == 0
    rc, out, err = run_papers("citations", "audit", "--run", "run-nonexistent",
                              cwd=tmp_path)
    assert rc == 2
    env = json.loads(out)
    assert env["status"] == "error"
