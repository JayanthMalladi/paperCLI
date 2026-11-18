"""arXiv adapter (Phase 2).

Verified facts (research/provider-docs/arxiv.md + provider-docs.json):
* Query interface ``https://export.arxiv.org/api/query`` (Atom 1.0 feed),
  no auth.
* TOU pacing: **1 request / 3 s, single connection** -- enforced with a
  dedicated ``Pacer(3.0)`` on this adapter's transport.
* Paging is ``start`` + ``max_results`` (no cursor). ``id_list`` fetches
  the listed articles; ``search_query`` searches; both filters.
* URLs always carry a ``vN`` suffix even when requested bare -- version
  identity is preserved (``abs/{id}v{n}``, ``pdf/{id}v{n}``).
* Full-text PDF retrieval: ``https://arxiv.org/pdf/{id}v{n}`` with
  version identity; a bare id means "latest version".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from papercli.providers.base import (
    CredentialStatus,
    ProviderAdapter,
    ProviderInfo,
)
from papercli.providers.http_layer import HttpError, HttpTransport
from papercli.providers.identifiers import arxiv_abs_url, arxiv_pdf_url, normalize_arxiv_id
from papercli.providers.registry import register_provider

__all__ = ["ArxivAdapter", "ORDER_HINT"]

ORDER_HINT = 90  # arXiv serves direct, version-identity PDFs

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"


class ArxivAdapter(ProviderAdapter):
    """Preprint metadata/search and version-preserving PDF retrieval."""

    info = ProviderInfo(
        name="arxiv",
        credential_env="",
        credential_optional=True,     # no credential needed at all
        base_url="https://export.arxiv.org/api/query",
    )

    #: TOU: one request every 3 seconds, single connection.
    PACING_SECONDS = 3.0

    def __init__(self, transport: HttpTransport | None = None):
        self._transport = transport

    def credential_status(self, config: Any, env: dict | None = None) -> CredentialStatus:
        pdoc = (config.get("providers.arxiv") or {}) if config else {}
        if not pdoc.get("enabled", True):
            return CredentialStatus.DISABLED
        return CredentialStatus.NOT_REQUIRED

    def preflight(self, config: Any, env: dict | None = None) -> dict[str, Any]:
        return {
            "provider": "arxiv",
            "credential": "not_required",
            "endpoint": self.info.base_url,
            "pacing": f"1 request / {int(self.PACING_SECONDS)} s, single connection (TOU)",
            "note": "Atom XML feed; PDFs preserve vN version identity",
        }

    def _get_transport(self, transport: HttpTransport | None) -> HttpTransport:
        if transport is not None:
            return transport
        if self._transport is None:
            from papercli.providers.http_layer import Pacer
            self._transport = HttpTransport(pacer=Pacer(self.PACING_SECONDS))
        return self._transport

    # -- metadata -------------------------------------------------------------
    def lookup_id_list(self, raw_ids: str, config: Any, env: dict | None = None,
                       transport: HttpTransport | None = None, cache=None,
                       *, version: str | None = None) -> dict[str, Any]:
        """Fetch one arXiv id (``id_list``) and normalize it.

        ``raw_ids`` may be ``1605.0000``, ``cond-mat/0207270``, with or
        without ``vN``; the feed's returned links carry the actual version,
        which is preserved (latest version when requested bare).
        """
        base, req_version = normalize_arxiv_id(raw_ids)
        feed_id = f"{base}{version or req_version or ''}"
        tr = self._get_transport(transport)
        resp = tr.get(self.info.base_url, params={"id_list": feed_id},
                      cache=cache, cache_ttl=24 * 3600, cache_negative=True)
        if resp.status_code != 200:
            raise HttpError(f"arXiv id_list returned HTTP {resp.status_code}",
                            url=self.info.base_url, status_code=resp.status_code,
                            retryable=resp.status_code in (500, 502, 503, 504))
        parsed = _parse_feed(resp.content)
        if parsed["error"]:
            raise HttpError(f"arXiv feed error: {parsed['error']}",
                            url=self.info.base_url, retryable=False)
        entries = parsed["entries"]
        if not entries:
            # arXiv returns an empty feed with an error entry, or 404s the
            # body: an unknown id is a valid empty outcome, not a crash.
            return {"provider": "arxiv", "kind": "id_list", "found": False,
                    "entries": [], "total_results": 0, "request_url": self.info.base_url,
                    "requested_id": base, "requested_version": version or req_version}
        return {
            "provider": "arxiv",
            "kind": "id_list",
            "found": True,
            "entries": entries,
            "total_results": parsed["total_results"],
            "requested_id": base,
            "requested_version": version or req_version,
            "request_url": self.info.base_url,
            "fetched_at": parsed.get("fetched_at"),
            "order_hint": ORDER_HINT,
        }

    def search(
        self,
        query: str,
        config: Any,
        env: dict | None = None,
        transport: HttpTransport | None = None,
        cache=None,
        *,
        search_query: str | None = None,
        start: int = 0,
        max_results: int = 25,
    ) -> dict[str, Any]:
        """Preprint search (``search_query`` Lucene form or auto-wrapped)."""
        sq = search_query or f'all:{_quote(query)}'
        tr = self._get_transport(transport)
        resp = tr.get(self.info.base_url,
                      params={"search_query": sq, "start": start,
                              "max_results": max(1, int(max_results))},
                      cache=None)  # search pages are not cached (bounded traversal)
        if resp.status_code != 200:
            raise HttpError(f"arXiv search returned HTTP {resp.status_code}",
                            url=self.info.base_url, status_code=resp.status_code,
                            retryable=resp.status_code >= 500)
        parsed = _parse_feed(resp.content)
        if parsed["error"]:
            raise HttpError(f"arXiv feed error: {parsed['error']}",
                            url=self.info.base_url, retryable=False)
        return {
            "provider": "arxiv",
            "kind": "search",
            "query": query,
            "search_query": sq,
            "start": start,
            "max_results": int(max_results),
            "entries": parsed["entries"],
            "total_results": parsed["total_results"],
            "next_start": start + len(parsed["entries"]),
            "completeness": "partial",  # one page of start/max_results
            "request_url": self.info.base_url,
            "fetched_at": parsed.get("fetched_at"),
            "order_hint": ORDER_HINT,
        }

    # -- full text ---------------------------------------------------------------
    def pdf_url(self, raw_id: str) -> str:
        """Version-preserving PDF URL for a bare or versioned arXiv id."""
        base, version = normalize_arxiv_id(raw_id)
        return arxiv_pdf_url(base, version)

    def abs_url(self, raw_id: str) -> str:
        base, version = normalize_arxiv_id(raw_id)
        return arxiv_abs_url(base, version)


def _quote(text: str) -> str:
    """Wrap a free-text search in arXiv's quoted-form."""
    return text.strip()


def _parse_feed(content: bytes) -> dict[str, Any]:
    """Parse the Atom 1.0 feed into normalized entries (version identity kept)."""
    from papercli.providers.http_layer import _utc_now

    result: dict[str, Any] = {"entries": [], "total_results": None,
                              "error": None, "fetched_at": _utc_now()}
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        result["error"] = f"unparseable Atom feed: {e}"
        return result

    total = root.find(f"{OPENSEARCH}totalResults")
    result["total_results"] = int(total.text) if total is not None and total.text else 0

    for entry in root.findall(f"{ATOM}entry"):
        eid = _text(entry, f"{ATOM}id")
        error_el = entry.find(f"{ARXIV}error")
        if error_el is not None or (eid and "api_error" in eid.lower()):
            result["error"] = _text(entry, f"{ATOM}summary") or _text(entry, f"{ARXIV}error")
            continue
        version = None
        if eid:
            # The feed's canonical id always carries a vN suffix (the latest
            # version when the request was bare): preserve that identity.
            mv = re.search(r"v(\d+)$", eid)
            if mv:
                version = f"v{mv.group(1)}"
        authors = [_text(a, f"{ATOM}name") for a in entry.findall(f"{ATOM}author")]
        links = {
            _text(l, "type") or _text(l, "rel") or "alternate": _text(l, None)
            for l in entry.findall(f"{ATOM}link")
        }
        # Prefer the documented rel= attributes when present.
        abs_url, pdf_url = None, None
        for l in entry.findall(f"{ATOM}link"):
            rel = l.get("rel") or "alternate"
            href = l.get("href") or ""
            if rel == "alternate" and not abs_url:
                abs_url = href
            if rel == "related" and (l.get("type") or "").lower() == "application/pdf":
                pdf_url = href
        primary = entry.find(f"{ARXIV}primary_category")
        result["entries"].append({
            "arxiv_id": eid,
            "version": version,
            "title": re.sub(r"\s+", " ", _text(entry, f"{ATOM}title") or ""),
            "published": _text(entry, f"{ATOM}published"),
            "updated": _text(entry, f"{ATOM}updated"),
            "abstract": _text(entry, f"{ATOM}summary"),
            "authors": [a for a in authors if a],
            "primary_category": (primary.get("term") if primary is not None else None),
            "comment": _text(entry, f"{ARXIV}comment"),
            "journal_ref": _text(entry, f"{ARXIV}journal_ref"),
            "doi": _text(entry, f"{ARXIV}doi"),
            "abs_url": abs_url or (eid if eid else None),
            "pdf_url": pdf_url,
        })
    return result


def _text(el, tag: str | None) -> str | None:
    if tag is None:
        return el.get("href") if el is not None and el.get("href") else None
    node = el.find(tag) if el is not None else None
    if node is None or node.text is None:
        return None
    return node.text


register_provider(ArxivAdapter)

