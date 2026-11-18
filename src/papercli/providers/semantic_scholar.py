"""Semantic Scholar adapter (Phase 2) -- optional enrichment only.

S2 is NOT in the first-release retrieval path: it is enrichment / chaining
(spec section 8 "optional Semantic Scholar for enrichment/chaining").

Keyless access is shared-IP and frequently 429s (verified planning fact), so
the adapter degrades gracefully: on 429/403/404/miss, the call is SKIPPED
and recorded (``skipped`` + reason) -- it is NEVER a run failure. An
optional ``SEMANTIC_SCHOLAR_API_KEY`` is attached when set (S2 sends it as
``Bearer``; keyless requests carry no auth header).
"""

from __future__ import annotations

from typing import Any

from papercli.providers.base import (
    CredentialStatus,
    ProviderAdapter,
    ProviderInfo,
)
from papercli.providers.http_layer import HttpError, HttpTransport
from papercli.providers.identifiers import normalize_arxiv_id as _normalize_arxiv
from papercli.providers.registry import register_provider

__all__ = ["SemanticScholarAdapter", "ORDER_HINT"]

ORDER_HINT = 0  # enrichment only; never a full-text source


class SemanticScholarAdapter(ProviderAdapter):
    """Related-papers / citation enrichment with graceful degradation."""

    info = ProviderInfo(
        name="s2",
        credential_env="SEMANTIC_SCHOLAR_API_KEY",
        credential_optional=True,   # keyless is the default; key is optional
        base_url="https://api.semanticscholar.org/graph/v1",
    )

    PACING_SECONDS = 0.12  # shared-IP keyless pool; stay gentle

    def __init__(self, transport: HttpTransport | None = None):
        self._transport = transport

    def credential_status(self, config: Any, env: dict | None = None) -> CredentialStatus:
        import os
        pdoc = (config.get("providers.s2") or {}) if config else {}
        if not pdoc.get("enabled", True):
            return CredentialStatus.DISABLED
        ref = pdoc.get("key_env") or "SEMANTIC_SCHOLAR_API_KEY"
        val = ((env or {}).get(ref) or os.environ.get(ref) or "").strip()
        if not val:
            # Keyless is a valid (default) mode -- PRESENT means "usable".
            return CredentialStatus.NOT_REQUIRED
        return CredentialStatus.PRESENT

    def preflight(self, config: Any, env: dict | None = None) -> dict[str, Any]:
        import os
        ref = "SEMANTIC_SCHOLAR_API_KEY"
        has_key = bool(((env or {}).get(ref) or os.environ.get(ref) or "").strip())
        return {
            "provider": "s2",
            "credential": "present" if has_key else "keyless_default",
            "optional": True,
            "endpoint": self.info.base_url,
            "note": "optional enrichment; 429/403/miss -> skip + record, never a run failure",
        }

    # -- internals -----------------------------------------------------------
    def _get_transport(self, transport: HttpTransport | None) -> HttpTransport:
        if transport is not None:
            return transport
        if self._transport is None:
            from papercli.providers.http_layer import Pacer
            self._transport = HttpTransport(pacer=Pacer(self.PACING_SECONDS))
        return self._transport

    def _headers(self, config: Any, env: dict | None) -> dict[str, str] | None:
        """Optional S2 API key as a Bearer header (never leak the value)."""
        import os
        pdoc = (config.get("providers.s2") or {}) if config else {}
        ref = pdoc.get("key_env") or "SEMANTIC_SCHOLAR_API_KEY"
        key = ((env or {}).get(ref) or os.environ.get(ref) or "").strip()
        if key:
            return {"Authorization": f"Bearer {key}"}
        return None

    # -- operations ----------------------------------------------------------
    def paper_by_id(self, paper_id: str, config: Any, env: dict | None = None,
                    transport: HttpTransport | None = None, cache=None) -> dict[str, Any]:
        """Enrich one paper; on 404/429/403 return a *recorded skip*."""
        return self._enrich(
            f"/paper/{paper_id}",
            paper_id=paper_id,
            config=config, env=env, transport=transport, cache=cache,
        )

    def paper_by_doi(self, doi: str, config: Any, env: dict | None = None,
                     transport: HttpTransport | None = None, cache=None) -> dict[str, Any]:
        """Enrich by DOI via S2 external ids (``DOI:10....``)."""
        return self.paper_by_id(f"DOI:{doi.strip().lower()}", config=config,
                                env=env, transport=transport, cache=cache)

    def paper_by_arxiv(self, arxiv_id: str, config: Any, env: dict | None = None,
                       transport: HttpTransport | None = None, cache=None) -> dict[str, Any]:
        """Enrich by arXiv id via S2 external ids (``ARXIV:1605.0000``)."""
        base, _ver = _normalize_arxiv(arxiv_id)
        return self.paper_by_id(f"ARXIV:{base}", config=config, env=env,
                                transport=transport, cache=cache)

    def search(self, query: str, config: Any, env: dict | None = None,
               transport: HttpTransport | None = None, cache=None,
               *, limit: int = 10) -> dict[str, Any]:
        """S2 paper search (enrichment-grade). 429/403/miss -> recorded skip."""
        return self._enrich(
            "/paper/search",
            paper_id=query,
            config=config, env=env, transport=transport, cache=cache,
            params={"query": query, "limit": max(1, int(limit))},
        )

    def related_papers(self, paper_id: str, config: Any, env: dict | None = None,
                       transport: HttpTransport | None = None, cache=None,
                       *, limit: int = 10) -> dict[str, Any]:
        return self._enrich(
            f"/paper/{paper_id}/relations",
            paper_id=paper_id, config=config, env=env, transport=transport,
            cache=cache, params={"limit": max(1, int(limit))},
        )

    def citations(self, paper_id: str, config: Any, env: dict | None = None,
                  transport: HttpTransport | None = None, cache=None,
                  *, limit: int = 10) -> dict[str, Any]:
        return self._enrich(
            f"/paper/{paper_id}/citations",
            paper_id=paper_id, config=config, env=env, transport=transport,
            cache=cache, params={"limit": max(1, int(limit))},
        )

    def _enrich(
        self,
        path: str,
        *,
        paper_id: str,
        config: Any,
        env: dict | None,
        transport: HttpTransport | None,
        cache,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One S2 call with graceful-degrade semantics.

        Returns ``{"provider": "s2", "skipped": bool, "reason": str | None,
        "data": dict | None}``. 429/403/404/transport failures are skips
        (recorded, not exceptions); the run continues.
        """
        tr = self._get_transport(transport)
        url = f"{self.info.base_url}{path}"
        base_params = dict(params or {})
        headers = self._headers(config, env)
        try:
            resp = tr.get(url, params=base_params or None, headers=headers,
                          cache=cache, cache_ttl=6 * 3600, cache_negative=True)
        except HttpError as e:
            if e.retryable or e.status_code in (401, 403):
                # Shared-IP keyless pool / rate limit / transient exhaustion:
                # record the skip and continue the run (never a run failure).
                return {"provider": "s2", "kind": path.strip("/"),
                        "paper_id": paper_id, "skipped": True,
                        "reason": f"{e.message}", "data": None}
            raise  # guard violations etc. must surface
        if resp.status_code in (401, 403, 429):
            return {"provider": "s2", "kind": path.strip("/"), "paper_id": paper_id,
                    "skipped": True,
                    "reason": f"HTTP {resp.status_code} (keyless pool / rate limit)",
                    "data": None}
        if resp.status_code == 404:
            return {"provider": "s2", "kind": path.strip("/"), "paper_id": paper_id,
                    "skipped": True, "reason": "not found", "data": None}
        if not resp.ok:
            return {"provider": "s2", "kind": path.strip("/"), "paper_id": paper_id,
                    "skipped": True, "reason": f"HTTP {resp.status_code}", "data": None}
        return {"provider": "s2", "kind": path.strip("/"), "paper_id": paper_id,
                "skipped": False, "reason": None, "data": resp.json(),
                "fetched_at": resp.fetched_at}


register_provider(SemanticScholarAdapter)

