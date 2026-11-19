# PaperCLI (`agent-papercli`)

An agent-driven, evidence-backed literature-review CLI. You supply a
research brief and resource limits, leave the computer, and get back a
complete output bundle — or a clearly labeled partial one — with every
substantive claim linked to identified source evidence.

This is **Phase 1 (Foundation) + Phase 2 (Providers)** of the build.
The machine contract, configuration system, JSON Schemas, provider
extension point, storage migration framework, preflight doctor, and
test/CI scaffolding are in place. Phase 2 adds the live provider
adapters (Unpaywall, Crossref, OpenAlex, arXiv, Semantic Scholar
enrichment) and the `search` / `resolve` / `fetch` commands running
against the real APIs behind a shared guarded HTTP layer (TLS on, URL
guard, per-provider pacing, bounded retry, retrieval cache). Phase 3
adds the storage entities (Task/Run/Evidence + document parse-quality
fields), the lease-based task queue, isolated document parsing (PDF / 
repository XML / HTML, with scanned-page flagging, the wrong-paper
identity guard, and exact evidence-location resolution), and the
`read` / `ingest` commands. The research pipeline, the model adapter,
the detached worker, and exporters come in later phases. See **What is
implemented vs not** below.

## Install

```
git clone https://github.com/JayanthMalladi/paperCLI
cd paperCLI
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e .
```

(POSIX: `source .venv/bin/activate`.)

## Quickstart

```
papers init                # creates .papercli/config.toml + a brief template
papers init --env          # also drops a .env example (local, git-ignored)
papers doctor --json       # preflight: config, credentials, model endpoint, storage, migrations, exporters
```

1. `papers init` writes `.papercli/config.toml` (secrets are
   environment-variable *references*, never values) and a research brief
   template.
2. Fill in `contact.email`, then put real secret values in a local `.env`
   (copy the shown example; it is git-ignored).
3. `papers doctor --json` reports readiness. It exits 0 on a fresh
   unconfigured project (warnings are reported, not treated as fatal).

Discover the machine contract without platform-specific integration:

```
papers schema --list
papers schema
```

## Command surface

Everything in the full contract is implemented (Phases 1–7):

| Command | Status |
|---|---|
| `papers init [--env]` | implemented |
| `papers config show --redact` | implemented |
| `papers doctor --json` | implemented |
| `papers schema [--list \| --validate]` | implemented |
| `papers search "Q" --providers crossref,openalex,arxiv,s2 --limit 50` | implemented (Phase 2) |
| `papers resolve "DOI\|arXiv-id"` | implemented (Phase 2) |
| `papers fetch "IDENTIFIER" --out ./papers` | implemented (Phase 2, retrieval order) |
| `papers read FILE --pages 1-3 --max-chars 18000` | implemented (Phase 3) |
| `papers ingest FILE --run RUN_ID` | implemented (Phase 3, wrong-paper guarded) |
| `papers version`, `papers --help` | implemented (human-readable exceptions) |
| `papers review plan / run / status / logs / resume / cancel / export` | implemented (Phase 4 plan; Phase 6 lifecycle; Phase 7 export) |
| `papers task next / submit` | implemented (Phase 4 lease protocol; Phase 6 CLI) |
| `papers evidence list` | implemented (Phase 4) |
| `papers citations audit` | implemented (Phase 5) |
| `papers skill export --out DIR` | implemented (Phase 7) |
| `papers mcp serve` | implemented (Phase 7, stdio MCP server) |

The full contract (all commands exist and are parseable; `skill export`
is implemented in Phase 7; see "What is implemented vs not" for the
current truth):

```
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
papers mcp serve
```

## MCP server (connect an external agent session, Phase 7)

`papers mcp serve` runs a stdio Model-Context-Protocol server whose
tools wrap the CLI's core functions 1:1 -- the tool surface IS the CLI
contract (no second schema that could drift): every `tools/call` is
converted to CLI argv and dispatched through the same `main()`, and
each result is the CLI's JSON envelope (success, error codes, and
warnings included).

Tools: `search`, `resolve`, `fetch`, `read`, `ingest`, `review_plan`,
`review_run`, `review_status`, `review_logs`, `review_resume`,
`review_cancel`, `review_export`, `task_next`, `task_submit`,
`evidence_list`, `citations_audit`, `doctor`, `schema`.

Connect an agent session by launching the server as a stdio child and
speaking the MCP protocol over its stdin/stdout (newline-delimited
JSON-RPC 2.0: `initialize` -> `initialized` -> `tools/list` /
`tools/call`):

```
# From the project root (Windows console):
uv run papers mcp serve
# or a stdio subprocess, e.g. in Python:
import subprocess, json
p = subprocess.Popen(["papers", "mcp", "serve"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     text=True, cwd=".")
p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method":
                          "initialize", "params":
                          {"protocolVersion": "2024-11-05"}}) + "\n")
# ... read responses one line at a time; see the `schema` tool for the
# full machine contract.
```

The transport is a small pure-Python JSON-RPC implementation
(`src/papercli/agent_contract/mcp_server.py`, ~the handshake +
`tools/list` + `tools/call` over stdio) with no third-party MCP
dependency. Unknown tools/methods return structured JSON-RPC or
envelope errors, never a crash; a live `search` still needs the
providers' network/credentials as with the CLI.

## Machine output

Default stdout is **one JSON envelope**. Batch and follow-log streams use
one envelope per line. Diagnostics go to stderr. `--help` and `--version`
are the human-readable exceptions.

```json
{"schema_version":"1.0","status":"ok","data":{"run_id":"opaque-id","state":"running"},"warnings":[]}
```

```json
{"schema_version":"1.0","status":"error","error":{"code":"CONFIG_REQUIRED","message":"Configure the Unpaywall contact email.","retryable":false,"details":{"field":"contact.email"}}}
```

Exit codes: `0` success · `1` internal · `2` input/config/preflight (and
`NOT_IMPLEMENTED`) · `3` reserved (removed legacy verification) · `4`
retrieval unavailable · `5` network/provider · `6` partial run · `7`
run paused/limit reached · `130` interrupted.

## Configuration and secrets

Precedence: **CLI args > environment variables > run/project config
(`.papercli/config.toml`) > user config > documented defaults.**

Secrets are stored **only as env-var references** in committed files.
Values live in the environment or a local, git-ignored `.env`. The config
template matches the spec example. Effective non-secret configuration can
be saved with each run (`Config.snapshot()` is the designed hook): it keeps
secret fields as their variable-name references, never the values.

## JSON Schemas

Published under `src/papercli/agent_contract/schemas/`, versioned
independently of the package (`schema_version` is currently `1.0`):
`envelope`, `brief`, `plan`, `tasks`, `screening`, `evidence`, `claims`,
`bibliography`, `run_summary`. `papers schema` prints or validates
against them.

## Run-time budget guardrails (defaults)

The brief/plan template carries the documented ceilings (spec §7):
`wall_minutes 360 · max_search_queries 30 · max_discovered_records 1000 ·
max_full_text_papers 60 · max_model_calls 300 · max_input_tokens 1000000 ·
max_output_tokens 150000 · max_estimated_cost_usd null`. These are example
ceilings, not promised throughput or cost. A null monetary cap is only
valid with hard token/call limits set.

## What is implemented vs not (Phases 1–7, honestly)

**Implemented**
- `init`, `config show --redact`, `doctor --json`, `schema`, `version`,
  `--help` — all emit one JSON envelope with the spec exit-code table.
- Live provider adapters (Phase 2): **Unpaywall** (DOI OA lookup; closed
  records are valid; PDF locations before landing pages), **Crossref**
  (DOI metadata + bibliographic search, polite pool via mailto, cursor
  paging), **OpenAlex** (discovery + filters + cursor paging; sends the
  key when set, records the USD-credit budget concept; a provider failure
  is never a complete search), **arXiv** (id normalization + Atom metadata
  + version-preserving PDF URLs, 1-req/3-s single-connection pacing),
  and **Semantic Scholar** as optional keyless enrichment (429/403/miss →
  recorded skip, never a run failure).
- `papers search`, `papers resolve`, `papers fetch` are live: one JSON
  envelope with records, per-provider provenance (query, date, cursor,
  count, completeness flag), and dedup; Search/Paper/Document rows are
  persisted in SQLite. `fetch` runs the spec retrieval order
  (local cache → Unpaywall locations → repository/publisher full text →
  record unavailable), byte-validates PDFs/HTML/XML, and returns
  `access_level` (full_text / html_landing / unavailable; unavailable is
  exit 4).
- Document parsing + storage (Phase 3): `papers read FILE --pages 1-3
  --max-chars 18000` extracts numbered pages / section / paragraph
  locations with a JSON envelope (text, locations, warnings);
  `papers ingest FILE --run RUN_ID` imports a local PDF/HTML/XML into
  the run's Document registry, reusing the Phase-2 hash + source +
  access-status persistence. PDF parsing runs in an isolated child
  process (spec 16); scanned/empty pages are flagged (OCR out of scope,
  disclosed in the warnings), and a parsed document whose embedded
  title/DOI does not match the requested paper is rejected (wrong-paper
  guard), never silently accepted.
- Durable entities + task queue (Phase 3): migrations `0002`/`0003` add
  Task/Run/Evidence fields (idempotency key = task type + input/content
  hash + protocol revision; lease TTL with lost-worker recovery) and a
  `document_text` index so every evidence excerpt resolves to exact
  stored source text. A lease-based claim protocol
  (`claim → leased → running → succeeded/failed/retry_wait`) is shared
  by standalone workers and external agents.
- Full JSON envelope + error shape; `NOT_IMPLEMENTED` stubs for every
  other contract command (each documents its planned phase).
- Layered config with the documented precedence, `.env` support, and
  secret-reference-only storage. `Config.snapshot()` hook for saving
  effective nonsecret config per run.
- Nine published JSON Schemas with round-trip validation.
- Provider extension contract: abstract base + registry, live adapters,
  a shared guarded HTTP layer, and the retrieval cache (Phase 2).
- SQLite migration framework + `migrations/0001.sql` (durable entities);
  `0002`/`0003` extend Task/Run/Evidence/Document in place (Phase 3).
- Preflight doctor: config syntax, contact email, provider credentials,
  model-endpoint reachability/capability (report-only when the key is
  missing), writable storage, migration runner, exporter availability,
  and full-output secret redaction.
- pytest suite + CI (clean-venv install, `pytest`, secret-scan).


**Phase 4 – research pipeline** (deterministic core)
- Protocol generation from a validated brief, query expansion,
  screening, and the deterministic layers (discovery → dedup →
  screening → expansion).
- Durable task DAG + lease-based queue shared by standalone workers
  and external agents (idempotent by task type + input hash +
  protocol revision).

**Phase 5 – model-driven reasoning + citations**
- An OpenAI-compatible model adapter with a shared model budget +
  rate limiter (reservations before dispatch; a deficit is
  disclosed, never silently absorbed).
- Screening of uncertain records, finding extraction, theme-organized
  synthesis, and bounded critique: every claim carries evidence ids +
  registry citation keys; the model may SELECT references but cannot
  invent bibliography entries (registry-only, spec 10).
- `papers citations audit`: deterministic claim-support + reference-
  identity checks (key validation, duplicates, orphans, wrong-paper
  verdicts, correction/retraction metadata availability WITH its
  limits).

**Phase 6 – the detached worker + run lifecycle**
- `papers review run --detach`: a process-independent worker (durable
  handshake + heartbeat, never a PID check), lock-guarded single
  writer, checkpoint after every task.
- `status` / `logs [--follow]` / `resume [--detach]` / `cancel`:
  explicit recovery, a changed-brief check, and a cancel that writes
  the checkpoint + partial bundle.
- The Phase-6 status surface: `run-summary.json` builder (honest
  state / limits / usage / counts / budget / failures / artifact
  inventory / reproducibility metadata) and the export bundle surface.

**Phase 7 – MCP + export + skill + docs**
- The rendered exporters: `review.md` (canonical narrative review),
  `references.bib` + `references.csl.json` (registry-only, no
  drift), `review.pdf` + `review.docx` (from the SAME review.md; a
  missing renderer dependency is recorded `missing` WITH a reason,
  never silent), plus `coverage-and-limitations.md` +
  `citation-audit.json` (spec 14).
- `papers mcp serve`: a stdio MCP server wrapping the CLI commands
  1:1 (the tool surface IS the CLI contract; results are the CLI
  envelopes).
- `papers skill export --out DIR`: the portable agent skill
  (SKILL.md + references; exporting does not install it).
- The user guide: install → run → read the bundle → connect an agent
  (`docs/05-user-guide.md`), the MCP connection guide
  (`docs/04-mcp-connection.md`), and the honest limitations above.

**Not yet implemented / out of scope (spec section 2)**
- Browser automation, paywall bypass (Sci-Hub), CAPTCHA solving, and
  code execution of content found in papers (spec section 16: never
  in scope).
- External notifications / webhooks (disabled by default; no built-in
  path).
- Automatic meta-analysis / statistical synthesis; the pipeline
  produces an evidence-linked narrative/systematic-style review, not
  pooled estimates.
- A universal exhaustive-coverage guarantee (spec section 19: an
  honest count of what was found, never a claim that all relevant
  papers were found).
- Web dashboard; the machine contract is the output bundle + the MCP
  server.
- GUI / interactive TUI; the CLI is machine-first (one JSON envelope
  per command).

## Live provider smoke tests

The network smoke tests are marked `@pytest.mark.network` and skip when
offline. They exercise one Unpaywall DOI lookup, one Crossref works query,
one OpenAlex works query (with and without a key), one arXiv `id_list`
fetch, and one keyless Semantic Scholar call that expects a graceful skip.

```
python -m pytest -m network
```

## Development

```
pip install -e ".[dev]"
python -m pytest
```

Tests exercise the CLI through a Python subprocess (argument arrays, no
shell — Windows-safe). They run without a separate console install.

## License

MIT. See `ATTRIBUTION.md` for upstream design references and the greenfield
decision.

See `docs/` for build notes, the provider facts used, and migration notes.
