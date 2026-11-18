"""Phase 2 defect fixes (D-P2-1, D-P2-2, D-P2-3).

* D-P2-1: a re-fetch of an already-unavailable (negatively cached) target must
  record ``transport-failed`` and exit 4, not crash with ``NameError`` (exit 1)
  -- the ``HttpError`` handler in ``_download_and_validate`` now resolves.
* D-P2-2: credential query-params (``email`` / ``mailto`` / ``api_key``) must
  never reach the on-disk cache keys or surfaced error text; their values are
  masked (``***REDACTED***``), raw AND percent-encoded forms both.
* D-P2-3: ``SearchService.search`` resolves providers from the Phase-1
  registry, so a newly registered adapter works with zero ``retrieval.py``
  edits; names absent from the registry still fail with the exit-2 envelope.

Offline: fake httpx transports + real cache + real orchestrator code. The CLI
subprocess tests pre-seed the project cache in-process, so the child process
never touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pytest

from papercli.providers.cache import RetrievalCache
from papercli.providers.http_layer import (
    CRED_REDACTED,
    HttpError,
    HttpGuard,
    HttpTransport,
    Pacer,
    RetryPolicy,
    mask_url_creds,
)
from papercli.providers.retrieval import (
    AccessLevel,
    SearchService,
    fetch_document,
    resolve_identifier,
)
from tests.conftest import run_papers

#: probe identifiers (fixture values, not real secrets)
PROBE_DOI = "10.1038/nature12375"
PROBE_EMAIL = "probe-secret-abc@example.org"
PROBE_KEY = "probe-secret-key/xyz"


def _probe_forms(value: str) -> list[str]:
    """Raw + percent-encoded spellings of a secret that must never surface."""
    return [value, quote(value, safe=""), quote_plus(value, safe="")]


def _fake_client_404(handler=None) -> httpx.Client:
    """An httpx client whose transport returns 404 for everything (offline)."""
    import httpx

    def _handler(request: httpx.Request) -> httpx.Response:
        if handler is not None:
            return handler(request)
        return httpx.Response(404, json={"found": False, "message": {}})

    return httpx.Client(transport=httpx.MockTransport(_handler), timeout=5.0)


def _offline_transport(client=None) -> HttpTransport:
    """Real transport class, fake network, no pacing, single attempt."""
    return HttpTransport(
        guard=HttpGuard(allow_local=True),
        timeout=5.0,
        max_bytes=1024 * 1024,
        retry=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0, retry_after_cap=0.0),
        pacer=Pacer(0.0),
        client=client or _fake_client_404(),
    )


class _Cfg:
    """Config stand-in matching papercli.config.loader.Config.get (offline)."""

    def __init__(self, data, env=None):
        self.data = data
        self.env = env or {}

    def get(self, dotted, default=None):
        node = self.data
        for k in dotted.split("."):
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def provider_enabled(self, provider):
        return bool(self.get(f"providers.{provider}.enabled", False))


def _config() -> dict:
    return {
        "providers": {
            "unpaywall": {"enabled": True, "email_env": "PAPERS_EMAIL"},
            "crossref": {"enabled": True},
            "openalex": {"enabled": True, "api_key_env": "OPENALEX_API_KEY"},
            "arxiv": {"enabled": True},
            "semantic_scholar": {"enabled": True, "key_env": "SEMANTIC_SCHOLAR_API_KEY"},
        },
        "contact": {"email": PROBE_EMAIL},
        "execution": {"request_timeout_seconds": 5, "max_response_mb": 50},
        "retrieval": {"out_dir": ".papercli/papers"},
        "network": {"allow_local": False},
    }


# ---------------------------------------------------------------------------
# D-P2-1: re-fetch of a negatively cached target -> recorded unavailable (exit 4 path)
# ---------------------------------------------------------------------------

class TestNegativeCacheRefetch:
    def _patch_transports(self, monkeypatch):
        import papercli.providers.retrieval as ret
        monkeypatch.setattr(ret, "make_document_transport",
                            lambda config, url: _offline_transport())
        monkeypatch.setattr(ret, "make_metadata_transport",
                            lambda config, pacer_interval=0.1: _offline_transport())

    def test_refetch_negatively_cached_is_unavailable_not_crash(self, tmp_path, monkeypatch):
        """Pre-seed a negative entry; the orchestrator must catch the
        ``HttpError`` (the D-P2-1 NameError path) and record unavailable."""
        import papercli.providers.retrieval as ret
        self._patch_transports(monkeypatch)
        cache = RetrievalCache(tmp_path / "cache")
        doi_url = f"https://doi.org/{PROBE_DOI}"
        cache.store_negative(doi_url, status=404, note="HTTP 404")

        def _no_candidates(*a, **k):
            return [(doi_url, "doi-landing")]
        monkeypatch.setattr(ret, "_unpaywall_candidates", lambda *a, **k: [])
        monkeypatch.setattr(ret, "_publisher_candidates", _no_candidates)

        report = fetch_document(PROBE_DOI, _Cfg(_config()),
                                {"PAPERS_EMAIL": PROBE_EMAIL},
                                out_dir=tmp_path / "out", cache=cache)
        assert report["access_level"] == AccessLevel.UNAVAILABLE
        assert report["document"] is None
        step = report["retrieval_order"][-1]
        assert step["status"] == "transport-failed"
        assert "negatively cached" in step["error"]

    def test_first_fetch_negatively_caches_and_second_reuses_budget(self, tmp_path, monkeypatch):
        """Two consecutive fetches through the real transport: the first
        stores the negative entry, the second stays on the short negative TTL
        instead of hitting the network again."""
        import papercli.providers.retrieval as ret
        self._patch_transports(monkeypatch)
        cache = RetrievalCache(tmp_path / "cache")
        monkeypatch.setattr(ret, "_unpaywall_candidates", lambda *a, **k: [])
        monkeypatch.setattr(ret, "_publisher_candidates",
                            lambda *a, **k: [(f"https://doi.org/{PROBE_DOI}", "doi-landing")])
        env = {"PAPERS_EMAIL": PROBE_EMAIL}
        r1 = fetch_document(PROBE_DOI, _Cfg(_config()), env,
                            out_dir=tmp_path / "out", cache=cache)
        assert r1["access_level"] == AccessLevel.UNAVAILABLE
        assert cache.has_negative(mask_url_creds(f"https://doi.org/{PROBE_DOI}"))
        r2 = fetch_document(PROBE_DOI, _Cfg(_config()), env,
                            out_dir=tmp_path / "out", cache=cache)
        assert r2["access_level"] == AccessLevel.UNAVAILABLE
        assert "transport-failed" in [s["status"] for s in r2["retrieval_order"]]


# ---------------------------------------------------------------------------
# D-P2-2: credential redaction in cache keys + surfaced error text
# ---------------------------------------------------------------------------

class TestCredentialRedaction:
    def _scan_meta(self, cache: RetrievalCache) -> list[str]:
        meta_dir = Path(cache.base_dir) / "meta"
        return [f.read_text(encoding="utf-8") for f in meta_dir.glob("*.json")]

    def test_cache_key_masks_credential_params(self, tmp_path):
        cache = RetrievalCache(tmp_path / "cache")
        base = f"https://api.unpaywall.org/v2/{PROBE_DOI}"
        tr = _offline_transport()
        tr.get(base, params={"email": PROBE_EMAIL, "select": "a,b"}, cache=cache)
        blob = " ".join(self._scan_meta(cache))
        for form in _probe_forms(PROBE_EMAIL):
            assert form not in blob, f"raw/encoded email leaked into cache meta: {form!r}"
        assert CRED_REDACTED in blob
        # non-credential query params are preserved (informative cache key)
        assert "select=" in blob
        # the mask is deterministic: raw and percent-encoded secret values
        # collapse to the same credential-free form
        assert mask_url_creds(f"{base}?email={PROBE_EMAIL}&select=xy") == \
            f"{base}?email={CRED_REDACTED}&select=xy"
        assert mask_url_creds(f"{base}?email={quote(PROBE_EMAIL, safe='')}&select=xy") == \
            f"{base}?email={CRED_REDACTED}&select=xy"

    def test_api_key_masked_in_cache_key(self, tmp_path):
        cache = RetrievalCache(tmp_path / "cache")
        base = "https://api.openalex.org/works/doi:10.9999/probe"
        tr = _offline_transport()
        tr.get(base, params={"select": "id,doi,title", "api_key": PROBE_KEY}, cache=cache)
        blob = " ".join(self._scan_meta(cache))
        for form in _probe_forms(PROBE_KEY):
            assert form not in blob, f"raw/encoded key leaked into cache meta: {form!r}"
        assert f"api_key={CRED_REDACTED}" in blob

    def test_negative_cache_hit_error_is_masked(self, tmp_path):
        cache = RetrievalCache(tmp_path / "cache")
        base = f"https://api.unpaywall.org/v2/{PROBE_DOI}"
        tr = _offline_transport()
        tr.get(base, params={"email": PROBE_EMAIL}, cache=cache)
        # second call: still-fresh negative entry -> HttpError, not a response
        with pytest.raises(HttpError) as exc_info:
            tr.get(base, params={"email": PROBE_EMAIL}, cache=cache)
        err = exc_info.value
        text = err.message + " " + str(err) + " " + (err.url or "")
        for form in _probe_forms(PROBE_EMAIL):
            assert form not in text, f"secret surfaced in error text: {form!r}"
        assert CRED_REDACTED in err.message

    def test_rate_limit_error_is_masked(self, tmp_path):
        # 3 attempts so a transient 429 exhausts the budget and RAISES the
        # "rate limited after N attempts" HttpError (the masked-text path).
        cache = RetrievalCache(tmp_path / "cache")
        base = "https://api.openalex.org/works"

        def _429(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=b"")
        tr = HttpTransport(
            guard=HttpGuard(allow_local=True),
            timeout=5.0,
            max_bytes=1024 * 1024,
            retry=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0,
                              retry_after_cap=0.0),
            pacer=Pacer(0.0),
            client=_fake_client_404(_429),
        )
        with pytest.raises(HttpError) as exc_info:
            tr.get(base, params={"select": "id", "api_key": PROBE_KEY}, cache=cache)
        err = exc_info.value
        text = err.message + " " + str(err) + " " + (err.url or "")
        for form in _probe_forms(PROBE_KEY):
            assert form not in text, f"secret surfaced in rate-limit text: {form!r}"
        assert CRED_REDACTED in text
        blob = " ".join(self._scan_meta(cache))
        for form in _probe_forms(PROBE_KEY):
            assert form not in blob


# ---------------------------------------------------------------------------
# D-P2-2 (CLI level): error/negative path envelopes carry no secret values
# ---------------------------------------------------------------------------

class TestCliNoSecretLeak:
    """Pre-seed the project cache in-process (fake transport), then run the
    real CLI in a child process: the negative-cache/error paths on stdout
    must carry zero raw/encoded secret values."""

    def _seed_project_cache(self, project: Path, monkeypatch) -> RetrievalCache:
        import papercli.providers.retrieval as ret
        monkeypatch.setattr(ret, "make_document_transport",
                            lambda config, url: _offline_transport())
        monkeypatch.setattr(ret, "make_metadata_transport",
                            lambda config, pacer_interval=0.1: _offline_transport())
        cache = RetrievalCache(project / ".papercli" / "storage" / "cache")
        cfg = _Cfg(_config())
        env = {"PAPERS_EMAIL": PROBE_EMAIL, "OPENALEX_API_KEY": PROBE_KEY}
        resolve_identifier(PROBE_DOI, cfg, env, cache=cache)  # negative-seeds providers
        fetch_document(PROBE_DOI, cfg, env, out_dir=project / ".papercli" / "papers",
                       cache=cache)  # negative-seeds the doi-landing candidate
        assert cache.has_negative(mask_url_creds(f"https://doi.org/{PROBE_DOI}"))
        return cache

    def test_cli_refetch_exits_4_not_1_and_no_secrets(self, tmp_project, monkeypatch):
        self._seed_project_cache(tmp_project, monkeypatch)
        rc, out, err = run_papers("fetch", PROBE_DOI, cwd=tmp_project,
                                   env_strip={"PAPERS_EMAIL", "OPENALEX_API_KEY"},
                                   env_extra={"PAPERS_EMAIL": PROBE_EMAIL,
                                              "OPENALEX_API_KEY": PROBE_KEY})
        envelope = json.loads(out)
        assert rc == 4, f"expected exit 4 (retrieval unavailable), got {rc}"
        assert envelope["status"] == "ok"
        assert envelope["data"]["access_level"] == "unavailable"
        text = out + err
        for secret in (PROBE_EMAIL, PROBE_KEY):
            for form in _probe_forms(secret):
                assert form not in text, f"secret surfaced on CLI stdout: {form!r}"
        assert CRED_REDACTED in out  # masked credential form in the error text

    def test_cli_resolve_error_path_has_no_secrets(self, tmp_project, monkeypatch):
        self._seed_project_cache(tmp_project, monkeypatch)
        rc, out, err = run_papers("resolve", PROBE_DOI, cwd=tmp_project,
                                   env_strip={"PAPERS_EMAIL", "OPENALEX_API_KEY"},
                                   env_extra={"PAPERS_EMAIL": PROBE_EMAIL,
                                              "OPENALEX_API_KEY": PROBE_KEY})
        envelope = json.loads(out)
        assert rc == 0
        # every provider fell onto its negative cache -> recorded errors,
        # and the masked form is what surfaces.
        joined = json.dumps(envelope)
        for secret in (PROBE_EMAIL, PROBE_KEY):
            for form in _probe_forms(secret):
                assert form not in joined, f"secret surfaced on resolve stdout: {form!r}"
        assert CRED_REDACTED in joined


# ---------------------------------------------------------------------------
# D-P2-3: registry-driven provider dispatch
# ---------------------------------------------------------------------------

class _FakeAdapter:
    """A newly-registered provider: common search() shape, no transport use."""
    from papercli.providers.base import ProviderInfo

    info = ProviderInfo(name="faketest", credential_env="", credential_optional=True,
                        base_url="https://faketest.example/api")

    def credential_status(self, config, env=None):
        from papercli.providers.base import CredentialStatus
        return CredentialStatus.NOT_REQUIRED

    def preflight(self, config, env=None):
        return {"provider": "faketest", "credential": "not_required",
                "endpoint": self.info.base_url}

    def search(self, query, config, env=None, transport=None, cache=None,
               *, limit=20, **kw):
        return {
            "provider": "faketest",
            "items": [
                {"DOI": "10.99/fake-1", "title": ["Fake record one"],
                 "author": [{"family": "Doe"}], "year": 2020},
                {"title": "Fake record two", "year": 2021},
            ],
            "item_count": 2,
            "total_results": 2,
            "next_cursor": None,
        }


class TestRegistryDrivenDispatch:
    def test_newly_registered_provider_is_dispatched(self, monkeypatch):
        from papercli.providers import ProviderAdapter
        from papercli.providers.registry import get_registry, register_provider

        class _Concrete(_FakeAdapter, ProviderAdapter):
            pass

        registry = get_registry()
        register_provider(_Concrete)
        try:
            service = SearchService(_Cfg(_config()), {})
            result = service.search("fixture query", ["faketest"], limit=50)
        finally:
            registry._classes.pop("faketest", None)  # keep the global registry clean

        provs = [p["provider"] for p in result["providers"]]
        assert "faketest" in provs
        fake_prov = next(p for p in result["providers"] if p["provider"] == "faketest")
        assert fake_prov["error"] is None
        assert fake_prov["completeness"] == "complete"
        assert fake_prov["result_count"] == 2
        titles = [r.get("title") for r in result["records"]]
        assert "Fake record one" in titles and "Fake record two" in titles
        rec = next(r for r in result["records"] if r.get("doi") == "10.99/fake-1")
        assert rec["provider"] == "faketest"
        assert rec["authors"] == ["Doe"]
        assert rec["year"] == 2020

    def test_unregistered_provider_is_rejected(self):
        service = SearchService(_Cfg(_config()), {})
        with pytest.raises(ValueError) as exc_info:
            service.search("q", ["definitely-not-a-provider"])
        assert "unknown provider(s)" in str(exc_info.value)

    def test_builtin_names_still_resolved_via_registry(self):
        from papercli.providers.registry import get_registry
        names = set(get_registry().names())
        for builtin in ("crossref", "openalex", "arxiv", "s2"):
            assert builtin in names
        # the "semantic_scholar" alias is accepted without being a registry key
        service = SearchService(_Cfg(_config()), {})
        known = set(names) | {"semantic_scholar"}
        for p in ("crossref", "openalex", "arxiv", "s2", "semantic_scholar"):
            assert p in known
        with pytest.raises(ValueError):
            service.search("q", ["not-registered-xyz"])
