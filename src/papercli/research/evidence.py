"""Evidence extraction structure (Phase 4, spec section 9).

Deterministic extraction of structured evidence records from parsed full
text into the evidence registry (Phase-3 minimum schema), keyed to the
document + stable (page, section, paragraph) locations. The model-driven
"finding" interpretation is Phase 5 -- this phase records the *structure*
only:

* location: the numbered page / section / paragraph of the stored index;
* supporting_excerpt: a byte-exact slice of the stored source text
  (resolves through the Phase-3 ``document_text`` index -- acceptance
  "Extraction");
* claim_type candidate: section heuristics map to
  ``reported_result`` / ``method`` / ``limitation``;
* methods / population_or_system: null where unknown (never invented);
* extraction_warnings: carried over from the document parse + the
  D-1/D-3 parse-quality grade (scanned/partial documents are disclosed,
  never represented as fully read).

The **never-infer** rule (spec section 9) is enforced in code: records
whose access level is below ``partial_full_text`` (``abstract_only``,
``metadata_only``, ``unavailable``) get NO evidence rows -- a full-text
finding is never inferred from metadata.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from papercli.storage.db import PaperStore

__all__ = [
    "AccessLevelGate",
    "classify_claim_type",
    "extract_evidence_for_document",
    "extract_evidence_for_run",
    "SECTION_CLAIM_TYPES",
]

#: Section-name heuristics -> claim_type candidate (spec section 9
#: "claim_type candidate based on section heuristics"). Unknown sections
#: default to ``reported_result`` (the most common evidence role).
SECTION_CLAIM_TYPES: dict[str, str] = {
    "results": "reported_result",
    "result": "reported_result",
    "findings": "reported_result",
    "experiments": "reported_result",
    "results and discussion": "reported_result",
    "discussion": "reported_result",
    "conclusion": "reported_result",
    "conclusions": "reported_result",
    "methods": "method",
    "method": "method",
    "materials and methods": "method",
    "materials & methods": "method",
    "materials": "method",
    "experimental design": "method",
    "protocol": "method",
    "participants": "method",
    "population": "method",
    "materials": "method",
    "limitations": "limitation",
    "limitation": "limitation",
    "threats to validity": "limitation",
    "discussion and limitations": "limitation",
}

#: Access levels that gate evidence extraction OUT (the never-infer rule).
NEVER_INFER_LEVELS = ("abstract_only", "metadata_only", "unavailable")


class AccessLevelGate:
    """The "never infer full-text findings from metadata" gate.

    ``usable(level)`` is True only for levels at or above
    ``partial_full_text``. Screening/extraction layers MUST consult it:
    gated levels produce zero evidence rows.
    """

    RANK = ("unavailable", "metadata_only", "abstract_only",
            "partial_full_text", "full_text")

    def usable(self, access_level: str | None) -> bool:
        return (access_level or "unavailable") in ("partial_full_text",
                                                    "full_text")


#: Excerpt budget per document (bounded deterministic extraction; the
#: Phase-5 model layer may add findings beyond these).
MAX_EXCERPTS_PER_DOCUMENT = 40
MAX_EXCERPT_CHARS = 600


def classify_claim_type(section: str | None) -> str:
    """Deterministic claim_type candidate from a section name."""
    key = (section or "").strip().lower()
    if not key:
        return "reported_result"
    for name, ct in SECTION_CLAIM_TYPES.items():
        if key == name:
            return ct
    if "limit" in key:
        return "limitation"
    if "method" in key or "material" in key or "design" in key:
        return "method"
    return "reported_result"


_ACCESS_STATUS_TO_LEVEL = {
    "retrieved_full_text": "full_text",
    "retrieved_html_landing": "partial_full_text",
    "retrieved_abstract": "abstract_only",
    "unavailable": "unavailable",
}


def _normalize_access_level(level: str | None) -> str | None:
    """Map a retrieval-time access_status onto a spec section 9 level.

    Phase-2 retrieval rows store an ``access_status``
    (``retrieved_full_text`` / ``retrieved_html_landing`` /
    ``unavailable``) without the capped spec level; this keeps the gate
    honest: an HTML landing is NOT full text, and an unavailable record
    is gated out.
    """
    if not level:
        return None
    if level in AccessLevelGate.RANK:
        return level
    return _ACCESS_STATUS_TO_LEVEL.get(level)


def _document_access_level(store: PaperStore, document_id: str | None,
                           paper_id: str | None) -> str | None:
    """The effective access level for the paper/document (D-1 capping).

    Document rows carry the capped ``access_level``; when that column is
    not yet filled (retrieval-time rows), the retrieval
    ``access_status`` is mapped onto the spec level. When no document
    exists, the paper-level ``access_level`` set by screening is used.
    """
    if document_id:
        row = store.document_row(document_id)
        if row:
            level = _normalize_access_level(row.get("access_level"))
            if level:
                return level
            return _normalize_access_level(row.get("access_status"))
    if paper_id:
        row = store._conn.execute(
            "SELECT access_level FROM papers WHERE id = ?",
            (paper_id,)).fetchone()
        if row and row["access_level"]:
            return _normalize_access_level(row["access_level"])
    return None


def extract_evidence_for_document(
    store: PaperStore,
    *,
    run_id: str,
    paper_id: str,
    document_id: str,
) -> dict[str, Any]:
    """Deterministic extraction for one parsed document.

    Returns ``{"evidence_ids", "count", "gated", "access_level",
    "parse_grade", "disclosures"}``. Gated documents (never-infer rule)
    yield ZERO rows.
    """
    doc = store.document_row(document_id)
    if doc is None:
        raise KeyError(f"document {document_id} not found")
    access_level = _normalize_access_level(doc.get("access_level")) or \
        _normalize_access_level(doc.get("access_status"))
    grade = doc.get("parse_quality")
    gate = AccessLevelGate()
    if not gate.usable(access_level):
        return {
            "evidence_ids": [],
            "count": 0,
            "gated": True,
            "access_level": access_level,
            "parse_grade": grade,
            "disclosures": [
                f"access_level={access_level or 'unknown'}; evidence "
                "extraction is gated -- a full-text finding is never "
                "inferred from metadata (spec section 9)"],
        }

    rows = store.document_text(document_id)
    warnings_doc = json.loads(doc.get("parse_warnings_json") or "[]")
    disclosures: list[str] = []
    if grade in ("scanned", "partial", "poor"):
        disclosures.append(
            f"parse_quality={grade}; access_level capped to "
            f"{access_level}; scanned/empty pages are not fully read "
            "(OCR out of scope)")
    if "scanned" in str(doc.get("scanned_flag")):
        pass  # grade disclosure above covers the scanned case

    # One evidence record per non-empty stored paragraph, in stable index
    # order. The excerpt is a byte-exact slice of the row text (the
    # Phase-3 index guarantees it resolves against stored source text).
    evidence_ids: list[str] = []
    count = 0
    for row in rows:
        if count >= MAX_EXCERPTS_PER_DOCUMENT:
            disclosures.append(
                f"extraction bounded at {MAX_EXCERPTS_PER_DOCUMENT} "
                "evidence rows; remaining paragraphs unrecorded "
                "(model-driven interpretation is Phase 5)")
            break
        text = str(row.get("text") or "")
        stripped = text.strip()
        if not stripped:
            continue
        excerpt = stripped[:MAX_EXCERPT_CHARS]
        truncated = len(stripped) > MAX_EXCERPT_CHARS
        location = {"page": row["page"], "section": row["section"],
                    "paragraph": row["paragraph"]}
        rec_warnings = list(warnings_doc)
        if truncated:
            rec_warnings.append(
                f"excerpt truncated to {MAX_EXCERPT_CHARS} chars of "
                f"{len(stripped)}")
        ev_id = store.record_evidence(
            paper_id,
            document_id=document_id,
            access_level=access_level,
            location=location,
            supporting_excerpt=excerpt,
            finding="deterministic-location record; model interpretation "
                    "pending (Phase 5)",
            claim_type=classify_claim_type(row.get("section")),
            methods=None,
            population_or_system=None,
            limitations=[],
            extraction_warnings=rec_warnings,
        )
        evidence_ids.append(ev_id)
        count += 1

    out: dict[str, Any] = {
        "evidence_ids": evidence_ids,
        "count": count,
        "gated": False,
        "access_level": access_level,
        "parse_grade": grade,
        "disclosures": disclosures,
    }
    return out


def extract_evidence_for_run(
    store: PaperStore,
    run_id: str,
    provider: Any = None,
) -> dict[str, Any]:
    """Extract evidence for every usable document in the run.

    Document access levels are re-read from the registry (D-1: capped
    levels stay capped); gated papers yield zero rows and are reported.
    """
    results: list[dict[str, Any]] = []
    gated: list[str] = []
    rows = store._conn.execute(
        "SELECT d.id, d.paper_id FROM documents d "
        "JOIN papers p ON p.id = d.paper_id "
        "WHERE p.run_id = ? ORDER BY d.id", (run_id,)).fetchall()
    for d in rows:
        res = extract_evidence_for_document(
            store, run_id=run_id, paper_id=d["paper_id"],
            document_id=d["id"])
        res["document_id"] = d["id"]
        res["paper_id"] = d["paper_id"]
        if res["gated"]:
            gated.append(d["paper_id"])
        results.append(res)
    return {
        "documents": len(rows),
        "evidence_total": sum(r["count"] for r in results),
        "gated_papers": gated,
        "results": results,
    }
