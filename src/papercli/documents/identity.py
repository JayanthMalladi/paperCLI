"""Wrong-paper guard (acceptance test 'Identity').

A parsed document whose embedded title/DOI does not match the requested
paper's identity must be rejected or flagged -- never silently accepted.
PDFs carry embedded ``/Title`` and DOI-shaped strings; repository XML
carries ``<article-id pub-type="doi">`` and ``<title>``. When the run
context supplies the requested paper id, ingestion runs this check and
rejects on a definite mismatch; when identity evidence is absent or
ambiguous the document is ingested but *flagged* (disclosed, not hidden).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

__all__ = ["check_identity", "normalize_title", "normalize_doi"]

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9a-z]")
_DOI_SUFFIX_DOTS = re.compile(r"\.+$")


def normalize_title(title: str) -> str:
    """Lowercase, collapse whitespace, and drop non-alphanumerics.

    Title matching compares on this normalization so font/ligature/case
    differences do not cause false mismatches.
    """
    t = unicodedata.normalize("NFKD", title or "").lower()
    t = _WS.sub(" ", t)
    t = _NON_ALNUM.sub("", t)
    return t


def normalize_doi(doi: str) -> str:
    """DOI normalization: lowercase, trim trailing punctuation.

    Valid suffix punctuation is preserved (spec section 8: do not strip
    valid suffix characters).
    """
    d = (doi or "").strip().lower()
    d = d.replace("https://doi.org/", "").replace("http://doi.org/", "")
    return _DOI_SUFFIX_DOTS.sub("", d.strip())


def _title_words(title: str) -> set[str]:
    """Lowercased word set of a title (punctuation stripped per word)."""
    words = []
    for w in (title or "").lower().split():
        w = _NON_ALNUM.sub("", w)
        if w:
            words.append(w)
    return set(words)


def _title_overlap(a: str, b: str) -> float:
    """Word overlap fraction (min side): how much of the shorter title's
    words appear in the longer title."""
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return 0.0
    smaller, bigger = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(smaller & bigger) / len(smaller)


def check_identity(parsed: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    """Compare a document's embedded identity against the requested paper.

    ``parsed`` carries ``title`` and/or ``doi`` (from PDF /Title + DOI
    scan, or repository XML). ``requested`` carries the run context:
    ``doi``, ``arxiv_id``, ``title`` (any subset; an empty dict means no
    context and the check is skipped).

    Returns::

        {"status": "matched" | "unmatched" | "inconclusive" | "skipped",
         "details": {...}}

    * ``unmatched``  -- a definite identity field (DOI) conflicts, or the
      embedded title is clearly a different paper. Callers reject.
    * ``inconclusive`` -- evidence present but not decisive (e.g. only a
      partial title, no DOI). Callers flag, they do not reject.
    * ``matched`` -- available fields agree with the requested identity.
    """
    if not requested:
        return {"status": "skipped", "details": {}}

    p_title = (parsed.get("title") or "").strip()
    p_doi = parsed.get("doi")
    r_doi = requested.get("doi")
    r_title = (requested.get("title") or "").strip()

    details: dict[str, Any] = {
        "embedded_title": p_title or None,
        "embedded_doi": p_doi or None,
        "requested_doi": normalize_doi(r_doi) if r_doi else None,
        "requested_title": r_title or None,
    }

    doi_verdict: str | None = None
    if p_doi and r_doi:
        pn, rn = normalize_doi(str(p_doi)), normalize_doi(r_doi)
        if pn == rn:
            doi_verdict = "match"
        else:
            doi_verdict = "mismatch"
    elif p_doi and not r_doi:
        # Embedded DOI present, requester gave no DOI to check against:
        # the embedded DOI is positive evidence of identity, not a conflict.
        doi_verdict = None

    title_verdict: str | None = None
    if p_title and r_title:
        ov = _title_overlap(p_title, r_title)
        details["title_overlap"] = round(ov, 3)
        if ov >= 0.85:
            title_verdict = "match"
        elif ov < 0.4:
            title_verdict = "mismatch"
        else:
            title_verdict = None

    if doi_verdict == "mismatch":
        details["reason"] = f"embedded DOI {p_doi} != requested DOI {r_doi}"
        return {"status": "unmatched", "details": details}
    if doi_verdict == "match" and title_verdict == "match":
        details["reason"] = "DOI and title both agree"
        return {"status": "matched", "details": details}
    if doi_verdict == "match" and title_verdict is None:
        details["reason"] = "DOI agrees; title evidence insufficient to confirm"
        return {"status": "matched", "details": details}
    if title_verdict == "mismatch":
        details["reason"] = ("embedded title does not match the requested "
                             f"paper (overlap {details.get('title_overlap')})")
        return {"status": "unmatched", "details": details}
    if doi_verdict == "match" or title_verdict == "match":
        details["reason"] = "available fields agree with the requested identity"
        return {"status": "matched", "details": details}
    details["reason"] = "no decisive identity evidence to compare"
    return {"status": "inconclusive", "details": details}
