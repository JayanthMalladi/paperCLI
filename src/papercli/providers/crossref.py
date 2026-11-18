"""Crossref adapter (Phase 2).

Verified facts (research/provider-docs/crossref.md + provider-docs.json):
* Base ``https://api.crossref.org``; no API key. Polite pool via the
  ``mailto`` query param (observed ~3 req/s limit on the polite pool, no
  hard number published -- so the adapter paces at 1 req / 0.35 s and
  honors 429 + Retry-After via the shared bounded retry).
* ``/works`` supports ``query.bibliographic`` / ``query.title`` filters,
  structured ``filter=``, and ``rows`` + ``offset`` (offset <= 10000) plus
  cursor paging (``cursor=*`` -> ``next-cursor``; cursors expire in 5
  minutes). Rows are capped at 1000 per request.
* There is **no** ``/events`` endpoint (live 404) -- not called.
* Crossref is a metadata service: it is **not** treated as a full-text
  repository (spec section 8).
"""

from __future__ import annotations

from typing import Any

from papercli.providers.base import (
    CredentialStatus,
    ProviderAdapter,
    ProviderInfo,
)
from papercli.providers.http_layer import HttpError, HttpTransport
from papercli.providers.registry import register_provider

__all__ = ["CrossrefAdapter", "ORDER_HINT"]

ORDER_HINT = 50  # Crossref is metadata only; its URLs are landing/DOI pages


class CrossrefAdapter(ProviderAdapter):
    """DOI metadata lookup + bibliographic search over Crossref works."""

    info = ProviderInfo(
        name="crossref",
        credential_env="PAPERS_EMAIL",
        credential_optional=True,   # mailto is polite, not required
        base_url="https://api.crossref.org",
    )

    #: Polite-pool pacing: observed limit is ~3 req/s; stay under it.
    PACING_INTERVAL_SECONDS = 0.35

    def __init__(self, transport: HttpTransport | None = None):
        self._transport = transport

    def credential_status(self, config: Any, env: dict | None = None) -> CredentialStatus:
        pdoc = (config.get("providers.crossref") or {}) if config else {}
        if not pdoc.get("enabled", True):
            return CredentialStatus.DISABLED
        # mailto uses the contact email; the provider is usable without it.
        import os
        email = (config.get("contact.email") or "").strip() if config else ""
        email = email or (env or {}).get("PAPERS_EMAIL", "") or os.environ.get("PAPERS_EMAIL", "")
        if not email or "REPLACE" in email.upper():
            return CredentialStatus.MISSING
        return CredentialStatus.PRESENT

    def preflight(self, config: Any, env: dict | None = None) -> dict[str, Any]:
        status = self.credential_status(config, env)
        polite = status == CredentialStatus.PRESENT
        return {
            "provider": "crossref",
            "credential": status.value,
            "polite_pool": polite,
            "endpoint": self.info.base_url,
            "note": "mailto engages the polite pool (~3 req/s observed); not a full-text repo",
        }

    # -- operations ----------------------------------------------------------
    def _mailto(self, config: Any, env: dict | None = None) -> str | None:
        import os
        email = ""
        if config:
            email = (config.get("contact.email") or "").strip()
        if not email:
            email = (env or {}).get("PAPERS_EMAIL", "") or os.environ.get("PAPERS_EMAIL", "")
        email = email.strip()
        if not email or "REPLACE" in email.upper():
            return None
        return email

    def _get_transport(self, transport: HttpTransport | None) -> HttpTransport:
        if transport is not None:
            return transport
        if self._transport is not None:
            return self._transport
        from papercli.providers.http_layer import Pacer
        self._transport = HttpTransport(pacer=Pacer(self.PACING_INTERVAL_SECONDS))
        return self._transport

    def lookup_doi(self, doi: str, config: Any, env: dict | None = None,
                   transport: HttpTransport | None = None, cache=None) -> dict[str, Any]:
        """DOI metadata (singleton ``/works/{doi}``)."""
        from papercli.providers.identifiers import normalize_doi

        norm = normalize_doi(doi)
        tr = self._get_transport(transport)
        url = f"{self.info.base_url}/works/{norm}"
        params = {"mailto": self._mailto(config, env)} if self._mailto(config, env) else None
        resp = tr.get(url, params=params, cache=cache, cache_ttl=7 * 24 * 3600,
                      cache_negative=True)
        payload = resp.json() if resp.content else {}
        item = payload.get("message", payload)
        warnings = []
        returned = item.get("DOI")
        if returned and normalize_doi(returned) != norm:
            warnings.append(f"returned DOI {returned!r} does not match requested {norm!r}")
        return {
            "provider": "crossref",
            "kind": "doi_lookup",
            "requested_doi": norm,
            "record": item,
            "record_sha256": None,
            "fetched_at": resp.fetched_at,
            "from_cache": resp.from_cache,
            "response_status": resp.status_code,
            "warnings": warnings,
        }

    def search(
        self,
        query: str,
        config: Any,
        env: dict | None = None,
        transport: HttpTransport | None = None,
        cache=None,
        *,
        limit: int = 20,
        query_field: str | None = "bibliographic",
        filters: dict[str, str] | None = None,
        cursor: str | None = None,
        save_cursor: Any = None,
    ) -> dict[str, Any]:
        """Bibliographic / title search over ``/works``.

        Uses the documented cursor mechanism when paging (cursors expire in
        5 minutes, so the cursor is saved via ``save_cursor`` when provided).
        ``limit`` is clamped to Crossref's rows ceiling (1000).
        """
        tr = self._get_transport(transport)
        rows = max(1, min(int(limit), 1000))
        params: dict[str, Any] = {"rows": rows}
        if query_field:
            params[f"query.{query_field}"] = query
        mailto = self._mailto(config, env)
        if mailto:
            params["mailto"] = mailto
        if filters:
            parts = [f"{k}:{v}" for k, v in filters.items() if v not in (None, "")]
            if parts:
                params["filter"] = ",".join(parts)
        if cursor:
            params["cursor"] = cursor
        else:
            params["cursor"] = "*"
        # Crossref cursor paging: cursor=* + rows>0; keep offset off.

        url = f"{self.info.base_url}/works"
        resp = tr.get(url, params=params, cache=cache, cache_ttl=2 * 3600,
                      cache_negative=True)
        if resp.status_code in (400, 404):
            # Cursor expired or malformed: caller may fall back to offset.
            raise HttpError(
                f"Crossref /works returned HTTP {resp.status_code} "
                f"(expired or bad cursor?)",
                url=url, status_code=resp.status_code, retryable=False,
            )
        payload = resp.json()
        message = payload.get("message", payload)
        items = message.get("items", [])
        next_cursor = message.get("next-cursor")
        total = message.get("total-results")
        if save_cursor is not None and next_cursor:
            save_cursor(next_cursor)
        return {
            "provider": "crossref",
            "kind": "search",
            "query": query,
            "query_field": query_field,
            "items": items,
            "item_count": len(items),
            "total_results": total,
            "next_cursor": next_cursor,
            "cursor_expires_seconds": 300,
            "completeness": "partial",  # one page, never the whole result set
            "request_url": url,
            "fetched_at": resp.fetched_at,
            "from_cache": resp.from_cache,
            "order_hint": ORDER_HINT,
        }


register_provider(CrossrefAdapter)

