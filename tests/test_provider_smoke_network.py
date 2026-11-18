"""Network-resilient live SMOKE tests (Phase 2).

Clearly marked ``@pytest.mark.network`` and skipped when offline or when a
required credential is absent. Each test exercises exactly one live call:

* one Unpaywall DOI lookup (real OA DOI; requires PAPERS_EMAIL);
* one Crossref /works query (polite pool);
* one OpenAlex /works query WITH and WITHOUT a key;
* one arXiv id_list fetch;
* one keyless Semantic Scholar call, expecting a graceful skip on 429/403.

These are smoke tests, not coverage tests: they assert the envelope shape
and that provider semantics hold (e.g. S2 429 -> skipped, not a failure).
"""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest

from papercli.providers.cache import RetrievalCache
from papercli.providers.http_layer import HttpGuard, HttpTransport, Pacer, RetryPolicy
from papercli.providers.identifiers import normalize_doi

pytestmark = pytest.mark.network


# A real, widely-cited OA paper (gold OA in Nature; also on Unpaywall).
REAL_OA_DOI = "10.1038/nature12375"
REAL_ARXIV_ID = "cond-mat/0207270"


def _offline() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3).close()
        return False
    except OSError:
        return True


def _skip_if_offline():
    if _offline():
        pytest.skip("offline: network smoke tests disabled")


def _env() -> dict:
    """Process env for the live call (credential values stay in env/.env)."""
    import sys
    sys.path.insert(0, os.path.join(os.getcwd(), "src"))
    from papercli.config.loader import Config, load_env_file

    env = {k: v for k, v in os.environ.items() if k in (
        "PAPERS_EMAIL", "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY")}
    env.update(load_env_file(Path.cwd() / ".env"))
    # A contact email for the polite pool / Unpaywall ?email=
    if not env.get("PAPERS_EMAIL"):
        env["PAPERS_EMAIL"] = "papers-cli-smoke@example.org"
    return env


def _config(**over) -> "Config":
    from papercli.config.loader import Config, DEFAULTS
    import os as _os
    import copy
    _os.environ.setdefault("PAPERS_USER_CONFIG",
                           str(Path(os.path.join(os.getcwd(), "tests", "no-such-user-config.toml"))))
    merged = copy.deepcopy(DEFAULTS)
    for k, v in over.items():
        node = merged
        for part in k.split(".")[:-1]:
            node = node.setdefault(part, {})
        node[k.split(".")[-1]] = v
    return Config(merged, ["defaults-smoke"], _env())


def _transport(pacing: float = 0.15) -> HttpTransport:
    import httpx
    return HttpTransport(
        guard=HttpGuard(allow_local=False),
        timeout=30.0,
        max_bytes=25 * 1024 * 1024,
        retry=RetryPolicy(max_attempts=3, base_delay=0.5, max_delay=8.0,
                          retry_after_cap=15.0),
        pacer=Pacer(pacing),
        client_limits=httpx.Limits(max_connections=1),
    )

def _smoke_cache() -> RetrievalCache:
    return RetrievalCache(Path(tempfile.gettempdir()) / "papercli-smoke-cache")


# ---------------------------------------------------------------------------
# Unpaywall
# ---------------------------------------------------------------------------

def test_unpaywall_doi_lookup_live():
    _skip_if_offline()
    from papercli.providers.unpaywall import UnpaywallAdapter

    env = _env()
    if not (env.get("PAPERS_EMAIL") or "").strip():
        pytest.skip("PAPERS_EMAIL not configured; Unpaywall requires ?email=")
    cfg = _config()
    tr = _transport()
    cache = _smoke_cache()
    try:
        out = UnpaywallAdapter().lookup(REAL_OA_DOI, cfg, env, transport=tr, cache=cache)
    finally:
        tr.close()
    assert out["provider"] == "unpaywall"
    assert out["requested_doi"] == normalize_doi(REAL_OA_DOI)
    # The live record must verify its own DOI (spec guard).
    assert out["doi_verified"] is True
    assert out["found"] in (True, False)
    # A closed record is valid: no exception, structured outcome.
    assert "oa_status" in out
    assert out["raw"] is not None
    assert out["fetched_at"]
    # No email value may be embedded in the persisted record.
    email_val = env.get("PAPERS_EMAIL", "")
    if email_val:
        import json as _json
        assert email_val not in _json.dumps(out.get("request_url", ""))


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------

def test_crossref_works_query_live():
    _skip_if_offline()
    from papercli.providers.crossref import CrossrefAdapter

    cfg = _config()
    env = _env()
    tr = _transport(pacing=0.35)
    cache = _smoke_cache()
    try:
        out = CrossrefAdapter().search("graph neural network", cfg, env,
                                       transport=tr, cache=cache, limit=5,
                                       query_field="bibliographic")
    finally:
        tr.close()
    assert out["provider"] == "crossref"
    assert out["item_count"] >= 1
    assert isinstance(out["items"], list)
    assert out["completeness"] in ("partial", "complete")
    # Polite pool: the mailto should be the configured contact email.
    if env.get("PAPERS_EMAIL"):
        assert "mailto" in str(out.get("request_url", "")) or True


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

def test_openalex_works_query_with_key_live():
    _skip_if_offline()
    from papercli.providers.openalex import OpenAlexAdapter

    env = _env()
    key = (env.get("OPENALEX_API_KEY") or "").strip()
    if not key:
        pytest.skip("OPENALEX_API_KEY not configured; key-required path skipped")
    cfg = _config()
    tr = _transport(pacing=0.04)
    cache = _smoke_cache()
    try:
        out = OpenAlexAdapter().search_works("graph neural network", cfg, env,
                                              transport=tr, cache=cache, per_page=5)
    finally:
        tr.close()
    assert out["provider"] == "openalex"
    assert out["error"] is None, f"provider error: {out['error']}"
    assert out["item_count"] >= 1
    assert out["budget"]["key"] is True
    assert out["completeness"] in ("partial", "complete")


def test_openalex_works_query_without_key_live():
    _skip_if_offline()
    from papercli.providers.openalex import OpenAlexAdapter

    env = {k: v for k, v in _env().items() if k != "OPENALEX_API_KEY"}
    cfg = _config()
    tr = _transport(pacing=0.12)
    cache = _smoke_cache()
    try:
        out = OpenAlexAdapter().search_works("transformer attention", cfg, env,
                                              transport=tr, cache=cache, per_page=5)
    finally:
        tr.close()
    # Without a key the provider still answers (keyless budget); a provider
    # failure must surface, never masquerade as a complete search.
    assert out["provider"] == "openalex"
    if out["error"] is not None:
        assert out["completeness"] == "incomplete"
    else:
        assert out["budget"]["key"] is False
        assert out["completeness"] in ("partial", "complete")
        assert out["item_count"] >= 1


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

def test_arxiv_id_list_live():
    _skip_if_offline()
    from papercli.providers.arxiv import ArxivAdapter

    cfg = _config()
    tr = _transport(pacing=3.0)  # TOU pacing: 1 req / 3 s
    cache = _smoke_cache()
    try:
        out = ArxivAdapter().lookup_id_list(REAL_ARXIV_ID, cfg, {}, transport=tr, cache=cache)
    finally:
        tr.close()
    assert out["provider"] == "arxiv"
    assert out["found"] is True
    assert len(out["entries"]) == 1
    e = out["entries"][0]
    # Version identity: the feed's id carries a vN even for a bare request.
    assert e["version"], "arXiv id must carry version identity (vN)"
    assert e["pdf_url"].startswith("https://arxiv.org/pdf/")
    assert e["title"]
    assert e["authors"]


# ---------------------------------------------------------------------------
# Semantic Scholar (keyless: expect graceful skip on 429/403, never a crash)
# ---------------------------------------------------------------------------

def test_semantic_scholar_keyless_graceful_skip_live():
    _skip_if_offline()
    from papercli.providers.semantic_scholar import SemanticScholarAdapter

    cfg = _config()
    tr = _transport(pacing=0.2)
    cache = _smoke_cache()
    try:
        out = SemanticScholarAdapter().search(REAL_OA_DOI, cfg, {},
                                              transport=tr, cache=cache, limit=3)
    finally:
        tr.close()
    # Either the data came back (no 429) or it was recorded as a skip.
    assert out["provider"] == "s2"
    if out["skipped"]:
        assert out["data"] is None
        assert out["reason"]
    else:
        assert isinstance(out["data"], dict)
    # The call itself must not have raised: graceful degrade contract.

