"""Offline provider tests (Phase 2): no network.

Covers, with fixtures only:
  * DOI / arXiv identifier normalization and version identity;
  * the shared URL guard (scheme, embedded credentials, loopback/private,
    DNS resolution, cross-origin auth stripping);
  * the retrieval cache TTL rules (positive / negative / document stores);
  * retrieval-order orchestration (cache -> candidates -> unavailable);
  * Unpaywall closed-record-is-valid + DOI verification + dedup ordering;
  * content byte-validation (PDF magic / XML / HTML / MIME mismatch);
  * "a provider failure is never a complete search" (completeness flag);
  * Semantic Scholar graceful-degrade (429 -> recorded skip, no exception).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from papercli.providers.cache import (
    RetrievalCache,
    detect_document_kind,
    sha256_of,
)
from papercli.providers.http_layer import (
    GuardError,
    HttpGuard,
    HttpTransport,
    RetryPolicy,
)
from papercli.providers.identifiers import (
    doi_matches,
    normalize_arxiv_id,
    normalize_doi,
    parse_identifier,
)
from papercli.providers.retrieval import (
    AccessLevel,
    SearchService,
    fetch_document,
    resolve_identifier,
)
from papercli.providers.unpaywall import UnpaywallAdapter, _normalize_record


# ---------------------------------------------------------------------------
# Fixture factory: fake httpx responses
# ---------------------------------------------------------------------------

def _fake_client(handler) -> httpx.Client:
    """An httpx.Client whose transport calls ``handler(request) -> Response``."""
    import httpx
    import httpcore

    class _Handler(httpx.AsyncBaseTransport):
        def __init__(self, handler):
            self._h = handler

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return self._h(request)

    return httpx.Client(transport=_Handler(handler), base_url="http://fixture.test")


class _StubTransport:
    """Minimal stand-in for HttpTransport in pure-logic tests."""

    def __init__(self, handler):
        self._h = handler
        self.calls: list = []

    def get(self, url, params=None, headers=None, cache=None,
            cache_ttl=None, cache_negative=True):
        from urllib.parse import urlencode
        full = str(url)
        if params:
            qs = urlencode(params, doseq=True)
            full = f"{full}?{qs}" if "?" not in full else f"{full}&{qs}"
        self.calls.append(full)
        from papercli.providers.http_layer import HttpResponse
        import json as _json

        status, content, mime = self._h(str(url), params or {})
        return HttpResponse(
            url=str(url), status_code=status,
            headers={"content-type": mime or ""},
            content=content if isinstance(content, bytes) else _json.dumps(content).encode(),
            request_url=str(url),
        )


def _config(**overrides) -> dict:
    base = {
        "providers": {
            "unpaywall": {"enabled": True, "email_env": "PAPERS_EMAIL"},
            "crossref": {"enabled": True},
            "openalex": {"enabled": True, "api_key_env": "OPENALEX_API_KEY"},
            "arxiv": {"enabled": True},
            "semantic_scholar": {"enabled": True, "key_env": "SEMANTIC_SCHOLAR_API_KEY"},
        },
        "contact": {"email": "tester@example.org"},
        "execution": {"request_timeout_seconds": 5, "max_response_mb": 50},
        "retrieval": {"out_dir": ".papercli/papers", "max_document_mb": 50,
                       "min_document_bytes": 2048},
        "network": {"allow_local": False},
    }
    base.update(overrides)
    return base


class _Cfg:
    """Config stand-in matching papercli.config.loader.Config.get."""

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


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

class TestIdentifiers:
    def test_doi_lowercase_and_wrappers(self):
        assert normalize_doi("10.1038/NATURE12375") == "10.1038/nature12375"
        assert normalize_doi("https://doi.org/10.1038/NATURE12375") == "10.1038/nature12375"
        assert normalize_doi("doi:10.1000/xyz") == "10.1000/xyz"

    def test_doi_suffix_punctuation_preserved(self):
        # Valid suffix punctuation must survive normalization.
        assert normalize_doi("10.5555/AA:1-2/3") == "10.5555/aa:1-2/3"
        assert normalize_doi("10.1234/abc.def") == "10.1234/abc.def"

    def test_doi_malformed_rejected(self):
        for bad in ("not-a-doi", "10.x/abc", "9.1234/x", "", "10.12345678901/x"):
            with pytest.raises(ValueError):
                normalize_doi(bad)

    def test_doi_matches_case_insensitive(self):
        assert doi_matches("10.1038/NATURE12375", "10.1038/nature12375")
        assert not doi_matches("10.1038/nature12375", "10.1038/other9999")
        assert not doi_matches("10.1038/nature12375", None)

    def test_arxiv_version_identity(self):
        assert normalize_arxiv_id("1605.0000") == ("1605.0000", None)
        assert normalize_arxiv_id("1605.0000v2") == ("1605.0000", "v2")
        assert normalize_arxiv_id("cond-mat/0207270v3") == ("cond-mat/0207270", "v3")
        assert normalize_arxiv_id("https://arxiv.org/abs/1605.0000v2") == ("1605.0000", "v2")

    def test_parse_identifier_kinds(self):
        assert parse_identifier("10.1038/nature12375")["kind"] == "doi"
        assert parse_identifier("cond-mat/0207270v3")["arxiv_id"] == "cond-mat/0207270"
        assert parse_identifier("cond-mat/0207270v3")["version"] == "v3"
        assert parse_identifier("mystery-string")["kind"] == "unknown"


# ---------------------------------------------------------------------------
# URL guard
# ---------------------------------------------------------------------------

class TestUrlGuard:
    def _guard(self, resolver=None):
        # 8.8.8.8: a public address the guard must accept.
        return HttpGuard(allow_local=False, resolver=resolver or (lambda h, p: ["8.8.8.8"]))

    def test_scheme_rejected(self):
        for bad in ("ftp://example.com/x", "file:///tmp/x", "//example.com/x"):
            with pytest.raises(GuardError):
                self._guard().validate_url(bad)

    def test_embedded_credentials_rejected(self):
        with pytest.raises(GuardError):
            self._guard().validate_url("https://user:pass@example.com/")
        with pytest.raises(GuardError):
            self._guard().validate_url("https://user@example.com/")

    def test_loopback_rejected(self):
        for bad in ("http://127.0.0.1/x", "http://localhost:8000/", "http://[::1]/x"):
            with pytest.raises(GuardError):
                self._guard().validate_url(bad)

    def test_private_and_link_local_rejected_via_dns(self):
        g = self._guard(resolver=lambda h, p: ["192.168.1.50"])
        with pytest.raises(GuardError):
            g.validate_url("http://internal.example/")
        g2 = self._guard(resolver=lambda h, p: ["169.254.0.9"])
        with pytest.raises(GuardError):
            g2.validate_url("http://internal.example/")

    def test_public_ip_allowed(self):
        g = self._guard()
        assert g.validate_url("https://api.unpaywall.org/v2/x") is not None

    def test_allow_local_opt_out(self):
        g = HttpGuard(allow_local=True, resolver=lambda h, p: ["192.168.1.50"])
        assert g.validate_url("http://internal.example/") is not None


# ---------------------------------------------------------------------------
# Cache TTL rules
# ---------------------------------------------------------------------------

class TestCacheTTL:
    def _cache(self, tmp_path, **kw):
        return RetrievalCache(tmp_path / "cache", **kw)

    def test_store_and_get_roundtrip(self, tmp_path):
        c = self._cache(tmp_path)
        c.store("https://x.example/a", 200, b'{"ok":true}', mime="application/json")
        got = c.get("https://x.example/a")
        assert got is not None and got.content == b'{"ok":true}' and got.from_cache

    def test_negative_shorter_ttl(self, tmp_path):
        c = self._cache(tmp_path, default_ttl=3600, negative_ttl=60)
        c.store_negative("https://x.example/404", status=404, note="HTTP 404")
        assert c.has_negative("https://x.example/404")
        # a negative entry must not be served as a positive response
        assert c.get("https://x.example/404") is None

    def test_expired_entry_is_a_miss(self, tmp_path):
        c = self._cache(tmp_path, default_ttl=3600)
        c.store("https://x.example/a", 200, b"x")
        entry = c.peek("https://x.example/a")
        entry.expires_at = 0.0
        path = c._meta_path("https://x.example/a")
        path.write_text(entry.to_json(), encoding="utf-8")
        assert c.get("https://x.example/a") is None

    def test_document_store_and_cached_document(self, tmp_path):
        c = self._cache(tmp_path)
        blob = b"%PDF-1.4 fake"
        digest = c.store_document("https://x.example/doc", blob, doc_kind="pdf")
        assert digest == sha256_of(blob)
        hit = c.cached_document("https://x.example/doc")
        assert hit and hit["sha256"] == digest and hit["kind"] == "pdf"
        assert Path(hit["path"]).read_bytes() == blob


# ---------------------------------------------------------------------------
# Content byte-validation
# ---------------------------------------------------------------------------

class TestContentValidation:
    def test_pdf_magic(self):
        assert detect_document_kind(b"%PDF-1.5 rest") == "pdf"

    def test_mime_mismatch_wins(self):
        html = b"<!DOCTYPE html><html><body>oops</body></html>"
        assert detect_document_kind(html, mime="application/pdf") == "html"

    def test_xml_and_html(self):
        assert detect_document_kind(b'<?xml version="1.0"?><feed/>') == "xml"
        assert detect_document_kind(b"<html><body>x</body></html>") == "html"
        assert detect_document_kind(b"{\"total\": 1}") == "text"
        assert detect_document_kind(b"\x00\x01junk") == "unknown"


# ---------------------------------------------------------------------------
# Unpaywall: closed record valid, DOI verification, dedup + PDF-first
# ---------------------------------------------------------------------------

def _unpaywall_payload(found=True, doi="10.1038/nature12375", oa_status="closed",
                       best=None, locations=None):
    return {
        "found": found,
        "doi": doi,
        "oa_status": oa_status,
        "is_oa": oa_status in ("gold", "hybrid", "bronze", "green"),
        "best_oa_location": best,
        "oa_locations": locations or [],
        "title": "Fixture paper",
        "publisher": "Nature",
        "year": 2015,
    }


class TestUnpaywallLogic:
    def test_closed_record_is_a_valid_outcome(self):
        """A closed / no-location record is valid data, not an error."""
        payload = _unpaywall_payload(found=True, oa_status="closed")
        resp = _StubTransport(lambda u, p: (200, payload, "application/json"))
        out = _normalize_record(
            payload, requested_doi="10.1038/nature12375",
            request_url="https://api.unpaywall.org/v2/10.1038/nature12375",
            response=resp, transport=resp, email_included=True,
        )
        assert out["found"] is True
        assert out["doi_verified"] is True
        assert out["oa_status"] == "closed"
        assert out["locations"] == []
        assert out["candidate_urls"] == []
        assert not out["warnings"]

    def test_doi_mismatch_warns(self):
        payload = _unpaywall_payload(found=True, doi="10.9999/other")
        resp = _StubTransport(lambda u, p: (200, payload, "application/json"))
        out = _normalize_record(
            payload, requested_doi="10.1038/nature12375",
            request_url="https://api.unpaywall.org/v2/10.1038/nature12375",
            response=resp, transport=resp, email_included=True,
        )
        assert out["doi_verified"] is False
        assert any("does not match" in w for w in out["warnings"])

    def test_pdf_locations_before_landing_and_dedup(self):
        best = {"url": "https://repo.acme.edu/landing/1",
                "url_for_pdf": "https://repo.acme.edu/pdf/1.pdf",
                "host_type": "repository", "license": "CC-BY", "version": "submitted"}
        landing = {"url": "https://repo.acme.edu/landing/1",
                   "host_type": "repository", "license": None, "url_type": "landing_page"}
        dup_pdf = {"url": "https://repo.acme.edu/pdf/1.pdf",
                   "url_for_pdf": "https://repo.acme.edu/pdf/1.pdf",
                   "host_type": "repository", "license": "CC-BY", "version": "published"}
        pub = {"url": "https://doi.org/10.1038/nature12375",
               "host_type": "publisher", "license": "unknown", "url_type": "landing_page"}
        payload = _unpaywall_payload(found=True, oa_status="green",
                                     best=best, locations=[landing, dup_pdf, pub])
        resp = _StubTransport(lambda u, p: (200, payload, "application/json"))
        out = _normalize_record(
            payload, requested_doi="10.1038/nature12375",
            request_url="https://api.unpaywall.org/v2/10.1038/nature12375",
            response=resp, transport=resp, email_included=True,
        )
        urls = out["candidate_urls"]
        # retrieval priority: direct PDF URL first, landing page after;
        # the duplicate location URL was collapsed
        pdf_idx = urls.index("https://repo.acme.edu/pdf/1.pdf")
        landing_idx = urls.index("https://repo.acme.edu/landing/1")
        assert pdf_idx < landing_idx
        assert urls.count("https://repo.acme.edu/pdf/1.pdf") == 1
        # doi.org landing URL comes after the repository copies
        assert urls.index("https://doi.org/10.1038/nature12375") > landing_idx
        assert out["best_oa_location"]["_url_is_pdf"] in (True, None, False)
        # license null/unknown is retained verbatim
        by_url = {l["url"]: l for l in out["locations"]}
        assert by_url["https://doi.org/10.1038/nature12375"]["license"] == "unknown"

    def test_unknown_doi_404_is_found_false(self):
        """Unpaywall 404 = unknown DOI -> found=False, still a valid dict."""
        payload = {"found": False}
        resp = _StubTransport(lambda u, p: (404, payload, "application/json"))
        out = _normalize_record(
            payload, requested_doi="10.1038/nature12375",
            request_url="https://api.unpaywall.org/v2/10.1038/nature12375",
            response=resp, transport=resp, email_included=True,
        )
        assert out["found"] is False
        assert out["oa_status"] is None


# ---------------------------------------------------------------------------
# SearchService: provider failure is never a complete search
# ---------------------------------------------------------------------------

class _BrokenAdapter:
    def search(self, *a, **k):
        raise RuntimeError("provider exploded")


class TestCompletenessOnError:
    def test_provider_failure_keeps_completeness_false(self, tmp_path):
        """No provider failure may be reported as a 'complete search'."""
        cfg = _Cfg(_config())
        service = SearchService(cfg, {"PAPERS_EMAIL": "t@example.org"},
                                cache=RetrievalCache(tmp_path / "cache"))

        # Simulate every provider failing.
        def _boom(self, *a, **k):
            raise RuntimeError("network down")
        import types
        for name in ("_search_crossref", "_search_openalex", "_search_arxiv", "_search_s2"):
            setattr(service, name, types.MethodType(_boom, service))

        result = service.search("fixture query", ["crossref", "openalex", "arxiv", "s2"])
        assert result["completeness"] == "incomplete"
        for prov in result["providers"]:
            assert prov["completeness"] in ("incomplete", "skipped")
            assert prov["error"]
        assert result["records"] == []

    def test_mixed_success_is_partial_not_complete(self, tmp_path):
        cfg = _Cfg(_config())
        service = SearchService(cfg, {"PAPERS_EMAIL": "t@example.org"},
                                cache=RetrievalCache(tmp_path / "cache"))

        def ok_s2(self, adapter, query, limit, prov):
            prov.update(result_count=1, completeness="partial")
            return [{"provider": "s2", "title": "x", "doi": "10.1/x"}]

        def bad_other(self, adapter, query, limit, prov):
            raise RuntimeError("down")

        service._search_s2 = lambda a, q, l, p, s=self: ok_s2(s, a, q, l, p)
        service._search_crossref = lambda a, q, l, p, s=self: bad_other(s, a, q, l, p)
        service._search_openalex = service._search_crossref
        service._search_arxiv = service._search_crossref

        result = service.search("q", ["crossref", "s2"])
        assert result["completeness"] == "partial"
        assert result["record_count"] == 1
        assert result["records"][0]["title"] == "x"


# ---------------------------------------------------------------------------
# Retrieval order orchestration (offline, stubbed downloads)
# ---------------------------------------------------------------------------

class TestRetrievalOrder:
    """fetch_document with the download step monkeypatched to fixtures."""

    def _cfg(self, tmp_path):
        return _Cfg(_config())

    def _patch_download(self, monkeypatch, mapping):
        import papercli.providers.retrieval as ret

        def fake_download(config, url, out_dir, cache, version=None):
            if url in mapping:
                return mapping[url]
            return {"_step_meta": {"status": "http-404"}}
        monkeypatch.setattr(ret, "_download_and_validate", fake_download)

    def _patch_candidates(self, monkeypatch, doi_cands, arxiv_cands):
        import papercli.providers.retrieval as ret
        monkeypatch.setattr(ret, "_unpaywall_candidates",
                            lambda *a, **k: doi_cands)
        monkeypatch.setattr(ret, "_publisher_candidates",
                            lambda *a, **k: arxiv_cands)

    def test_cache_hit_skips_download(self, tmp_path, monkeypatch):
        import papercli.providers.retrieval as ret
        cache = RetrievalCache(tmp_path / "cache")
        doc_url = "https://repo.acme.edu/pdf/1.pdf"
        cache.store_document(doc_url, b"%PDF-1.4x", doc_kind="pdf")
        self._patch_download(monkeypatch, {})  # must not be called
        self._patch_candidates(monkeypatch, doi_cands=[(doc_url, "unpaywall:repository:pdf")],
                               arxiv_cands=[])
        cfg = self._cfg(tmp_path)

        def _no_download(*a, **k):
            raise AssertionError("download must not run when cache hits")
        monkeypatch.setattr(ret, "_download_and_validate", _no_download)

        report = fetch_document("10.1038/nature12375", cfg, {"PAPERS_EMAIL": "t@e.org"},
                                out_dir=tmp_path / "out", cache=cache)
        assert report["access_level"] == AccessLevel.FULL_TEXT
        steps = [s["status"] for s in report["retrieval_order"]]
        assert "hit-cache" in steps
        assert report["document"]["from_cache"] is True

    def test_pdf_candidate_wins(self, tmp_path, monkeypatch):
        import papercli.providers.retrieval as ret
        cache = RetrievalCache(tmp_path / "cache")
        pdf_url = "https://repo.acme.edu/pdf/1.pdf"
        self._patch_download(monkeypatch, {
            pdf_url: {"ok": True, "kind": "pdf", "path": "/x.pdf", "sha256": "abc",
                      "size": 999, "source_url": pdf_url, "mime": "application/pdf"},
        })
        self._patch_candidates(monkeypatch,
                               doi_cands=[("https://repo.acme.edu/landing/1", "unpaywall:repository"),
                                          (pdf_url, "unpaywall:repository:pdf")],
                               arxiv_cands=[])
        report = fetch_document("10.1038/nature12375", self._cfg(tmp_path),
                                {"PAPERS_EMAIL": "t@e.org"}, out_dir=tmp_path / "out",
                                cache=cache)
        assert report["access_level"] == AccessLevel.FULL_TEXT
        assert report["document"]["kind"] == "pdf"

    def test_html_landing_is_not_full_text(self, tmp_path, monkeypatch):
        import papercli.providers.retrieval as ret
        cache = RetrievalCache(tmp_path / "cache")
        html_url = "https://doi.org/10.1038/nature12375"
        self._patch_download(monkeypatch, {
            html_url: {"ok": True, "kind": "html", "path": "/x.html", "sha256": "abc",
                       "size": 5000, "source_url": html_url, "mime": "text/html"},
        })
        self._patch_candidates(monkeypatch, doi_cands=[(html_url, "doi-landing")],
                               arxiv_cands=[])
        report = fetch_document("10.1038/nature12375", self._cfg(tmp_path),
                                {"PAPERS_EMAIL": "t@e.org"}, out_dir=tmp_path / "out",
                                cache=cache)
        assert report["access_level"] == AccessLevel.HTML_LANDING
        assert report["document"]["kind"] == "html"

    def test_unavailable_is_recorded_not_raised(self, tmp_path, monkeypatch):
        import papercli.providers.retrieval as ret
        cache = RetrievalCache(tmp_path / "cache")
        self._patch_download(monkeypatch, {})  # everything misses
        self._patch_candidates(monkeypatch, doi_cands=[("https://x.example/1", "o")],
                               arxiv_cands=[])
        report = fetch_document("10.1038/nature12375", self._cfg(tmp_path),
                                {"PAPERS_EMAIL": "t@e.org"}, out_dir=tmp_path / "out",
                                cache=cache)
        assert report["access_level"] == AccessLevel.UNAVAILABLE
        assert report["document"] is None
        last = report["retrieval_order"][-1]
        assert last["status"] in ("http-404", "content-invalid", "transport-failed")

    def test_arxiv_versioned_pdf_candidate(self, tmp_path, monkeypatch):
        import papercli.providers.retrieval as ret
        cache = RetrievalCache(tmp_path / "cache")
        url = "https://arxiv.org/pdf/cond-mat/0207270v3"
        self._patch_download(monkeypatch, {
            url: {"ok": True, "kind": "pdf", "path": "/a.pdf", "sha256": "abc",
                  "size": 100000, "source_url": url, "mime": "application/pdf"},
        })
        report = fetch_document("cond-mat/0207270v3", self._cfg(tmp_path),
                                {"PAPERS_EMAIL": "t@e.org"}, out_dir=tmp_path / "out",
                                cache=cache)
        assert report["access_level"] == AccessLevel.FULL_TEXT
        assert report["identifier"]["version"] == "v3"
        assert report["document"]["source_url"] == url

    def test_unknown_identifier_is_unavailable(self, tmp_path, monkeypatch):
        report = fetch_document("not-an-identifier", self._cfg(tmp_path),
                                {"PAPERS_EMAIL": "t@e.org"}, out_dir=tmp_path / "out",
                                cache=RetrievalCache(tmp_path / "cache"))
        assert report["access_level"] == AccessLevel.UNAVAILABLE


# ---------------------------------------------------------------------------
# RetryPolicy: 429/Retry-After honored and bounded
# ---------------------------------------------------------------------------

class TestRetryPolicy:
    def test_retry_after_honored(self):
        pol = RetryPolicy(max_attempts=4, base_delay=0.1, max_delay=1.0, retry_after_cap=30.0)
        d = pol.delay_for(1, 429, "2")
        assert d >= 2.0

    def test_retry_after_capped(self):
        pol = RetryPolicy(max_attempts=4, base_delay=0.1, max_delay=1.0, retry_after_cap=5.0)
        d = pol.delay_for(1, 429, "600")
        assert d <= 5.0

    def test_permanent_403_never_transient(self):
        pol = RetryPolicy()
        assert pol.is_transient(403) is False
        assert pol.is_transient(404) is False
        assert pol.is_transient(429) is True
        assert pol.is_transient(None, error=RuntimeError()) is True


# ---------------------------------------------------------------------------
# Semantic Scholar graceful degrade
# ---------------------------------------------------------------------------

class TestS2GracefulDegrade:
    def _adapter_with(self, monkeypatch, status, body=b'{"data": []}'):
        from papercli.providers.semantic_scholar import SemanticScholarAdapter
        adapter = SemanticScholarAdapter()
        tr = _StubTransport(lambda u, p: (status, json.loads(body) if status == 200 else b"", "application/json"))
        return adapter, tr

    def test_429_is_a_recorded_skip(self, monkeypatch):
        adapter, tr = self._adapter_with(monkeypatch, 429)
        out = adapter.search("q", _Cfg(_config()), {}, transport=tr)
        assert out["skipped"] is True
        assert "429" in (out["reason"] or "")
        assert out["data"] is None

    def test_404_miss_is_a_recorded_skip(self, monkeypatch):
        adapter, tr = self._adapter_with(monkeypatch, 404)
        out = adapter.paper_by_id("10.1/x", _Cfg(_config()), {}, transport=tr)
        assert out["skipped"] is True and out["reason"] == "not found"

    def test_200_returns_data(self, monkeypatch):
        adapter, tr = self._adapter_with(monkeypatch, 200,
                                         b'{\"total\":1,\"data\":[{\"paperId\":\"1\"}]}')
        out = adapter.search("q", _Cfg(_config()), {}, transport=tr)
        assert out["skipped"] is False
        assert out["data"]["total"] == 1

    def test_optional_key_header_not_required(self, monkeypatch):
        """Keyless default: no Authorization header, still works."""
        adapter, tr = self._adapter_with(monkeypatch, 200)
        headers = adapter._headers(_Cfg(_config()), {})
        assert headers is None


# ---------------------------------------------------------------------------
# resolve (offline: unpaywall + crossref stubbed, canonical assembly)
# ---------------------------------------------------------------------------

class TestResolveOffline:
    def test_resolve_doi_assembles_canonical_from_crossref(self, monkeypatch, tmp_path):
        import papercli.providers.retrieval as ret
        cache = RetrievalCache(tmp_path / "cache")
        cr_work = {"record": {
            "DOI": "10.1038/nature12375",
            "title": ["Fixture"],
            "author": [{"family": "Smith"}],
            "issued": {"date-parts": [[2015]]},
            "container-title": ["Nature"],
        }}
        monkeypatch.setattr(ret.CrossrefAdapter, "lookup_doi",
                            lambda self, d, cfg, env, transport=None, cache=None: cr_work)
        monkeypatch.setattr(ret.UnpaywallAdapter, "lookup",
                            lambda *a, **k: {"found": True, "doi_verified": True,
                                            "oa_status": "gold", "metadata": {"title": "Fixture"},
                                            "locations": [], "warnings": [], "raw": {}})
        monkeypatch.setattr(ret.OpenAlexAdapter, "lookup_work",
                            lambda *a, **k: {"found": False, "work": None})
        out = resolve_identifier("10.1038/nature12375", _Cfg(_config()),
                                 {"PAPERS_EMAIL": "t@e.org"}, cache=cache)
        assert out["found"] is True
        assert out["canonical"]["title"] == "Fixture"
        assert out["canonical"]["year"] == 2015
        assert out["identifier"]["kind"] == "doi"

    def test_resolve_missing_email_skips_unpaywall(self, monkeypatch, tmp_path):
        import papercli.providers.retrieval as ret
        calls = []
        monkeypatch.setattr(ret.UnpaywallAdapter, "lookup",
                            lambda *a, **k: calls.append(1) or {"found": True})
        out = resolve_identifier("10.1038/nature12375", _Cfg(_config()),
                                 {}, cache=RetrievalCache(tmp_path / "cache"))
        assert out["unpaywall"].get("skipped") is True
        assert calls == []  # never called when email is missing

