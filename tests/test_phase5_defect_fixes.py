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
