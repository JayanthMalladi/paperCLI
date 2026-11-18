"""OpenAlex adapter (Phase 2).

Verified facts (research/provider-docs/openalex.md + provider-docs.json):
* Base ``https://api.openalex.org``; works are discovered at ``/works``
  (singleton, filter, search) with cursor pagination.
* USD-credit budget model (NOT a fixed calls-per-window quota): keyless is
  $0.10/day at 10 req/s per IP; an activated free key is $1/day at 30 req/s.
  The adapter records this budget concept in its result envelope.
* Cursor paging PAST page 1 requires a key (401/403 without one) -- the
  key (``OPENALEX_API_KEY``) is sent as ``?api_key=`` when present, and a
  keyless cursor follow-up surfaces the provider failure, never a
  complete-search claim.
* Filters of interest: ``publication_date``/``from_publication_date`` /
  ``to_publication_date``, ``type``, ``open_access.is_oa``; citation
  relationships arrive via the ``citations``/``referenced_works`` fields.
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

__all__ = ["OpenAlexAdapter", "ORDER_HINT"]

ORDER_HINT = 55  # OpenAlex is discovery/metadata + OA links; not full text


class OpenAlexAdapter(ProviderAdapter):
    """Discovery, filters, cursor pagination, and citation relationships."""

    info = ProviderInfo(
        name="openalex",
        credential_env="OPENALEX_API_KEY",
        credential_optional=True,
        base_url="https://api.openalex.org",
    )

    #: Pacing stays inside the published per-second budgets.
    PACING_KEYLESS_SECONDS = 0.11   # ~10 req/s keyless
    PACING_KEY_SECONDS = 0.035      # ~30 req/s with key

    def __init__(self, transport: HttpTransport | None = None):
        self._transport = transport

    def credential_status(self, config: Any, env: dict | None = None) -> CredentialStatus:
        pdoc = (config.get("providers.openalex") or {}) if config else {}
        if not pdoc.get("enabled", True):
            return CredentialStatus.DISABLED
        ref = pdoc.get("api_key_env") or "OPENALEX_API_KEY"
        import os
        envmap = env if env is not None else {}
        val = (envmap.get(ref) or os.environ.get(ref) or "").strip()
        if not val:
            # Optional credential: usable keyless with the smaller budget.
            return CredentialStatus.PRESENT
        return CredentialStatus.PRESENT

    def preflight(self, config: Any, env: dict | None = None) -> dict[str, Any]:
        ref = ((config.get("providers.openalex") or {}) or {}).get("api_key_env") or "OPENALEX_API_KEY"
        import os
        envmap = env if env is not None else {}
        has_key = bool((envmap.get(ref) or os.environ.get(ref) or "").strip())
        return {
            "provider": "openalex",
            "credential": "present" if has_key else "keyless",
            "credential_env_ref": ref,
            "endpoint": self.info.base_url,
            "budget": ("$1/day @ 30 req/s" if has_key else "$0.10/day @ 10 req/s"),
            "note": "cursor paging past page 1 requires an activated key",
        }

    # -- internals -----------------------------------------------------------
    def _key(self, config: Any, env: dict | None = None) -> str | None:
        pdoc = (config.get("providers.openalex") or {}) if config else {}
        ref = pdoc.get("api_key_env") or "OPENALEX_API_KEY"
        import os
        envmap = env if env is not None else {}
        val = (envmap.get(ref) or os.environ.get(ref) or "").strip()
        return val or None

    def _get_transport(self, config: Any, env: dict | None, transport: HttpTransport | None) -> HttpTransport:
        if transport is not None:
            return transport
        if self._transport is None:
            from papercli.providers.http_layer import Pacer
            interval = self.PACING_KEY_SECONDS if self._key(config, env) else self.PACING_KEYLESS_SECONDS
            self._transport = HttpTransport(pacer=Pacer(interval))
        return self._transport

    def _paged_request(
        self,
        config: Any,
        env: dict | None,
        tr: HttpTransport,
        cache,
        *,
        cursor: str | None,
        per_page: int,
        filters: list[str],
        search: str | None,
        select: str,
        api_key: str | None,
        url: str,
    ):
        params: dict[str, Any] = {
            "per-page": max(1, min(int(per_page), 200)),
            "select": select,
        }
        if filters:
            params["filter"] = ",".join(filters)
        if search:
            params["search"] = search
        if cursor:
            params["cursor"] = cursor
        if api_key:
            params["api_key"] = api_key
        resp = tr.get(url, params=params, cache=cache, cache_ttl=2 * 3600,
                      cache_negative=True)
        return resp

    # -- operations ----------------------------------------------------------
    def search_works(
        self,
        query: str,
        config: Any,
        env: dict | None = None,
        transport: HttpTransport | None = None,
        cache=None,
        *,
        per_page: int = 25,
        filters: list[str] | None = None,
        cursor: str | None = None,
        save_cursor: Any = None,
        use_search: bool = True,
    ) -> dict[str, Any]:
        """Discover works (filter and/or text search) with cursor paging.

        A provider failure (keyless cursor past page 1 -> 401/403, 429s,
        transport errors) must NEVER be reported as a complete search: the
        returned ``completeness`` stays ``"incomplete"`` and the error is
        surfaced in ``error`` so the caller records a partial run.
        """
        tr = self._get_transport(config, env, transport)
        api_key = self._key(config, env)
        url = f"{self.info.base_url}/works"
        # Valid select fields only (verified against the live API).
        # Citation relationships: referenced_works/related_works +
        # cited_by_count. There is no separate "citations" select field.
        select = (
            "id,doi,title,display_name,publication_date,publication_year,type,"
            "authorships,open_access,primary_location,locations,"
            "cited_by_count,referenced_works,related_works,content_urls"
        )
        result: dict[str, Any] = {
            "provider": "openalex",
            "kind": "search",
            "query": query,
            "items": [],
            "item_count": 0,
            "total_results": None,
            "next_cursor": None,
            "completeness": "incomplete",
            "error": None,
            "budget": self._budget_note(api_key),
            "request_url": url,
            "from_cache": False,
            "order_hint": ORDER_HINT,
        }
        try:
            resp = self._paged_request(
                config, env, tr, cache, cursor=cursor, per_page=per_page,
                filters=filters or [], search=query if use_search else None,
                select=select, api_key=api_key, url=url,
            )
        except HttpError as e:
            result["error"] = f"{type(e).__name__}: {e.message}"
            return result  # provider failure, still an incomplete search
        if resp.status_code not in (200, 201, 206):
            result["error"] = f"HTTP {resp.status_code}"
            return result

        payload = resp.json()
        meta = payload.get("meta", {})
        works = payload.get("results", [])
        result.update(
            items=works,
            item_count=len(works),
            total_results=meta.get("count"),
            next_cursor=meta.get("next_cursor") or meta.get("cursor"),
            from_cache=resp.from_cache,
            fetched_at=resp.fetched_at,
        )
        if save_cursor is not None:
            save_cursor(meta.get("next_cursor") or meta.get("cursor"))
        # Completeness semantics: one page of a large result set is never
        # the whole set; it is "complete" only when the provider says the
        # result set fit in the returned page.
        result["completeness"] = "complete" if (
            result["total_results"] is not None
            and result["total_results"] <= max(1, min(int(per_page), 200))
        ) else "partial"
        return result

    def lookup_work(self, identifier: str, config: Any, env: dict | None = None,
                   transport: HttpTransport | None = None, cache=None,
                   *, with_citations: bool = False) -> dict[str, Any]:
        """Singleton work lookup (by OpenAlex id, DOI, or free-text W:xxx).

        ``with_citations`` selects the citation relationship fields so the
        caller can chain follow-on discovery (spec section 8).
        """
        tr = self._get_transport(config, env, transport)
        api_key = self._key(config, env)
        select = ("id,doi,title,display_name,publication_date,publication_year,type,"
                  "authorships,open_access,primary_location,locations,cited_by_count")
        if with_citations:
            # Relationship fields (works referenced, related works, URLs)
            select += ",referenced_works,related_works,content_urls"
        target = str(identifier).strip()
        if not (target.startswith("W") and target[1:2].isdigit()):
            # Normalise DOIs / bare strings into the W:<id> or DOI form.
            if target.lower().startswith("10."):
                target = f"doi:{target.lower()}"
            else:
                target = f"search:{target}"
        url = f"{self.info.base_url}/works/{target}"
        params: dict[str, Any] = {"select": select}
        if api_key:
            params["api_key"] = api_key
        try:
            resp = tr.get(url, params=params, cache=cache,
                          cache_ttl=7 * 24 * 3600, cache_negative=True)
        except HttpError as e:
            return {"provider": "openalex", "kind": "lookup", "found": False,
                    "error": f"{type(e).__name__}: {e.message}", "work": None}
        if resp.status_code == 404:
            return {"provider": "openalex", "kind": "lookup", "found": False,
                    "work": None, "response_status": 404}
        payload = resp.json()
        return {
            "provider": "openalex",
            "kind": "lookup",
            "found": True,
            "work": payload,
            "response_status": resp.status_code,
            "fetched_at": resp.fetched_at,
            "from_cache": resp.from_cache,
            "order_hint": ORDER_HINT,
        }

    def _budget_note(self, api_key: str | None) -> dict[str, Any]:
        """Record the USD-credit budget concept (spec: do not mislabel it)."""
        if api_key:
            return {"model": "usd-credit", "daily_budget_usd": 1.0,
                    "per_second": 30, "key": True}
        return {"model": "usd-credit", "daily_budget_usd": 0.10,
                "per_second": 10, "key": False}


register_provider(OpenAlexAdapter)

