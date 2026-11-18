# PaperCLI: Agentic Literature Review — Build Handoff

Version: 1.0  
Prepared: 18 November 2026  
Owner: Jayanth  
Status: specification only; implementation requires the owner's explicit “go.”

## 1. Product outcome

Build a portable CLI that an AI agent can use to perform an evidence-backed literature review. The user supplies a research brief and resource limits before leaving the computer. A persistent worker discovers papers, screens them, retrieves available full text, extracts evidence, synthesizes a review, verifies references and claim support, and writes a complete output bundle.

The user should be able to inspect useful results in the morning even if some papers are inaccessible, an API fails, or a resource limit is reached. The process must accurately distinguish completed research from partial work.

Remove Sci-Hub completely: source adapter, flags, environment variables, configuration, documentation examples, and dependency on its website or verification sessions. No CAPTCHA or browser workflow is required for the new product.

### Success definition

- A fresh installation can be configured and checked before a run.
- An agent can discover the command contract without platform-specific integration.
- A detached worker survives the initiating agent or terminal exiting.
- Every substantive review claim is linked to identified source evidence or explicitly marked as synthesis/inference.
- All references are generated from a saved registry; no invented references or bibliographic fields.
- Failed retrievals and abstract-only evidence are disclosed.
- Interruptions can be resumed without repeating completed tasks unnecessarily.
- Runs respect configured time, query, paper, token, concurrency, and spending constraints.

## 2. Scope and boundaries

### First release

1. Native Python CLI, prioritizing Windows; retain Linux/macOS compatibility.
2. Agent-driven and standalone execution sharing one durable research workspace.
3. Unpaywall, Crossref, OpenAlex, arXiv, and local-PDF ingestion.
4. Structured screening, full-text extraction, thematic synthesis, citation audits.
5. JSON/JSONL agent interfaces and Markdown, PDF, DOCX, BibTeX, CSL-JSON, CSV exports.
6. Checkpointing, detached execution, status, logs, cancellation, resume, budgets.
7. Portable agent skill and examples for subprocess invocation.

### Subsequent adapters

DataCite, PubMed/PMC, Semantic Scholar, OCR, additional model providers, VPS deployment, and optional MCP exposure. Design extension interfaces now; do not make these prerequisites for the first working review.

### Excluded

Sci-Hub; paywall bypass; browser automation; automatic account registration; mandatory Google Scholar scraping; fabricated experimental findings; automatic meta-analysis; automatic claims of exhaustive coverage; a web dashboard; default email sending; arbitrary execution of code found in papers.

Default output is a narrative literature-review draft with a documented search method. A systematic-review profile must record a protocol, reconcile screening counts, and disclose reviewer limitations. It must not claim that automated reviewers constitute independent human screening or that the search is exhaustive.

## 3. Reference implementations and lessons

These repositories were inspected as design references, not installed as product dependencies:

- [K-Dense Scientific Agent Skills](https://github.com/K-Dense-AI/scientific-agent-skills/tree/main)
- [PaperOrchestra](https://github.com/google-research/paper-orchestra)

### K-Dense

Relevant files:

- `skills/literature-review/SKILL.md`
- `skills/literature-review/scripts/search_databases.py`
- `skills/literature-review/scripts/verify_citations.py`
- `skills/citation-management/SKILL.md`

Adopt explicit stages: scope, search, screen, extract/appraise, synthesize, verify, export. Use a short skill entry point with supporting references and executable utilities. Preserve search provenance and distinguish records, reports, and studies.

Observed limits: its `search_databases.py` processes already-normalized records rather than performing database searches. DOI verification establishes registration/metadata, not whether a claim is supported. The skill itself is procedural guidance for an agent, not an unattended runner.

### PaperOrchestra

Relevant files:

- `methods/agents/literature_review_agent.py`
- `methods/paper_writer.py`
- `utils/scholar_utils.py`
- `paper_writing_cli.py`

Adopt separate planning, discovery, synthesis, and critique responsibilities; typed intermediate objects; bounded concurrent work; bibliography and citation-map generation.

Observed limits in the inspected literature path: Gemini/Google Search proposes candidates, Semantic Scholar enriches them, and synthesis receives metadata and abstracts truncated to 1,500 characters. This is not proof of full-text reading. The inspected workflow uses in-memory task tracking and broad retries rather than the durable task state required here. Some exceptions are silently discarded, and the Scholar utility prints its API key; neither behavior should be inherited.

Do not fork either repository wholesale. Selective reuse requires checking the exact source license, retaining notices, recording upstream commit/version, and evaluating behavior independently. At implementation start, pin reviewed upstream commits because repository contents change.

## 4. Execution modes

### Agent-driven

An external agent owns reasoning and calls CLI primitives to search, retrieve, inspect evidence, submit structured screening/extraction results, and export. No model API key is required by the CLI for deterministic primitives. The external agent's lifetime and usage limits govern continued reasoning.

Validate all agent-submitted artifacts against schemas and the saved source registry. The agent cannot inject arbitrary bibliography entries into a final export without validation.

### Standalone runner

The CLI owns orchestration and calls a configured model endpoint for planning, screening, extraction, synthesis, and critique. The external agent only needs to launch and monitor the job. This mode requires a configured model backend, working credentials or a reachable local endpoint, and explicit resource limits.

The worker is independent of the originating chat, but depends on its host being powered, awake, connected, and able to reach configured services. Closing the terminal must not kill a detached job. Host reboot recovery is explicit resume in v1; automatic boot-time service installation is not required.

Support one tested OpenAI-compatible chat-completions adapter first, with configurable base URL, model, key environment variable, structured-output capability, and context limits. Support local compatible endpoints through the same contract where feasible. Additional native provider adapters can follow. Do not claim universal protocol compatibility; capability-test the selected backend.

## 5. User journeys

### Setup once

1. Install the package.
2. Run `papers init` to create local config and a brief template.
3. Enter the real contact email and configure secrets locally.
4. Run `papers doctor` to test enabled providers, model capability, storage, and exporters.

### Overnight run

1. User or agent writes a brief.
2. `papers review plan` produces a validated research protocol and resource estimate.
3. User or agent launches `papers review run --detach` with approved limits.
4. Worker checkpoints progress and writes incremental artifacts.
5. User opens the completed bundle, or a clearly labeled partial bundle, next morning.

### Interrupted run

Inspect status and failure reasons; fix configuration if needed; resume the same run. Completed immutable evidence artifacts remain available. Changed briefs/configuration require a new run revision, not silent mutation of prior evidence.

## 6. CLI contract

Proposed command surface; all commands below are requirements for the new build, not claims about the current package.

```sh
papers init
papers config show --redact
papers doctor --json
papers schema
papers search "QUERY" --providers crossref,openalex,arxiv --limit 50
papers resolve "IDENTIFIER"
papers fetch "IDENTIFIER" --out ./papers
papers read FILE --pages 1-3 --max-chars 18000
papers ingest FILE --run RUN_ID

papers review plan --brief brief.md --out review-plan.yaml
papers review run --plan review-plan.yaml --detach
papers review status RUN_ID
papers review logs RUN_ID --follow
papers review resume RUN_ID --detach
papers review cancel RUN_ID
papers review export RUN_ID --formats md,pdf,docx,bib,csl,csv

papers task next --run RUN_ID --worker AGENT_ID
papers task submit TASK_ID --input result.json
papers evidence list --run RUN_ID
papers citations audit --run RUN_ID
papers skill export --out ./papercli-skill
```

`task next/submit` supplies a lease-based protocol for external agents. Standalone workers use the same task queue. Reject invalid, stale, duplicated, or source-inconsistent submissions explicitly; idempotent repeated submissions with the same payload hash return the prior result.

### Machine output

Default stdout is one JSON envelope. Batch and follow-log streams use one envelope per line. Diagnostic logging goes to stderr. `--help` and `--version` are human-readable exceptions. Optional human formatting must be explicit.

```json
{
  "schema_version": "1.0",
  "status": "ok",
  "data": {"run_id": "opaque-id", "state": "running"},
  "warnings": []
}
```

```json
{
  "schema_version": "1.0",
  "status": "error",
  "error": {
    "code": "CONFIG_REQUIRED",
    "message": "Configure the Unpaywall contact email.",
    "retryable": false,
    "details": {"field": "contact.email"}
  }
}
```

Distinguish command success from review quality: a successful `status` call can describe a failed or partial run. Successful detached launch means the worker accepted the job and acquired its lock, not that the review completed.

| Exit | Meaning |
|---|---|
| 0 | Command succeeded |
| 1 | Unexpected/internal failure |
| 2 | Input/configuration/preflight failure |
| 3 | Removed legacy verification code; reserve, never repurpose silently |
| 4 | Retrieval unavailable/no matching result |
| 5 | Network, access, or provider rate-limit error |
| 6 | Batch or foreground run completed partially |
| 7 | Run paused/limit reached |
| 130 | Interrupted |

Publish JSON Schemas for envelopes, plan, tasks, screening, evidence, claims, bibliography, and run summary. Version schemas independently of package versions.

## 7. Research brief and protocol

Required brief fields: topic/question, purpose, scope, desired outputs, and resource limits. Defaults may fill optional fields but must appear in the saved protocol.

```yaml
schema_version: '1.0'
question: 'REPLACE WITH RESEARCH QUESTION'
purpose: 'Thesis background and research-gap identification'
review_type: narrative
date_range: {from: null, to: null}
languages: [en]
include_preprints: true
seed_identifiers: []
inclusion_criteria: []
exclusion_criteria: []
themes_to_investigate: []
outputs: [md, pdf, docx, bib, csl, csv]
citation_style: ieee
limits:
  wall_minutes: 360
  max_search_queries: 30
  max_discovered_records: 1000
  max_full_text_papers: 60
  max_model_calls: 300
  max_input_tokens: 1000000
  max_output_tokens: 150000
  max_estimated_cost_usd: null
```

These numeric values are example ceilings, not promised throughput or cost. The setup must require explicitly chosen run limits before unattended reasoning. A null monetary cap is allowed only when hard token/call limits are set and the user explicitly accepts an uncapped dollar estimate or uses an unpriced local backend.

Planning generates database-specific queries and identifies assumptions, ambiguities, and expected coverage. Missing essential scope/configuration is resolved before launch. An unattended worker never waits indefinitely for a conversational answer.

## 8. Provider behavior

### Unpaywall — required first-release adapter

Documentation: https://unpaywall.org/products/api

Request `GET https://api.unpaywall.org/v2/{doi}?email={REAL_EMAIL}`. Contact email is required; no API key. Documentation reviewed in this planning session states 100,000 calls/day; verify current service requirements during implementation and respect stricter returned limits.

- Normalize DOI syntax without stripping valid suffix punctuation.
- Verify returned DOI against requested DOI.
- Preserve raw response, timestamp, OA status, and location metadata.
- Collect best location and remaining locations; deduplicate URLs.
- Try direct PDF URLs first, then landing pages.
- Retain version, host type, license (including null/unknown), and landing/PDF URL distinctions.
- A closed/no-location record is a valid lookup outcome, not an API error.
- Cache metadata with configurable TTL; use shorter TTL for negative availability.
- Respect 429/Retry-After and bounded transient retries.
- Do not use the retired Unpaywall search endpoint.

### Discovery and metadata

Crossref: DOI metadata, bibliographic search, polite contact identification. Do not treat it as a full-text repository.

OpenAlex: broad discovery, filters, cursor pagination, and citation relationships. Require an activated account key for the configured overnight profile when pagination needs it. Check live budgets/capabilities during preflight. Documentation: https://help.openalex.org/api/authentication/

arXiv: identifier normalization, preprint metadata/search, full-text PDF retrieval; preserve version identity. Apply its published request pacing.

Follow-up adapters: DataCite for appropriate DOI registrants; PubMed/PMC for biomedical work; optional Semantic Scholar for enrichment/chaining. Search coverage must be chosen by domain rather than assuming a fixed provider list is comprehensive.

### Retrieval order

1. Validated local document cache or user-imported document.
2. Unpaywall locations for DOI inputs.
3. Applicable repository or publisher-provided accessible full text.
4. Record full text as unavailable and continue.

Do not equate a link with a retrieved report. Handle redirects, relative URLs, MIME inconsistencies, response-size ceilings, actual PDF validation, HTML/XML full text, and bounded traversal. Do not retry permanent 401/403 blocks indefinitely.

## 9. Screening and evidence

Save title/abstract and full-text screening decisions separately. Each includes decision, criterion IDs, rationale, evidence basis, model/agent identity, prompt version, and timestamp. Ambiguous records enter an uncertainty queue; a bounded automated recheck may resolve them. Remaining uncertainty is reported.

Deduplicate by normalized identifiers first. Use normalized title/authors/year as candidate duplicate signals; do not merge solely on a fuzzy title score. Link preprints, journal reports, and related studies without deleting provenance. Reports and studies are distinct concepts.

Parse full text into numbered pages or stable section/paragraph locations. Prefer structured repository XML when available. Preserve extraction warnings and original source bytes. Flag scanned/empty pages; OCR is optional follow-up work and absence must be disclosed.

Evidence record minimum:

```json
{
  "evidence_id": "ev-opaque",
  "paper_id": "paper-opaque",
  "document_id": "doc-opaque",
  "access_level": "full_text",
  "location": {"page": 5, "section": "Results", "paragraph": null},
  "supporting_excerpt": "Short source passage",
  "finding": "Structured interpretation of the passage",
  "claim_type": "reported_result",
  "methods": {},
  "population_or_system": null,
  "limitations": [],
  "extraction_warnings": []
}
```

Access levels: `full_text`, `partial_full_text`, `abstract_only`, `metadata_only`, `unavailable`. Never infer full-text findings from metadata. Mark author interpretation and cross-paper synthesis explicitly. Numerical findings require traceable source support, units, and population/task context.

## 10. Synthesis and citation quality

Organize the review around research questions/themes. Compare methods, evidence strength, results, contradictions, limitations, and unresolved gaps. Include negative/contradictory evidence where found. Citation popularity may prioritize reading but cannot substitute for quality appraisal or eligibility.

Generate drafts from structured evidence, with stable citation keys supplied by the registry. The model may select existing references but cannot fabricate new bibliographic entries.

Maintain a claim ledger linking substantive draft claims to evidence IDs. Validate reference identity separately from semantic support. Audit labels: `supported`, `partially_supported`, `unsupported`, `uncertain`, `inference`. Preserve automated critique and bounded revisions. Unsupported claims must be removed, narrowed, or prominently marked unresolved before finalization.

Check available correction/retraction metadata and record the source/date and limits of that check. No coverage claim that all retractions were ruled out. Missing metadata stays missing; article numbers, online-first status, and no-DOI records are valid bibliographic cases.

Use CSL/citeproc-compatible formatting for final reference styles. Validate citation keys, bibliography usage, duplicates, and orphan references. DOI resolution alone must never set a claim to `supported`.

## 11. Architecture and durable data

Suggested modules: `cli`, `config`, `providers`, `models`, `documents`, `research`, `jobs`, `storage`, `citations`, `exports`, and `agent_contract`.

Use typed schemas and a SQLite database with migrations, transactions, foreign keys, and a local single-writer strategy. Files hold immutable raw responses, source documents, parsed text, prompts/results, and exports; database rows link them with hashes and provenance.

Core entities:

| Entity | Essential fields |
|---|---|
| Run | ID, protocol hash, config snapshot, state, timestamps, limits, counters |
| Task | Type, input hash, dependencies, state, attempt, lease, result hash |
| Search | Provider, query, filters, date, cursor, results, completeness |
| Paper | Canonical IDs, title, authors, year, metadata provenance |
| Report link | Related paper/version/study, relation and confidence |
| Document | Paper, source URL, hash, version, access status, parse quality |
| Screening | Stage, decision, criteria, rationale, reviewer |
| Evidence | Document, location, excerpt, finding, limitations |
| Claim | Draft location, claim text, evidence IDs, audit status |
| Model call | Model, prompt hash/version, tokens, usage/cost, outcome |
| Artifact | Type, path, hash, run revision, generation status |

Run states: `planned`, `queued`, `running`, `paused`, `completed`, `partial`, `failed`, `cancelled`. Task states: `pending`, `leased`, `running`, `succeeded`, `retry_wait`, `failed`, `skipped`.

Leases expire after worker loss and allow recovery. Checkpoints occur after every task. Idempotency keys include task type, input/content hash, protocol revision, model/prompt version, and relevant settings. Reusing retrieval cache must not silently reuse an obsolete scientific interpretation.

## 12. Overnight worker and budgets

- Detach through native process APIs; avoid shell command interpolation.
- Confirm successful worker startup through a durable handshake/heartbeat.
- Use per-run locks and leases to prevent duplicate work.
- Keep provider rate limits separate from model concurrency limits.
- Retry transient failures with jitter and bounded backoff; defer long waits through scheduler state.
- On cancellation, stop scheduling new tasks, terminate bounded child work where safe, checkpoint and export available results.
- On deadline/budget exhaustion, reserve time/tokens for a partial synthesis and status bundle.
- Enforce budgets before dispatch using reservations; reconcile with returned usage.
- Dollar costs are estimates unless authoritative provider usage is available. Enforce hard token/call ceilings regardless.
- An unknown model price cannot be treated as free. Show uncertainty and require an explicit policy.
- Heartbeat age reports stale/possibly interrupted; do not claim continued execution from a PID alone.
- No global change to host sleep settings; setup explains host requirements and can offer an explicit optional keep-awake setting later.

## 13. Configuration and secrets

Config precedence: explicit CLI arguments > environment variables > run/project config > user config > documented defaults. Save effective nonsecret configuration with each run. Store only secret variable references in plans/manifests.

```toml
[contact]
email = "REPLACE_WITH_REAL_EMAIL"

[providers.unpaywall]
enabled = true
email_env = "PAPERS_EMAIL"

[providers.crossref]
enabled = true

[providers.openalex]
enabled = true
api_key_env = "OPENALEX_API_KEY"

[providers.arxiv]
enabled = true

[model]
adapter = "openai_compatible"
base_url = "REPLACE_WITH_SELECTED_ENDPOINT"
model = "REPLACE_WITH_TESTED_MODEL_ID"
api_key_env = "PAPERS_LLM_API_KEY"

[execution]
network_workers = 4
model_workers = 2
request_timeout_seconds = 30
max_response_mb = 50
```

Optional future variables: `SEMANTIC_SCHOLAR_API_KEY`, `NCBI_API_KEY`, `NCBI_EMAIL`. No invented emails, bundled keys, or unnecessary required accounts. Support a local ignored `.env` file or environment variables; OS keyring is optional. Redact credentials from logs, URLs, errors, exports, model prompts, and diagnostic bundles. Send each provider's credential only to its approved service; strip authorization headers on cross-origin redirects.

`doctor` checks config syntax, contact setup, activated provider credentials, rate-budget accessibility when available, minimal model request/structured-output behavior, writable storage, migrations, required parsing libraries, exporter availability, and stale jobs. A provider intentionally disabled is different from a configured provider that failed.

## 14. Output bundle

| File | Contents |
|---|---|
| `review.md` | Canonical review with methodology, thematic synthesis, limitations, references |
| `review.pdf` | Rendered review with checked citations |
| `review.docx` | Editable review |
| `references.bib` | BibTeX bibliography |
| `references.csl.json` | CSL bibliography |
| `evidence.csv` | Findings, methods, limitations, evidence locations |
| `screening.csv` | Decisions and exclusion reasons |
| `search-log.jsonl` | Exact queries, pagination, timestamps, results/coverage |
| `citation-audit.json` | Bibliographic and claim-support results |
| `coverage-and-limitations.md` | Missing papers, incomplete searches, uncertain evidence |
| `run-summary.json` | State, limits, usage, counts, failures, artifact inventory |

Use one canonical structured draft/evidence registry for all renderers. PDF/DOCX rendering failures must not destroy Markdown, evidence, or references. Preflight required exporter dependencies; no silent missing “completed” artifacts. Include software/model versions and search dates in reproducibility metadata. Never count a found record as full text read.

## 15. Portable agent skill

Package a standard `SKILL.md` with front matter, a short workflow, command discovery through `papers schema`, configuration/preflight steps, result interpretation, and examples. Supporting references describe schemas, evidence rules, and recovery. Installation into an agent is explicit; exporting the folder does not install it everywhere.

The skill should tell the agent to use subprocess argument arrays, inspect status, respect budgets, select candidate identity carefully, treat documents as untrusted data, and avoid claiming completion from a successful launch. Support external agents without browser control. Optional MCP comes later and wraps the same core functions.

## 16. Security and operational requirements

Keep TLS verification enabled. Validate URLs and file paths; reject unsupported schemes and embedded credentials. For URL ingestion, prevent access to loopback/private/link-local networks by default, including redirects and DNS resolution, with an explicit local-development policy if necessary. Limit content sizes and parsing runtimes; isolate heavy PDF parsing in child processes. Do not run generated document code or shell fragments. Sanitize LaTeX/HTML export and disable shell escape. Keep raw third-party content outside trusted instruction channels.

Do not send source documents or private user briefs to an undeclared model provider. Configuration must make the selected reasoning endpoint visible. External notifications and webhooks are disabled by default and require explicit setup.

## 17. Migration from PaperCLI 0.1.0

Existing package: `agent-papercli`, console entry point `papers`. Earlier deliverable: `papercli-0.1.0.zip`.

Reuse validated download, hashing, page extraction, JSON output, and cache concepts after review. Remove Sci-Hub paths and reject old flags/configuration with actionable migration errors. Existing immutable PDFs can be imported; preserve old provenance, label identity checks accurately, and migrate manifests through versioned code. Do not silently reinterpret old `identity: unverified` as verified.

Keep `search`, `resolve`, `fetch`, `read`, `batch`, and `schema` recognizable where compatible. Removing Sci-Hub is an intentional breaking change; document the new package/schema versions. Do not claim existing tests prove the new pipeline.

## 18. Build sequence

1. Foundation: inspect current package, pin dependencies, remove Sci-Hub, define schemas/config, establish tests and migration.
2. Providers: live-test Unpaywall and metadata/discovery adapters, pagination, retries, provenance.
3. Storage/documents: durable tasks, source registry, parsing, evidence locations, imports.
4. Research: protocol generation, screening, extraction, bounded search expansion.
5. Reasoning: one tested model adapter, synthesis, critic, claim ledger, budgets.
6. Worker: detached execution, locks/leases, status, cancellation, recovery.
7. Export/skill: citations, outputs, portable skill, install documentation.
8. Acceptance: real-topic pilot, failure injection, overnight soak, package release.

Each phase must leave runnable commands and documented limitations. No broad “done” claim based only on unit tests or rendered output.

## 19. Acceptance tests

| Area | Pass condition |
|---|---|
| Sci-Hub removal | No runtime adapter/config/flags; old inputs return migration guidance |
| Fresh install | Clean supported Windows environment installs, passes preflight, runs sample |
| Unpaywall | Live DOI lookup and real valid PDF retrieval with actual user email |
| Unpaywall alternatives | First failed location falls back; closed record is distinguished from API outage |
| Discovery | Queries/cursors saved; provider failure never mislabeled as complete search |
| Identity | Wrong-paper PDF fixture is rejected/flagged despite being parseable |
| Extraction | Evidence locations and excerpts resolve to exact stored source text |
| Missing text | Scanned/partial papers are flagged and not represented as fully read |
| Screening | Decisions use saved criteria; duplicates retain provenance |
| Citation audit | Fabricated ID/key, wrong metadata, unsupported claim, and orphan citation are caught |
| Resume | Forced worker termination resumes without loss or duplicate committed tasks |
| Detached launch | Initiating terminal/agent exits while worker completes and saves outputs |
| Limits | Deadline/token/call/cost policy stops dispatch and preserves partial artifacts |
| Rate limits | 429/Retry-After honored with bounded recovery |
| Concurrency | Multiple callers cannot corrupt run state or double-spend budget reservations |
| Secrets | Logs, JSON, redirects, and export bundles contain no test secret values |
| Exports | References and claims match across Markdown, PDF, DOCX; failures reported explicitly |
| Live pilot | Owner checks known papers, thematic quality, findings, and reference accuracy |
| Overnight soak | Long run produces morning bundle or honest partial state with recoverable checkpoints |

Track quantitative test results: discovered/deduplicated/screened/retrieved/read/cited counts; retrieval coverage; sampled claim-support accuracy; reference-identity accuracy; cost and elapsed time. Report metrics honestly without promising a universal accuracy percentage.

## 20. Inputs required before live overnight use

- Real contact email for Unpaywall and chosen identification policies.
- Activated OpenAlex key for the overnight discovery profile.
- Selected model endpoint/model and locally configured credentials, or working local endpoint.
- Explicit token/call/runtime and monetary-budget policy.
- Research brief and desired output style.
- A powered, awake host and sufficient disk space.

These inputs do not block building the deterministic components after authorization. Do not request secrets in chat; use local setup. No accounts or jobs have been created by this specification.

## 21. Handoff instruction to the implementing agent

Treat this document as the product/build specification. First inspect the current PaperCLI code and identify reusable parts. Do not start implementation until Jayanth explicitly says “go.” After authorization, proceed through the build sequence, maintain checkpoints, and test actual behavior. Ask only for decisions or credentials that materially block the next dependent step. Keep all secret entry local. Deliver source, installation package, example brief/config, portable skill, validation results, and honest limitations. A working unattended evidence-backed review is the completion criterion.

## 22. Primary references

- Unpaywall API: https://unpaywall.org/products/api
- Unpaywall schema: https://unpaywall.org/data-format
- OpenAlex authentication: https://help.openalex.org/api/authentication/
- Crossref REST API: https://www.crossref.org/documentation/retrieve-metadata/rest-api/
- K-Dense source: https://github.com/K-Dense-AI/scientific-agent-skills
- PaperOrchestra source: https://github.com/google-research/paper-orchestra

Service contracts, quotas, model availability, and upstream implementations are time-sensitive. Verify official documentation and pin tested versions during implementation. The repository observations above describe the code inspected during the planning conversation; they are not blanket quality claims about either project.
