"""Parse-quality grading + access-level capping (Phase 4, fixes D-1/D-3).

Phase 3 stored a *kind* in ``documents.parse_quality`` ("pdf"/"xml"/
"html") and disclosed scanned/empty pages through warnings, but never
computed a degraded-extraction *grade* nor capped ``access_level``.
This module derives both from the Phase-3 parse signals:

* **grade** -- ``good | partial | poor | scanned`` (D-3);
* **capped access level** (D-1) -- a parsed-but-scanned document is at
  most ``partial_full_text``; a document with no extracted text is never
  represented as having text. Spec section 19 "Missing text": scanned/
  partial papers are flagged, never represented as fully read.

The module is pure (stdlib only) so both ``documents.ingest`` and the
research layers can use it without new dependencies.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "compute_parse_grade",
    "cap_document_access_level",
    "has_extracted_text",
    "grade_disclosure",
    "PARSE_GRADES",
]

PARSE_GRADES = ("good", "partial", "poor", "scanned")

#: Warnings that mean "text that was there could not be extracted" (D-3
#: CID/ligature/multi-column signal) rather than "the page has no text".
_DEGRADED_HINTS = re.compile(
    r"cid|ligature|multi-column|glyph|font|encoding|fallback|truncated",
    re.I,
)
_EMPTY_HINTS = re.compile(r"empty|scanned|no extractable text|no pages", re.I)


def has_extracted_text(report: dict[str, Any]) -> bool:
    """True when the parse produced at least one non-empty text location."""
    for s in report.get("text_index") or []:
        if str(s.get("text") or "").strip():
            return True
    return False


def compute_parse_grade(report: dict[str, Any]) -> str:
    """Derive the degraded-extraction grade from Phase-3 parse signals.

    Inputs (all optional; a missing key is treated as "no signal"):

    * ``scanned`` -- any page is image-only (OCR out of scope);
    * ``pages`` -- ``[{"page", "text", ...}]`` for the coverage ratio;
    * ``warnings`` -- per-page CID/empty/scanned extraction warnings;
    * ``page_count``.

    Rules (most severe wins):

    * any scanned page                 -> ``scanned``
    * no text anywhere (coverage 0)    -> ``poor``
    * warnings + coverage < 100%       -> ``partial``
    * otherwise                        -> ``good``
    """
    warnings = [str(w) for w in (report.get("warnings") or [])]
    if report.get("scanned"):
        return "scanned"

    pages = report.get("pages")
    if pages:
        with_text = [p for p in pages if str(p.get("text") or "").strip()]
        total = len(pages)
        coverage = (len(with_text) / total) if total else 0.0
        empty_majority = len(with_text) < total / 2
    else:
        # XML/HTML reports: a "no paragraphs found" style warning means
        # the structure was not extractable.
        if any(_EMPTY_HINTS.search(w) for w in warnings):
            coverage = 0.0
            empty_majority = True
        else:
            coverage = 1.0
            empty_majority = False

    if coverage == 0.0 or empty_majority:
        # Distinguish "nothing extracted" (poor) from "some extracted,
        # some degraded".
        if not has_extracted_text(report):
            return "poor"
        if any(_EMPTY_HINTS.search(w) for w in warnings):
            return "poor"
    if warnings or coverage < 1.0:
        return "partial"
    return "good"


def cap_document_access_level(
    current: str | None,
    grade: str,
    text_present: bool,
) -> str:
    """Cap a document's access level from its parse grade (fixes D-1).

    ``current`` is the retrieval-level status (e.g. ``full_text``). The
    returned level is the *effective* access level:

    * ``good``      -- no cap (keep ``current``, default ``full_text``);
    * ``scanned``   -- at most ``partial_full_text``; with no extracted
      text at all, at most ``abstract_only``/``metadata_only``;
    * ``partial``   -- at most ``partial_full_text``;
    * ``poor``      -- at most ``abstract_only`` (some text) or
      ``metadata_only`` (no text). Never ``full_text``.

    The cap only ever LOWERS the level; it never raises it.
    """
    rank = ("unavailable", "metadata_only", "abstract_only",
            "partial_full_text", "full_text")

    def idx(level: str) -> int:
        return rank.index(level) if level in rank else rank.index("full_text")

    current = current or "full_text"
    if current in ("unavailable", "metadata_only", "abstract_only"):
        # Already below any full-text claim; keep as-is.
        return current

    cap: str
    if grade == "good":
        cap = "full_text"
    elif grade == "scanned":
        cap = "partial_full_text" if text_present else "abstract_only"
    elif grade == "partial":
        cap = "partial_full_text" if text_present else "abstract_only"
    else:  # poor
        cap = "abstract_only" if text_present else "metadata_only"

    if idx(cap) < idx(current):
        return cap
    return current


def grade_disclosure(grade: str, access_level: str,
                     warnings: list[str] | None = None) -> list[str]:
    """Warning strings that must ride along with screening/extraction.

    Keeps "never represented as fully read" honest: the grade + the
    (capped) access level are surfaced in ``extraction_warnings``.
    """
    out: list[str] = []
    if grade not in ("good",):
        out.append(
            f"parse_quality={grade}; access_level capped to {access_level} "
            "(scanned/empty pages are not fully read; OCR is out of scope)")
    for w in warnings or []:
        out.append(w)
    return out
