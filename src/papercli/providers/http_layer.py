"""Shared HTTP transport, URL guarding, pacing, and bounded retry (Phase 2).

Every provider adapter talks to the network through this layer so that the
security and pacing requirements from spec sections 8 and 16 are enforced
exactly once:

* TLS verification is ON (httpx default, never disabled here).
* URLs are validated: only ``http``/``https``; embedded credentials in the
  URL are rejected; loopback / private / link-local / reserved targets are
  rejected by default, including redirect targets and DNS resolution. An
  explicit local-dev opt-out (``network.allow_local`` in config) disables
  the private-target checks only.
* Response bodies are streamed with a content-size ceiling.
* Redirects are followed manually (bounded), re-validating each target;
  authentication headers are stripped on cross-origin redirects.
* Transient failures (429/5xx, connection errors, timeouts) get a bounded
  retry with exponential backoff + jitter, honoring ``Retry-After``.
  Permanent 4xx (401/403/404) are returned to the caller, never retried.

Pacing is per-provider (arXiv: 1 request / 3 s on a single connection;
Crossref polite pool ~3 req/s; OpenAlex 30 req/s with key, 10 keyless).
The pacing state lives on the shared transport pool, so a CLI invocation
that runs several providers still respects each provider's published pace.
"""

from __future__ import annotations

import ipaddress
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlparse

import httpx

__all__ = [
    "GuardError",
    "HttpError",
    "HttpResponse",
    "HttpGuard",
    "HttpTransport",
    "Pacer",
    "RetryPolicy",
    "default_resolver",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GuardError(ValueError):
    """A URL or redirect target failed the network guard."""


class HttpError(Exception):
    """A transport-level failure (guard, size ceiling, exhausted retries)."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.url = url
        self.status_code = status_code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# URL guard
# ---------------------------------------------------------------------------

def default_resolver(host: str, port: int) -> list[str]:
    """Resolve a host to IP strings via the system resolver."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [i[4][0] for i in infos]


# Carrier-grade NAT / shared-address space (100.64.0.0/10): not flagged as
# private on every stdlib version, so check it explicitly.
_TLA = ipaddress.ip_network("100.64.0.0/10")
_RESERVED6 = ipaddress.ip_network("fe80::/10")


def is_public_ip(ip: str) -> bool:
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if (
        obj.is_loopback
        or obj.is_private
        or obj.is_link_local
        or obj.is_reserved
        or obj.is_unspecified
        or obj.is_multicast
    ):
        return False
    if obj.version == 4 and ipaddress.ip_address(ip) in _TLA:
        return False
    return True


class HttpGuard:
    """Validates request URLs and redirect targets.

    ``resolver`` is injectable (tests use a fake; the default is the system
    resolver). DNS results are memoized per process.
    """

    ALLOWED_SCHEMES = ("http", "https")

    def __init__(
        self,
        allow_local: bool = False,
        resolver: Callable[[str, int], list[str]] | None = None,
    ):
        self.allow_local = allow_local
        self._resolver = resolver or default_resolver
        self._dns_memo: dict[tuple[str, int], list[str]] = {}

    def resolve(self, host: str, port: int) -> list[str]:
        key = (host, port)
        if key not in self._dns_memo:
            self._dns_memo[key] = self._resolver(host, port)
        return self._dns_memo[key]

    def validate_url(self, url: str) -> str:
        """Validate ``url``; raise :class:`GuardError` when it is unsafe.

        Checks, in order: absolute http/https URL, no embedded credentials,
        and (unless the local-dev opt-out is on) the target host is public
        (IP literal or every DNS A/AAAA record is public).
        """
        parsed = urlparse(str(url).strip())
        if parsed.scheme.lower() not in self.ALLOWED_SCHEMES:
            raise GuardError(f"unsupported URL scheme: {parsed.scheme!r}")
        host = parsed.hostname
        if not host:
            raise GuardError(f"URL has no host: {url!r}")
        if parsed.username or parsed.password:
            raise GuardError("embedded credentials in URL are not allowed")
        if self.allow_local:
            return str(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        if host.lower() in ("localhost", "0.0.0.0", "::"):
            raise GuardError(f"loopback host is not allowed: {host}")
        try:
            ipaddress.ip_address(host)
            ips = [host]  # IP literal
        except ValueError:
            ips = self.resolve(host, port)
        for ip in ips:
            if not is_public_ip(ip):
                raise GuardError(
                    f"host {host!r} resolves to a non-public address "
                    f"(loopback/private/link-local/reserved); local-dev opt-out is off"
                )
        return str(url)


# ---------------------------------------------------------------------------
# Pacing + retry
# ---------------------------------------------------------------------------

class Pacer:
    """Enforce a minimum interval between requests (single-client pacing).

    Holds the shared "next allowed time" so that sequential requests to the
    same provider never exceed the published pace (e.g. arXiv 1 req / 3 s).
    """

    def __init__(self, min_interval: float = 0.0):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.request_count = 0

    def wait(self) -> float:
        """Block until the provider allows the next request.

        Returns the seconds actually slept (0.0 when nothing was awaited).
        """
        if self.min_interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                sleep_for = self._next_at - now
            else:
                sleep_for = 0.0
                self._next_at = now + self.min_interval
            self.request_count += 1
        if sleep_for > 0:
            time.sleep(sleep_for)
            return sleep_for
        return 0.0


class RetryPolicy:
    """Bounded transient retry with exponential backoff + jitter.

    Honors ``Retry-After`` (delta-seconds or HTTP-date) within a cap so a
    hostile or buggy header can never stall the CLI indefinitely. 429 and
    5xx are transient; permanent 4xx are never retried by the transport.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        retry_after_cap: float = 15.0,
        transient_statuses: Iterable[int] = (429, 500, 502, 503, 504),
    ):
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_after_cap = retry_after_cap
        self.transient_statuses = set(transient_statuses)

    def is_transient(self, status_code: int | None, error: Exception | None = None) -> bool:
        if error is not None:
            return True  # connection/timeout class failures
        return status_code in self.transient_statuses

    def parse_retry_after(self, value: str | None) -> float | None:
        if not value:
            return None
        value = value.strip()
        try:
            return float(value)
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(value)
            if when is None:
                return None
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None

    def delay_for(
        self,
        attempt: int,
        status_code: int | None,
        retry_after: str | None,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> float:
        """Backoff for the ``attempt``-th failure (1-based)."""
        exp = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        delay = exp * (0.5 + 0.5 * rng(0.0, 1.0))  # jitter in [50%, 100%] of exp
        ra = self.parse_retry_after(retry_after)
        if ra is not None:
            delay = max(delay, min(ra, self.retry_after_cap))
        return delay


# ---------------------------------------------------------------------------
# Response + transport
# ---------------------------------------------------------------------------

#: Headers that must not leak to another origin on a cross-origin redirect.
_AUTH_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key"}


@dataclass
class HttpResponse:
    """A final (post-redirect) response with the validated, size-checked body."""

    url: str                      #: final URL after redirects
    status_code: int
    headers: dict[str, str]      #: lowercased header names
    content: bytes
    request_url: str             #: the URL the caller asked for
    fetched_at: str = ""         #: UTC ISO-8601 timestamp
    from_cache: bool = False

    def json(self) -> Any:
        return json_loads(self.content)

    @property
    def mime(self) -> str | None:
        return self.headers.get("content-type")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


import json as _json  # noqa: E402


def json_loads(data: bytes) -> Any:
    return _json.loads(data.decode("utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class HttpTransport:
    """Guarded, paced, bounded-retry GET transport.

    Non-2xx final statuses are *returned* (the provider decides what they
    mean -- e.g. Unpaywall 404 is a valid no-record answer); transport
    failures, guard violations, size-overflow, and exhausted transient
    retries raise :class:`HttpError`.
    """

    def __init__(
        self,
        *,
        guard: HttpGuard | None = None,
        timeout: float = 30.0,
        max_bytes: int = 50 * 1024 * 1024,
        retry: RetryPolicy | None = None,
        pacer: Pacer | None = None,
        follow_redirects: bool = True,
        max_redirects: int = 5,
        client: httpx.Client | None = None,
        client_limits: httpx.Limits | None = None,
    ):
        self._guard = guard or HttpGuard()
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._retry = retry or RetryPolicy()
        self._pacer = pacer
        self._follow_redirects = follow_redirects
        self._max_redirects = max_redirects
        if client is None:
            kwargs: dict = dict(timeout=timeout, follow_redirects=False, verify=True)
            if client_limits is not None:
                kwargs["limits"] = client_limits
            self._client = httpx.Client(**kwargs)
        else:
            self._client = client
        self._owns_client = client is None
        self.last_attempt_count = 0

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- internals ----------------------------------------------------------
    def _stream_get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        with self._client.stream(
            "GET", url, headers=dict(headers), follow_redirects=False
        ) as resp:
            status = resp.status_code
            rh = {k.lower(): v for k, v in resp.headers.items()}
            content = self._read_body(resp)
        return HttpResponse(
            url=url,
            status_code=status,
            headers=rh,
            content=content,
            request_url=url,
            fetched_at=_utc_now(),
        )

    def _read_body(self, resp: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes(chunk_size=65536):
            total += len(chunk)
            if total > self._max_bytes:
                raise HttpError(
                    f"response body exceeds the {self._max_bytes}-byte ceiling",
                    url=str(resp.request.url),
                    retryable=False,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _redirect_targets_public(self, hops: list[str]) -> None:
        for target in hops:
            self._guard.validate_url(target)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cache: "RetrievalCacheType" | None = None,
        cache_ttl: float | None = None,
        cache_negative: bool = True,
    ) -> HttpResponse:
        """GET ``url`` with guard, pacing, retry, and optional caching."""
        request_url = str(url)
        if params:
            request_url = str(httpx.URL(request_url).copy_merge_params(params))

        if cache is not None:
            hit = cache.get(request_url)
            if hit is not None:
                return hit
            # A still-fresh NEGATIVE entry means the last attempt failed
            # recently; the short negative TTL is our re-try budget. Skip
            # the network while it is valid.
            if cache.has_negative(request_url):
                raise HttpError(
                    f"retrieval for {request_url} is negatively cached "
                    f"(retry budget not yet elapsed)",
                    url=request_url, retryable=True,
                )

        req_headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        current = request_url
        redirect_hops: list[str] = []
        attempts = 0
        last_transient: HttpResponse | None = None

        while True:
            attempts += 1
            self.last_attempt_count = attempts
            if self._pacer is not None:
                self._pacer.wait()
            try:
                resp = self._walk_redirects(current, req_headers, redirect_hops)
            except HttpError:
                if cache is not None and cache_negative:
                    cache.store_negative(request_url, status=None, note="transport failure")
                raise
            except httpx.TransportError as e:
                # connection reset / timeout class: transient
                if cache is not None and cache_negative:
                    cache.store_negative(request_url, status=None, note=type(e).__name__)
                if attempts < self._retry.max_attempts:
                    self._sleep_backoff(attempts, None, None)
                    continue
                raise HttpError(
                    f"transport failure for {request_url}: {type(e).__name__}",
                    url=request_url, retryable=True,
                ) from e

            status = resp.status_code
            if self._retry.is_transient(status) and attempts < self._retry.max_attempts:
                self._sleep_backoff(attempts, status, resp.headers.get("retry-after"))
                current = resp.url  # retry against the same target
                last_transient = resp
                continue

            # Final status (2xx, permanent 4xx, or exhausted transients).
            if cache is not None:
                if 200 <= status < 300:
                    cache.store(request_url, status, resp.content,
                               mime=resp.mime, ttl=cache_ttl)
                elif cache_negative:
                    cache.store_negative(request_url, status=status,
                                         note=f"HTTP {status}")
            if last_transient is not None and status == 429:
                # A 429 that survived all bounded attempts is a rate-limit
                # error for the caller (exit 5 class), not a usable body.
                raise HttpError(
                    f"rate limited after {attempts} attempts: {request_url}",
                    url=request_url, status_code=status, retryable=True,
                )
            return resp

    def _walk_redirects(
        self,
        url: str,
        headers: dict[str, str],
        record_hops: list[str],
    ) -> HttpResponse:
        current = url
        req_headers = dict(headers)
        hops = 0
        while True:
            self._guard.validate_url(current)  # scheme + credentials + DNS
            resp = self._stream_get(current, req_headers)
            status = resp.status_code
            if not (300 <= status < 400) or not self._follow_redirects:
                return resp
            location = resp.headers.get("location")
            if not location:
                return resp  # redirect without a target: final, empty body
            hops += 1
            if hops > self._max_redirects:
                raise HttpError(
                    f"too many redirects (>{self._max_redirects}) from {url}",
                    url=url, retryable=False,
                )
            target = urljoin(current, location)
            record_hops.append(target)
            self._guard.validate_url(target)  # redirect targets re-validated
            if _netloc_of(current) != _netloc_of(target):
                for h in _AUTH_HEADERS:
                    req_headers.pop(h, None)  # never leak auth cross-origin
            current = target

    def _sleep_backoff(self, attempt: int, status: int | None, retry_after: str | None) -> None:
        delay = self._retry.delay_for(attempt, status, retry_after)
        if delay > 0:
            time.sleep(delay)


def _netloc_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


# Type alias used in the get() signature (avoids an import cycle with cache).
class RetrievalCacheType:
    def get(self, url: str) -> HttpResponse | None: ...
    def store(self, url: str, status: int, content: bytes, mime: str | None = None,
              ttl: float | None = None) -> None: ...
    def store_negative(self, url: str, status: int | None = None,
                       note: str | None = None, ttl: float | None = None) -> None: ...

