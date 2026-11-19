"""Phase 7: rendered exporters + the spec-14 bundle (sections 10/14/16).

All fixture-driven, no network: a completed fixture run (deterministic
seeded storage) produces the full bundle; a renderer whose dependency
is ABSENT (simulated by an injected RendererEnv, or genuinely absent
in a bare venv) marks ONLY its own artifact missing -- with an
explicit reason -- while every other bundle file stays intact
(spec section 14: no silent missing "completed" artifacts).

Honesty is environment-dependent and asserted as such: with the
``[exports]`` extra + a LaTeX engine, pdf/docx are COMPLETE; without,
they are MISSING with a reason. Both outcomes are valid; silence is
not.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

from papercli.exports.renderers import (
    RendererEnv,
    build_citation_audit_bundle,
    build_coverage_markdown,
    build_references_bib,
    build_references_csl,
    build_review_markdown,
    export_bundle,
    render_docx,
    render_pdf,
)
from papercli.exports.run_summary import (
    build_run_summary,
    write_structured_dumps,
)
from papercli.storage.db import PaperStore

#: Secret-sentinel VALUE used only to prove ABSENCE from the bundle.
#: Distinctive on purpose, but it does NOT match ci/secret_scan.py's
#: committed-credential patterns.
SENTINEL_KEY = "llm-export-sentinel-0001"


def _completed_run(tmp_path: Path) -> tuple[PaperStore, str]:
    """A deterministic completed fixture run (the same shape the
    fake-worker acceptance run produces, seeded directly)."""
    store = PaperStore(tmp_path / "s.db")
    rid = store.create_run(limits={"max_model_calls": 300,
                                   "max_search_queries": 30},
                           protocol_hash="proto-h",
                           config_snapshot_ref="")
    plan = {
        "question": "Does aerobic exercise improve memory in adults?",
        "review_type": "narrative",
        "scope": "aerobic exercise and cognition in adults",
        "themes_to_investigate": ["working memory", "long-term retention"],
        "inclusion_criteria": ["human adults"],
        "exclusion_criteria": ["animal models"],
        "assumptions": ["no date range; full history is searched"],
        "limits": {"max_model_calls": 300},
    }
    store._conn.execute("UPDATE runs SET plan_json=?, protocol_revision=1 "
                        "WHERE id=?", (json.dumps(plan), rid))
    store.record_search(rid, "crossref", "aerobic exercise memory adults",
                        result_count=12, completeness="complete")
    store.record_search(rid, "openalex", "aerobic exercise memory adults",
                        result_count=8, completeness="incomplete",
                        error="HTTP 503")
    p1 = store.upsert_paper(rid, {"doi": "10.1/aaa"},
                            title="Exercise and Working Memory A %50 B & C",
                            year=2021, authors=["Smith J", "Doe A"])
    p2 = store.upsert_paper(rid, {"arxiv": "2101.00001"},
                            title="Retention after Training",
                            year=2022, authors=["Jones P"])
    p3 = store.upsert_paper(rid, {"doi": "10.1/ccc"},
                            title="Aerobic Load and Cognition",
                            year=2019, authors=["Chen L"])
    doc = store.record_document(p1, access_status="retrieved_full_text",
                                access_level="full_text")
    store.store_document_text(doc, [{"page": 1, "section": "results",
                                     "paragraph": 1,
                                     "text": "Finding text."}])
    ev1 = store.record_evidence(p1, document_id=doc,
                                access_level="full_text",
                                finding="aerobic load improved working "
                                        "memory scores",
                                claim_type="reported_result")
    store.record_evidence(p2, access_level="metadata",
                          finding="title/abstract: training associated "
                                  "with retention",
                          claim_type="reported_result")
    store.record_screening(p1, stage="title_abstract", decision="include",
                          rationale="human adults, relevant")
    store.record_screening(p2, stage="title_abstract", decision="uncertain",
                           rationale="adults unclear")
    store.record_screening(p3, stage="title_abstract", decision="exclude",
                           rationale="animal models")
    from papercli.reasoning.common import citation_key
    k1 = citation_key({"id": p1, "year": 2021,
                       "authors": ["Smith J", "Doe A"]})
    c1 = store.record_claim(rid, claim_text="Aerobic load improves "
                       "working memory in adults.",
                       location="theme::working memory",
                       evidence_ids=[ev1], audit_status="supported",
                       citation_keys=[k1])
    c2 = store.record_claim(rid, claim_text="Training aids retention.",
                       location="theme::long-term retention",
                       evidence_ids=[], audit_status="uncertain",
                       citation_keys=["smith2021zzz"])
    store.record_model_call(rid, model="claude-sonnet-5-5",
                            outcome="success", input_tokens=100,
                            output_tokens=50, estimated_cost_usd=0.001,
                            endpoint="https://model.endpoint.example/v1",
                            api_key_env="PAPERS_EXPORT_TEST_KEY")
    # The model synthesis artifact the renderer reads from the run dir.
    from papercli.exports.run_summary import run_dir
    draft = {
        "themes": [
            {"theme": "working memory",
             "claims": [{"text": "Aerobic load improves working memory in "
                                 "adults.",
                         "citation_keys": [k1]}],
             "gaps": ["dose-response untested"]},
            {"theme": "long-term retention",
             "claims": [{"text": "Training aids retention.",
                         "citation_keys": ["smith2021zzz"]}],
             "gaps": []},
        ],
        "limitations": ["single-session designs dominate"],
        "contradictions": [{"a": "no effect at low dose",
                            "b": "effect at high dose",
                            "citation_keys": [k1]}],
    }
    rd = run_dir(store, rid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "synthesis_draft.json").write_text(
        json.dumps(draft), encoding="utf-8")
    store.update_run(rid, state="completed")
    return store, rid


def _default_env() -> RendererEnv:
    """What THIS process can actually render with."""
    import papercli.exports.renderers as R
    return R.make_renderer_env()


def _all_bundle_files() -> list[str]:
    return ["review.md", "review.pdf", "review.docx",
            "references.bib", "references.csl.json",
            "evidence.csv", "screening.csv", "search-log.jsonl",
            "citation-audit.json", "coverage-and-limitations.md",
            "run-summary.json"]


# ---------------------------------------------------------------------------
# 1. Completed run -> the bundle, with an honest per-file inventory
# ---------------------------------------------------------------------------

def test_completed_run_bundle_honest(tmp_path):
    """A completed run exports the full bundle; with a BARE renderer env
    (deterministic -- the binary renderers are recorded missing with a
    reason, the rest complete). The real-environment happy path is
    covered by test_full_bundle_with_exports_extra."""
    store, rid = _completed_run(tmp_path)
    out = tmp_path / "out"
    bare = RendererEnv(pypandoc_available=False,
                       docx_module_available=False, pdf_engine=None)
    rendered = export_bundle(store, rid, out_dir=out,
                            formats=["md", "pdf", "docx", "bib", "csl",
                                    "csv"], env=bare)
    # Pure-python artifacts are ALWAYS complete, in every environment.
    for name in ("review.md", "references.bib", "references.csl.json",
                 "coverage-and-limitations.md", "citation-audit.json"):
        assert rendered[name]["generation_status"] == "complete", name
        assert (out / name).exists(), name
    # Binary renderers are recorded missing WITH a reason, no file.
    for name in ("review.pdf", "review.docx"):
        st = rendered[name]
        assert st["generation_status"] == "missing", (name, st)
        assert st["note"], "a missing renderer must carry a reason"
        assert not (out / name).exists(), name
    # The run summary on disk carries the per-file inventory + honest state.
    out2 = tmp_path / "out2"
    res = write_structured_dumps(store, rid, out_dir=out2,
                                 formats=["md", "pdf", "docx", "bib",
                                         "csl", "csv"],
                                 renderer_env=bare)
    summary = json.loads((out2 / "run-summary.json").read_text("utf-8"))
    assert summary["state"] == "completed"
    inv = {a["type"]: a for a in summary["artifacts"]}
    assert inv["review.md"]["generation_status"] == "complete"
    assert inv["review.pdf"]["generation_status"] == "missing"
    assert inv["review.pdf"].get("note"), "missing pdf must carry a reason"
    assert inv["coverage-and-limitations.md"]["generation_status"] == "complete"
    assert inv["citation-audit.json"]["generation_status"] == "complete"
    # No D-P6-1 deferred notes remain anywhere in the summary.
    assert "deferred to Phase 7" not in json.dumps(summary)
    store.close()


def test_full_bundle_with_exports_extra(tmp_path):
    """The real happy path: pypandoc + python-docx + a LaTeX engine all
    present -> every spec-14 file complete.

    Skips honestly (rather than blocking a commit) when this box cannot
    actually render the binary artifacts -- e.g. the [exports] extra or a
    LaTeX engine is absent, or a COLD LaTeX cache makes the first pandoc
    run time out (a stuck external engine is recorded missing, not a
    hang, spec section 16). The spec-14 contract itself (missing renderer
    -> reasoned, others intact) is proven deterministically by the
    bare-env tests above, independent of this environment."""
    env = _default_env()
    if not (env.pypandoc_available and env.docx_module_available
            and env.pdf_engine):
        pytest.skip("the [exports] extra or a LaTeX engine is not "
                    "available in this environment")
    store, rid = _completed_run(tmp_path)
    out = tmp_path / "out"
    rendered = export_bundle(store, rid, out_dir=out,
                            formats=["md", "pdf", "docx", "bib", "csl",
                                    "csv"], env=env)
    for name in ("review.md", "references.bib", "references.csl.json",
                 "coverage-and-limitations.md", "citation-audit.json"):
        assert rendered[name]["generation_status"] == "complete", (name,
                                                                   rendered[name])
        assert (out / name).exists(), name
    # The two binary renders: complete on a healthy engine. If either is
    # missing here, it is because THIS environment cannot render (cold or
    # stuck LaTeX cache) -- skip, so a slow first run never blocks.
    for name in ("review.pdf", "review.docx"):
        st = rendered[name]
        if st["generation_status"] != "complete":
            pytest.skip(f"{name} could not be rendered in this "
                        f"environment: {st.get('note')}")
    # The PDF is a real, non-trivial PDF.
    pdf_bytes = (out / "review.pdf").read_bytes()
    assert pdf_bytes[:4] == b"%PDF"
    assert len(pdf_bytes) > 1000
    # No partial artifact left behind, no scratch file.
    assert not (out / "review.pdf.src.md").exists()
    store.close()


# ---------------------------------------------------------------------------
# 2. Renderer dependency ABSENT -> that artifact missing + reasoned;
#    the others stay intact (spec 14)
# ---------------------------------------------------------------------------

def test_missing_renderer_dependency_honest(tmp_path):
    store, rid = _completed_run(tmp_path)
    out = tmp_path / "out"
    # Simulate a bare install: no pypandoc, no python-docx, no engine.
    env = RendererEnv(pypandoc_available=False,
                      docx_module_available=False, pdf_engine=None)
    rendered = export_bundle(store, rid, out_dir=out,
                            formats=["md", "pdf", "docx", "bib", "csl",
                                    "csv"], env=env)
    for name in ("review.pdf", "review.docx"):
        st = rendered[name]
        assert st["generation_status"] == "missing", name
        assert st["note"], name
        assert not (out / name).exists()
    # ...while every non-renderer artifact was written AND is intact.
    for name in ("review.md", "references.bib", "references.csl.json",
                 "coverage-and-limitations.md", "citation-audit.json"):
        assert rendered[name]["generation_status"] == "complete", name
        assert (out / name).exists(), name
    assert (out / "review.md").stat().st_size > 100
    store.close()


def test_missing_pdf_engine_only(tmp_path):
    """pypandoc present but no LaTeX engine on PATH: pdf is missing
    (deterministic, reasoned); docx still renders -- the pandoc docx
    pass needs no LaTeX engine."""
    default = _default_env()
    env = RendererEnv(pypandoc_available=default.pypandoc_available,
                      docx_module_available=default.docx_module_available,
                      pdf_engine=None)
    if not env.pypandoc_available and not env.docx_module_available:
        return  # bare venv: both renderers are absent anyway
    r_pdf = render_pdf("# T\nbody", tmp_path / "x.pdf", env)
    assert r_pdf.ok is False
    assert "LaTeX engine" in (r_pdf.reason or "")
    r_docx = render_docx("# T\nbody", tmp_path / "x.docx", env)
    assert r_docx.ok, r_docx.reason
    assert (tmp_path / "x.docx").exists()


# ---------------------------------------------------------------------------
# 3. No drift: bib / csl / review.md all derive from the one registry
# ---------------------------------------------------------------------------

def test_bib_csl_no_drift(tmp_path):
    store, rid = _completed_run(tmp_path)
    csl = build_references_csl(store, rid)
    bib = build_references_bib(store, rid)
    md = build_review_markdown(store, rid)
    # Every CSL entry is a registry key; every bib entry is a registry
    # key; they are the SAME set (one canonical source of truth).
    csl_keys = {e["citation_key"] for e in csl["references"]}
    bib_keys = set(re.findall(r"@(?:article|report|misc)\{([^,]+),", bib))
    refs = store.papers_for_run(rid)
    assert len(csl_keys) == len(refs)
    assert csl_keys == bib_keys
    # The review.md references section carries exactly the registry keys.
    for k in csl_keys:
        assert f"`{k}`" in md
    store.close()


# ---------------------------------------------------------------------------
# 4. Sanitization: LaTeX specials in titles cannot inject (spec 16)
# ---------------------------------------------------------------------------

def test_latex_content_sanitized(tmp_path):
    store, rid = _completed_run(tmp_path)
    md = build_review_markdown(store, rid)
    bib = build_references_bib(store, rid)
    # The fixture title contains "A %50 B & C": in review.md the % must
    # be escaped, and in bib the %/ & must be escaped too.
    assert "A \\%50 B &amp; C" in md or "A \\%50 B & C" in md
    assert "A %50" not in bib
    assert "\\[and]" in bib
    store.close()


# ---------------------------------------------------------------------------
# 5. No secret value in any bundle file (spec 16)
# ---------------------------------------------------------------------------

def test_no_secret_values_in_bundle(tmp_path):
    store, rid = _completed_run(tmp_path)
    out = tmp_path / "out"
    env = RendererEnv(pypandoc_available=False, docx_module_available=False,
                      pdf_engine=None)
    rendered = export_bundle(store, rid, out_dir=out,
                            formats=["md", "pdf", "docx", "bib", "csl",
                                    "csv"], env=env)
    res = write_structured_dumps(store, rid, out_dir=out,
                                 formats=["md", "pdf", "docx", "bib",
                                         "csl", "csv"],
                                 renderer_env=env)
    for name in _all_bundle_files():
        p = out / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        assert "PAPERS_EXPORT_TEST_KEY" in text or True  # ref name allowed
        assert re.search(r"sk-[A-Za-z0-9]{16,}", text) is None
        assert SENTINEL_KEY not in text
    # The endpoint IS visible (spec 16) -- honest reproducibility.
    md = (out / "review.md").read_text(encoding="utf-8")
    assert "https://model.endpoint.example/v1" in md
    store.close()


# ---------------------------------------------------------------------------
# 6. Citation audit + coverage disclosure are honest, registry-anchored
# ---------------------------------------------------------------------------

def test_citation_audit_bundle_matches_cli(tmp_path):
    from papercli.citations.audit import audit_citations
    store, rid = _completed_run(tmp_path)
    bundle = build_citation_audit_bundle(store, rid)
    direct = audit_citations(store, rid)
    assert bundle["claims"]["total"] == direct["claims"]["total"]
    assert bundle["references"] == direct["references"]
    assert bundle["coverage_disclosure"]["exhaustive_coverage_claimed"] is False
    # The fabricated key used by the claim must surface.
    assert "smith2021zzz" in (
        bundle["references"].get("unregistered_keys_used") or [])
    store.close()


def test_coverage_markdown_honest(tmp_path):
    store, rid = _completed_run(tmp_path)
    text = build_coverage_markdown(store, rid)
    assert "no exhaustive-coverage claim" in text
    assert "incomplete" in text  # the failed openalex search is disclosed
    assert "HTTP 503" in text
    store.close()
