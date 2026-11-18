# Crossref REST API — Provider Docs Findings

Researched: 2026-11-18 (live fetch + live read-only calls)
Docs read:
- https://www.crossref.org/documentation/retrieve-metadata/rest-api/ (overview, static HTML)
- https://api.crossref.org/swagger-docs (authoritative OpenAPI 2.0 spec, 88 KB — the page points here)
- https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/
- https://www.crossref.org/documentation/retrieve-metadata/content-negotiation/
- https://www.crossref.org/documentation/retrieve-metadata/simple-text-query/

## Base URL
- `https://api.crossref.org/` (REST API doc, "Quick start": "Use https://api.crossref.org/ as your
  base URL.")

## Auth
- No API key, no sign-up. Doc: "No sign-up is required to use the REST API."
- **Polite pool via `mailto` param.** Doc example: `https://api.crossref.org/works?rows=0&mailto=yourmail@company.org`
  ("Use the polite pool"). OpenAPI: `mailto` = "The email address to identify yourself and access
  the 'polite pool'."
- Live confirmation (2026-11-18): a polite request returned headers `x-api-pool: polite-array`,
  `x-rate-limit-limit: 3`, `x-rate-limit-interval: 1s`, `x-concurrency-limit: 3` → the polite
  pool is throttled to ~3 req/s, concurrency 3.

## Rate limits
- The doc does NOT publish a single hard numeric limit in the REST API page text.
- What IS documented/observed:
  - Polite pool (mailto) is a slower, queued tier: observed `x-rate-limit-limit: 3` / 1s /
    concurrency 3 (from live headers, not stated in doc prose).
  - The API serves "over two billion calls to our API each month" (REST API page blog blurb).
  - 429 responses occur when over-limit; the API returns JSON error messages
    (`{"status":"error", "message-type": ...}`). (Observed a live 429 on the bare root `/`.)
- **The exact published per-second/daily quota is unconfirmed from the doc text**; treat
  polite-pool as ~3 req/s observed, and honor 429 + backoff in the build.

## Endpoints (from OpenAPI spec)
- `/works` (list), `/works/{doi}` (singleton), `/works/{doi}/agency`
- `/journals`, `/journals/{issn}`, `/journals/{issn}/works`
- `/funders`, `/funders/{id}`, `/funders/{id}/works`
- `/members`, `/members/{id}`, `/members/{id}/works`
- `/prefixes`, `/prefixes/{prefix}`, `/prefixes/{prefix}/works`
- `/types`, `/types/{id}`, `/types/{id}/works`
- `/licenses`
- **No `/events` endpoint in the current spec.** The build spec's "works vs events vs funders"
  is not reflected in the current endpoint table: `events` is not a top-level REST endpoint.
  (Post-publication updates historically came via "Crossmark"/content-update, not a
  first-class `/events` route here.) **Discrepancy worth flagging.**
- Live check: `GET /events` → 404 (`route-not-found`).

## Bibliographic search params
- `query` (generic), plus field-scoped forms confirmed in the OpenAPI "Queries" section:
  - `query.bibliographic` = "only incorporates fields used when referencing the content item
    (title, authors, volume, journal name, etc.)"
  - `query.title`, `query.abstract`, etc. (simple-text-query + works filters).
- `filter` param for structured filters (see rest-api-filters page).
- Live confirmation (2026-11-18): `query.bibliographic=graph neural network` → 200,
  `query.title=transformer attention` → 200.

## Pagination
- `rows` (page size, up to 1000 per list request), `offset` (skip up to 10,000).
- `cursor`: OpenAPI "Retrieving large result sets" — add `cursor=*` (with `rows>0`), read
  `next-cursor` from the response, pass it back; "Cursors expire after 5 minutes if not used."
  Stop when items returned < `rows`. **Recommend cursors over offset** (no page limit, faster).
- Live confirmation: `cursor=*` returned `next-cursor`; following it returned 200 with a new
  cursor.

## Response format
- JSON; mime `application/vnd.crossref-api-message+json`. Singleton / list / headers-only
  (HEAD) response types. `select` param to trim fields (e.g. `select=DOI,prefix,title`).

## Gotchas
- DOI in path must be URL-encoded (`10.1128%2Fmbio...`).
- Facet counts are approximate and can double-count records with multiple relationships.
- Don't treat Crossref as a full-text repository (matches build spec note).

## spec_claims_verification
| Claim | Doc says | Status |
|---|---|---|
| Base URL api.crossref.org | "Use https://api.crossref.org/ as your base URL" | confirmed |
| Polite pool = mailto param | OpenAPI: mailto "access the 'polite pool'" | confirmed |
| Bibliographic search (query.bibliographic / query.title / filter) | OpenAPI Queries + live 200s | confirmed |
| works / funders endpoints | both in spec | confirmed |
| "events" endpoint | NOT in current spec; live 404 | contradicted |
| Cursor pagination | OpenAPI cursors, 5-min expiry | confirmed |
| Rate limit (published number) | not in doc prose; polite-pool observed ~3 req/s | unconfirmed (number) |
