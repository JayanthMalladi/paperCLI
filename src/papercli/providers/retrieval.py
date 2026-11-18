"""Retrieval orchestrator (spec section 8) + CLI search/resolve services.

Implements the retrieval order exactly as specified:

  1. validated local document cache / user-imported document;
  2. Unpaywall locations for DOI inputs;
  3. applicable repository / publisher accessible full text;
  4. record full text UNAVAILABLE and continue.

A link is not a retrieved report: every candidate download is byte-validated
(PDF magic / XML / HTML sniff, size floor, MIME-inconsistency handling,
bounded redirects, relative URL resolution), raw bytes + hash + source URL +
access status are persisted (Document entity, spec section 11), and permanent
401/403 are never retried.

All network access goes through the guarded :class:`HttpTransport` pool
built by :func:`make_transports` (TLS on, URL guard, per-provider pacing,
bounded retry with jitter honoring Retry-After).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from papercli.providers.arxiv import ArxivAdapter
from papercli.providers.cache import RetrievalCache, detect_document_kind, sha256_of
from papercli.providers.crossref import CrossrefAdapter
from papercli.providers.http_layer import (
    HttpGuard,
    HttpTransport,
    Pacer,
    RetryPolicy,
)
from papercli.providers.openalex import OpenAlexAdapter
from papercli.providers.semantic_scholar import SemanticScholarAdapter
from papercli.providers.unpaywall import UnpaywallAdapter
from papercli.providers.identifiers import (
    arxiv_pdf_url,
    normalize_arxiv_id,
    normalize_doi,
    parse_identifier,
)
from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "AccessLevel",
    "ProviderProvenance",
    "SearchService",
    "resolve_identifier",
    "fetch_document",
    "make_guard",
    "make_metadata_transport",
    "make_document_transport",
]

#: Pacing per target host (seconds between requests), from verified docs.
_HOST_PACING: dict[str, float] = {
    "export.arxiv.org": 3.0,     # TOU: 1 req / 3 s
    "arxiv.org": 3.0,
    "api.crossref.org": 0.35,    # polite pool ~3 req/s observed
    "api.openalex.org": 0.11,    # keyless 10 req/s; key 30 req/s
    "api.unpaywall.org": 0.1,
    "api.semanticscholar.org": 0.12,
}


def _utc_now() -> str:
    return utc_now_iso()


def make_guard(config: Any, env: dict | None = None) -> HttpGuard:
    """Build the URL guard; local-dev opt-out is explicit config only."""
    allow_local = bool(config.get("network.allow_local", False)) if config else False
    return HttpGuard(allow_local=allow_local)


def _timeout_and_ceiling(config: Any) -> tuple[float, int]:
    timeout = float(config.get("execution.request_timeout_seconds", 30) or 30)
    max_mb = float(config.get("execution.max_response_mb", 50) or 50)
    return timeout, int(max_mb * 1024 * 1024)


def make_metadata_transport(config: Any, *, pacer_interval: float = 0.1) -> HttpTransport:
    """Guarded metadata transport (TLS on, bounded retry, gentle pacing).

    The transport owns its client (default construction) so ``close()``
    releases the pool.
    """
    guard = make_guard(config)
    timeout, ceiling = _timeout_and_ceiling(config)
    retry = RetryPolicy(
        max_attempts=3,
        base_delay=0.4,
        max_delay=6.0,
        retry_after_cap=10.0,
    )
    import httpx
    return HttpTransport(
        guard=guard,
        timeout=timeout,
        max_bytes=ceiling,
        retry=retry,
        pacer=Pacer(pacer_interval),
        client_limits=httpx.Limits(max_connections=4),
    )


def make_document_transport(config: Any, target_url: str) -> HttpTransport:
    """A content-download transport paced for the target host."""
    from urllib.parse import urlparse

    host = (urlparse(target_url).hostname or "").lower()
    interval = 0.2
    for prefix, secs in _HOST_PACING.items():
        if host == prefix or host.endswith("." + prefix):
            interval = secs
            break
    guard = make_guard(config)
    timeout, ceiling = _timeout_and_ceiling(config)
    retry = RetryPolicy(
        max_attempts=2,
        base_delay=0.5,
        max_delay=5.0,
        retry_after_cap=8.0,
    )
    import httpx
    # arXiv: single connection (TOU) for metadata AND pdf hosts.
    limits = httpx.Limits(max_connections=1) if "arxiv" in host else httpx.Limits(max_connections=2)
    return HttpTransport(
        guard=guard,
        timeout=timeout,
        max_bytes=ceiling,
        retry=retry,
        pacer=Pacer(interval),
        client_limits=limits,
    )


# ---------------------------------------------------------------------------
# Access levels (spec section 11: Document access_status)
# ---------------------------------------------------------------------------

class AccessLevel:
    FULL_TEXT = "full_text"        # validated usable full text on disk
    HTML_LANDING = "html_landing"   # accessible HTML, not confirmed full text
    UNAVAILABLE = "unavailable"    # recorded unavailable (retrieval order step 4)

    ALL = (FULL_TEXT, HTML_LANDING, UNAVAILABLE)


# ---------------------------------------------------------------------------
# Search service
# ---------------------------------------------------------------------------

class SearchService:
    """Run a provider search set and persist Search/Paper rows."""

    def __init__(self, config: Any, env: dict | None, *, cache: RetrievalCache | None = None,
                 store: PaperStore | None = None):
        self.config = config
        self.env = env if env is not None else {}
        self.cache = cache
        self.store = store

    # -- provider dispatch ----------------------------------------------------
    def search(self, query: str, providers: list[str], limit: int = 50) -> dict[str, Any]:
        """One JSON envelope body: records + per-provider provenance + dedup."""
        requested = [p.strip().lower() for p in providers if p.strip()]
        known = {"crossref", "openalex", "arxiv", "s2", "semantic_scholar"}
        unknown = [p for p in requested if p not in known]
        if unknown:
            raise ValueError(f"unknown provider(s): {', '.join(sorted(unknown))}")

        provs: list[str] = []
        for p in requested:
            if p == "semantic_scholar":
                p = "s2"
            provs.append(p)

        results: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        errors: list[str] = []

        # Lazy adapters (only for providers actually requested).
        cf = oa = ax = s2 = None
        if "crossref" in provs:
            cf = CrossrefAdapter()
        if "openalex" in provs:
            oa = OpenAlexAdapter()
        if "arxiv" in provs:
            ax = ArxivAdapter()
        if "s2" in provs:
            s2 = SemanticScholarAdapter()

        for name in provs:
            prov = {
                "provider": name,
                "query": query,
                "executed_at": _utc_now(),
                "cursor": None,
                "result_count": 0,
                "completeness": "incomplete",
                "error": None,
                "skipped": False,
                "records_included": 0,
            }
            try:
                if name == "crossref":
                    out = self._search_crossref(cf, query, limit, prov)
                elif name == "openalex":
                    out = self._search_openalex(oa, query, limit, prov)
                elif name == "arxiv":
                    out = self._search_arxiv(ax, query, limit, prov)
                else:
                    out = self._search_s2(s2, query, limit, prov)
            except Exception as e:  # noqa: BLE001 -- per-provider isolation
                prov["error"] = f"{type(e).__name__}: {e}"
                prov["completeness"] = "incomplete"
                errors.append(f"{name}: {prov['error']}")
                provenance.append(prov)
                continue
            if out is not None:
                results.extend(out)
            provenance.append(prov)

        records, dedup_from = self._dedup(results)
        overall = self._overall_completeness(provenance)
        return {
            "query": query,
            "providers": provenance,
            "records": records,
            "record_count": len(records),
            "deduplicated_from": dedup_from,
            "completeness": overall,
            "errors": errors,
            "limit": int(limit),
        }

    @staticmethod
    def _overall_completeness(provenance: list[dict]) -> str:
        """Overall search verdict. A provider failure can never yield
        'complete' (spec rule). Semantics:

        * 'complete'  -- no provider hard-errored or was skipped, and every
                         provider returned a complete result page.
        * 'partial'   -- at least one provider succeeded but coverage is not
                         total (some partial pages, or a graceful S2 skip,
                         or a mix of hard errors and successes).
        * 'incomplete'-- every requested provider hard-errored (no coverage).
        """
        if not provenance:
            return "incomplete"
        hard = [p for p in provenance if p.get("error")]
        skipped = [p for p in provenance if p.get("skipped")]
        if hard and len(hard) == len(provenance):
            return "incomplete"
        if hard or skipped:
            return "partial"
        all_complete = all(
            p.get("completeness") == "complete" for p in provenance
        )
        return "complete" if all_complete else "partial"

    # -- per-provider ---------------------------------------------------------
    def _search_crossref(self, adapter, query: str, limit: int, prov: dict) -> list[dict]:
        import httpx
        tr = make_metadata_transport(self.config, pacer_interval=0.35)
        try:
            out = adapter.search(query, self.config, self.env, transport=tr,
                                 cache=self.cache, limit=limit,
                                 save_cursor=lambda c: prov.update(cursor=c))
        finally:
            tr.close()
        prov["result_count"] = out["item_count"]
        prov["total_results"] = out["total_results"]
        prov["cursor"] = out["next_cursor"]
        prov["completeness"] = (
            "complete" if (out["total_results"] is not None
                           and out["total_results"] <= limit) else "partial"
        )
        records = [_record_crossref(i) for i in out["items"]]
        prov["records_included"] = len(records)
        return records

    def _search_openalex(self, adapter, query: str, limit: int, prov: dict) -> list[dict]:
        per_page = max(1, min(int(limit), 100))
        tr = make_metadata_transport(self.config,
                                     pacer_interval=0.035 if self._openalex_key() else 0.11)
        try:
            out = adapter.search_works(query, self.config, self.env, transport=tr,
                                       cache=self.cache, per_page=per_page,
                                       save_cursor=lambda c: prov.update(cursor=c))
        finally:
            tr.close()
        if out.get("error"):
            # Provider failure must NEVER be a complete search.
            prov["error"] = out["error"]
            prov["completeness"] = "incomplete"
            return []
        prov["result_count"] = out["item_count"]
        prov["total_results"] = out["total_results"]
        prov["cursor"] = out["next_cursor"]
        prov["completeness"] = out["completeness"]
        records = [_record_openalex(w) for w in out["items"]]
        prov["records_included"] = len(records)
        return records

    def _search_arxiv(self, adapter, query: str, limit: int, prov: dict) -> list[dict]:
        out = adapter.search(query, self.config, self.env, transport=None,
                             max_results=max(1, min(int(limit), 200)))
        prov["result_count"] = len(out["entries"])
        prov["total_results"] = out["total_results"]
        prov["completeness"] = (
            "complete" if (out["total_results"] is not None
                           and out["total_results"] <= max(1, min(int(limit), 200)))
            else "partial"
        )
        records = [_record_arxiv(e) for e in out["entries"]]
        prov["records_included"] = len(records)
        return records

    def _search_s2(self, adapter, query: str, limit: int, prov: dict) -> list[dict]:
        tr = make_metadata_transport(self.config, pacer_interval=0.12)
        try:
            out = adapter.search(query, self.config, self.env, transport=tr,
                                 cache=self.cache, limit=limit)
        finally:
            tr.close()
        if out.get("skipped"):
            # Graceful degrade: 429/403/miss is a recorded skip, NOT a
            # hard failure. ``error`` stays None so the overall-completeness
            # logic does not treat it as a provider error.
            prov["skipped"] = True
            prov["completeness"] = "skipped"
            prov["skip_reason"] = out.get("reason")
            return []
        data = out.get("data") or {}
        items = data.get("data", [])
        prov["result_count"] = len(items)
        prov["completeness"] = "partial"  # one page of S2 results
        records = [_record_s2(p) for p in items]
        prov["records_included"] = len(records)
        return records

    def _openalex_key(self) -> str | None:
        ref = self.config.get("providers.openalex.api_key_env", "OPENALEX_API_KEY")
        import os
        return (self.env.get(ref) or os.environ.get(ref) or "").strip() or None

    # -- dedup ------------------------------------------------------------------
    def _dedup(self, results: list[dict]) -> tuple[list[dict], int]:
        """Dedupe by DOI (case-insensitive), then by normalized title."""
        by_key: dict[str, dict] = {}
        order: list[str] = []
        for rec in results:
            doi = (rec.get("doi") or "").lower()
            title = " ".join((rec.get("title") or "").split()).lower()
            key = f"doi:{doi}" if doi else f"title:{title}"
            if key not in by_key:
                by_key[key] = rec
                order.append(key)
            else:
                existing = by_key[key]
                existing.setdefault("also_from", []).append(rec.get("provider"))
        deduped = [by_key[k] for k in order]
        return deduped, len(results)

    # -- persistence ------------------------------------------------------------
    def persist(self, result: dict[str, Any], run_id: str) -> None:
        """Persist Search + Paper rows for the search result."""
        if self.store is None:
            return
        for prov in result["providers"]:
            completeness = prov.get("completeness", "incomplete")
            error = prov.get("error")
            # A provider failure must never be stored as a complete search.
            if error and completeness in ("complete", "full"):
                completeness = "incomplete"
            self.store.record_search(
                run_id, prov["provider"], prov["query"],
                filters={"limit": result.get("limit")},
                cursor=prov.get("cursor"),
                result_count=prov.get("result_count"),
                completeness=completeness,
                error=error,
            )
        for rec in result["records"]:
            ids: dict[str, str] = {}
            if rec.get("doi"):
                ids["doi"] = rec["doi"].lower()
            if rec.get("arxiv_id"):
                ids["arxiv_id"] = rec["arxiv_id"]
            if rec.get("s2_id"):
                ids["s2"] = rec["s2_id"]
            self.store.upsert_paper(
                run_id, ids,
                title=rec.get("title"),
                authors=rec.get("authors"),
                year=rec.get("year"),
                metadata_provenance={"providers": rec.get("also_from", [rec.get("provider")])},
            )


def _record_crossref(item: dict) -> dict[str, Any]:
    doi = (item.get("DOI") or "").lower()
    titles = item.get("title") or []
    authors = [a.get("family", "") for a in (item.get("author") or []) if a.get("family")]
    year = (item.get("issued", {}).get("date-parts") or [[None]])[0][0]
    url = (item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
    return {
        "provider": "crossref",
        "doi": doi or None,
        "title": titles[0] if titles else None,
        "authors": authors or None,
        "year": int(year) if isinstance(year, (int, str)) and str(year).isdigit() else None,
        "url": url or None,
        "container_title": (item.get("container-title") or [None])[0],
        "issn": (item.get("ISSN") or [None])[0],
    }


def _record_openalex(w: dict) -> dict[str, Any]:
    authors = [a.get("author", {}).get("display_name")
               for a in (w.get("authorships") or [])
               if a.get("author", {}).get("display_name")]
    date = (w.get("publication_date") or "").strip()[:4]
    oa = w.get("open_access") or {}
    primary = w.get("primary_location") or {}
    return {
        "provider": "openalex",
        "openalex_id": w.get("id"),
        "doi": ((w.get("doi") or "").replace("https://doi.org/", "").lower() or None),
        "title": w.get("display_name") or w.get("title"),
        "authors": authors or None,
        "year": int(date) if date.isdigit() else None,
        "url": oa.get("oa_url") or primary.get("landing_page_url")
        or ((w.get("doi") or "").replace("https://doi.org/", "") and
             f"https://doi.org/{(w.get('doi') or '').replace('https://doi.org/', '').lower()}")
        or None,
        "open_access": {"is_oa": oa.get("is_oa"), "oa_status": oa.get("oa_status"),
                        "oa_url": oa.get("oa_url")},
        "content_urls": [c.get("url") for c in (w.get("content_urls") or [])
                         if isinstance(c, dict) and c.get("url")],
        "cited_by_count": w.get("cited_by_count"),
        "type": w.get("type"),
    }


def _record_arxiv(entry: dict) -> dict[str, Any]:
    base = entry.get("arxiv_id") or ""
    m = __import__("re").search(r"^(.*)v(\d+)$", base)
    return {
        "provider": "arxiv",
        "arxiv_id": m.group(1) if m else base,
        "version": m.group(0)[len(m.group(1)):] if m else entry.get("version"),
        "doi": entry.get("doi") or None,
        "title": entry.get("title") or None,
        "authors": entry.get("authors") or None,
        "year": (entry.get("published") or "")[:4] or None,
        "url": entry.get("abs_url"),
        "pdf_url": entry.get("pdf_url"),
        "abstract": entry.get("abstract"),
    }


def _record_s2(p: dict) -> dict[str, Any]:
    ext = p.get("externalIds") or {}
    year = None
    ystr = str(p.get("year") or p.get("publicationYear") or "")
    if ystr.isdigit():
        year = int(ystr)
    return {
        "provider": "s2",
        "s2_id": p.get("paperId"),
        "doi": ext.get("DOI"),
        "arxiv_id": ext.get("ArXiv"),
        "title": p.get("title"),
        "authors": [a.get("name") for a in (p.get("authors") or []) if a.get("name")],
        "year": year,
        "url": p.get("openAccessPdf") or (
            f"https://doi.org/{ext['DOI']}" if ext.get("DOI") else None
        ),
        "citation_count": p.get("citationCount"),
        "venue": p.get("venue"),
    }


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

def resolve_identifier(raw: str, config: Any, env: dict | None, *,
                       cache: RetrievalCache | None = None,
                       store: PaperStore | None = None,
                       run_id: str | None = None,
                       enrich: bool = True) -> dict[str, Any]:
    """Normalized metadata + OA locations for a DOI or arXiv identifier.

    Unpaywall is the OA source for DOIs (email required; a missing email
    records a skip, not an error). Crossref gives DOI metadata; arXiv gives
    preprint metadata; Semantic Scholar enrichment degrades gracefully.
    """
    ident = parse_identifier(raw)
    out: dict[str, Any] = {
        "input": raw,
        "identifier": {
            "kind": ident["kind"],
            "normalized": ident.get("doi") or ident.get("arxiv_id"),
            "version": ident.get("version"),
        },
        "unpaywall": None,
        "crossref": None,
        "openalex": None,
        "arxiv": None,
        "s2": None,
        "canonical": {},
        "warnings": [],
        "fetched_at": _utc_now(),
    }

    doi = ident.get("doi")
    arxiv = ident.get("arxiv_id")
    version = ident.get("version")

    # Unpaywall (DOIs only)
    if doi:
        ua = UnpaywallAdapter()
        email = (env.get("PAPERS_EMAIL") or "").strip() if env else ""
        import os
        if not email:
            ref = config.get("providers.unpaywall.email_env", "PAPERS_EMAIL")
            email = (env or {}).get(ref, "") or os.environ.get(ref, "")
        if email:
            try:
                tr = make_metadata_transport(config, pacer_interval=0.1)
                try:
                    out["unpaywall"] = ua.lookup(doi, config, env, transport=tr, cache=cache)
                finally:
                    tr.close()
            except Exception as e:  # noqa: BLE001
                out["unpaywall"] = {"error": f"{type(e).__name__}: {e}",
                                    "provider": "unpaywall"}
                out["warnings"].append(f"unpaywall: {e}")
        else:
            out["unpaywall"] = {"skipped": True,
                                "reason": "PAPERS_EMAIL not configured; "
                                          "Unpaywall requires the ?email= parameter"}

    # Crossref DOI metadata
    if doi:
        cf = CrossrefAdapter()
        try:
            tr = make_metadata_transport(config, pacer_interval=0.35)
            try:
                r = cf.lookup_doi(doi, config, env, transport=tr, cache=cache)
            finally:
                tr.close()
            out["crossref"] = r
        except Exception as e:  # noqa: BLE001
            out["crossref"] = {"error": f"{type(e).__name__}: {e}"}
            out["warnings"].append(f"crossref: {e}")

    # OpenAlex lookup (DOI or work id)
    if doi:
        oa = OpenAlexAdapter()
        try:
            tr = make_metadata_transport(config)
            try:
                r = oa.lookup_work(doi, config, env, transport=tr, cache=cache)
            finally:
                tr.close()
            out["openalex"] = r
        except Exception as e:  # noqa: BLE001
            out["openalex"] = {"error": f"{type(e).__name__}: {e}"}
            out["warnings"].append(f"openalex: {e}")

    # arXiv metadata
    if arxiv:
        ax = ArxivAdapter()
        try:
            out["arxiv"] = ax.lookup_id_list(arxiv, config, env, version=version,
                                             cache=cache)
        except Exception as e:  # noqa: BLE001
            out["arxiv"] = {"error": f"{type(e).__name__}: {e}"}
            out["warnings"].append(f"arxiv: {e}")

    # Canonical metadata: prefer unpaywall/crossref/openalex in that order.
    def _pick(*providers: str) -> tuple[str | None, dict]:
        for p in providers:
            node = out.get(p)
            if isinstance(node, dict) and not node.get("error"):
                if p == "crossref" and node.get("record"):
                    return p, node["record"]
                if p == "openalex" and node.get("work"):
                    return p, node["work"]
            if isinstance(node, dict) and (p in ("unpaywall",) and node.get("found")):
                return p, node
        return None, {}

    canon = {"doi": doi, "arxiv_id": arxiv, "version": version}
    if doi:
        src, node = _pick("crossref", "unpaywall", "openalex")
        if src == "crossref":
            canon["title"] = (node.get("title") or [None])[0]
            canon["authors"] = [a.get("family") for a in node.get("author", []) if a.get("family")]
            year = (node.get("issued", {}).get("date-parts") or [[None]])[0][0]
            canon["year"] = int(year) if str(year).isdigit() else None
            canon["container"] = (node.get("container-title") or [None])[0]
        elif src == "unpaywall":
            canon["title"] = (node.get("metadata") or {}).get("title")
            canon["year"] = (node.get("metadata") or {}).get("year")
            canon["oa_status"] = node.get("oa_status")
        elif src == "openalex":
            w = node
            canon["title"] = w.get("display_name")
            canon["authors"] = [a.get("author", {}).get("display_name")
                                for a in w.get("authorships", [])]
            d = (w.get("publication_date") or "").strip()[:4]
            canon["year"] = int(d) if d.isdigit() else None
    if arxiv and out.get("arxiv") and not out["arxiv"].get("error") and out["arxiv"].get("entries"):
        e0 = out["arxiv"]["entries"][0]
        canon.setdefault("title", e0.get("title"))
        canon.setdefault("authors", e0.get("authors"))
        canon["arxiv_doi"] = e0.get("doi")
        if e0.get("doi") and not doi:
            canon["doi"] = e0.get("doi")

    out["canonical"] = canon
    out["found"] = bool(out["canonical"].get("title")) or bool(
        (out.get("unpaywall") or {}).get("found")
    )

    if store is not None and run_id:
        ids: dict[str, str] = {}
        if canon.get("doi"):
            ids["doi"] = canon["doi"].lower()
        if canon.get("arxiv_id"):
            ids["arxiv_id"] = canon["arxiv_id"]
        if ids:
            store.upsert_paper(
                run_id, ids, title=canon.get("title"), authors=canon.get("authors"),
                year=canon.get("year"),
                metadata_provenance={"input": raw,
                                     "sources": [k for k in ("unpaywall", "crossref",
                                                              "openalex", "arxiv")
                                                 if out.get(k)]},
            )
    return out


# ---------------------------------------------------------------------------
# fetch (retrieval order)
# ---------------------------------------------------------------------------

_MAX_CANDIDATES = 6  # bounded traversal


def fetch_document(raw: str, config: Any, env: dict | None, *,
                   out_dir: Path | str,
                   cache: RetrievalCache | None = None,
                   store: PaperStore | None = None,
                   run_id: str | None = None,
                   paper_id: str | None = None) -> dict[str, Any]:
    """Run the spec retrieval order for one identifier and persist the result.

    Returns the fetch report; ``access_level`` is one of
    ``full_text | html_landing | unavailable``. Unavailable is a *recorded*
    outcome (retrieval order step 4), not an exception.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ident = parse_identifier(raw)
    doi = ident.get("doi")
    arxiv = ident.get("arxiv_id")
    version = ident.get("version")
    report: dict[str, Any] = {
        "input": raw,
        "identifier": {"kind": ident["kind"], "doi": doi, "arxiv_id": arxiv,
                       "version": version},
        "access_level": AccessLevel.UNAVAILABLE,
        "document": None,
        "retrieval_order": [],
        "warnings": [],
        "fetched_at": _utc_now(),
    }
    if ident["kind"] == "unknown":
        report["warnings"].append(f"unrecognized identifier: {raw!r}")
        report["retrieval_order"].append({"step": 4, "status": "unavailable",
                                          "note": "identifier not a DOI or arXiv id"})
        _persist_fetch(store, run_id, paper_id, report, cache, out_dir)
        return report

    # ---- candidate URLs (steps 2 + 3) ------------------------------------
    candidates: list[tuple[str, str]] = []  # (url, origin)
    if doi:
        candidates.extend(_unpaywall_candidates(doi, config, env, cache, report))
    if arxiv:
        candidates.append((arxiv_pdf_url(arxiv, version), "arxiv-metadata"))
    if doi:
        candidates.extend(_publisher_candidates(doi, config, env, cache, report))

    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for url, origin in candidates:
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        ordered.append((url, origin))
    ordered = ordered[:_MAX_CANDIDATES]
    report["candidates"] = [{"url": u, "origin": o} for u, o in ordered]

    for step_no, (url, origin) in enumerate(ordered, start=2):
        step = {"step": step_no, "url": url, "origin": origin, "status": "tried"}

        # 1) validated local document cache
        cached = cache.cached_document(url) if cache is not None else None
        if cached is not None:
            kind = cached.get("kind") or "unknown"
            if kind == "unknown":
                step["status"] = "cache-invalid"
                report["retrieval_order"].append(step)
                continue
            report["document"] = {
                "path": cached["path"],
                "sha256": cached["sha256"],
                "size": cached["size"],
                "kind": kind,
                "source_url": url,
                "origin": "local-cache",
                "from_cache": True,
            }
            report["access_level"] = AccessLevel.FULL_TEXT if kind == "pdf" else (
                AccessLevel.HTML_LANDING if kind == "html" else AccessLevel.FULL_TEXT
            )
            step["status"] = "hit-cache"
            report["retrieval_order"].append(step)
            _persist_fetch(store, run_id, paper_id, report, cache, out_dir)
            return report

        # 2/3) download + byte-validate
        step2 = _download_and_validate(config, url, out_dir, cache, version)
        step.update(step2.pop("_step_meta", {}))
        report["retrieval_order"].append(step)
        if step2.get("ok"):
            report["document"] = step2
            report["access_level"] = (AccessLevel.FULL_TEXT if step2["kind"] in ("pdf", "xml")
                                     else AccessLevel.HTML_LANDING)
            _persist_fetch(store, run_id, paper_id, report, cache, out_dir)
            return report

    # 4) record unavailable and continue
    report["access_level"] = AccessLevel.UNAVAILABLE
    _persist_fetch(store, run_id, paper_id, report, cache, out_dir)
    return report


def _unpaywall_candidates(doi: str, config: Any, env: dict | None,
                          cache, report: dict) -> list[tuple[str, str]]:
    """Step 2: Unpaywall locations (best first; PDF URLs before landing)."""
    email = ""
    if env:
        email = (env.get("PAPERS_EMAIL") or "").strip()
    import os
    if not email:
        email = os.environ.get("PAPERS_EMAIL", "")
    if not email:
        report["warnings"].append("PAPERS_EMAIL not configured; skipped Unpaywall locations")
        return []
    tr = make_metadata_transport(config, pacer_interval=0.1)
    try:
        rec = UnpaywallAdapter().lookup(doi, config, env, transport=tr, cache=cache)
    except Exception as e:  # noqa: BLE001
        report["warnings"].append(f"unpaywall lookup failed: {e}")
        return []
    finally:
        tr.close()
    if not rec.get("found"):
        return []
    urls: list[tuple[str, str]] = []
    for loc in rec.get("locations", []):
        pdf_url = loc.get("url_for_pdf")
        main_url = loc.get("url") or ""
        host_type = loc.get("host_type") or "unknown"
        lic = loc.get("license") or "unknown"
        label = f"unpaywall:{host_type}:{lic}"
        if pdf_url and pdf_url != main_url:
            urls.append((pdf_url, label + ":pdf"))
        if main_url:
            urls.append((main_url, label + (":landing" if loc.get("_url_is_pdf") is False else "")))
    return urls


def _publisher_candidates(doi: str, config: Any, env: dict | None,
                          cache, report: dict) -> list[tuple[str, str]]:
    """Step 3: repository / publisher accessible full text."""
    cands: list[tuple[str, str]] = []
    # OpenAlex open-access URL.
    try:
        tr = make_metadata_transport(config)
        try:
            r = OpenAlexAdapter().lookup_work(doi, config, env, transport=tr, cache=cache)
        finally:
            tr.close()
        if r.get("found"):
            work = r.get("work") or {}
            oa = work.get("open_access") or {}
            if oa.get("oa_url"):
                cands.append((oa["oa_url"], "openalex:oa_url"))
            loc = work.get("primary_location") or {}
            if loc.get("pdf_url"):
                cands.append((loc["pdf_url"], "openalex:primary_pdf"))
            for c in (work.get("content_urls") or []):
                if isinstance(c, dict) and c.get("url"):
                    cands.append((c["url"], "openalex:content_url"))
    except Exception as e:  # noqa: BLE001
        report["warnings"].append(f"openalex candidate lookup failed: {e}")
    # DOI landing page (publisher page, not full text).
    cands.append((f"https://doi.org/{doi.lower()}", "doi-landing"))
    return cands


def _download_and_validate(config: Any, url: str, out_dir: Path, cache,
                           version: str | None) -> dict[str, Any]:
    """Download one candidate URL; validate bytes; store raw + hash + status."""
    from urllib.parse import urljoin
    import httpx

    tr = make_document_transport(config, url)
    try:
        try:
            resp = tr.get(url, cache=cache, cache_ttl=None, cache_negative=True)
        except HttpError as e:
            return {"_step_meta": {"status": "transport-failed", "error": e.message}}
    finally:
        tr.close()

    if not resp.ok:
        # Permanent blocks (401/403/404) are NOT retried; move on.
        return {"_step_meta": {"status": f"http-{resp.status_code}",
                               "retryable": resp.status_code in (429, 500, 502, 503, 504)}}
    content = resp.content
    kind = detect_document_kind(content, resp.mime)
    if kind == "unknown" or len(content) < 512:
        return {"_step_meta": {"status": "content-invalid",
                               "note": f"kind={kind} size={len(content)}"}}
    digest = sha256_of(content)
    ext = {"pdf": ".pdf", "xml": ".xml", "html": ".html", "text": ".txt"}.get(kind, ".bin")
    filename = f"{digest[:16]}{ext}"
    target = out_dir / filename
    target.write_bytes(content)
    if cache is not None:
        cache.store_document(url, content, doc_kind=kind)
    return {
        "ok": True,
        "kind": kind,
        "path": str(target),
        "sha256": digest,
        "size": len(content),
        "source_url": resp.url,
        "origin": url,
        "from_cache": resp.from_cache,
        "mime": resp.mime,
        "accessed_at": resp.fetched_at,
    }


def _persist_fetch(store: PaperStore | None, run_id: str | None, paper_id: str | None,
                   report: dict[str, Any], cache: RetrievalCache | None, out_dir: Path) -> None:
    """Record the Document entity (spec section 11) for the fetch outcome.

    Documents reference a Paper row (foreign key); when the caller did not
    pin one, the paper is looked up by its canonical ids, and created when
    missing (fetch may run before any search did).
    """
    if store is None or not run_id:
        return
    if paper_id is None:
        ids: dict[str, str] = {}
        ident = report.get("identifier") or {}
        if ident.get("doi"):
            ids["doi"] = ident["doi"].lower()
        if ident.get("arxiv_id"):
            ids["arxiv_id"] = ident["arxiv_id"]
        paper_id = store.find_paper_by_ids(run_id, ids) if ids else None
        if paper_id is None:
            paper_id = store.upsert_paper(run_id, ids, title=None, authors=None,
                                          year=None,
                                          metadata_provenance={"created_by": "fetch"})
    doc = report.get("document")
    if doc is None:
        status = "unavailable"
    else:
        status = ("retrieved_full_text" if report["access_level"] == AccessLevel.FULL_TEXT
                  else "retrieved_html_landing")
    store.record_document(
        paper_id,
        source_url=(doc or {}).get("source_url"),
        content_hash=(doc or {}).get("sha256"),
        version=report["identifier"].get("version"),
        access_status=status,
        parse_quality=(doc or {}).get("kind"),
        raw_path=(doc or {}).get("path"),
    )

