"""Phase 4 (Research) tests: plan, screening, evidence, expansion, task CLI.

Offline, fixture-based (no network -- Phase 2 owns the network smoke
suite). Covers:

* **Plan** -- valid brief -> protocol YAML + planned Run + resource
  estimate; missing required field -> exit 2 naming the field; a changed
  brief -> a NEW run revision with the old Run immutable.
* **Screening** -- deterministic decisions with criterion IDs; an
  ambiguous record -> uncertainty queue -> bounded recheck -> reported
  unresolved; dedup: DOI-normalized duplicates LINKED not merged;
  fuzzy-similar titles linked as candidate signals only.
* **Evidence** -- full-text document -> byte-exact evidence rows;
  metadata/abstract-only documents -> ZERO rows (the never-infer gate);
  scanned document -> partial_full_text cap + grade recorded (D-1/D-3).
* **Task CLI** -- next leases a task; a second worker gets a different
  task; stale/duplicate/source-mismatch submits return the distinct
  codes; identical payload returns the prior result.
* **Expansion** -- fake registry adapters: provenance chains saved; a
  failing provider branch is flagged incomplete while others complete.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from papercli.storage.db import PaperStore  # noqa: E402


def _store(tmp_path: Path) -> PaperStore:
    return PaperStore(tmp_path / "phase4.db")


def _cfg(tmp_path: Path):
    from papercli.config.loader import Config
    return Config.load(cli_overrides={}, cwd=tmp_path)


SAMPLE_BRIEF = """
schema_version: '1.0'
question: Q?
purpose: purpose
scope: scope text
date_range: {from: '2015', to: '2026'}
languages: [en]
include_preprints: true
seed_identifiers: []
inclusion_criteria:
  - 'interventional exercise training in adults'
  - 'cognitive outcomes'
exclusion_criteria:
  - 'animal studies without a human arm'
themes_to_investigate:
  - 'aerobic exercise'
outputs: [md, csv]
limits:
  wall_minutes: 240
  max_search_queries: 8
  max_discovered_records: 200
  max_full_text_papers: 20
  max_model_calls: 100
  max_input_tokens: 200000
  max_output_tokens: 50000
  max_estimated_cost_usd: null
"""


def _write_brief(tmp_path: Path, text: str = SAMPLE_BRIEF,
                 name: str = "brief.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def test_plan_valid_brief_creates_protocol_yaml_and_run(tmp_path):
    from papercli.research.plan import plan_review

    store = _store(tmp_path)
    brief = _write_brief(tmp_path)
    out = tmp_path / "review-plan.yaml"
    res = plan_review(brief, out, store=store, cfg=_cfg(tmp_path), env={})

    assert res["run_state"] == "planned"
    row = store.run_row(res["run_id"])
    assert row["state"] == "planned"
    assert row["plan_json"], "the protocol must be stored on the Run row"
    assert row["protocol_hash"] == res["protocol_hash"]
    assert row["config_snapshot_ref"], "config snapshot ref must be saved"
    assert row["protocol_revision"] == 1

    # The saved YAML is a valid plan document (schema round-trip).
    from papercli.agent_contract import validate
    import yaml
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert doc["plan_id"] == res["plan_id"]
    assert doc["protocol_hash"] == res["protocol_hash"]
    assert validate(doc, "plan", full=False) == []
    assert doc["limits"]["max_discovered_records"] == 200

    # Resource estimate with the spec section 7 fields.
    est = res["plan"]["resource_estimate"]
    assert est["expected_queries"] > 0
    assert est["full_text_cap"] == 20
    assert est["wall_ceiling_minutes"] == 240
    assert est["cost_ceiling_usd"] is None

    # Deterministic per-provider variants (query + filters + include_preprints).
    provs = {q["provider"] for q in res["plan"]["queries"]
             if q.get("provider") != "(truncated)"}
    assert provs == {"crossref", "openalex", "arxiv", "s2"}
    cross = [q for q in res["plan"]["queries"] if q["provider"] == "crossref"][0]
    assert cross["filters"].get("from-pub-date") == "2015"
    assert cross["filters"].get("until-pub-date") == "2026"
    assert cross["include_preprints"] is True
    store.close()


def test_plan_defaults_recorded_in_protocol(tmp_path):
    from papercli.research.plan import plan_review

    brief_text = """
schema_version: '1.0'
question: Q?
purpose: purpose
scope: s
outputs: [md]
limits:
  wall_minutes: 60
  max_search_queries: 1
  max_discovered_records: 10
  max_full_text_papers: 5
  max_model_calls: 10
  max_input_tokens: 100
  max_output_tokens: 100
  max_estimated_cost_usd: null
"""
    store = _store(tmp_path)
    res = plan_review(_write_brief(tmp_path, brief_text),
                      tmp_path / "p.yaml", store=store, cfg=_cfg(tmp_path),
                      env={})
    # Defaults filled the optional fields AND are recorded (spec section 7).
    assert "include_preprints" in res["plan"]["defaults_applied"]
    assert res["plan"]["include_preprints"] is True
    assert res["plan"]["languages"] == ["en"]
    assert "languages" in res["plan"]["defaults_applied"]
    store.close()


def test_plan_missing_required_field_is_preflight_failure(tmp_path):
    from papercli.research.plan import BriefError, plan_review

    store = _store(tmp_path)
    bad = SAMPLE_BRIEF.replace("question: Q?\n", "")
    p = _write_brief(tmp_path, bad, "bad.yaml")
    with pytest.raises(BriefError) as exc:
        plan_review(p, tmp_path / "x.yaml", store=store,
                    cfg=_cfg(tmp_path), env={})
    assert "question" in exc.value.field
    # No Run row was created for the invalid brief.
    assert store._conn.execute(
        "SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    store.close()


def test_plan_missing_limit_field_named(tmp_path):
    from papercli.research.plan import BriefError, plan_review

    store = _store(tmp_path)
    # A brief that is VALID YAML but simply MISSING the max_model_calls
    # limit (rebuild the limits block rather than string-replace one line --
    # dropping the line mid-block corrupts the sibling keys' indentation).
    bad = "\n".join(
        line for line in SAMPLE_BRIEF.splitlines()
        if "max_model_calls" not in line) + "\n"
    p = _write_brief(tmp_path, bad, "bad2.yaml")
    with pytest.raises(BriefError) as exc:
        plan_review(p, tmp_path / "x.yaml", store=store,
                    cfg=_cfg(tmp_path), env={})
    assert "max_model_calls" in exc.value.field
    store.close()


def test_plan_null_cost_cap_requires_hard_token_calls(tmp_path):
    from papercli.research.plan import BriefError, plan_review

    store = _store(tmp_path)
    bad = SAMPLE_BRIEF.replace("max_model_calls: 100",
                               "max_model_calls: null")
    p = _write_brief(tmp_path, bad, "bad3.yaml")
    with pytest.raises(BriefError) as exc:
        plan_review(p, tmp_path / "x.yaml", store=store,
                    cfg=_cfg(tmp_path), env={})
    assert "max_estimated_cost_usd" in exc.value.field
    store.close()


def test_plan_changed_brief_is_new_revision_old_run_immutable(tmp_path):
    from papercli.research.plan import plan_review

    store = _store(tmp_path)
    out1, out2 = tmp_path / "p1.yaml", tmp_path / "p2.yaml"
    r1 = plan_review(_write_brief(tmp_path), out1,
                     store=store, cfg=_cfg(tmp_path), env={})
    row1_before = store.run_row(r1["run_id"])

    changed = SAMPLE_BRIEF.replace("aerobic exercise", "resistance exercise")
    r2 = plan_review(_write_brief(tmp_path, changed, "brief2.yaml"), out2,
                     store=store, cfg=_cfg(tmp_path), env={})

    # New revision + new run row; different protocol hash.
    assert r2["protocol_revision"] == 2
    assert r2["run_id"] != r1["run_id"]
    assert r2["protocol_hash"] != r1["protocol_hash"]
    # The old Run is byte-immutable.
    row1_after = store.run_row(r1["run_id"])
    assert row1_after["plan_json"] == row1_before["plan_json"]
    assert row1_after["protocol_hash"] == row1_before["protocol_hash"]
    assert row1_after["protocol_revision"] == 1
    store.close()


def test_plan_query_budget_truncation_recorded(tmp_path):
    from papercli.research.plan import expand_plan

    brief = {
        "question": "Q",
        "limits": {"max_search_queries": 2},
        "themes_to_investigate": ["a", "b", "c"],
        "include_preprints": True,
        "date_range": {"from": None, "to": None},
    }
    variants = expand_plan(brief, ["crossref", "openalex", "arxiv", "s2"])
    kept = [v for v in variants if v.get("provider") != "(truncated)"]
    assert len(kept) == 2, "expansion must stop at the query budget"
    assert variants[-1].get("provider") == "(truncated)"
    assert "max_search_queries=2" in variants[-1]["note"]


# ---------------------------------------------------------------------------
# Screening + dedup
# ---------------------------------------------------------------------------

def _screening_protocol() -> dict:
    return {
        "question": "Q",
        "date_range": {"from": "2015", "to": "2026"},
        "languages": ["en"],
        "inclusion_criteria": [
            "interventional exercise training in adults",
            "cognitive outcomes"],
        "exclusion_criteria": [
            "animal studies without a human arm"],
        "themes_to_investigate": ["aerobic exercise"],
        "limits": {"max_discovered_records": 50},
    }


def test_screening_deterministic_decisions_with_criterion_ids(tmp_path):
    from papercli.research.screening import ScreeningPipeline

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    pipe = ScreeningPipeline(store, rid, _screening_protocol())
    records = [
        {"provider": "crossref", "doi": "10.1/one",
         "title": "Aerobic exercise training improves cognitive outcomes in older adults",
         "authors": ["Smith"], "year": 2018,
         "abstract": "We ran an interventional exercise training protocol in adults and measured cognitive outcomes."},
        {"provider": "openalex", "doi": "10.1/two",
         "title": "Rat exercise cognition study", "authors": ["Jones"],
         "year": 2016,
         "abstract": "Animal studies without a human arm; rats on wheels."},
    ]
    res = pipe.screen_records(records)
    by_decision = {}
    for d in res["decisions"]:
        by_decision.setdefault(d["decision"], []).append(d)
    assert len(by_decision["include"]) == 1
    inc = by_decision["include"][0]
    assert inc["criteria"] == ["inc-1", "inc-2"]
    assert inc["basis"] == "abstract"
    assert len(by_decision["exclude"]) == 1
    exc = by_decision["exclude"][0]
    assert exc["criteria"] == ["exc-1"]

    # Decisions are persisted rows with the full spec section 9 field set.
    rows = store.screenings_for_run(rid)
    assert len(rows) == 2
    for row in rows:
        assert row["stage"] == "title_abstract"
        assert row["reviewer"], "reviewer identity must be recorded"
        assert row["prompt_version"]
        assert row["decided_at"]
        assert row["evidence_basis"] in ("abstract", "metadata", "full_text")
    store.close()


def test_screening_ambiguous_record_goes_through_bounded_recheck(tmp_path):
    from papercli.research.screening import ScreeningPipeline

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    pipe = ScreeningPipeline(store, rid, _screening_protocol())
    rec = {"provider": "arxiv", "arxiv_id": "1805.00001",
           "title": "Neuronal plasticity during sleep",
           "authors": ["Lee"], "year": 2019, "abstract": "short"}
    res = pipe.screen_records([rec])
    assert res["uncertain"] == 1
    assert res["unresolved"] >= 1, "unresolvable records must be reported"
    # The uncertainty queue row exists, was rechecked exactly once, and is
    # reported unresolved (bounded: one recheck pass, spec section 9).
    urows = store.uncertainty_for_run(rid)
    assert len(urows) == 1
    assert urows[0]["rechecks"] == 1
    assert urows[0]["status"] == "unresolved"
    assert "bounded recheck" in urows[0]["reason"]
    # The uncertain decision itself was persisted (stage + decision record).
    srows = [s for s in store.screenings_for_run(rid)
             if s["decision"] == "uncertain"]
    assert srows and srows[0]["stage"] == "title_abstract"
    store.close()


def test_screening_recheck_resolves_out_of_range_year(tmp_path):
    from papercli.research.screening import ScreeningPipeline

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    pipe = ScreeningPipeline(store, rid, _screening_protocol())
    rec = {"provider": "s2", "s2_id": "z1",
           "title": "Old paper on unrelated topic",
           "authors": ["X"], "year": 1998, "abstract": "old"}
    res = pipe.screen_records([rec])
    assert res["resolved_by_recheck"] == 1
    assert res["unresolved"] == 0
    urows = store.uncertainty_for_run(rid)
    assert urows[0]["status"] == "resolved"
    assert "year" in urows[0]["reason"]
    store.close()


def test_dedup_doi_normalized_duplicates_linked_not_merged(tmp_path):
    from papercli.research.screening import ScreeningPipeline
    import json as _json

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    pipe = ScreeningPipeline(store, rid, _screening_protocol())
    records = [
        # Same DOI in different cases + with a URL wrapper -> normalized
        # identifier duplicate.
        {"provider": "crossref", "doi": "10.99/Report",
         "title": "Journal report of the study", "authors": ["A"],
         "year": 2020, "abstract": "x " * 20},
        {"provider": "arxiv", "arxiv_id": "2001.00001",
         "doi": "https://DOI.ORG/10.99/Report",
         "title": "Preprint of the study", "authors": ["A"],
         "year": 2019},
    ]
    pipe.screen_records(records)
    links = store.paper_links_for_run(rid)
    dup = [l for l in links if l["signals"].get("signal") == "normalized_doi"]
    assert dup, "normalized-DOI duplicates must be linked"
    assert all(l["confidence"] == 1.0 for l in dup)
    assert dup[0]["relation"] in ("preprint_of_report", "report_of_preprint",
                                  "duplicate")
    # Provenance preserved: BOTH papers still exist (no merge).
    papers = store.papers_for_run(rid)
    assert len(papers) == 2
    for pr in papers:
        assert _json.loads(pr["metadata_provenance_json"]).get("providers")
    store.close()


def test_dedup_fuzzy_titles_are_candidate_links_not_merges(tmp_path):
    from papercli.research.screening import ScreeningPipeline

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    pipe = ScreeningPipeline(store, rid, _screening_protocol())
    records = [
        {"provider": "crossref", "doi": "10.1/a",
         "title": "Aerobic exercise training and cognitive outcomes in older adults",
         "authors": ["Smith"], "year": 2018,
         "abstract": "x " * 25},
        {"provider": "s2", "s2_id": "b", "doi": "10.1/b",
         "title": "Aerobic Exercise Training: Cognitive Outcomes in Older Adults",
         "authors": ["Smith"], "year": 2018,
         "abstract": "y " * 25},
    ]
    res = pipe.screen_records(records)
    links = store.paper_links_for_run(rid)
    fuzzy = [l for l in links if l["signals"].get("signal")
             == "fuzzy_title_authors_year"]
    assert fuzzy, "fuzzy candidate signals must produce links"
    # Candidate links only: confidence strictly below the identifier level
    # and NEVER a merge (both rows kept).
    assert all(l["confidence"] < 0.9 for l in fuzzy)
    assert len(store.papers_for_run(rid)) == 2
    store.close()


def test_screening_csv_rows_are_the_decision_source(tmp_path):
    from papercli.research.screening import ScreeningPipeline, screening_csv_rows

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    pipe = ScreeningPipeline(store, rid, _screening_protocol())
    pipe.screen_records([
        {"provider": "crossref", "doi": "10.5/c", "title": "t",
         "authors": ["A"], "year": 2020, "abstract": "z " * 10},
    ])
    rows = screening_csv_rows(store, rid)
    assert rows, "screening.csv rows come from persisted decisions"
    needed = {"paper_id", "doi", "stage", "decision", "criterion_ids",
              "rationale", "evidence_basis", "reviewer", "prompt_version",
              "decided_at"}
    assert needed.issubset(set(rows[0].keys()))
    store.close()


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------

def test_evidence_full_text_rows_are_byte_exact(tmp_path):
    from papercli.documents.ingest import ingest_local_document
    from papercli.research.evidence import extract_evidence_for_document

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    rep = ingest_local_document(store, rid, str(FIXTURES / "sample_paper.pdf"))
    assert rep["parse_grade"] == "good"
    assert rep["access_level"] == "full_text"

    ev = extract_evidence_for_document(
        store, run_id=rid, paper_id=rep["paper_id"],
        document_id=rep["document_id"])
    assert not ev["gated"]
    assert ev["count"] > 0

    # Byte-exact: every stored excerpt is a verbatim slice of the stored
    # index text (acceptance "Extraction" re-checked at the research layer).
    texts = {r["text"] for r in store.document_text(rep["document_id"])}
    full = [e for e in store.evidence_for_run(rid)
            if e["document_id"] == rep["document_id"]]
    assert full, "evidence rows for the full-text document"
    for e in full:
        src_text = next(t for t in texts if e["supporting_excerpt"] in t)
        assert e["supporting_excerpt"] in src_text
    # claim_type candidate came from section heuristics.
    by_ct = {e["claim_type"] for e in full}
    assert "reported_result" in by_ct
    store.close()


def test_evidence_never_infer_gate_for_metadata_and_abstract(tmp_path):
    from papercli.research.evidence import (
        AccessLevelGate,
        extract_evidence_for_document,
    )

    gate = AccessLevelGate()
    assert not gate.usable("abstract_only")
    assert not gate.usable("metadata_only")
    assert not gate.usable("unavailable")
    assert gate.usable("partial_full_text")
    assert gate.usable("full_text")

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    for level in ("abstract_only", "metadata_only"):
        pid = store.upsert_paper(rid, {"doi": "10.7/" + level})
        doc = store.record_document(pid, access_status="unavailable",
                                    access_level=level,
                                    parse_quality="poor")
        store.store_document_text(doc, [
            {"page": 1, "section": "Results", "paragraph": 0,
             "text": "some text"}])
        ev = extract_evidence_for_document(
            store, run_id=rid, paper_id=pid, document_id=doc)
        assert ev["count"] == 0, level + " must produce ZERO evidence rows"
        assert ev["gated"] is True
        assert "never inferred from metadata" in ev["disclosures"][0]
    assert store.evidence_for_run(rid) == []
    store.close()


def test_evidence_scanned_document_is_capped_and_disclosed(tmp_path):
    from papercli.documents.ingest import ingest_local_document
    from papercli.research.evidence import extract_evidence_for_document

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    rep = ingest_local_document(
        store, rid, str(FIXTURES / "scanned_sample.pdf"))
    # D-1: scanned docs cap the effective access level.
    assert rep["parse_grade"] == "scanned"
    assert rep["access_level"] == "partial_full_text"
    row = store.document_row(rep["document_id"])
    assert row["parse_quality"] == "scanned"
    assert row["access_level"] == "partial_full_text"

    ev = extract_evidence_for_document(
        store, run_id=rid, paper_id=rep["paper_id"],
        document_id=rep["document_id"])
    # It HAS some text (page 1), so partial_full_text is usable; but the
    # records carry the disclosure.
    assert ev["count"] >= 1
    assert ev["gated"] is False
    assert ev["access_level"] == "partial_full_text"
    assert any("parse_quality=scanned" in d for d in ev["disclosures"])
    stored = [e for e in store.evidence_for_run(rid)
              if e["document_id"] == rep["document_id"]]
    assert all(e["access_level"] == "partial_full_text" for e in stored)
    assert all(e["extraction_warnings"] for e in stored), (
        "disclosure must ride on the evidence rows")
    store.close()


def test_parse_grade_rules(tmp_path):
    from papercli.research.quality import (
        cap_document_access_level,
        compute_parse_grade,
    )

    good = {"scanned": False, "warnings": [],
            "pages": [{"page": 1, "text": "hello"}],
            "text_index": [{"page": 1, "text": "hello"}]}
    assert compute_parse_grade(good) == "good"

    scanned = {"scanned": True, "warnings": ["page 2: scanned page"],
               "pages": [{"page": 1, "text": "text"},
                         {"page": 2, "text": ""}],
               "text_index": [{"page": 1, "text": "text"}]}
    assert compute_parse_grade(scanned) == "scanned"
    assert cap_document_access_level(
        "full_text", "scanned", text_present=True) == "partial_full_text"
    assert cap_document_access_level(
        "full_text", "scanned", text_present=False) == "abstract_only"

    no_text = {"scanned": False,
               "warnings": ["page 1: empty (no extractable text)"],
               "pages": [{"page": 1, "text": ""}],
               "text_index": []}
    assert compute_parse_grade(no_text) == "poor"
    assert cap_document_access_level(
        "full_text", "poor", text_present=False) == "metadata_only"

    # The cap only ever LOWERS, never raises.
    assert cap_document_access_level(
        "abstract_only", "good", text_present=False) == "abstract_only"

    # The D-3 grade is what ingest stores in documents.parse_quality.
    from papercli.documents.ingest import ingest_local_document
    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    rep = ingest_local_document(
        store, rid, str(FIXTURES / "sample_paper.pdf"))
    assert store.document_row(rep["document_id"])["parse_quality"] == "good"
    store.close()


# ---------------------------------------------------------------------------
# Task protocol (D-2): CLI next/submit
# ---------------------------------------------------------------------------

def _cli(*args, cwd=None):
    # The tests os.chdir() into tmp_project, so run_papers (cwd=None)
    # inherits that as the CLI working directory (the .papercli store).
    from tests.conftest import run_papers
    return run_papers(*args, cwd=cwd,
                      env_strip={"PAPERS_EMAIL", "OPENALEX_API_KEY",
                                 "PAPERS_LLM_API_KEY", "PAPERS_LLM_BASE_URL"})


def test_task_cli_next_lease_and_submit_codes(tmp_project, tmp_path):
    from papercli.jobs.tasks import TaskQueue
    from papercli.storage.db import PaperStore
    import os
    os.chdir(tmp_project)
    try:
        with PaperStore() as store:
            rid = store.create_run(state="planned")
            q = TaskQueue(store)
            t1 = q.enqueue(rid, "search", "h1",
                           payload={"provider": "openalex", "query": "q"})
            t2 = q.enqueue(rid, "retrieve", "h2", payload={"doi": "10.1/x"})

            # next: lease the first task for worker A.
            rc, out, _ = _cli("task", "next", "--run", rid,
                              "--worker", "agent-a")
            assert rc == 0
            env = json.loads(out)
            assert env["data"]["task"] is not None
            assert env["data"]["task"]["task_id"] == t1.task_id
            assert env["data"]["task"]["state"] == "leased"
            assert env["data"]["task"]["lease"]["owner"] == "agent-a"
            assert env["data"]["task"]["lease"]["expires_at"]

            # A second worker gets a DIFFERENT task (lease rules).
            rc, out, _ = _cli("task", "next", "--run", rid,
                              "--worker", "agent-b")
            env2 = json.loads(out)
            assert env2["data"]["task"]["task_id"] == t2.task_id

            # Queue drained: ok envelope with task null.
            rc, out, _ = _cli("task", "next", "--run", rid,
                              "--worker", "agent-c")
            env3 = json.loads(out)
            assert rc == 0
            assert env3["data"]["task"] is None

            # Submit: accepted on the lease owner.
            res = tmp_path / "r1.json"
            res.write_text(json.dumps({"input_hash": "h1", "records": []}),
                           encoding="utf-8")
            rc, out, _ = _cli("task", "submit", t1.task_id,
                              "--input", str(res), "--run", rid,
                              "--worker", "agent-a")
            assert rc == 0
            env = json.loads(out)
            assert env["data"]["status"] == "accepted"

            # Duplicate: same payload hash returns the prior result.
            rc, out, _ = _cli("task", "submit", t1.task_id,
                              "--input", str(res), "--run", rid,
                              "--worker", "agent-a")
            env = json.loads(out)
            assert env["data"]["status"] == "duplicate"
            assert "prior_result" in env["data"]

            # Source mismatch: wrong input_hash on a still-open task.
            bad = tmp_path / "r2.json"
            bad.write_text(json.dumps({"input_hash": "WRONG", "x": 1}),
                           encoding="utf-8")
            rc, out, _ = _cli("task", "submit", t2.task_id,
                              "--input", str(bad), "--run", rid,
                              "--worker", "agent-b")
            env = json.loads(out)
            assert rc == 2
            assert env["error"]["code"] == "TASK_SUBMIT_SOURCE_MISMATCH"

            # Invalid: unknown task id.
            rc, out, _ = _cli("task", "submit", "task-nope",
                              "--input", str(res))
            env = json.loads(out)
            assert rc == 2
            assert env["error"]["code"] in ("TASK_NOT_FOUND",
                                            "TASK_SUBMIT_INVALID")
    finally:
        os.chdir(REPO)


def test_task_submit_stale_lease_after_expiry(tmp_project, tmp_path):
    from papercli.jobs.tasks import TaskQueue
    from papercli.storage.db import PaperStore
    import os
    import time
    os.chdir(tmp_project)
    try:
        with PaperStore() as store:
            rid = store.create_run(state="planned")
            q = TaskQueue(store)
            t1 = q.enqueue(rid, "search", "h1", payload={"q": 1})

            rc, out, _ = _cli("task", "next", "--run", rid,
                              "--worker", "agent-a", "--lease-ttl", "1")
            time.sleep(1.4)  # let the lease expire
            res = tmp_path / "r.json"
            res.write_text(json.dumps({"input_hash": "h1"}),
                           encoding="utf-8")
            # The expired lease is stale: agent-a can no longer submit.
            rc, out, _ = _cli("task", "submit", t1.task_id,
                              "--input", str(res), "--run", rid,
                              "--worker", "agent-a")
            env = json.loads(out)
            assert rc == 2
            assert env["error"]["code"] == "TASK_SUBMIT_STALE"

            # ...but a new worker reclaims the recovered task.
            rc, out, _ = _cli("task", "next", "--run", rid,
                              "--worker", "agent-b")
            env = json.loads(out)
            assert rc == 0
            assert env["data"]["task"] is not None
            rc, out, _ = _cli("task", "submit", t1.task_id,
                              "--input", str(res), "--run", rid,
                              "--worker", "agent-b")
            env = json.loads(out)
            assert rc == 0
            assert env["data"]["status"] == "accepted"
    finally:
        os.chdir(REPO)


def test_task_submit_missing_input_file_exits_2(tmp_project, tmp_path):
    rc, out, _ = _cli("task", "submit", "task-x",
                      "--input", str(tmp_path / "nope.json"))
    assert rc == 2
    env = json.loads(out)
    assert env["error"]["code"] in ("INPUT_FILE_NOT_FOUND", "TASK_NOT_FOUND")


# ---------------------------------------------------------------------------
# Expansion (fixture adapters -- no network)
# ---------------------------------------------------------------------------

class _FakeOpenAlex:
    """Fixture adapter: the default branch succeeds with candidates."""

    def search_works(self, query, config, env, *, cache=None, per_page=25,
                     filters=None, cursor=None, save_cursor=None,
                     use_search=True):
        return {"items": [
            {"id": "W100", "doi": "https://doi.org/10.9/NEW1",
             "display_name": "Citing work one",
             "publication_date": "2020-05-01",
             "authorships": [{"author": {"display_name": "Cite A"}}]},
            {"id": "W200", "doi": "10.9/NEW2",
             "display_name": "Citing work two",
             "publication_date": "2021-01-01", "authorships": []},
        ], "completeness": "partial", "total_results": 50,
            "next_cursor": "c1", "error": None}


class _FailingOpenAlex:
    def search_works(self, *a, **k):
        return {"items": [], "completeness": "incomplete",
                "error": "HttpError: HTTP 503", "total_results": None,
                "next_cursor": None}


class _FakeS2:
    """Keyless S2: a recorded skip, never a failure."""

    def related_papers(self, paper_id, config, env, cache=None, limit=10):
        return {"provider": "s2", "skipped": True,
                "reason": "HTTP 429 (keyless pool / rate limit)",
                "data": None}


def _fakes(failing_openalex: bool = False):
    def factory(name):
        if name == "openalex":
            return _FailingOpenAlex() if failing_openalex else _FakeOpenAlex()
        if name == "s2":
            return _FakeS2()
        raise KeyError(name)
    return factory


def test_expansion_provenance_chains_saved(tmp_path):
    from papercli.research.expansion import expand_search

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    store.upsert_paper(rid, {"openalex": "W1", "doi": "10.1/seed1"},
                       title="Seed one")
    store.upsert_paper(rid, {"s2": "S2ID", "doi": "10.2/seed2"},
                       title="Seed two")
    protocol = {"limits": {"max_discovered_records": 100}}
    accepted = [dict(r) for r in store.papers_for_run(rid)]
    res = expand_search(store, rid, protocol, accepted, config=None,
                       env=None, cache=None, adapter_factory=_fakes())

    assert res["seeds"] == 2
    # S2 skip + OpenAlex partial -> the run's expansion is not complete.
    assert res["complete"] is False
    assert res["added"] == 2, "openalex candidates entered the registry"
    for b in res["branches"]:
        assert b.get("provenance_chain"), "every branch records its chain"
        if b["provider"] == "s2":
            assert b["completeness"] == "skipped"
            assert b["skip_reason"]

    # Provenance is persisted on the Search rows (seed -> via provider).
    exps = [s for s in store.searches_for_run(rid)
            if s["query"].startswith("expand:")]
    assert len(exps) == 2, "each branch persists one Search row"
    for s in exps:
        f = json.loads(s["filters_json"])
        assert f["kind"] == "expansion"
        assert "seed:" in f["provenance"]
        assert "via:" in f["provenance"]
        assert "date=" in f["provenance"]
    # Candidates entered the paper registry with provenance (no merge, no
    # delete of the seed rows).
    papers = store.papers_for_run(rid)
    citing = [p for p in papers if "Citing work" in (p.get("title") or "")]
    assert len(citing) == 2
    for p in citing:
        meta = json.loads(p["metadata_provenance_json"])
        assert meta["kind"] == "expansion"
        assert "seed:" in meta["provenance"]["chain"]
    store.close()


def test_expansion_cap_defers_candidates(tmp_path):
    from papercli.research.expansion import expand_search

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    store.upsert_paper(rid, {"openalex": "W9"}, title="Seed A")
    accepted = [dict(r) for r in store.papers_for_run(rid)]
    protocol = {"limits": {"max_discovered_records": 1}}
    res = expand_search(store, rid, protocol, accepted, config=None,
                       env=None, cache=None, adapter_factory=_fakes())
    assert res["added"] == 1
    assert res["deferred"] == 1, "cap must defer, not drop silently"
    branch = res["branches"][0]
    assert branch["deferred"] == 1
    exps = [s for s in store.searches_for_run(rid)
            if s["query"].startswith("expand:")]
    f = json.loads(exps[0]["filters_json"])
    assert f["deferred"] == 1
    store.close()


def test_expansion_failing_provider_flagged_not_complete(tmp_path):
    from papercli.research.expansion import expand_search

    store = _store(tmp_path)
    rid = store.create_run(state="planned")
    store.upsert_paper(rid, {"openalex": "W1"}, title="Seed")
    accepted = [dict(r) for r in store.papers_for_run(rid)]
    protocol = {"limits": {"max_discovered_records": 100}}
    res = expand_search(store, rid, protocol, accepted, config=None,
                       env=None, cache=None,
                       adapter_factory=_fakes(failing_openalex=True))
    assert res["complete"] is False
    assert any("openalex" in b for b in res["incomplete_branches"])
    # The failed branch is persisted with completeness=incomplete.
    exps = [s for s in store.searches_for_run(rid)
            if s["query"].startswith("expand:openalex")]
    assert exps and exps[0]["completeness"] == "incomplete"
    store.close()


# ---------------------------------------------------------------------------
# CLI: review plan / evidence list envelopes
# ---------------------------------------------------------------------------

def test_cli_review_plan_ok_and_exit_2_on_bad_brief(tmp_project, tmp_path):
    from tests.conftest import run_papers

    brief = tmp_path / "brief.yaml"
    brief.write_text(SAMPLE_BRIEF, encoding="utf-8")
    rc, out, _ = run_papers(
        "review", "plan", "--brief", str(brief), "--out",
        str(tmp_path / "plan.yaml"),
        cwd=tmp_project,
        env_strip={"PAPERS_EMAIL", "OPENALEX_API_KEY",
                   "PAPERS_LLM_API_KEY", "PAPERS_LLM_BASE_URL"})
    assert rc == 0, out
    env = json.loads(out)
    assert env["status"] == "ok"
    d = env["data"]
    assert d["run_state"] == "planned"
    assert d["protocol_revision"] == 1
    assert d["resource_estimate"]["full_text_cap"] == 20
    assert Path(d["plan_path"]).exists()

    # A second plan of the same brief -> a new run at revision 2.
    rc, out, _ = run_papers(
        "review", "plan", "--brief", str(brief), "--out",
        str(tmp_path / "plan2.yaml"),
        cwd=tmp_project,
        env_strip={"PAPERS_EMAIL", "OPENALEX_API_KEY",
                   "PAPERS_LLM_API_KEY", "PAPERS_LLM_BASE_URL"})
    env2 = json.loads(out)
    assert env2["data"]["protocol_revision"] == 2
    assert env2["data"]["run_id"] != d["run_id"]

    # Missing required field -> exit 2 naming the field.
    bad = tmp_path / "bad.yaml"
    bad.write_text(SAMPLE_BRIEF.replace("question: Q?\n", ""),
                   encoding="utf-8")
    rc, out, _ = run_papers(
        "review", "plan", "--brief", str(bad), "--out",
        str(tmp_path / "no.yaml"),
        cwd=tmp_project,
        env_strip={"PAPERS_EMAIL", "OPENALEX_API_KEY",
                   "PAPERS_LLM_API_KEY", "PAPERS_LLM_BASE_URL"})
    assert rc == 2
    env3 = json.loads(out)
    assert env3["status"] == "error"
    assert env3["error"]["code"] == "CONFIG_REQUIRED"
    assert "question" in env3["error"]["details"]["field"]


def test_cli_evidence_list_envelope(tmp_project, tmp_path):
    from tests.conftest import run_papers

    # Empty run: ok envelope, count 0.
    rc, out, _ = run_papers("evidence", "list", "--run", "run-missing",
                            cwd=tmp_project,
                            env_strip={"PAPERS_EMAIL"})
    assert rc == 2
    assert json.loads(out)["error"]["code"] == "CONFIG_REQUIRED"

    from papercli.storage.db import PaperStore
    from papercli.documents.ingest import ingest_local_document
    import os
    os.chdir(tmp_project)
    try:
        with PaperStore() as store:
            rid = store.create_run(state="planned")
            rep = ingest_local_document(
                store, rid, str(FIXTURES / "sample_paper.pdf"))
            from papercli.research.evidence import extract_evidence_for_document
            ev = extract_evidence_for_document(
                store, run_id=rid, paper_id=rep["paper_id"],
                document_id=rep["document_id"])

            rc, out, _ = run_papers("evidence", "list", "--run", rid,
                                    cwd=tmp_project,
                                    env_strip={"PAPERS_EMAIL"})
            env = json.loads(out)
            assert rc == 0
            assert env["data"]["count"] == ev["count"] > 0
            for rec in env["data"]["evidence"]:
                assert rec["paper_id"] == rep["paper_id"]
                assert rec["document_id"] == rep["document_id"]
    finally:
        os.chdir(REPO)
