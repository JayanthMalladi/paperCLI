# OpenAlex API — Provider Docs Findings

Researched: 2026-11-18 (live fetch + live read-only calls)
Docs read:
- https://help.openalex.org/api/authentication/ (fetched, 49 KB)
- https://help.openalex.org/api/paging.md
- https://help.openalex.org/api/filtering.md
- https://help.openalex.org/access/pricing.md
- https://help.openalex.org/access/example-costs.md
- https://help.openalex.org/data/works.md

## Endpoints
- Base: `https://api.openalex.org/`
- Works (main literature-discovery endpoint): `GET /works`, `GET /works/{id_or_doi}`.
- Others: authors, sources, institutions, topics, funders, awards, concepts, etc.
- Agent key issuance: `POST https://api.openalex.org/api-keys` (JSON body `{"email": ...}`),
  plus `GET /api-keys` (instructions as JSON).
- Usage: `GET /rate-limit?api_key=...` (free, never counts against budget).

## Auth
- **No key required for basic use.** Doc: "OpenAlex data is free, and so is casual use of
  the API — you can make basic queries with no key at all."
- Free API key (email-activated, free, $1/day budget): "it raises your daily budget 10×,
  lets you track your usage, and is required for cursor paging past the first page."
- Key states: no key, or a key with a verified email. Unactivated keys → 403
  `key_not_activated`.
- Sending a key: `?api_key=YOUR_KEY` query param **or** `Authorization: Bearer YOUR_KEY`
  header. "Both work identically."
- The spec's `OPENALEX_API_KEY` env-var pattern matches the doc's guidance ("An
  OPENALEX_API_KEY environment variable ... is a good home").

## Rate limits (current doc model)
OpenAlex now uses a **credit/budget model**, not a fixed calls-per-window number:
- **Per-second limits (no API key):** 10 req/s (counted per IP). Free/prepaid key: 30/s.
  Member: 50/s. Member+/Partner: 50/s.
  - Lower sub-limits: complex boolean searches (>5 AND/OR/NOT operators) 1/s keyless;
    wide topic/concept OR filters (>10 values) 1/s; semantic search 1/s.
- **Daily budgets (USD, not calls):** keyless = $0.10/day; free key = $1/day (10× the
  keyless budget); new accounts are paced to $0.10 per 5 minutes for their first $1
  (~50 min of steady use). Paid: Member $20/day, Member+ $100/day, Partner $200+/day.
- Per-operation cost (Example costs): single-entity GET **free**; list+filter
  **$0.10 per 1,000 calls**; search (`.search` filters or `search` param) **$1 per 1,000**;
  group_by $0.10/1k (on a list) or $1/1k (on a search); content download $10/1k.
- 429 reasons: `requests_per_second` / `broad_boolean_query` / `wide_or_filter` /
  `semantic_search` / `daily_budget`. 429 bodies carry `Retry-After`-type seconds;
  doc: "Wait the Retry-After seconds (usually 1) and retry."
- Header block on every response: `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
  `X-RateLimit-Credits-Used`, `X-RateLimit-Reset`; plus `meta` with cost + total count.

## spec_claims_verification
| Claim | Doc says | Status |
|---|---|---|
| "polite pool" default when no key | No "polite pool" term; no key = keyless $0.10/day + 10 req/s per IP | contradicted (term) / confirmed-in-substance |
| "activated key ≈ 100k calls/5min" | Not stated. A free key = $1/day; a list+filter page = $0.10/1k calls → ~10,000 list calls/day; search pages ~1,000/day | contradicted |
| Cursor pagination for work search | "Cursor paging ... needs a free API key past the first page: without a key, `cursor=*` works, but following `next_cursor` returns a 401." Basic paging cap: `page × per_page` ≤ 10,000; `per_page` max 100 | confirmed |
| Filters: publication_date, type, open_access | `publication_year`, `from_publication_date`/`to_publication_date`, `type:`, `open_access.is_oa:` (also `open_access.oa_status`), `doi`, `has_abstract`, `cited_by_count` — all in filtering.md | confirmed |
| "Require an activated account key for overnight profile when pagination needs it" | Exactly right: keyless cursor paging stops at page 1 (401 on next_cursor) | confirmed |

## Gotchas
- `open_access` is a nested field: filter with `open_access.is_oa:true`, select the whole
  `open_access` object (is_oa, oa_status, oa_url, any_repository_has_fulltext).
- OR filters (`|`): max 100 values per filter; OR is within one filter only, not between
  filters.
- `+` decodes to a space = AND inside a filter value; quote values containing spaces.
- Comma inside a value → wrap the value in double quotes.
- `per_page` 1-100 supported; 200 still accepted as deprecated legacy behavior.
- Don't cursor-page to download the whole dataset — use the monthly OpenAlex snapshot.
- Live check 2026-11-18: keyless `GET /works?filter=...&per_page=1&select=...` → 200;
  `cursor=*` keyless → 200 with `next_cursor` present (following it keyless would 401
  per doc; not exercised live to avoid hammering).
