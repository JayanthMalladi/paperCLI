# K-Dense scientific-agent-skills — upstream review

Repo: https://github.com/K-Dense-AI/scientific-agent-skills
Pinned commit: ec59a29a03d5c40f73ab40878df04860bc00e876 (shallow clone, 2026-11-18)
License: **MIT** (LICENSE.md, "Copyright (c) 2025 K-Dense Inc."; GitHub API confirms spdx_id=MIT)
Files reviewed: skills/literature-review/SKILL.md, scripts/search_databases.py, scripts/verify_citations.py, skills/citation-management/SKILL.md

## 1. Stages defined by the literature-review skill

The skill defines a **seven-phase workflow** (full command/template detail in references/core_workflow.md):

1. **Planning and scoping** — question, inclusion/exclusion criteria, scope.
2. **Systematic literature search** — multi-database searching, all queries recorded.
3. **Screening and selection** — title/abstract then full-text, counts kept for PRISMA.
4. **Data extraction and quality assessment** — structured extraction, risk-of-bias appraisal.
5. **Synthesis and analysis** — thematic or quantitative synthesis.
6. **Citation verification** — every citation checked against the actual source.
7. **Document generation** — assemble the review with complete bibliography.

It also defines discipline-appropriate support: PRISMA 2020 flow reporting, records/reports/studies
distinction, optional LLM-generated schematics (OpenRouter), PDF export via Pandoc/XeLaTeX.

The **citation-management** skill defines five phases:
1. Paper discovery/search (OpenAlex, PubMed, Google Scholar via `scholarly`).
2. Metadata extraction (DOI/PMID/PMCID/arXiv/URL -> full metadata; CrossRef primary for DOIs).
2.5. **Mandatory metadata enrichment via web search** — treat API records as incomplete; never invent fields.
3. BibTeX formatting (dedupe, rekey on merge, entry-type/field rules).
4. Citation validation (completeness, venue conformance, manuscript agreement, DOI resolution via CrossRef).
5. Integration with writing workflow (search -> extract -> format -> validate -> cite).

## 2. What search_databases.py actually does

**It does NOT do live database searches.** Its own docstring says so: "Processes normalized search
records locally; does not query databases." It is a pure offline post-processor:

- Input: a JSON array of normalized record objects (title, authors, year, source, doi, url, citations, ...).
  It explicitly **refuses raw provider envelopes** (`main()` checks input is a list of dicts, else fails with
  "input must be a normalized JSON array of record objects, not a provider response").
- Pipeline (CLI flags): `--deduplicate` (DOI-first dedupe, title fallback for DOI-less records; normalizes
  doi.org URLs), `--year-start/--year-end` filter, `--rank citations|year|relevance` (sort by field;
  non-finite values -> -inf), `--summary` (per-source counts, year distribution, avg/total citations to stderr),
  output formats json / markdown / bibtex (bibtex key = first_author+year, numeric `_2`, `_3` collision suffixes).
- No network calls at all. Live searching is delegated to the agent (e.g. `parallel-cli`, or the
  citation-management skill's own `search_openalex.py` / `search_pubmed.py` / `search_google_scholar.py`),
  whose results get normalized into JSON and fed to this script.

**Implication for PaperCLI:** the "search" and "process" stages are deliberately decoupled — the
agent does live search; the script is a deterministic, testable, offline record pipeline. This is a
clean pattern to copy: a search stage that emits normalized records, plus pure functions that
dedupe/filter/rank/format them.

## 3. What verify_citations.py verifies

**It does NOT verify that a citation supports a claim.** Its docstring and report field
`verification_scope` state this explicitly: "DOI registration and Crossref metadata, not claim support
or retraction screening." It verifies:

- **DOI extraction**: regex `10\.\d{4,9}/...` with URL-decoding, prose-punctuation stripping, and
  parenthesis balancing in suffixes; de-duplicated.
- **DOI registration** via the doi.org handle API: statuses `invalid_syntax`, `missing` (handle API
  code 100), `inconclusive` (anything else, incl. transient failures), `registered` (code 1 and the
  returned handle matches the normalized DOI).
- **Crossref metadata** (per-DOI single-work lookup, no key needed, optional `mailto`): title, authors,
  year (published-print -> online -> issued), journal, volume/issue/pages; `metadata_status`
  retrieved / unavailable.
- Optional URL accessibility check (HEAD, <400).
- Bounded retry only for transient codes (429/5xx) with `Retry-After` awareness; sequential,
  <=2 req/s to Crossref.
- JSON report with verified / failed / missing / inconclusive / metadata_unavailable buckets;
  exit code 1 if any DOI failed.

**Limits observed (K-Dense):**
- No claim-to-citation matching; registration != support (their own common-pitfall #4 says the same).
- Crossref doesn't cover all DOI agencies -> `inconclusive` / `metadata_unavailable` need manual review.
- DOI regex extraction is "inherently ambiguous" in prose; markdown closers handled heuristically.
- No retraction/correction screening (explicitly out of scope).
- The skill is prose-driven: the "systematic review" is run by the agent following SKILL.md phases;
  the scripts are deliberately narrow offline helpers, not an automated pipeline.
- Optional dependence on external paid tooling (parallel-cli, OpenRouter) for discovery/schematics.

## 4. License verdict

- Repo license: **MIT** (LICENSE.md + GitHub API; skill frontmatter also says `license: MIT license`).
- MIT allows reuse of patterns AND code, with attribution (retain copyright notice in copies or
  substantial portions).
- Caveat: the repo asks for academic citation of their arXiv paper when a skill "materially
  contributed" to a manuscript/code release — a soft attribution ask, not a license condition.
- Practical verdict for PaperCLI: **reuse patterns/prose freely; code may be copied under MIT with
  license/attribution retained.** Our CLI is an independent tool (not their manuscript), so the
  academic-citation ask does not apply to us directly.
