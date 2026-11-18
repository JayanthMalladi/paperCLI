# Corrected provider facts (Phase 1 reference)

Verified 2026-11-18 from live docs and read-only calls. These are the
authoritative facts the adapters and the preflight doctor rely on. The
**corrected** values are the ones in `research/provider-docs/`.

## Unpaywall
- `GET https://api.unpaywall.org/v2/{doi}?email=REAL_EMAIL`
- **Required `?email=` param.** No API key. ~100,000 calls/day soft limit.
- `oa_status`, `best_oa_location`, `oa_locations` are the key fields.
- **The search endpoint (`/v2/search`) is RETIRED — it returns 410 Gone.**
  Use OpenAlex search instead. Never call the retired endpoint.

## Crossref
- Base `https://api.crossref.org/`. No sign-up. Polite pool via `mailto`.
- `query.bibliographic`, `query.title`, `filter` are the search params.
- **There is NO `/events` endpoint** in the current spec (live 404).
  Post-publication updates come via Crossmark, not a first-class route.
- Cursor pagination preferred over offset; cursors expire after 5 minutes.

## OpenAlex (USD-credit model, corrected)
- **Not** a fixed calls-per-window quota. It is a credit/budget model.
- Keyless: $0.10/day, 10 req/s per IP. Free key: $1/day, 30/s.
- Cursor paging past the first page **requires a free API key**
  (keyless `next_cursor` → 401). This is why the overnight discovery
  profile needs an activated key.
- 429 reasons: `requests_per_second`, `broad_boolean_query`,
  `wide_or_filter`, `semantic_search`, `daily_budget`. Honor
  `Retry-After` and bounded backoff.

## arXiv
- `http://export.arxiv.org/api/query`. No auth, no key.
- **Pacing: one request every three seconds, single connection** (TOU).
- Response is an Atom 1.0 feed; `abs`/`pdf` URLs always carry a `vN`
  version suffix. Preserve the version identity, not just the bare id.

## LLM endpoint probe
- The probed model endpoint supports OpenAI chat-completions **and**
  Anthropic messages.
- Structured output is **accepted but NOT enforced** — the adapter must
  parse JSON leniently (strip fences, repair), validate against the JSON
  Schema after the fact, and re-prompt on validation failure.
