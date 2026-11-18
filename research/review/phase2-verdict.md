# Phase 2 (Providers) — OPUS Reviewer Verdict

Repo: `D:/prime-agent-harness/paperCLI` · branch `main` · Phase 2 = 4 commits
(`b960913`, `4f162af`, `586c03b`, `82e1326`) on top of the Phase-1 defect-fix
HEAD `3d00769`, ending at `82e1326`.

Every checklist item below was verified by execution (fresh venv install, live
CLI runs, full pytest, cache wipes, redirect/guard simulation, git-history and
working-tree greps, percent-encoded-form secret checks), not by reading claims.
All secret values are masked or shown only by length/identifier — no raw value
is printed.

## VERDICT: **FAIL**

Zero hard blockers to the core capability, and 93/93 tests are green in a fresh
venv with all 6 live smoke tests actually hitting real providers. However the
phase does **not** meet its own "no secret leak" gate: credentials are embedded
in the on-disk retrieval-cache keys and surface in CLI stdout on the
error/negative-cache path (major defect D-P2-2). A separate major defect,
D-P2-1 (a missing `HttpError` import), turns the spec-mandated exit-4
"retrieval unavailable" into an exit-1 internal crash on any re-fetch of an
already-unavailable DOI. Both must be fixed before this phase can be called
done.

## Checklist

| # | Item | Status | Evidence |
|---|------|--------|----------|
| 1 | Fresh venv + CLI envelope/exit codes | verified-ok | `uv venv` (CPython 3.12.8) + `uv pip install -e .` clean; `papers 0.1.0`. `search "quantum error correction" --providers crossref,openalex,arxiv --limit 5` → exit 0, exactly one parseable envelope on stdout, 0 stderr, `completeness=partial`, 15 records (5/provider). `resolve 10.1038/nature12375` → exit 0, one envelope, canonical Nature/2013 record correct, `unpaywall.found=true doi_verified=true oa_status=closed`. `fetch` → exit 4 (unavailable PLOS/PNAS), exit 0 (arXiv `cond-mat/0207270` → `full_text`). Caveat: re-fetch of an already-cached-unavailable DOI returns exit 1, not 4 — see D-P2-1. |
| 2 | Full pytest in fresh venv | verified-ok | `python -m pytest tests` → **93 passed, 0 failed, 0 skipped** (11.1s). The 6 `@pytest.mark.network` smoke tests are **not** skipped: after wiping the smoke cache they re-hit the network in 1.2s and repopulated 6 real entries (five `status:200` bodies of genuine size, one `status:429` negative for S2). |
| 3 | Adapters match live-doc facts | verified-ok (with D-P2-2 caveat) | **Unpaywall** `unpaywall.py`: email REQUIRED via `params={"email": email}` (line 128); DOI verify via `doi_matches` → `doi_verified`; closed/no-location record is valid (offline test + live `oa_status=closed` with `found=true`, no exception); `/v2/search` only mentioned descriptively, never called; 429 via shared `RetryPolicy`; cache TTL 6h default, 300s negative. **Crossref** `crossref.py`: polite `mailto` when email present; `cursor`+`rows` paging; `/events` mentioned descriptively only, not called; `ORDER_HINT=50`, metadata-only, not treated as full text. **OpenAlex** `openalex.py`: `params["api_key"]` sent when present (lines 123, 236); cursor paging; USD-credit `_budget_note` (key $1/30 rps, keyless $0.10/10 rps); on any provider error `completeness` stays `"incomplete"` + `error` set (offline `TestCompletenessOnError` + live). **arXiv** `arxiv.py`: `PACING_SECONDS=3.0` Pacer (1 req/3 s, single connection); version identity `vN` preserved by `normalize_arxiv_id` + feed link parsing; `start`+`max_results` paging; PDF validated as actual bytes. **S2** `semantic_scholar.py`: keyless 429/403/404 → recorded `skipped`, never a run failure (`TestS2GracefulDegrade` + live 429 negative); optional `Bearer` key header when set. Live arXiv fetch produced `%PDF-` magic, 193,982 bytes. |
| 4 | Retrieval order + honest access_level | verified-ok | `fetch_document` runs local cache → Unpaywall locations → repository/publisher → record unavailable (`retrieval.py`). PLOS `10.1371/journal.ppat1000488` → `doi-landing` HTTP 404 → `access_level=unavailable`, exit 4 (not `full_text` despite an HTML landing). arXiv id → `full_text`. HTML landing is classified `html_landing`, never `full_text` (only `pdf`/`xml` → `full_text`). Caveat: the *re*-fetch crash path is D-P2-1. |
| 5 | Security (TLS, URL guard, no embedded creds, cross-origin strip, size ceiling) | verified-ok (core guard) / violated-in-practice (D-P2-2) | `HttpTransport` builds httpx with `verify=True` (TLS on, never disabled). `HttpGuard` rejects loopback/private/link-local/reserved + embedded `user:pass` + non-http(s) schemes by default; `allow_local=True` opt-out allows private targets (verified: 8 reject cases + 1 public control + opt-out all correct). Cross-origin redirect strips `authorization`/`cookie`/`x-api-key`/`proxy-authorization` (verified via fake-client simulation; same-origin keeps auth). Size ceiling enforced (`max_bytes` → non-retryable `HttpError`, verified). **But** the guard cannot prevent what the adapters do *before* the guard: they embed `?email=`/`?api_key=` into the request URL that becomes the cache key and the error message → D-P2-2. |
| 6 | No secret leak | **violated** (D-P2-2) | **Clean:** 0 `.env` values in the 4 new commits (`git diff 3d00769..82e1326`), 0 in tracked `src`/`tests` at `82e1326`, 0 in live-smoke stdout/stderr (raw *and* percent-encoded forms), 0 in first-run `search`/`resolve`/`fetch` stdout (`request_url` is stripped via `url_base()`). **Violated:** (a) the on-disk cache key is the full request URL incl. query (`http_layer.py:404`), so `.papercli/storage/cache/meta/*.json` (gitignored, untracked) persist `?email=<…>` and `?api_key=<…>`; (b) on the negative-cache/error path `HttpError.message` embeds that credential-bearing URL and `retrieval.py` copies it into the CLI `warnings`/`error` JSON — confirmed `resPLOS.out` on stdout carries the email (URL-encoded) and the OpenAlex key (raw + encoded). |
| 7 | No Sci-Hub | verified-ok | Whole-tree grep for `sci|si-hub`: 10 hits, **all** permitted descriptive/migration notes (`ATTRIBUTION.md`, `PaperCLI_Build_Handoff.md`, `docs/03-migration-note.md`, `research/review/phase1-verdict.md`). Zero functional mentions — no adapter, flag, env var, or config key. |
| 8 | Provider extension contract | verified-ok (minor gap, D-P2-3) | All 5 adapters subclass Phase-1 `ProviderAdapter` and call `register_provider`; no core (non-provider) file imports a concrete adapter module (`cli/main.py` only imports `providers.cache`/`providers.retrieval`). Registry drives `doctor` credential checks. **Gap:** `SearchService.search` (`retrieval.py`) hardcodes the 5 adapters + a `known` name set, so a genuinely new registered adapter passes `doctor` but is rejected by `search --providers <new>` until `retrieval.py` is edited. |
| 9 | Windows-safety spot check | verified-ok | No POSIX-only calls in any new provider file (no `os.fork`/`fcntl`/`signal.SIG*`/`select`/`os.uname`/`os.getuid`/`mmap`/`/dev/`). `pathlib` + `tmp.replace()` atomic write + `sqlite3` all Windows-portable; full suite + live CLI ran on Windows. |

## Defects

**D-P2-1 — major — `src/papercli/providers/retrieval.py:835-836`**
`_download_and_validate` catches `except HttpError as e` but `HttpError` is not
imported (the `http_layer` import at lines 32-37 lists only
`HttpGuard, HttpTransport, Pacer, RetryPolicy`). Whenever `http_layer.get()`
raises `HttpError` (a negative-cache hit, a size-overflow, or exhausted
retries) on a re-fetch of a previously-unavailable DOI, the handler itself
raises `NameError: name 'HttpError' is not defined` → CLI exits **1**
(`INTERNAL_ERROR`) instead of the spec-mandated **4** (retrieval
unavailable). Reproduced: clean-cache `fetch 10.1371/journal.ppat1000488` →
exit 4; re-run with the negative cache populated → exit 1. *Fix: add
`HttpError` to the `http_layer` import block.*

**D-P2-2 — major — `src/papercli/providers/http_layer.py:404` (+ `unpaywall.py:128`, `openalex.py:123,236`, `retrieval.py` error surfacing)**
Credentials are embedded into the request URL query (`?email=…`, `?api_key=…`),
and that URL becomes the retrieval-cache key (`copy_merge_params` at line 404)
and the `HttpError.message` text. Result: (a) both secrets are persisted to
local, gitignored cache JSON under `.papercli/storage/cache/meta/*.json`, and
(b) on the negative-cache/error path `retrieval.py` copies that
secret-bearing message into the CLI stdout `warnings`/`error` JSON — confirmed
in a live `resolve` of a negatively-cached DOI. Violates §16 "no embedded
creds" and §19 "JSON … contain no … secret values". First-run happy paths are
clean because `unpaywall.url_base()` already strips the query for the
`request_url` field — the error/cache-key paths do not. *Fix: cache on the
credential-free request URL/path and strip any credential query-param from
`HttpError.message` before it reaches output (reuse the `url_base` masking).*

**D-P2-3 — minor — `src/papercli/providers/retrieval.py` `SearchService.search`**
Provider dispatch and the `known` provider-name set are hardcoded rather than
driven by the Phase-1 registry, so a newly registered adapter (which passes
`doctor`) is rejected by `search` until core is edited — weakening the "plugs
in without touching core" contract. *Fix: resolve adapters (and their record
mappers) from `get_registry()` keyed by provider name.*

## Gate disposition

- zero blockers: met (no hard blocker to the core provider capability)
- 93 tests green in a fresh venv: **met**
- live smoke tests ran and returned real provider responses: **met**
- retrieval-order honesty verified: **met**
- no secret leak: **NOT met** — D-P2-2 (secrets in local cache JSON and in CLI
  stdout on the error/negative-cache path)
- no Sci-Hub: **met**

FAIL on the "no secret leak" gate + the D-P2-1 exit-code regression. Fixing
D-P2-1 and D-P2-2 (both one-line-class) and re-verifying the re-fetch exit
code and the stdout/cache secret scan would clear the gate; D-P2-3 can follow
in a later commit.
