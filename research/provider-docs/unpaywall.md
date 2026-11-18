# Unpaywall API — Provider Docs Findings

Researched: 2026-11-18 (live fetch + live read-only calls)
Docs read:
- https://unpaywall.org/products/api  (SPA; content embedded in https://unpaywall.org/js/app.dd2a222a.js)
- https://unpaywall.org/data-format   (same SPA bundle)

Note on retrieval: both URLs return a JavaScript SPA shell (no static HTML content).
The authoritative text is bundled in the app JS (`app.dd2a222a.js`); the facts below are
quoted from that bundle, which is what the live pages render.

## Endpoint
- `GET https://api.unpaywall.org/v2/{doi}`
- Returns a "DOI Object".
- **Only** endpoint supported: "The current version of the API is Version 2, and this is
  the only version supported."

## Auth / required params
- **Required `?email=` param.** Doc text: "Requests must include your email as a parameter
  at the end of the URL, like this: `api.unpaywall.org/my/request?email=YOUR_EMAIL`."
- No API key. Free, programmatic access.

## Rate limit (spec claim: 100,000/day)
- Doc text (API page, "Rate limits" heading): **"Please limit use to 100,000 calls per day.
  If you need faster access, you'll be better served by downloading the entire database
  snapshot for local access."**
- Status: **CONFIRMED** — the live doc states exactly 100,000 calls/day. This is a soft
  "please limit" guidance, not a hard enforced quota stated with a 429 number.
- The docs do NOT publish a specific 429 / Retry-After behavior, throttle number, or
  per-second limit. (429/Retry-After handling in the build spec is a client-side robustness
  note, not something the Unpaywall doc confirms.) **Unconfirmed from doc.**

## Response fields (data-format page)
Confirmed present on the DOI Object:
- `oa_status` (String): "gold, hybrid, bronze, green or closed."
- `best_oa_location` (Object|null): "The best OA Location Object we could find for this DOI."
- `oa_locations` (List): "List of all the OA Location objects associated with this resource."
- Also: `first_oa_location` (Object|null), `oa_locations_embargoed (beta)`, `is_oa`,
  `has_repository_copy`, `doi`, `doi_url`, `title`, `publisher`, `year`, `published_date`,
  `updated`, `journal_name`, `journal_is_oa`, `journal_is_in_doaj`, `journal_issns`,
  `genre`, `data_standard`.
- OA Location object fields: `host_type` (publisher|repository), `is_best`, `license`,
  `oa_date`, `pmh_id`, `url`, `version`, etc.
- Doc warning: "New fields may be added at any time... you shouldn't rely on the number of
  fields being fixed."
- `z_authors` is **Deprecated** — use OpenAlex `authorships` instead.

## DOI normalization gotchas (from doc)
- `doi` field: "This is always lowercase."
- `doi_url`: "This field simply contains [https://doi.org/] prepended to the doi field...
  in its correct format according to the Crossref DOI display guidelines."
- The API accepts "a valid DOI" and returns that DOI object; the build spec's "verify the
  returned DOI against the requested DOI" is the correct guard (case differences are the
  main hazard). The doc does not itself enumerate suffix-punctuation gotchas; that comes
  from the Crossref display guidelines it links to. **Partially confirmed** (lowercase +
  Crossref display rules).

## Notable changes from the spec
- **The Unpaywall search endpoint is RETIRED.** Doc text:
  "GET /v2/search?query=... — Retired 18 September 2026. This endpoint now returns
  410 Gone. Use OpenAlex search instead: `https://api.openalex.org/works?search=...`.
  OpenAlex is the successor to Unpaywall, from the same team and the same data; every
  result includes `open_access` and `best_oa_location`."
- Semi-annual Unpaywall snapshots are no longer created; use the OpenAlex monthly snapshot.
- The build spec's "Do not use the retired Unpaywall search endpoint" is now strongly
  corroborated by the doc (410 Gone).

## spec_claims_verification
| Claim | Doc says | Status |
|---|---|---|
| Required `?email=` param | "Requests must include your email as a parameter" | confirmed |
| No API key | "free, programmatic access", no key mentioned | confirmed |
| 100,000 calls/day | "Please limit use to 100,000 calls per day." | confirmed |
| 429 / Retry-After behavior | not published in the live doc | unconfirmed |
| `oa_status`, `best_oa_location`, `oa_locations` fields | all present on data-format page | confirmed |
| DOI normalization gotchas | `doi` is lowercase; `doi_url` follows Crossref display rules | confirmed (partial) |
| Retired search endpoint = 410 | "Retired 18 September 2026... now returns 410 Gone" | confirmed |
