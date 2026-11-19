"""Rendered exporters (Phase 7, spec sections 10/14/16).

One canonical source of truth for every renderer (spec section 14):

* ``references.bib`` and ``references.csl.json`` are derived ONLY from
  the bibliography registry (spec section 10: no invented entries;
  CSL/citeproc-compatible fields);
* ``review.md`` is the canonical narrative review, built from the
  structured draft (synthesis task artifact) + evidence registry +
  claim ledger + bibliography registry;
* ``review.pdf`` and ``review.docx`` render from that SAME
  ``review.md`` -- never from a second text source.

Every renderer PREFLIGHTS its own dependency (spec section 14: no
silent missing "completed" artifacts). A renderer whose dependency is
missing, or whose render fails, is recorded ``missing`` with an
explicit reason; the Markdown / BibTeX / CSL / CSV artifacts are
NEVER destroyed or blocked by a renderer failure.

Security (spec section 16): user/model content is escaped for the
LaTeX/HTML sinks (shell escape is off for the pandoc LaTeX pass;
markdown text is fenced so raw content cannot inject formatting), and
no generated document content is ever executed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "RendererEnv",
    "make_renderer_env",
    "RenderResult",
    "render_docx",
    "render_pdf",
    "build_review_markdown",
    "build_references_bib",
    "build_references_csl",
    "build_coverage_markdown",
    "build_citation_audit_bundle",
    "export_bundle",
]


# ---------------------------------------------------------------------------
# Renderer dependency preflight (spec section 14)
# ---------------------------------------------------------------------------

PANDOC_MODULE = "pypandoc"
DOCX_MODULE = "docx"
#: The LaTeX engines pypandoc can drive the PDF pass with. pdflatex is
# pandoc's documented default on Windows and the first engine tried;
# xelatex (unicode-capable) is the fallback when pdflatex is absent.
PDF_ENGINES = ("pdflatex", "xelatex")


@dataclass
class RendererEnv:
    """What is available to the renderers in this process. Overridable
    in tests to simulate a dependency being absent."""
    pypandoc_available: bool
    docx_module_available: bool
    pdf_engine: str | None

    def pypandoc(self):
        """The pypandoc module, or None when the preflight failed."""
        if not self.pypandoc_available:
            return None
        import pypandoc
        return pypandoc

    def docx_module(self):
        """The python-docx module, or None when the preflight failed."""
        if not self.docx_module_available:
            return None
        import docx
        return docx


def make_renderer_env() -> RendererEnv:
    """Preflight the exporter dependencies (spec section 14)."""
    import importlib.util
    pypandoc_ok = importlib.util.find_spec(PANDOC_MODULE) is not None
    docx_ok = importlib.util.find_spec(DOCX_MODULE) is not None
    engine = next((e for e in PDF_ENGINES if shutil.which(e)), None)
    return RendererEnv(pypandoc_available=pypandoc_ok,
                       docx_module_available=docx_ok,
                       pdf_engine=engine)


def _pypandoc_cli_ok(env: RendererEnv) -> bool:
    """The pypandoc wheel bundles a pandoc binary; if the module loads
    but cannot locate its binary, the PDF/DOCX pandoc pass is unavailable."""
    pp = env.pypandoc()
    if pp is None:
        return False
    try:
        pp.get_pandoc_version()
        return True
    except Exception:  # noqa: BLE001 - a broken bundle counts as missing
        return False


# ---------------------------------------------------------------------------
# Content escaping (spec section 16: sanitize LaTeX/HTML export)
# ---------------------------------------------------------------------------

_MD_ESCAPES = (
    ("\\", "\\\\"),
    ("`", "\\`"),
    ("&", "&amp;"),
    ("%", "\\%"),
    ("$", "\\$"),
)


def _esc(text: Any) -> str:
    """Escape user/model content for the markdown sink so raw text can
    never open a code fence or a LaTeX/HTML construct."""
    s = str(text if text is not None else "")
    for k, v in _MD_ESCAPES:
        s = s.replace(k, v)
    return s


_BIB_ESCAPES = (
    ("{", "\\{"),
    ("}", "\\}"),
    ("&", "\\[and]"),
    ("%", "\\%"),
    ("$", "\\$"),
    ("#", "\\#"),
    ("_", "\\_"),
    ("~", "\\textasciitilde{}"),
    ("^", "\\textasciicircum{}"),
)


def _bib_escape(text: Any) -> str:
    """Escape a registry value for a BibTeX field. Non-ASCII characters
    are kept verbatim: the pdf pass runs under a unicode-capable engine
    (xelatex) and the docx sink is UTF-8."""
    s = str(text if text is not None else "").strip()
    for k, v in _BIB_ESCAPES:
        s = s.replace(k, v)
    return s


# ---------------------------------------------------------------------------
# Registry -> bibliography (spec section 10: the registry is the ONLY source)
# ---------------------------------------------------------------------------

def _registry_refs(store, run_id: str) -> list[dict[str, Any]]:
    from papercli.reasoning.common import bibliography_for_run
    return bibliography_for_run(store, run_id)


def build_references_csl(store, run_id: str) -> dict[str, Any]:
    """CSL-JSON bibliography derived ONLY from the bibliography registry."""
    refs = _registry_refs(store, run_id)
    entries = []
    for r in refs:
        ids = r.get("identifiers") or {}
        ctype = "report" if ids.get("arxiv") else "journal-article"
        authors = [{"literal": str(a)} for a in (r.get("authors") or [])]
        entry: dict[str, Any] = {
            "type": ctype,
            "citation_key": r.get("citation_key"),
            "title": r.get("title"),
            "author": authors or None,
            "issued": ({"date-parts": [[r["year"]]]}
                       if r.get("year") else None),
            "DOI": ids.get("doi"),
            "arxiv": ids.get("arxiv"),
            "PMID": ids.get("pmid"),
            "PMCID": ids.get("pmcid"),
            "URL": ids.get("url"),
        }
        if r.get("provenance"):
            entry["provenance"] = r["provenance"]
        entries.append(entry)
    return {"run_id": run_id, "schema_version": "1.0", "references": entries}


def build_references_bib(store, run_id: str) -> str:
    """BibTeX derived from the SAME registry the CSL-JSON uses
    (one canonical source of truth, spec section 14)."""
    lines = [
        "% references.bib -- generated from the run bibliography registry.",
        "% Spec section 10: registry-only entries, no invented references.",
        "",
    ]
    for r in _registry_refs(store, run_id):
        ids = r.get("identifiers") or {}
        ctype = "report" if ids.get("arxiv") else "article"
        lines.append(f"@{ctype}{{{r['citation_key']},")
        if r.get("title"):
            lines.append(f"  title = {{{_bib_escape(r['title'])}}},")
        authors = r.get("authors") or []
        if authors:
            lines.append("  author = {" +
                         " and ".join(_bib_escape(a) for a in authors) + "},")
        if r.get("year"):
            lines.append(f"  year = {{{r['year']}}},")
        for field in ("doi", "pmid", "pmcid", "url"):
            v = ids.get(field)
            if v:
                lines.append(f"  {field} = {{{_bib_escape(v)}}},")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The canonical review.md
# ---------------------------------------------------------------------------

def _synthesis_draft(store, run_id: str) -> dict[str, Any] | None:
    """The model synthesis artifact, when the synthesize task ran
    (agent-driven runs without a model have no draft -- disclosed)."""
    from papercli.exports.run_summary import run_dir
    p = run_dir(store, run_id) / "synthesis_draft.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _claim_citations(c: Mapping[str, Any],
                     refs: Mapping[str, Mapping[str, Any]]) -> str:
    keys = [k for k in (c.get("citation_keys") or []) if k in refs]
    return " ".join(f"[{k}]" for k in keys)


def build_review_markdown(store, run_id: str) -> str:
    """The canonical narrative review (spec section 14: methodology,
    thematic synthesis, limitations, references + a reproducibility
    block carrying software/model versions and search dates)."""
    import sys as _sys

    from papercli import __version__ as papercli_version
    from papercli.reasoning.common import run_protocol
    from papercli.exports.run_summary import (_endpoint_used, _model_used,
                                              _search_dates)

    protocol = run_protocol(store, run_id) or {}
    refs = _registry_refs(store, run_id)
    refmap = {r["citation_key"]: r for r in refs}
    claims = store.claims_for_run(run_id)
    screenings = store.screenings_for_run(run_id)
    searches = store.searches_for_run(run_id)
    calls = store.model_calls_for_run(run_id)
    row = store.run_row(run_id) or {}
    dates = _search_dates(store, run_id)
    draft = _synthesis_draft(store, run_id)

    L: list[str] = []
    L.append(f"# Literature review: {_esc(protocol.get('question') or run_id)}")
    L.append("")
    L.append(f"Run `{run_id}` -- state: **{row.get('state')}**. "
             "Canonical narrative review generated from the structured "
             "draft, evidence registry, claim ledger, and bibliography "
             "registry (one canonical source of truth, spec section 14).")
    L.append("")

    L.append("## Methodology")
    L.append("")
    L.append(f"- **Question:** {_esc(protocol.get('question'))}")
    if protocol.get("scope"):
        L.append(f"- **Scope:** {_esc(protocol['scope'])}")
    L.append(f"- **Review type:** "
             f"{_esc(protocol.get('review_type') or 'narrative')}")
    themes = protocol.get("themes_to_investigate") or []
    L.append("- **Themes:** " +
             (", ".join(_esc(t) for t in themes) if themes
              else "none supplied (question-driven only)"))
    inc = protocol.get("inclusion_criteria") or []
    exc = protocol.get("exclusion_criteria") or []
    L.append("- **Inclusion criteria:** " +
             ("; ".join(_esc(c) for c in inc) if inc else "none supplied"))
    L.append("- **Exclusion criteria:** " +
             ("; ".join(_esc(c) for c in exc) if exc else "none supplied"))
    providers = sorted({s.get("provider") for s in searches
                        if s.get("provider")} or ["(none recorded)"])
    L.append("- **Search providers:** " +
             ", ".join(_esc(p) for p in providers))
    L.append(f"- **Search window:** {dates.get('from') or 'unknown'} to "
             f"{dates.get('to') or 'unknown'}")
    decided = [s for s in screenings if s.get("decision")]
    L.append(f"- **Screening:** {len(decided)} screening decision(s) recorded "
             f"({sum(1 for s in decided if s['decision'] == 'include')} include, "
             f"{sum(1 for s in decided if s['decision'] == 'exclude')} exclude, "
             f"{sum(1 for s in decided if s['decision'] == 'uncertain')} "
             f"uncertain).")
    if protocol.get("assumptions"):
        L.append("- **Recorded assumptions:**")
        for a in protocol["assumptions"]:
            L.append(f"  - {_esc(a)}")
    L.append("")

    L.append("## Thematic synthesis")
    L.append("")
    themed: list[Mapping[str, Any]] = []
    if draft is not None and (draft.get("themes") or []):
        for t in draft["themes"]:
            if not isinstance(t, dict):
                continue
            L.append(f"### {_esc(t.get('theme') or '(untitled theme)')}")
            L.append("")
            theme_loc = f"theme::{t.get('theme')}"
            theme_claims = [c for c in claims
                            if str(c.get("location") or c.get("draft_location")
                                   or "").startswith(theme_loc)]
            if theme_claims:
                for c in theme_claims:
                    cite = _claim_citations(c, refmap)
                    label = c.get("audit_status") or "uncertain"
                    mark = ""
                    if label in ("unsupported", "uncertain"):
                        mark = (f"_{label} -- unresolved, prominently "
                                "marked before finalization: _ ")
                    L.append(f"{mark}- {_esc(c.get('claim_text'))}{cite}")
            else:
                for c in (t.get("claims") or []):
                    cite = " ".join(f"[{k}]" for k in
                                    (c.get("citation_keys") or []))
                    L.append(f"- {_esc(c.get('text'))}{cite}")
            for g in (t.get("gaps") or []):
                L.append(f"- **Gap:** {_esc(g)}")
            L.append("")
            themed.append(t)
    else:
        L.append("No model synthesis draft is available for this run "
                 "(agent-driven mode, or the synthesize task did not "
                 "run); the claim ledger below carries the full set of "
                 "recorded substantive claims.")
        L.append("")
        for c in claims:
            cite = _claim_citations(c, refmap)
            L.append(f"- {_esc(c.get('claim_text'))}{cite}")
        L.append("")
    unthemed = [c for c in claims
                if not str(c.get("location") or c.get("draft_location")
                           or "").startswith("theme::")]
    if unthemed:
        L.append("### Claims not organized under a recorded theme")
        L.append("")
        for c in unthemed:
            cite = _claim_citations(c, refmap)
            L.append(f"- {_esc(c.get('claim_text'))}{cite}")
        L.append("")
    contras = (draft or {}).get("contradictions") or []
    if contras:
        L.append("## Contradictory evidence")
        L.append("")
        for x in contras:
            L.append(f"- {_esc(x.get('a'))} vs {_esc(x.get('b'))} "
                     f"{' '.join(f'[{k}]' for k in (x.get('citation_keys') or []))}")
        L.append("")

    L.append("## Limitations")
    L.append("")
    lims: list[str] = []
    if draft and draft.get("limitations"):
        lims += [str(s) for s in draft["limitations"] if isinstance(s, str)]
    lims += [
        "Coverage is bounded by the recorded searches and retrieval order; "
        "no exhaustive-coverage claim is made (see "
        "coverage-and-limitations.md, spec section 14).",
        "Unsupported or uncertain claims are marked unresolved above; "
        "they must be removed, narrowed, or kept marked before "
        "finalization (spec section 10).",
    ]
    for s in lims:
        L.append(f"- {_esc(s)}")
    L.append("")

    L.append("## Reproducibility")
    L.append("")
    L.append(f"- PaperCLI: agent-papercli {papercli_version} (python "
             f"{_sys.version.split()[0]})")
    model_used = _model_used(calls)
    endpoint_used = _endpoint_used(calls)
    if model_used or endpoint_used:
        L.append(f"- Model: {model_used or 'unknown'} @ "
                 f"{endpoint_used or 'unknown endpoint'} (the API key is "
                 "recorded only as its environment-variable reference "
                 "name, never its value -- spec section 16)")
    L.append(f"- Search dates: {dates.get('from') or 'unknown'} to "
             f"{dates.get('to') or 'unknown'}")
    L.append(f"- Protocol revision: {row.get('protocol_revision')} "
             f"(protocol hash {_esc(row.get('protocol_hash'))})")
    L.append("")

    L.append("## References")
    L.append("")
    if refs:
        for r in refs:
            bits = []
            authors = r.get("authors") or []
            if authors:
                bits.append(", ".join(str(a) for a in authors))
            if r.get("year"):
                bits.append(str(r["year"]))
            if r.get("title"):
                bits.append(str(r["title"]))
            ids = r.get("identifiers") or {}
            link = ids.get("url") or (f"https://doi.org/{ids['doi']}"
                                      if ids.get("doi") else None)
            L.append(f"- `{r['citation_key']}` -- "
                     + " ".join(_esc(b) for b in bits)
                     + (f" <{link}>" if link else "")
                     + f" (access: {_esc(r.get('access_level') or 'metadata')})")
    else:
        L.append("No references in the bibliography registry for this run.")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# The remaining spec-14 bundle files
# ---------------------------------------------------------------------------

def build_coverage_markdown(store, run_id: str) -> str:
    """coverage-and-limitations.md -- honest: missing papers, incomplete
    searches, uncertain evidence. Never an exhaustive-coverage claim."""
    from papercli.citations.audit import audit_citations

    searches = store.searches_for_run(run_id)
    incomplete = [s for s in searches if s.get("completeness") != "complete"]
    errors = [s for s in searches if s.get("error")]
    papers = store.papers_for_run(run_id)
    conn = store._conn
    no_full_text = [
        p["id"] for p in papers
        if conn.execute(
            "SELECT 1 FROM documents d WHERE d.paper_id=? AND "
            "d.access_level='full_text'", (p["id"],)).fetchone() is None]
    uncertain = [s for s in store.screenings_for_run(run_id)
                 if s.get("decision") == "uncertain"]
    audit = audit_citations(store, run_id)

    L = [
        "# Coverage and limitations",
        "",
        "This run makes **no exhaustive-coverage claim** "
        "(spec sections 10/14). What is NOT covered is disclosed here.",
        "",
        f"- Searches that did NOT complete: {len(incomplete)} of "
        f"{len(searches)} recorded (provider failure, truncation, or a "
        f"recently failed request in the negative cache).",
    ]
    for s in incomplete:
        L.append(f"  - {s.get('provider')}: `{_esc(s.get('query'))}` -- "
                 f"completeness={_esc(s.get('completeness'))}"
                 + (f", error: {_esc(s.get('error'))}"
                    if s.get("error") else ""))
    L.append(f"- Papers with NO full-text document retrieved: "
             f"{len(no_full_text)} of {len(papers)} discovered record(s).")
    L.append("- Screening outcomes still 'uncertain' (not resolved by the "
             f"model or by filter signals): {len(uncertain)}.")
    refs = audit.get("references") or {}
    L.append(f"- Bibliographic gaps: "
             f"{len(refs.get('orphan_references') or [])} orphan "
             f"reference(s), "
             f"{len(refs.get('duplicate_keys') or [])} duplicate key(s), "
             f"{len(refs.get('unregistered_keys_used') or [])} "
             f"unregistered key(s) used by claims.")
    cr = audit.get("correction_retraction_check") or {}
    L.append(f"- Retraction/correction check: {_esc(cr.get('source'))}. "
             "Limits: " + "; ".join(_esc(x) for x in cr.get("limits") or []))
    L.append("")
    return "\n".join(L)


def build_citation_audit_bundle(store, run_id: str) -> dict[str, Any]:
    """citation-audit.json: the Phase-5 ``citations audit`` result,
    verbatim (one canonical source, no drift)."""
    from papercli.citations.audit import audit_citations
    return audit_citations(store, run_id)


# ---------------------------------------------------------------------------
# The two binary renderers (from the SAME canonical review.md)
# ---------------------------------------------------------------------------

@dataclass
class RenderResult:
    ok: bool
    reason: str | None = None
    via: str | None = None


def _tmp_md(md_text: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    p = out_path.with_name(out_path.name + ".src.md")
    p.write_text(md_text, encoding="utf-8")
    return p


#: Bounded render runtime (spec section 16): a stuck external LaTeX
#: engine must NOT hang the CLI / a test -- it becomes a recorded-missing
#: artifact with a reason instead.
PDF_RENDER_TIMEOUT_SECONDS = 180
DOCX_RENDER_TIMEOUT_SECONDS = 60


def _pandoc_binary(env: RendererEnv) -> str | None:
    """The pandoc binary path (bundled via pypandoc), or None."""
    if not env.pypandoc_available:
        return None
    pp = env.pypandoc()
    if pp is None:
        return None
    try:
        return pp.get_pandoc_path()
    except Exception:  # noqa: BLE001 - a broken bundle is a missing dep
        return None


def _kill_tree(pid: int) -> None:
    """Kill a process tree (pandoc -> LaTeX engine) without leaving
    orphaned engine children behind (Windows: taskkill /T; POSIX:
    kill the group)."""
    import subprocess
    import sys
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def _run_pandoc(binpath: str, args: list[str],
                timeout: float) -> RenderResult:
    """Run the pandoc binary with an argument array (no shell) and a
    bounded runtime (spec section 16); a timeout kills the whole
    process tree (a stuck external engine must not hang the CLI)."""
    import subprocess
    cmd = [binpath, *args]
    kwargs: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, shell=False)
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        return RenderResult(
            False,
            f"render timed out after {int(timeout)}s (a stuck external "
            f"LaTeX engine is recorded missing, not a hang)")
    if proc.returncode == 0:
        return RenderResult(True, None, "pandoc")
    tail = ((err or out or "").strip().splitlines())
    err_txt = " | ".join(tail[-3:])[:500]
    return RenderResult(False,
                        f"pandoc exited {proc.returncode}: {err_txt}",
                        "pandoc")
    if proc.returncode == 0:
        return RenderResult(True, None, "pandoc")
    # Pandoc reports the engine's stderr; keep only a bounded tail so a
    # runaway engine message cannot flood the artifact / envelope.
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    err = " | ".join(tail[-3:])[:500]
    return RenderResult(False,
                        f"pandoc exited {proc.returncode}: {err}",
                        "pandoc")


def render_pdf(md_text: str, out_path: Path,
               env: RendererEnv) -> RenderResult:
    """pandoc -> PDF under the prefetched LaTeX engine; shell escape is
    DISABLED (spec section 16). The preflight (``env``) is the source of
    truth: with no pypandoc, no working pandoc binary, or no LaTeX
    engine on PATH, the render is recorded missing WITHOUT attempting --
    the reason says what to install (spec section 14 preflight)."""
    if not env.pypandoc_available:
        return RenderResult(
            False,
            "pdf renderer unavailable: the 'pypandoc' module is not "
            "installed (pip install 'agent-papercli[exports]')")
    binpath = _pandoc_binary(env)
    if not binpath:
        return RenderResult(
            False,
            "pdf renderer unavailable: the bundled pandoc binary could "
            "not be located")
    if env.pdf_engine is None:
        return RenderResult(
            False,
            f"pdf renderer unavailable: no LaTeX engine found on PATH "
            f"({', '.join(PDF_ENGINES)}); install one to render "
            "review.pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = _tmp_md(md_text, out_path)
    args = ["-s", str(src), "-o", str(out_path), "--pdf-engine",
            env.pdf_engine, "-V", "shell-escape=off"]
    r = _run_pandoc(binpath, args, PDF_RENDER_TIMEOUT_SECONDS)
    if not r.ok:
        # A failed render never leaves a partial artifact on disk.
        out_path.unlink(missing_ok=True)
    return r


def render_docx(md_text: str, out_path: Path,
                env: RendererEnv) -> RenderResult:
    """pandoc -> docx (no LaTeX engine needed); when the pandoc pass is
    unavailable or fails, fall back to python-docx. Both sinks are plain,
    editable documents; the pandoc pass is bounded (spec section 16)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    binpath = _pandoc_binary(env)
    pandoc_fail: RenderResult | None = None
    if binpath:
        src = _tmp_md(md_text, out_path)
        r = _run_pandoc(binpath, ["-s", str(src), "-o", str(out_path)],
                        DOCX_RENDER_TIMEOUT_SECONDS)
        if r.ok:
            return r
        pandoc_fail = r
    docx = env.docx_module()
    if docx is None:
        if pandoc_fail is not None:
            return RenderResult(
                False,
                f"pandoc docx render failed ({pandoc_fail.reason}) and "
                "the python-docx fallback is not installed (pip install "
                "'agent-papercli[exports]')")
        return RenderResult(
            False,
            "docx renderer unavailable: no usable pandoc binary and "
            "python-docx is not installed (pip install "
            "'agent-papercli[exports]')")
    doc = docx.Document()
    for line in md_text.splitlines():
        if line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.lstrip().startswith("- "):
            doc.add_paragraph(line.lstrip()[2:], style="List Bullet")
        elif line.strip():
            doc.add_paragraph(line.strip())
    doc.save(str(out_path))
    return RenderResult(True, None, "python-docx")

def export_bundle(store, run_id: str, out_dir: Path | str | None = None,
                  formats: list[str] | None = None,
                  env: RendererEnv | None = None) -> dict[str, Any]:
    """Render the spec-14 bundle artifacts for the requested formats.

    ``formats`` (default: all of md,pdf,docx,bib,csl,csv) selects which
    of the rendered / bibliographic files are produced; the disclosure
    files (coverage-and-limitations.md, citation-audit.json) are always
    part of the bundle (spec section 14).

    Returns ``{name: {"path", "generation_status": complete|missing,
    "note"}}``. A renderer whose dependency is missing, or whose render
    fails, marks ONLY that artifact missing with an explicit reason;
    the Markdown / BibTeX / CSL / CSV artifacts are never destroyed or
    blocked (spec section 14).
    """
    out = Path(out_dir) if out_dir else _run_dir_fn(store, run_id)
    out.mkdir(parents=True, exist_ok=True)
    env = env or make_renderer_env()
    requested = {f.strip().lower() for f in (formats or []) if f.strip()}
    if not requested:
        requested = set(_DEFAULT_FORMATS)

    result: dict[str, dict[str, Any]] = {}

    def record(name: str, status: str, note: str | None = None,
               path: Path | None = None) -> None:
        result[name] = {
            "path": str(path) if path else str(out / name),
            "generation_status": status,
            "note": note,
        }

    # -- review.md: the canonical narrative review (pure python) ----------
    md_text = ""
    if "md" in requested or "pdf" in requested or "docx" in requested:
        md_text = build_review_markdown(store, run_id)
        if "md" in requested:
            p = out / "review.md"
            p.write_text(md_text, encoding="utf-8")
            record("review.md", "complete", path=p)

    # -- references.bib + references.csl.json: registry-only (spec 10) ---
    if "bib" in requested:
        p = out / "references.bib"
        p.write_text(build_references_bib(store, run_id),
                     encoding="utf-8")
        record("references.bib", "complete", path=p)
    if "csl" in requested:
        p = out / "references.csl.json"
        p.write_text(json.dumps(build_references_csl(store, run_id),
                                indent=2, ensure_ascii=False,
                                default=str), encoding="utf-8")
        record("references.csl.json", "complete", path=p)

    # -- binary renderers from the SAME canonical review.md ---------------
    if md_text:
        if "pdf" in requested:
            r = render_pdf(md_text, out / "review.pdf", env)
            record("review.pdf", "complete" if r.ok else "missing",
                   None if r.ok else r.reason,
                   out / "review.pdf" if r.ok else None)
        if "docx" in requested:
            r = render_docx(md_text, out / "review.docx", env)
            record("review.docx", "complete" if r.ok else "missing",
                   None if r.ok else r.reason,
                   out / "review.docx" if r.ok else None)

    # -- disclosure files: always in the bundle (spec section 14) --------
    p = out / "coverage-and-limitations.md"
    p.write_text(build_coverage_markdown(store, run_id), encoding="utf-8")
    record("coverage-and-limitations.md", "complete", path=p)
    p = out / "citation-audit.json"
    p.write_text(json.dumps(build_citation_audit_bundle(store, run_id),
                             indent=2, ensure_ascii=False,
                             default=str), encoding="utf-8")
    record("citation-audit.json", "complete", path=p)

    # Remove the pandoc scratch files: the canonical source is review.md.
    for scratch in ("review.pdf.src.md", "review.docx.src.md"):
        sp = out / scratch
        if sp.exists():
            sp.unlink()
    return result


_DEFAULT_FORMATS = ("md", "pdf", "docx", "bib", "csl", "csv")


def _run_dir_fn(store, run_id: str) -> Path:
    from papercli.exports.run_summary import run_dir
    return run_dir(store, run_id)
