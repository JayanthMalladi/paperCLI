"""Unpaywall adapter (Phase 2).

Verified facts (research/provider-docs/unpaywall.md + provider-docs.json):
* ``GET https://api.unpaywall.org/v2/{doi}?email=...`` -- the email query
  parameter is REQUIRED; there is no API key.
* ``oa_status`` is one of ``{gold, hybrid, bronze, green, closed}``; a
  closed / no-location record is a **valid** lookup outcome, not an error.
* The ``/v2/search`` endpoint was RETIRED (2026-09-18, now 410 Gone);
  Unpaywall search is delegated to OpenAlex, and this adapter never calls
  the retired endpoint.
* 100k calls/day is soft guidance; 429/Retry-After behavior is not
  published, so we apply the shared bounded-retry policy (429 honored via
  the Retry-After header when present, generic backoff otherwise).

Adapter duties (spec section 8):
* normalize the DOI without stripping valid suffix punctuation;
* verify the returned DOI matches the requested DOI;
* preserve the raw response + fetch timestamp + oa_status + location metadata;
* collect best + all OA locations and deduplicate URLs;
* order candidate URLs so direct PDF URLs come before landing pages;
* retain version / host_type / license (null or unknown) and the
  landing-vs-PDF URL distinction;
* cache metadata with a configurable TTL (shorter for negatives).
"""

from __future__ import annotations

import json
from typing import Any

from papercli.providers.base import (
    CredentialStatus,
    ProviderAdapter,
    ProviderInfo,
)
from papercli.providers.http_layer import HttpError, HttpTransport
from papercli.providers.cache import sha256_of as _sha256_of
from papercli.providers.identifiers import doi_matches, normalize_doi
from papercli.providers.registry import register_provider

__all__ = ["UnpaywallAdapter", "ORDER_HINT", "PDF_HOST_HINTS"]

ORDER_HINT = 100  # Unpaywall OA locations: candidate full-text sources


#: Hosts that serve OA repositories as landing pages (vs. direct PDFs).
_PDF_URL_HINTS = ("/pdf/", ".pdf", "fulltext", "repository")
_PDF_HOST_HINTS = (
    "core.ac.uk",
    "repository.",
    "eprints.",
    "scholar.archive.org",
    "escholarship.org",
    "openaccess",
    "proquest.",
    "medrxiv.",
    "ssrn.com",
)


class UnpaywallAdapter(ProviderAdapter):
    """Live Unpaywall DOI lookup."""

    info = ProviderInfo(
        name="unpaywall",
        credential_env="PAPERS_EMAIL",
        credential_optional=False,
        base_url="https://api.unpaywall.org/v2",
    )

    def __init__(self, transport: HttpTransport | None = None):
        self._transport = transport

    # -- ProviderAdapter contract ------------------------------------------
    def credential_status(self, config: Any, env: dict | None = None) -> CredentialStatus:
        pdoc = (config.get("providers.unpaywall") or {}) if config else {}
        if not pdoc.get("enabled", True):
            return CredentialStatus.DISABLED
        ref = pdoc.get("email_env") or "PAPERS_EMAIL"
        envmap = env if env is not None else {}
        import os
        val = (envmap.get(ref) or os.environ.get(ref) or "").strip()
        if not val or "REPLACE" in val.upper():
            return CredentialStatus.MISSING
        return CredentialStatus.PRESENT

    def preflight(self, config: Any, env: dict | None = None) -> dict[str, Any]:
        status = self.credential_status(config, env)
        return {
            "provider": "unpaywall",
            "credential": status.value,
            "credential_env_ref": "PAPERS_EMAIL",
            "endpoint": self.info.base_url,
            "note": "email param required; /v2/search is retired (410) and never called",
        }

    # -- live operations -----------------------------------------------------
    def _email(self, config: Any, env: dict | None = None) -> str:
        import os
        pdoc = (config.get("providers.unpaywall") or {}) if config else {}
        ref = pdoc.get("email_env") or "PAPERS_EMAIL"
        envmap = env if env is not None else {}
        val = (envmap.get(ref) or os.environ.get(ref) or "").strip()
        return val

    def lookup(self, doi: str, config: Any, env: dict | None = None,
               transport: HttpTransport | None = None, cache=None) -> dict[str, Any]:
        """Look up one DOI. Always returns a dict (closed is a valid outcome).

        Raises :class:`HttpError` on transport failure / rate limit; a 404
        from Unpaywall (unknown DOI) is returned as
        ``{"found": False, ...}`` rather than an exception -- the record
        itself (including absence of locations) is the data.
        """
        norm = normalize_doi(doi)  # ValueError on malformed input
        email = self._email(config, env)
        if not email:
            raise HttpError(
                "Unpaywall requires the ?email= parameter; set PAPERS_EMAIL",
                retryable=False,
            )

        tr = transport or self._transport or HttpTransport()
        # The email is sent only at the transport level; it never appears in
        # the persisted request URL (spec section 16: no email values in
        # JSON output).
        url = f"{self.info.base_url}/{norm}"
        resp = tr.get(url, params={"email": email}, cache=cache,
                      cache_ttl=6 * 3600, cache_negative=True)
        email_included = True

        payload: dict[str, Any]
        if resp.status_code == 404:
            payload = {"found": False}
        else:
            payload = json.loads(resp.content)

        return _normalize_record(payload, requested_doi=norm,
                                 request_url=url, response=resp,
                                 transport=tr, email_included=email_included)


def _location_sort_key(loc: dict[str, Any]) -> tuple:
    """Direct PDF URLs first, then repositories, then landing pages."""
    url = (loc.get("url") or "").lower()
    pdf_host = (loc.get("url_for_pdf") or "").lower()
    is_pdf = (loc.get("url_type") == "application/pdf"
              or ".pdf" in url or "/pdf" in url)
    has_pdf = bool(loc.get("url_for_pdf"))
    repo = loc.get("host_type") == "repository"
    hint = any(h in pdf_host for h in _PDF_HOST_HINTS)
    return (0 if (is_pdf or has_pdf) else 1, 0 if hint else 1, 0 if repo else 1)


def url_base(request_url: str) -> str:
    """Request URL without query params (keeps any email out of records)."""
    from urllib.parse import urlparse
    p = urlparse(request_url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _normalize_record(payload: dict[str, Any], *, requested_doi: str,
                      request_url: str, response: Any, transport: HttpTransport,
                      email_included: bool = False) -> dict[str, Any]:
    """Shape the raw Unpaywall DOI object (spec section 8 rules)."""
    # Unpaywall carries no "found" key on its DOI object: a 2xx answer IS a
    # valid record (including a closed, location-less one). An explicit
    # "found" in the payload (fixture style) is honored when present.
    if "found" in payload:
        found = bool(payload["found"])
    else:
        found = getattr(response, "status_code", 200) in (200, 201, 206)
    returned_doi = payload.get("doi")
    doi_ok = doi_matches(requested_doi, returned_doi)

    oa_status = payload.get("oa_status")
    best = payload.get("best_oa_location")
    locations: list[dict[str, Any]] = [l for l in (payload.get("oa_locations") or []) if l]

    # Dedup URLs, preserve best-first ordering, PDF URLs before landings.
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for loc in ([best] if best else []) + locations:
        url = loc.get("url") or ""
        key = url.lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(loc)
        elif not key:
            ordered.append(loc)

    def enriched(loc: dict[str, Any]) -> dict[str, Any]:
        # Retain every field verbatim (version, host_type, license incl.
        # null/unknown) and mark the landing-vs-PDF URL distinction.
        out = dict(loc)
        pdf_url = loc.get("url_for_pdf")
        out["_url_is_pdf"] = (loc.get("url_type") == "application/pdf") or bool(
            pdf_url and pdf_url != loc.get("url")
        )
        return out

    candidate_locations = [enriched(l) for l in ordered]
    candidate_locations.sort(key=_location_sort_key)

    # Retrieval-priority URL list: for each location, its direct PDF URL
    # (url_for_pdf) comes before its landing URL; URLs are globally deduped.
    retrieval_urls: list[str] = []
    seen_urls: set[str] = set()
    for loc in candidate_locations:
        for u in (loc.get("url_for_pdf"), loc.get("url")):
            if u and u.lower() not in seen_urls:
                seen_urls.add(u.lower())
                retrieval_urls.append(u)

    warnings: list[str] = []
    if found and not doi_ok:
        warnings.append(
            f"returned DOI {returned_doi!r} does not match requested {requested_doi!r}"
        )

    return {
        "provider": "unpaywall",
        "found": found,
        "requested_doi": requested_doi,
        "returned_doi": returned_doi,
        "doi_verified": doi_ok,
        "oa_status": oa_status,
        "is_oa": payload.get("is_oa"),
        "has_repository_copy": payload.get("has_repository_copy"),
        "best_oa_location": (
            {**best,
             "_url_is_pdf": (best.get("url_type") == "application/pdf")
            or bool(best.get("url_for_pdf") and best.get("url_for_pdf") != best.get("url"))}
            if best else None
        ),
        "locations": candidate_locations,
        "candidate_urls": retrieval_urls,
        "metadata": {
            "title": payload.get("title"),
            "journal_name": payload.get("journal_name"),
            "publisher": payload.get("publisher"),
            "year": payload.get("year"),
            "published_date": payload.get("published_date"),
            "genre": payload.get("genre"),
            "doi_url": payload.get("doi_url"),
        },
        "raw": payload,
        "raw_sha256": _sha256_of(getattr(response, "content", b"")),
        "request_url": url_base(request_url),
        "email_included": bool(email_included),
        "response_status": getattr(response, "status_code", None),
        "fetched_at": getattr(response, "fetched_at", None),
        "from_cache": bool(getattr(response, "from_cache", False)),
        "order_hint": ORDER_HINT,
        "warnings": warnings,
    }


register_provider(UnpaywallAdapter)

