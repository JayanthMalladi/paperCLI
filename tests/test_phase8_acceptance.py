"""Phase 8 acceptance DELTA (spec section 19 rows not already covered).

Phases 2-7 cover most of the 20 acceptance rows (identity, extraction,
screening, citation audit, resume, detached launch, limits, rate limits,
concurrency, secrets, exports, live smoke). This file adds ONLY the
deltas, fully offline (fixtures + fakes + injected clocks + the installed
distribution), so the whole non-network suite stays green before the
overnight launch:

* **Failure-injection matrix** -- one fixture run + targeted faults, each
  handled per its 19 pass condition, never silently completed:
  (a) a provider 429 / keyless-S2 branch is flagged ``incomplete`` +
  disclosed, never a complete search;
  (b) a wrong-paper PDF (parseable but mismatched identity) is REJECTED;
  an inconclusive-identity document is FLAGGED (never silently verified);
  (c) a scanned PDF is FLAGGED and capped to partial_full_text (never a
  full-text read);
  (d) a fabricated (unregistered) citation key is flagged by the audit;
  (e) an interrupted-then-resumed run re-dispatches ONLY the pending work;
  a clean COMPLETED run is a no-op resume (never re-run).
* **Metrics honesty** -- a completed run's run-summary.json +
  citation-audit.json carry the discovered/dedup/screened/retrieved/
  read/cited counts + a no-exhaustive-coverage disclosure; a PARTIAL run
  is honestly labeled (never "completed", never "all retractions ruled
  out", never a universal-accuracy claim).
* **Fresh install** (the 19 "Fresh install" row) -- a clean supported 3.12
  install (this repo's ``.v8`` venv = ``uv venv --python 3.12`` +
  ``uv pip install -e .[exports,dev]``) can be CONFIGURED +
  preflight-checked + plan-validated before a run. The from-zero build is
  reported in RELEASE.md (batch 3).

No secrets: every fixture value here is a distinctive sentinel that does
NOT match ci/secret_scan.py's committed-credential patterns.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from papercli.agent_contract.schemas import validate
from papercli.citations.audit import audit_citations, claim_audit_rows
from papercli.documents.ingest import IngestRejected, ingest_local_document
from papercli.exports.renderers import build_coverage_markdown
from papercli.exports.run_summary import build_run_summary, write_structured_dumps
from papercli.jobs.recovery import resume_run
from papercli.storage.db import PaperStore

from tests.conftest import run_papers
from tests.test_phase6_acceptance import _crash_marker, _dead_pid
from tests.test_phase6_worker import (
    DEFAULT_RECORDS,
    FAKE_LIMITS,
    _fake_documents,
    _fake_model_cfg,
    _new_worker,
    _run_full,
    _seed_run,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"

# A requested-paper context for the wrong-paper fixture (matches the
# fixture the Phase-3 identity tests use as the "correct" paper).
PAPER_CTX = {"doi": "10.9999/rev.2023", "title": "Effects of X on Y"}


def _never(needle: str, hay: str) -> bool:
    return not re.search(re.escape(needle), hay, re.IGNORECASE)


def _installed_papers() -> list[str] | None:
    """The INSTALLED ``papers`` console script (a real distribution install
    via ``uv pip install -e .``), resolved next to the running
    interpreter, or None when the package is only on ``sys.path``."""
    base = Path(sys.executable).parent
    for name in ("papers.exe", "papers"):
        if (base / name).exists():
            return [str(base / name)]
    return None


def _run_papers_via(argv: list[str], *args) -> tuple[int, str, str]:
    """Drive the installed ``papers`` executable (a real subprocess)."""
    import subprocess
    proc = subprocess.run([*argv, *args], cwd=str(REPO),
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# A search service that injects a provider failure into a real fixture run
# ---------------------------------------------------------------------------

def _failing_search_factory(store, records):
    """A FakeSearchService whose openalex branch returns a 429-style error.
    The failure-injection seam for the discovery / rate-limit row: a
    provider failure must land as an ``incomplete`` + disclosed branch,
    never a complete search (spec sections 8/19)."""
    import types
    from tests.test_phase6_worker import FakeSearchService

    svc = FakeSearchService(records)
    svc.store = store

    def search(self, query, providers, limit=50):
        base = FakeSearchService.search(self, query, providers, limit=limit)
        provs = []
        for prov in base["providers"]:
            if prov.get("provider") == "openalex":
                prov = dict(prov, completeness="incomplete",
                            error="HTTP 429 (rate limited; Retry-After "
                                  "honored, bounded recovery)")
            provs.append(prov)
        base["providers"] = provs
        base["completeness"] = "incomplete"
        base["errors"] = [p["error"] for p in provs if p.get("error")]
        return base

    def persist(self, result, run_id):
        # Record the per-provider error so the run's search branches carry
        # the disclosure (the base FakeSearchService persist does not).
        for prov in result.get("providers") or []:
            self.store.record_search(
                run_id, prov.get("provider", "x"), prov.get("query"),
                result_count=prov.get("result_count"),
                completeness=prov.get("completeness"),
                error=prov.get("error"))
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

    svc.persist = types.MethodType(persist, svc)
    svc.search = types.MethodType(search, svc)
    return lambda: svc


# ===========================================================================
# 1. Failure-injection matrix (one fixture run + targeted faults)
# ===========================================================================

def test_failure_matrix_provider_429_flagged_not_complete(tmp_path):
    """(a) A provider 429 / keyless-S2 branch is flagged incomplete +
    disclosed in run-summary AND coverage-and-limitations -- never
    mislabeled a complete search (spec 19 "Rate limits" + "Discovery")."""
    from papercli.jobs.worker import RunWorker
    store = PaperStore(tmp_path / "fi.db")
    rid = _seed_run(store, limits=FAKE_LIMITS)
    _fake_documents(store, rid)
    worker = RunWorker(
        store, rid, model_cfg=_fake_model_cfg(),
        search_service_factory=_failing_search_factory(store, DEFAULT_RECORDS),
        retry_delay_seconds=0.0, retry_jitter_seconds=0.0)
    summary = worker.execute()

    # The run closes (the failure is a bounded, disclosed branch, not a
    # crash), and it is honest about what did NOT complete.
    assert summary["state"] in ("completed", "partial")
    branches = {b["provider"]: b for b in summary["branches"]}
    oa = branches.get("openalex")
    assert oa is not None, "the openalex branch must be recorded"
    assert oa["completeness"] != "complete", \
        "a 429-failed provider must never be stored as a complete search"
    assert oa.get("error"), "the provider failure must be disclosed"
    # The disclosure file names the incomplete search.
    cov = build_coverage_markdown(store, rid)
    assert "did NOT complete" in cov
    assert "openalex" in cov
    store.close()


def test_failure_matrix_wrong_paper_rejected_inconclusive_flagged(tmp_path):
    """(b) A parseable-but-wrong PDF is REJECTED; an inconclusive-identity
    document is FLAGGED (not silently verified) (spec 19 "Identity")."""
    store = PaperStore(tmp_path / "fi-wp.db")
    rid = store.create_run()
    wrong = str(FIXTURES / "wrong_paper.pdf")
    with pytest.raises(IngestRejected) as exc:
        ingest_local_document(store, rid, wrong,
                              requested_identity=PAPER_CTX)
    assert "wrong-paper" in exc.value.reason
    # Nothing was committed for the wrong paper.
    assert store.papers_for_run(rid) == []
    store.close()

    # FLAG path: an inconclusive-identity document (embedded evidence
    # present but not decisive) is ingested + flagged, not silently
    # verified. The citation audit surfaces it under wrong_paper_identity.
    store2 = PaperStore(tmp_path / "fi-wp2.db")
    rid2 = store2.create_run()
    p = store2.upsert_paper(rid2, {"doi": "10.9999/rev.2023"},
                             title="Effects of X on Y", year=2023,
                             authors=["Smith"])
    doc = store2.record_document(
        p, source_url="local:unlabeled.pdf",
        access_status="retrieved_full_text",
        parse_quality="good", access_level="full_text")
    store2.record_document_identity(
        doc, parse_warnings=[], page_count=3, scanned=False,
        identity_status="inconclusive",
        identity={"embedded_title": None, "embedded_doi": None})
    assert store2.document_row(doc)["identity_status"] == "inconclusive"
    refs = audit_citations(store2, rid2)["references"]
    assert any(d["identity_status"] == "inconclusive"
               for d in refs["wrong_paper_identity"]["flagged"]), \
        "an inconclusive-identity document must be flagged in the audit"
    store2.close()


def test_failure_matrix_scanned_pdf_capped_not_full_read(tmp_path):
    """(c) A scanned PDF is FLAGGED and capped below full_text -- it is
    never represented as a fully read full-text document (spec 19
    "Missing text")."""
    store = PaperStore(tmp_path / "fi-scan.db")
    rid = store.create_run()
    p = store.upsert_paper(rid, {"doi": "10.7777/scan.2022"},
                           title="Scanned Sample", year=2022, authors=["A"])
    scanned = str(FIXTURES / "scanned_sample.pdf")
    rep = ingest_local_document(store, rid, scanned,
                                requested_identity={"doi": "10.7777/scan.2022"})
    assert rep["rejected"] is False
    assert rep["scanned"] is True, "the scanned flag must be set"
    assert rep["access_level"] != "full_text", \
        "a scanned document must not be a clean full-text read"
    assert any("OCR" in w or "scanned" in w.lower()
               for w in rep["parse_warnings"]), \
        "the missing-OCR disclosure must be present"
    assert store.document_row(rep["document_id"])["access_level"] \
        == rep["access_level"]
    # The run-summary counts it under read_partial, not full_text.
    s = build_run_summary(store, rid)
    assert s["counts"]["read_partial"] >= 1
    assert s["counts"]["retrieved"] >= 1
    store.close()


def test_failure_matrix_fabricated_citation_key_flagged(tmp_path):
    """(d) A claim citing a key that is NOT in the bibliography registry
    is flagged by the audit (spec 19 "Citation audit": fabricated key)."""
    store = PaperStore(tmp_path / "fi-cite.db")
    rid = store.create_run(state="running")
    p1 = store.upsert_paper(rid, {"doi": "10.1/p1"}, title="Alpha",
                             year=2021, authors=["Alpha A"])
    store.record_document(p1, access_status="retrieved_full_text",
                           identity_status="matched")
    store.record_claim(rid, claim_text="cites a nonexistent key",
                        location="theme::T1", evidence_ids=[],
                        citation_keys=["ghostpaper2099"],
                        audit_status="uncertain")
    data = audit_citations(store, rid)
    assert "ghostpaper2099" in data["references"]["unregistered_keys_used"], \
        "a fabricated citation key must be flagged"
    rows = claim_audit_rows(store, rid)
    assert any("ghostpaper2099" in d for d in rows[0]["disclosures"])
    store.close()


def test_interruption_then_resume_runs_only_pending(tmp_path):
    """(e) A worker that DIED mid-run -> resume re-dispatches ONLY the
    pending/retriable work; completed hashes are UNCHANGED (no duplicate
    committed work). A clean COMPLETED run is a no-op resume (never
    re-run) (spec 19 "Resume")."""
    from papercli.jobs.detach import acquire_run_lock, _write_json_atomic
    store = PaperStore(tmp_path / "fi-resume.db")
    w1, summary, rid = _run_full(store, model_cfg=_fake_model_cfg())
    assert summary["state"] == "completed"
    all_tasks = w1.queue.list(rid)
    keep, fail_one, rest = all_tasks[:3], all_tasks[3], all_tasks[4:]
    hashes_kept = {t.task_id: t.result_hash for t in keep}
    assert all(h is not None for h in hashes_kept.values())

    # Force a crash: the in-flight task -> retry_wait (past due), the rest
    # -> pending; the run row is RUNNING with a stale lock + dead holder.
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
    store.update_run(rid, state="running")
    _crash_marker(store, rid, _dead_pid())

    launched = {}

    def child_fn(**kw):
        g = acquire_run_lock(store, rid, background_heartbeat=False)
        g.release()
        launched["yes"] = True
        _write_json_atomic(kw["work_dir"] / "handshake.json", {
            "accepted": True, "pid": 0, "run_id": rid, "start_ts": "",
            "token": kw.get("handshake_token")})
        return None

    res = resume_run(store, rid, child_fn=child_fn)
    assert res.launched is True and res.resumed is True
    assert any("stale" in n for n in res.notes)
    assert launched.get("yes") is True

    # The resumed worker re-dispatches ONLY the unfinished tasks; the 3
    # succeeded stay byte-identical, and the task SET is unchanged.
    w2 = _new_worker(store, rid, model_cfg=_fake_model_cfg(),
                     records=DEFAULT_RECORDS)
    w2.execute()
    after = {t.task_id: t.result_hash for t in w2.queue.list(rid)}
    assert len(after) == len(all_tasks), "no duplicate committed work"
    for tid, h in hashes_kept.items():
        assert after[tid] == h, f"completed task {tid} was re-run"
    assert after[fail_one.task_id] is not None, \
        "the unfinished task must be re-dispatched on resume"

    # A clean COMPLETED run is a no-op resume: it never re-launches.
    res2 = resume_run(store, rid)
    assert res2.launched is False and res2.resumed is False
    assert res2.state == "completed"
    store.close()


# ===========================================================================
# 2. Metrics honesty (run-summary.json + citation-audit.json)
# ===========================================================================

def _seeded_completed_run(tmp_path: Path) -> tuple[PaperStore, str]:
    """A deterministic completed fixture run, seeded directly (the same
    shape test_phase7_exports._completed_run uses: a complete crossref
    branch + an incomplete openalex (503) branch + screened decisions +
    evidence + claims + a completed state)."""
    store = PaperStore(tmp_path / "s.db")
    rid = store.create_run(limits={"max_model_calls": 300,
                                   "max_search_queries": 30},
                           protocol_hash="proto-h", config_snapshot_ref="")
    plan = {"question": "Does aerobic exercise improve memory in adults?",
            "review_type": "narrative", "scope": "aerobic exercise",
            "limits": {"max_model_calls": 300}}
    store._conn.execute(
        "UPDATE runs SET plan_json=?, protocol_revision=1 WHERE id=?",
        (json.dumps(plan), rid))
    store.record_search(rid, "crossref", "aerobic memory adults",
                        result_count=12, completeness="complete")
    store.record_search(rid, "openalex", "aerobic memory adults",
                        result_count=8, completeness="incomplete",
                        error="HTTP 503")
    p1 = store.upsert_paper(rid, {"doi": "10.1/aaa"},
                             title="Exercise and Working Memory",
                             year=2021, authors=["Smith J"])
    p2 = store.upsert_paper(rid, {"doi": "10.1/bbb"},
                             title="Retention after Training",
                             year=2022, authors=["Jones P"])
    store.record_screening(p1, stage="title_abstract", decision="include",
                           rationale="human adults, relevant")
    store.record_screening(p2, stage="title_abstract", decision="uncertain",
                           rationale="adults unclear")
    doc = store.record_document(p1, access_status="retrieved_full_text",
                                 access_level="full_text")
    store.store_document_text(doc, [{"page": 1, "section": "results",
                                     "paragraph": 1,
                                     "text": "Finding text."}])
    ev1 = store.record_evidence(p1, document_id=doc, access_level="full_text",
                                 finding="aerobic load improved memory",
                                 claim_type="reported_result")
    # Cite the REAL registry key (generated from the saved paper rows, spec
    # section 10: the model selects among registry keys, never invents one).
    from papercli.reasoning.common import bibliography_for_run
    refs = bibliography_for_run(store, rid)
    k1 = next(r["citation_key"] for r in refs if str(r["paper_id"]) == str(p1))
    store.record_claim(rid, claim_text="Aerobic load improves memory.",
                        location="theme::working memory",
                        evidence_ids=[ev1], audit_status="supported",
                        citation_keys=[k1])
    store.record_model_call(
        rid, model="test-model", outcome="success", input_tokens=100,
        output_tokens=50, estimated_cost_usd=0.001,
        endpoint="https://model.endpoint.example/v1",
        api_key_env="PAPERS_TEST_LLM_KEY")
    store.update_run(rid, state="completed")
    return store, rid


def test_completed_run_metrics_are_honest(tmp_path):
    """A completed run's run-summary.json + citation-audit.json carry the
    discovered/dedup/screened/retrieved/read/cited counts and a
    no-exhaustive-coverage disclosure that makes NO exhaustive-coverage
    or universal-accuracy claim (spec 19 "track quantitative results")."""
    store, rid = _seeded_completed_run(tmp_path)
    out = tmp_path / "out"
    write_structured_dumps(store, rid, out_dir=out,
                           formats=["md", "pdf", "docx", "bib", "csl",
                                    "csv"])
    rs = json.loads(Path(out, "run-summary.json").read_text("utf-8"))
    counts = rs["counts"]
    for key in ("discovered", "deduplicated", "screened", "retrieved",
                "read", "cited"):
        assert key in counts, f"run-summary missing counts[{key!r}]"
    assert counts["discovered"] == 2
    assert counts["cited"] >= 1, "the synthesized claim cites a registry key"
    assert "exhaustive-coverage" in counts["note"]
    assert rs["state"] == "completed"

    # citation-audit.json (the Phase-5 audit, verbatim) discloses coverage:
    # the retraction check is an availability check, and its own disclaimer
    # says it is "NOT a ruling that no retractions exist".
    ca = json.loads(Path(out, "citation-audit.json").read_text("utf-8"))
    assert ca["coverage_disclosure"]["exhaustive_coverage_claimed"] is False
    cr = ca["correction_retraction_check"]
    assert any("NOT a ruling" in lim for lim in cr["limits"])
    assert "no exhaustive-coverage" in cr["note"],         "the retraction check must disclose it is not a coverage claim"
    assert _never("universal accuracy", json.dumps(ca))

    # The disclosure markdown makes the no-exhaustive-coverage claim.
    cov = Path(out, "coverage-and-limitations.md").read_text("utf-8")
    assert "no exhaustive-coverage claim" in cov
    store.close()


def test_partial_run_summary_never_completed(tmp_path):
    """A PARTIAL run (budget exhausted mid-DAG) is honestly labeled
    partial -- never "completed", never "all retractions ruled out",
    never a universal-accuracy claim (spec 19 + 6)."""
    store = PaperStore(tmp_path / "partial.db")
    limits = dict(FAKE_LIMITS, max_model_calls=1,
                  max_input_tokens=10_000_000)
    _run_full(store, model_cfg=_fake_model_cfg(), limits=limits)
    rid = _last_rid(store)
    s = build_run_summary(store, rid)
    assert s["state"] in ("partial", "failed")
    assert s["state"] != "completed", \
        "an exhausted run must not be 'completed'"
    blob = json.dumps(s)
    assert _never("all retractions ruled out", blob)
    assert _never("universal accuracy", blob)
    # The summary notes say so explicitly.
    assert any("never" in n and "completed" in n for n in s["notes"])
    store.close()


def _last_rid(store: PaperStore) -> str:
    rows = [r for r in store._conn.execute("SELECT id FROM runs").fetchall()]
    return rows[-1]["id"]


# ===========================================================================
# 3. Fresh install (the 19 "Fresh install" row)
# ===========================================================================

def test_fresh_install_supported_env_preflight_and_plan(tmp_path):
    """A clean supported 3.12 install can be CONFIGURED + preflight-checked
    + plan-validated BEFORE a run: ``papers init`` + ``papers doctor``
    (healthy) + ``papers review plan`` on a tiny brief all succeed on a
    fresh project (spec 19 "Fresh install"). The .v8 venv is that clean
    install; the installed console script + schema surface are driven when
    the suite runs from a distribution install."""
    # 1) When installed as a distribution, the console script + schema
    #    surface are runnable (proves a real install, not just src on
    #    sys.path).
    exe = _installed_papers()
    if exe:
        rc, out, err = _run_papers_via(exe, "--version")
        assert rc == 0 and "papers 0.1.0" in out, f"{out} {err}"
        rc, out, _ = _run_papers_via(exe, "schema", "--list")
        assert rc == 0 and json.loads(out)["status"] == "ok"

    # 2) A brand-new project: init + doctor preflight is healthy.
    proj = tmp_path / "fresh"
    proj.mkdir()
    rc, out, _ = run_papers("init", cwd=proj)
    assert rc == 0 and json.loads(out)["status"] == "ok"
    rc, out, _ = run_papers("doctor", "--json", "--skip-network",
                            cwd=proj)
    assert rc == 0, f"doctor on a fresh project must exit 0: {out}"
    env = json.loads(out)
    assert env["status"] == "ok"
    assert env["data"]["summary"]["healthy"] is True

    # 3) A tiny brief validates into a planned Run (no overnight launch).
    brief = proj / "tiny-brief.yaml"
    brief.write_text(
        "schema_version: '1.0'\n"
        "question: Q?\n"
        "purpose: p\n"
        "scope: s\n"
        "languages: [en]\n"
        "outputs: [md, csv]\n"
        "limits:\n"
        "  wall_minutes: 60\n"
        "  max_search_queries: 4\n"
        "  max_discovered_records: 50\n"
        "  max_full_text_papers: 5\n"
        "  max_model_calls: 20\n"
        "  max_input_tokens: 50000\n"
        "  max_output_tokens: 8000\n"
        "  max_estimated_cost_usd: null\n",
        encoding="utf-8")
    rc, out, _ = run_papers(
        "review", "plan", "--brief", str(brief),
        "--out", str(proj / "plan.yaml"), cwd=proj,
        env_strip={"PAPERS_EMAIL", "OPENALEX_API_KEY",
                   "PAPERS_LLM_API_KEY", "PAPERS_LLM_BASE_URL"})
    assert rc == 0, f"plan validation must succeed on a fresh install: {out}"
    env = json.loads(out)
    assert env["status"] == "ok"
    assert env["data"]["run_state"] == "planned"
    assert env["data"]["protocol_hash"]
    # The written plan YAML is valid against the plan schema.
    import yaml
    doc = yaml.safe_load((proj / "plan.yaml").read_text(encoding="utf-8"))
    assert validate(doc, "plan") == []
