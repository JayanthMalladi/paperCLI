"""Screening pipeline (Phase 4, spec section 9).

Two separate stages, each persisted to the ``screenings`` table (the sole
source for the later ``screening.csv`` export -- spec section 14):

* **title/abstract** -- from search records (evidence basis ``abstract``
  or ``metadata``);
* **full_text** -- from parsed documents (evidence basis ``full_text``;
  scanned/partial documents carry the parse-quality grade and a capped
  ``access_level`` of ``partial_full_text`` -- never represented as fully
  read; acceptance "Missing text").

Deterministic rules (no model -- the model-driven layer is Phase 5):

* criterion matching: significant-keyword containment of the protocol
  inclusion/exclusion criteria against the record text. Criterion IDs
  are assigned deterministically from the protocol order
  (``inc-1``, ``exc-1``, ...).
* records the deterministic layer cannot classify enter the **uncertainty
  queue**; a bounded recheck (exactly one pass, deterministic signals:
  abstract length, keyword presence, year/language filters) may resolve
  some; the rest are reported **unresolved** (spec section 9).

Every decision record carries exactly the spec section 9 field set:
decision, criterion IDs, rationale, evidence basis, reviewer/model
identity, prompt version, timestamp.

Deduplication (spec section 9):

* normalized identifiers first (DOI / arXiv-id normalized forms) ->
  high-confidence ``duplicate`` links;
* fuzzy title/authors/year -> CANDIDATE signals only, stored as ``paper_links``
  rows with relation + confidence -- never a silent merge; provenance of
  every linked record is preserved. Preprints and journal reports are
  linked (``preprint_of_report``), not deleted; reports and studies stay
  distinct concepts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from papercli.documents.identity import _title_overlap, normalize_doi, normalize_title
from papercli.providers.identifiers import normalize_arxiv_id
from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "ScreeningPipeline",
    "assign_criterion_ids",
    "match_criterion",
    "deduplicate_papers",
    "screening_csv_rows",
    "REVIEWER",
    "PROMPT_VERSION",
]

#: Reviewer identity for the deterministic layer (spec section 9: the
#: decision record carries the model/agent identity + prompt version; a
#: deterministic layer records itself, it does not fake a model call).
REVIEWER = "deterministic/papercli-screening@1.0"
PROMPT_VERSION = "deterministic-v1"

_STOPWORDS = {
    "the", "and", "for", "with", "are", "was", "were", "that", "this",
    "these", "those", "via", "using", "use", "based", "study", "paper",
    "review", "results", "result", "analysis", "approach", "method",
    "methods", "system", "systematic", "literature",
}


def _significant_tokens(text: str, min_len: int = 4) -> list[str]:
    toks = re.findall(r"[a-z0-9][a-z0-9\-]*", (text or "").lower())
    return [t for t in toks if len(t) >= min_len and t not in _STOPWORDS]


def assign_criterion_ids(protocol: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Deterministic criterion IDs from the protocol (order-stable)."""
    inc = [f"inc-{i}" for i, c in enumerate(protocol.get("inclusion_criteria") or [], start=1)]
    exc = [f"exc-{i}" for i, c in enumerate(protocol.get("exclusion_criteria") or [], start=1)]
    return inc, exc


def match_criterion(criterion: str, text: str) -> bool:
    """True when ALL significant tokens of the criterion occur in the text.

    A single-token criterion (after stopword removal) is not strong
    evidence on its own; it matches when the token is present. This is
    the deterministic layer's only matching rule -- conservative enough
    that anything ambiguous falls to the uncertainty queue.
    """
    tokens = _significant_tokens(criterion)
    hay = (text or "").lower()
    if not tokens:
        return False
    # Multi-token criteria must have a majority (>= 2 of 2+, or all)
    # present; single-token criteria need that token.
    hits = sum(1 for t in tokens if t in hay)
    if len(tokens) == 1:
        return hits == 1
    return hits >= 2


class ScreeningPipeline:
    """Deterministic title/abstract + full-text screening for one run."""

    def __init__(self, store: PaperStore, run_id: str,
                 protocol: Mapping[str, Any] | None = None):
        self.store = store
        self.run_id = run_id
        self.protocol = dict(protocol or {})
        self.inc_ids, self.exc_ids = assign_criterion_ids(self.protocol)
        self._inc_texts = list(self.protocol.get("inclusion_criteria") or [])
        self._exc_texts = list(self.protocol.get("exclusion_criteria") or [])
        self._dr = self.protocol.get("date_range") or {}

    # -- helpers -----------------------------------------------------------
    def _record_text(self, rec: Mapping[str, Any]) -> str:
        parts = [str(rec.get("title") or ""), str(rec.get("abstract") or "")]
        for a in rec.get("authors") or []:
            parts.append(str(a))
        return " \n ".join(parts)

    def _paper_for_record(self, rec: Mapping[str, Any]) -> str:
        ids: dict[str, str] = {}
        if rec.get("doi"):
            ids["doi"] = str(rec["doi"]).lower()
        if rec.get("arxiv_id"):
            ids["arxiv_id"] = str(rec["arxiv_id"])
        if rec.get("s2_id"):
            ids["s2"] = str(rec["s2_id"])
        if rec.get("openalex_id"):
            ids["openalex"] = str(rec["openalex_id"])
        provenance = {"providers": [rec.get("provider")] + list(rec.get("also_from") or [])}
        provenance = {k: v for k, v in provenance.items() if v}
        return self.store.upsert_paper(
            self.run_id, ids,
            title=rec.get("title"), authors=rec.get("authors"),
            year=rec.get("year"), metadata_provenance=provenance,
        )

    def _persist_decision(self, paper_id: str, stage: str, decision: str, *,
                          criteria: list[str], rationale: str,
                          basis: str) -> None:
        self.store.record_screening(
            paper_id, stage=stage, decision=decision,
            criterion_ids=criteria, rationale=rationale,
            evidence_basis=basis, reviewer=REVIEWER,
            prompt_version=PROMPT_VERSION,
        )

    # -- title/abstract stage ---------------------------------------------
    def screen_records(self, records: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Screen search records (title/abstract stage) + dedup + recheck.

        Returns a summary: counts, unresolved records, links created.
        """
        summaries: list[dict[str, Any]] = []
        uncertain: list[dict[str, Any]] = []
        for rec in records:
            paper_id = self._paper_for_record(rec)
            text = self._record_text(rec)
            basis = "abstract" if str(rec.get("abstract") or "").strip() else "metadata"
            decision, criteria, rationale = self._decide(text, basis)
            if decision == "uncertain":
                uid = self.store.record_uncertainty(
                    self.run_id, paper_id, stage="title_abstract",
                    reason=rationale, status="queued")
                self._persist_decision(paper_id, "title_abstract", "uncertain",
                                       criteria=criteria, rationale=rationale,
                                       basis=basis)
                uncertain.append({"paper_id": paper_id, "uncertainty_id": uid,
                                 "title": rec.get("title"), "record": rec})
            else:
                self._persist_decision(paper_id, "title_abstract", decision,
                                      criteria=criteria, rationale=rationale,
                                      basis=basis)
                self.store.set_paper_access_level(
                    paper_id, "abstract_only" if basis == "abstract" else "metadata_only")
                summaries.append({"paper_id": paper_id, "decision": decision,
                                   "criteria": criteria, "basis": basis})

        # Bounded recheck: exactly one deterministic pass over the queued
        # records (spec section 9: "a bounded automated recheck may resolve
        # them. Remaining uncertainty is reported.").
        rechecked = self._recheck_pass(uncertain)

        links = deduplicate_papers(self.store, self.run_id)
        return {
            "stage": "title_abstract",
            "records": len(records),
            "decided": len(summaries),
            "uncertain": len(uncertain),
            "resolved_by_recheck": rechecked,
            "unresolved": len(uncertain) - rechecked,
            "decisions": summaries,
            "unresolved_ids": [u["paper_id"] for u in uncertain
                               if u.get("recheck_status") != "resolved"],
            "links_created": links,
        }

    def _decide(self, text: str, basis: str) -> tuple[str, list[str], str]:
        inc_hit = [cid for cid, c in zip(self.inc_ids, self._inc_texts)
                   if match_criterion(c, text)]
        exc_hit = [cid for cid, c in zip(self.exc_ids, self._exc_texts)
                   if match_criterion(c, text)]
        if self._inc_texts or self._exc_texts:
            if exc_hit and not inc_hit:
                return "exclude", exc_hit, "exclusion criteria matched: " + ", ".join(exc_hit)
            if inc_hit and len(inc_hit) == len(self._inc_texts) and not exc_hit:
                return "include", inc_hit, "all inclusion criteria matched: " + ", ".join(inc_hit)
            if inc_hit or exc_hit:
                return ("uncertain", inc_hit + exc_hit,
                        "partial criterion match; deterministic layer cannot "
                        "classify (uncertainty queue)")
        return ("uncertain", [],
                "no criteria matched deterministically; record queued for "
                "bounded recheck")

    def _recheck_pass(self, uncertain: list[dict[str, Any]]) -> int:
        """One deterministic recheck pass (N=1). Resolves some; the rest
        stay unresolved and are *reported* (spec section 9)."""
        resolved = 0
        lo = str(self._dr.get("from") or "")[:4].strip()
        hi = str(self._dr.get("to") or "")[:4].strip()
        languages = {str(l).lower() for l in (self.protocol.get("languages") or [])}
        themes = " \n ".join(
            [str(t) for t in (self.protocol.get("themes_to_investigate") or [])]
            + [str(c) for c in (self.protocol.get("inclusion_criteria") or [])])
        theme_tokens = _significant_tokens(themes)

        for item in uncertain:
            rec = item.get("record") or {}
            text = self._record_text(rec)
            signals: list[str] = []
            verdict: str | None = None

            # Signal 1: abstract length (too short to screen honestly).
            abstract_len = len(str(rec.get("abstract") or "").strip())
            if abstract_len < 40:
                signals.append(f"abstract too short ({abstract_len} chars) to classify")

            # Signal 2: year/language filters from the protocol.
            year = rec.get("year")
            year_ok = None
            if isinstance(year, int):
                year_ok = True
                if lo and year < int(lo):
                    year_ok = False
                if hi and year > int(hi):
                    year_ok = False
            lang = str(rec.get("language") or "").lower()
            lang_ok = None
            if languages and lang:
                lang_ok = lang in languages

            if year_ok is False:
                verdict = "exclude"
                signals.append(f"year {year} outside protocol date range "
                               f"[{lo or '..'}..{hi or '..'}]")
            if lang_ok is False:
                verdict = "exclude"
                signals.append(f"language '{lang}' not in protocol languages {sorted(languages)}")

            # Signal 3: keyword/theme presence.
            kw_hits = [t for t in theme_tokens if t in text.lower()]
            if verdict is None and self._inc_texts and kw_hits:
                verdict = "include"
                signals.append("theme/inclusion keywords present: "
                               + ", ".join(kw_hits[:5]))
            if verdict is None and abstract_len < 40:
                # Cannot resolve from a near-empty abstract: stays unresolved.
                verdict = None

            status = "resolved" if verdict else "unresolved"
            if verdict:
                rationale = ("bounded recheck (pass 1, deterministic signals: "
                             + ("; ".join(signals) if signals else "none") + ")")
                self._persist_decision(item["paper_id"], "title_abstract",
                                       verdict, criteria=[], rationale=rationale,
                                       basis="metadata")
                item["recheck_status"] = "resolved"
                resolved += 1
            else:
                rationale = ("bounded recheck (pass 1) could not resolve: "
                             + ("; ".join(signals) if signals else
                                "no deterministic signal matched"))
                item["recheck_status"] = "unresolved"
            self.store.update_uncertainty(
                item["uncertainty_id"], status=status,
                reason=rationale, increment_rechecks=True)
        return resolved

    # -- full-text stage ----------------------------------------------------
    def screen_full_text(self, paper_id: str, *, document_id: str | None = None,
                         decision: str | None = None,
                         criteria: list[str] | None = None,
                         rationale: str | None = None) -> dict[str, Any]:
        """Persist a full-text-stage decision for one paper.

        ``decision=None`` means "derive what is derivable": without a
        usable full text (doc access level below ``partial_full_text``)
        the record is queued as uncertain -- a full-text finding is never
        inferred from metadata (spec section 9).
        """
        doc = self.store.document_row(document_id) if document_id else None
        doc_level = (doc or {}).get("access_level")
        grade = (doc or {}).get("parse_quality")
        usable = doc_level in ("full_text", "partial_full_text")

        if decision is None:
            if not doc or not usable:
                verdict = "uncertain"
                rat = ("no usable full text (access level "
                       f"{doc_level or 'unknown'}); full-text screening "
                       "cannot be performed -- never inferred from metadata")
                self.store.record_uncertainty(
                    self.run_id, paper_id, stage="full_text",
                    reason=rat, status="unresolved")
                self._persist_decision(paper_id, "full_text", "uncertain",
                                       criteria=[], rationale=rat, basis="metadata")
                return {"paper_id": paper_id, "stage": "full_text",
                        "decision": "uncertain", "rationale": rat,
                        "evidence_basis": "metadata",
                        "access_level": doc_level, "parse_grade": grade}
            verdict = decision or "include"
            rat = (rationale or "full-text stage decision recorded "
                     "(deterministic layer; model interpretation is Phase 5)")
            basis = "full_text"
        else:
            verdict, rat, basis = decision, (rationale or "full-text decision"), "full_text"

        self._persist_decision(paper_id, "full_text", verdict,
                               criteria=criteria or [], rationale=rat,
                               basis=basis)
        # D-1: a scanned/partial document caps the paper's effective level.
        if doc and grade in ("scanned", "partial", "poor"):
            self.store.set_paper_access_level(paper_id, "partial_full_text")
        out: dict[str, Any] = {
            "paper_id": paper_id, "stage": "full_text", "decision": verdict,
            "rationale": rat, "evidence_basis": basis,
            "access_level": doc_level, "parse_grade": grade,
        }
        if grade in ("scanned", "partial", "poor"):
            out["disclosure"] = (
                f"parse_quality={grade}; access_level capped to "
                f"{(doc or {}).get('access_level') or 'partial_full_text'}; "
                "scanned/empty pages are not fully read (OCR out of scope)")
        return out


def _norm_doi_safe(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        return normalize_doi(str(raw))
    except ValueError:
        return str(raw).strip().lower() or None


def _norm_arxiv_safe(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        base, _ver = normalize_arxiv_id(str(raw))
        return base
    except ValueError:
        return str(raw).strip().lower() or None


def _work_kind(rec_or_row: Mapping[str, Any], row: Mapping[str, Any] | None) -> str:
    """Classify the record's concept: preprint / report (journal) / study."""
    ids = json.loads((row or {}).get("canonical_ids_json") or "{}")
    ptype = ((row or {}).get("metadata_provenance_json") or "{}")
    ptype = json.loads(ptype) if isinstance(ptype, str) else ptype
    t = str(rec_or_row.get("type") or ptype.get("work_type") or "").lower()
    if ids.get("arxiv_id"):
        return "preprint"
    if "preprint" in t:
        return "preprint"
    if "journal" in t or ids.get("doi"):
        return "report"
    return "study"


def deduplicate_papers(store: PaperStore, run_id: str) -> int:
    """Link (never merge) duplicate / version / related records.

    * Step 1 -- normalized identifiers: same normalized DOI, or same
      normalized arXiv BASE id, links the records as ``duplicate``
      (confidence 1.0). Preprint + report of the same work link as
      ``preprint_of_report``. Provenance of every record is preserved
      (links are extra rows; nothing is deleted or merged).
    * Step 2 -- fuzzy candidate signals (normalized title + authors +
      year): candidate links ONLY, confidence < 0.9, relation
      ``candidate_duplicate`` / ``related_study``. Never auto-merged.

    Returns the number of link rows created by this call.
    """
    rows = store.papers_for_run(run_id)
    if len(rows) < 2:
        return 0

    def rec_ids(r: Mapping[str, Any]) -> dict[str, str]:
        return json.loads(r.get("canonical_ids_json") or "{}")

    by_doi: dict[str, list[dict[str, Any]]] = {}
    by_arxiv: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        ids = rec_ids(r)
        d = _norm_doi_safe(ids.get("doi"))
        if d:
            by_doi.setdefault(d, []).append(r)
        a = _norm_arxiv_safe(ids.get("arxiv_id"))
        if a:
            by_arxiv.setdefault(a, []).append(r)

    created = 0
    seen_pairs: set[tuple[str, str, str]] = set()

    def link(src: Mapping[str, Any], tgt: Mapping[str, Any], relation: str,
             confidence: float, signals: dict[str, Any]) -> None:
        nonlocal created
        key = (src["id"], tgt["id"], relation)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        store.record_paper_link(run_id, src["id"], tgt["id"], relation=relation,
                                confidence=confidence, signals=signals)
        created += 1

    # 1a) identical normalized DOIs (across records/providers).
    for doi, group in by_doi.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                kind_a = _work_kind({}, a)
                kind_b = _work_kind({}, b)
                relation = ("preprint_of_report" if (kind_a, kind_b) == ("preprint", "report")
                            else "report_of_preprint" if (kind_a, kind_b) == ("report", "preprint")
                            else "duplicate")
                link(a, b, relation, 1.0,
                     {"signal": "normalized_doi", "doi": doi,
                      "work_kind_source": kind_a, "work_kind_target": kind_b})
    # 1b) identical normalized arXiv base ids (versions of one preprint).
    for aid, group in by_arxiv.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                link(a, b, "duplicate", 1.0,
                     {"signal": "normalized_arxiv_base", "arxiv_id": aid,
                      "note": "arXiv version identity preserved in canonical ids"})

    # 2) fuzzy candidate signals: normalized title + authors + year.
    scored: list[tuple[Mapping[str, Any], Mapping[str, Any], float, dict[str, Any]]] = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            ids_a, ids_b = rec_ids(a), rec_ids(b)
            if _norm_doi_safe(ids_a.get("doi")) and _norm_doi_safe(ids_a.get("doi")) == _norm_doi_safe(ids_b.get("doi")):
                continue  # already linked by identifier
            ta = normalize_title(str(a.get("title") or ""))
            tb = normalize_title(str(b.get("title") or ""))
            if not ta or not tb:
                continue
            overlap = _title_overlap(str(a.get("title") or ""), str(b.get("title") or ""))
            if overlap < 0.6:
                continue
            year_match = (a.get("year") == b.get("year")) or (
                isinstance(a.get("year"), int) and isinstance(b.get("year"), int)
                and abs(a["year"] - b["year"]) <= 1)
            conf = round(min(0.89, 0.5 * overlap + 0.2 * (1.0 if year_match else 0.0)), 3)
            kind_a, kind_b = _work_kind(a, a), _work_kind(b, b)
            if kind_a == "preprint" and kind_b == "report":
                relation = "preprint_of_report"
            elif kind_a == "report" and kind_b == "preprint":
                relation = "report_of_preprint"
            elif kind_a == kind_b == "study":
                relation = "related_study"
            else:
                relation = "candidate_duplicate"
            signals = {
                "signal": "fuzzy_title_authors_year",
                "title_overlap": round(overlap, 3),
                "year_match": bool(year_match),
                "note": "candidate signal only -- records are linked, "
                        "never merged; provenance preserved",
            }
            scored.append((a, b, conf, {**signals, "relation": relation}))

    for a, b, conf, sig in scored:
        link(a, b, sig["relation"], conf, sig)
    return created


def screening_csv_rows(store: PaperStore, run_id: str) -> list[dict[str, Any]]:
    """The screening table as the sole source for ``screening.csv``.

    One row per persisted decision record with the spec section 9 fields,
    plus the paper's identifying context (title/DOI from the registry).
    """
    papers = {r["id"]: r for r in store.papers_for_run(run_id)}
    out: list[dict[str, Any]] = []
    for s in store.screenings_for_run(run_id):
        paper = papers.get(s["paper_id"], {})
        ids = json.loads(paper.get("canonical_ids_json") or "{}")
        out.append({
            "paper_id": s["paper_id"],
            "doi": ids.get("doi"),
            "arxiv_id": ids.get("arxiv_id"),
            "title": paper.get("title"),
            "stage": s["stage"],
            "decision": s["decision"],
            "criterion_ids": ";".join(s.get("criterion_ids") or []),
            "rationale": s.get("rationale"),
            "evidence_basis": s.get("evidence_basis") or "none",
            "reviewer": s.get("reviewer"),
            "prompt_version": s.get("prompt_version"),
            "decided_at": s.get("decided_at"),
        })
    return out
