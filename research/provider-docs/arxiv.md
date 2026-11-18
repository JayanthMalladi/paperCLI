# arXiv API — Provider Docs Findings

Researched: 2026-11-18 (live fetch + live read-only calls)
Docs read:
- https://arxiv.org/help/api  (redirects to https://info.arxiv.org/help/api/index.html — API access hub)
- https://info.arxiv.org/help/api/basics.html (API Basics)
- https://info.arxiv.org/help/api/user-manual.html (API User's Manual)
- https://info.arxiv.org/help/api/tou.html (Terms of Use for arXiv APIs — pacing lives here)

## Base URL
- Query interface: `http://export.arxiv.org/api/query` (doc examples all use http; https also
  works: `https://export.arxiv.org/api/query`).
- No auth, no API key. Doc: "There is no arXiv-supplied software that must be downloaded."

## Auth requirements
- None for the metadata API. Noncommercial projects must use the acknowledgement statement
  "Thank you to arXiv for use of its open access interoperability." Commercial products
  should review brand-use rules / consider arXivLabs affiliation.

## Rate limit / published pacing (spec claim: "apply its published request pacing")
- **The actual number: one request every three seconds, single connection.**
  TOU text (fetched live): "When using the legacy APIs (including OAI-PMH, RSS, and the
  arXiv API), **make no more than one request every three seconds, and limit requests to a
  single connection at a time.** These limits may change in the future."
- The limit applies to "all of the machines under your control as a whole" — the TOU
  explicitly forbids distributing across more machines to beat the pacing.
- No 429/Retry-After contract is published for the arXiv API; over-pacing risks blocks,
  not a documented retry header.
- **CONFIRMED** for the build spec: "3 second pacing" is exactly the doc number.

## Query parameters
- `search_query`: a Lucene-ish query string, e.g. `all:electron`, `ti:electron`.
  Search fields include `all`, `ti` (title), `au` (author), `abs` (abstract), etc.
- `id_list`: comma-delimited list of arXiv ids.
- `start`: result offset (0-based). `max_results`: page size (docs example uses 10; the API
  accepts larger values — check the cap before bulk pulls, not documented in these pages).
- **search_query vs id_list logic** (User's Manual §3.1.1.1):
  - only `search_query` → articles matching the query
  - only `id_list` → the listed articles
  - both → articles in `id_list` that also match `search_query` (result filter)
- Paging = `start` + `max_results`; no cursor.

## Response format
- **Atom 1.0 XML feed** (namespace `http://www.w3.org/2005/Atom`), with OpenSearch
  extension elements: `opensearch:totalResults`, `startIndex`, `itemsPerPage`.
- Entry elements: `<id>` (canonical `http://arxiv.org/abs/{id}`), `<published>`,
  `<updated>`, `<title>`, `<summary>` (abstract), `<author>`, `arxiv:primary_category`,
  `arxiv:comment`, `arxiv:journal_ref`, plus `<link>` elements:
  - `rel="alternate" type="text/html"` → `https://arxiv.org/abs/{id}v{n}`
  - `rel="related" type="application/pdf"` → `https://arxiv.org/pdf/{id}v{n}`
  - `rel="self"` → the arxiv.org API query URL that reproduces the result
- **abs/pdf URL patterns (live-verified 2026-11-18):** querying
  `id_list=cond-mat/0207270` (no version suffix) returned latest-version links
  `.../abs/cond-mat/0207270v3` and `.../pdf/cond-mat/0207270v3` — i.e. URLs always carry a
  vN suffix even when you request the id without one.

## Version identity (vN suffix)
- User's Manual §5.1.1 "A Note on Article Versions": "The first time an article is posted,
  it is given a version number of 1. When subsequent corrections are made to an article, it
  is resubmitted, and the version number is incremented. At any time, any version of an
  article may be retrieved."
  - id_list without suffix → **latest version**.
  - `id_list=cond-mat/0207270v1` → that exact version.
- The build spec's "preserve version identity" is doc-confirmed; store the vN returned in
  `<id>`/links, not just the bare id.

## Errors
- "Errors are returned as Atom feeds with a single entry representing the error. The
  `<summary>` for the error contains a helpful error message, and the `<link>` element
  contains a url to a more detailed explanation." (User's Manual §3.4)

## spec_claims_verification
| Claim | Doc says | Status |
|---|---|---|
| Base URL export.arxiv.org/api/query | "http://export.arxiv.org/api/query?search_query=all:electron" | confirmed |
| id_list vs search_query | User's Manual §3.1.1.1 logic table | confirmed |
| Atom feed response | "returns the results in the Atom 1.0 format" | confirmed |
| 3-second pacing | TOU: "no more than one request every three seconds... single connection" | confirmed |
| abs/pdf URL patterns | live: `/abs/{id}v{n}`, `/pdf/{id}v{n}` | confirmed |
| vN version identity | "append vn to the id"; latest = no suffix | confirmed |
| max_results cap for bulk pulls | not stated in these pages | unconfirmed |
