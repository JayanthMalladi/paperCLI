"""Evidence location resolution (acceptance test 'Extraction').

Every evidence location must resolve to *exact* stored source text. This
module keeps the ``document_text`` index as the single source of truth and
resolves ``(page, section, paragraph)`` locations -- or free excerpts --
to the stored row they match, so excerpts can be verified character-for-
character against what was parsed and stored.

An excerpt that does not appear verbatim in the index resolves to None;
callers must record that as an extraction warning, never silently accept
an approximation.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from papercli.storage.db import PaperStore

__all__ = [
    "EvidenceIndex",
    "make_location",
    "resolve_location",
    "resolve_excerpt",
    "record_evidence",
]


def make_location(page: int | None = None, section: str | None = None,
                  paragraph: int | None = None) -> dict[str, Any]:
    """Build an evidence location object (spec section 9 shape)."""
    return {"page": page, "section": section, "paragraph": paragraph}


class EvidenceIndex:
    """Read side of the document_text index for one document."""

    def __init__(self, store: PaperStore, document_id: str):
        self._store = store
        self._doc = document_id
        self._rows: list[dict[str, Any]] = store.document_text(document_id)

    def locations(self, page: int | None = None) -> list[dict[str, Any]]:
        return self._rows if page is None else [
            r for r in self._rows if r["page"] == page]

    def resolve(self, location: Mapping[str, Any] | None,
                excerpt: str | None = None) -> dict[str, Any] | None:
        """Resolve a location/excerpt to the exact stored row.

        * With ``location``: the row at (page, section, paragraph) when
          given, else the first row on that page containing the excerpt.
        * With only ``excerpt``: the first row whose stored text contains
          it verbatim (the 'Extraction' guarantee).
        * Both: the excerpt must occur in the located row; otherwise None.
        """
        loc = dict(location or {})
        page = loc.get("page")
        section = loc.get("section")
        paragraph = loc.get("paragraph")

        candidates = self._rows
        if page is not None:
            candidates = [r for r in candidates if r["page"] == page]
        if paragraph is not None:
            matches: list[dict[str, Any]] = []
            for r in candidates:
                if r["paragraph"] != paragraph:
                    continue
                if section is not None and r["section"] != section:
                    continue
                matches.append(r)
            rows = matches
        else:
            rows = candidates

        if excerpt:
            needle = excerpt.strip()
            for r in rows:
                if needle in r["text"]:
                    return r
            # Section-qualified but paragraph unknown: search within section.
            if section is not None and page is None:
                for r in self._rows:
                    if r["section"] == section and needle in r["text"]:
                        return r
            return None
        if rows:
            return rows[0]
        return None

    def resolve_excerpt(self, excerpt: str) -> dict[str, Any] | None:
        return self.resolve(None, excerpt=excerpt)

    def location_for_excerpt(self, excerpt: str) -> dict[str, Any] | None:
        """Infer the (page, section, paragraph) location of an excerpt."""
        row = self.resolve_excerpt(excerpt)
        if row is None:
            return None
        return make_location(row["page"], row["section"], row["paragraph"])


def resolve_location(index: EvidenceIndex, location: Mapping[str, Any] | None,
                     excerpt: str | None = None) -> dict[str, Any] | None:
    """Resolve a location/excerpt to the exact stored row."""
    return index.resolve(location, excerpt)


def record_evidence(
    store: PaperStore,
    paper_id: str,
    *,
    document_id: str | None,
    access_level: str,
    location: Mapping[str, Any] | None,
    supporting_excerpt: str,
    finding: str,
    claim_type: str | None = None,
    methods: Mapping[str, Any] | None = None,
    population_or_system: str | None = None,
    limitations: list[str] | None = None,
) -> dict[str, Any]:
    """Validate an evidence record against the index and persist it.

    The excerpt MUST resolve to exact stored source text (acceptance test
    'Extraction'). When it cannot, the record is still persisted but with
    an ``extraction_warnings`` entry disclosing the resolution failure --
    a source-inconsistent evidence record is never silently accepted.

    Returns the evidence dict (the spec section 9 minimum shape).
    """
    warnings: list[str] = []
    row = None
    if document_id:
        index = EvidenceIndex(store, document_id)
        row = index.resolve(location, excerpt=supporting_excerpt)
        if row is None:
            warnings.append(
                "excerpt did not resolve to exact stored source text; "
                "recorded with a location-resolution warning")

    # Fill location from the resolved row when it resolved.
    loc_out: dict[str, Any]
    if row is not None:
        loc_out = make_location(row["page"], row["section"], row["paragraph"])
    else:
        loc_out = make_location(
            (location or {}).get("page"),
            (location or {}).get("section"),
            (location or {}).get("paragraph"),
        )

    evidence_id = store.record_evidence(
        paper_id,
        document_id=document_id,
        access_level=access_level,
        location=loc_out,
        supporting_excerpt=supporting_excerpt,
        finding=finding,
        claim_type=claim_type,
        methods=dict(methods or {}),
        population_or_system=population_or_system,
        limitations=list(limitations or []),
        extraction_warnings=warnings,
    )
    return {
        "evidence_id": evidence_id,
        "paper_id": paper_id,
        "document_id": document_id,
        "access_level": access_level,
        "location": loc_out,
        "supporting_excerpt": supporting_excerpt,
        "finding": finding,
        "claim_type": claim_type,
        "methods": dict(methods or {}),
        "population_or_system": population_or_system,
        "limitations": list(limitations or []),
        "extraction_warnings": warnings,
    }
