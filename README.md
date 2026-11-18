# PaperCLI (`agent-papercli`)

An agent-driven, evidence-backed literature-review CLI. You supply a
research brief and resource limits, leave the computer, and get back a
complete output bundle — or a clearly labeled partial one — with every
substantive claim linked to identified source evidence.

This is **Phase 1 (Foundation)** of the build. The machine contract,
configuration system, JSON Schemas, provider extension point, storage
migration framework, preflight doctor, and test/CI scaffolding are in
place. Live provider work, document parsing, the research pipeline, the
model adapter, the detached worker, and exporters come in later phases.
See **What is implemented vs not** below.

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

Implemented in Phase 1:

| Command | Status |
|---|---|
| `papers init [--env]` | implemented |
| `papers config show --redact` | implemented |
| `papers doctor --json` | implemented |
| `papers schema [--list \| --validate]` | implemented |
| `papers version`, `papers --help` | implemented (human-readable exceptions) |

The full contract (all commands below exist and are parseable; every
unimplemented one returns a well-formed `NOT_IMPLEMENTED` envelope with
`exit 2` and documents its planned phase):

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
```

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

## What is implemented vs not (Phase 1, honestly)

**Implemented**
- `init`, `config show --redact`, `doctor --json`, `schema`, `version`,
  `--help` — all emit one JSON envelope with the spec exit-code table.
- Full JSON envelope + error shape; `NOT_IMPLEMENTED` stubs for every other
  contract command (each documents its planned phase).
- Layered config with the documented precedence, `.env` support, and
  secret-reference-only storage. `Config.snapshot()` hook for saving
  effective nonsecret config per run.
- Nine published JSON Schemas with round-trip validation.
- Provider extension contract: abstract base + registry (no live network
  code yet).
- SQLite migration framework + `migrations/0001.sql` (durable entities).
- Preflight doctor: config syntax, contact email, provider credentials,
  model-endpoint reachability/capability (report-only when the key is
  missing), writable storage, migration runner, exporter availability,
  and full-output secret redaction.
- pytest suite + CI (clean-venv install, `pytest`, secret-scan).

**Not yet implemented (later phases)**
- Live provider adapters (Unpaywall, Crossref, OpenAlex, arXiv) and
  retrieval/search.
- Document parsing, evidence locations, imports.
- Research pipeline: protocol generation, screening, extraction,
  synthesis, citation audit.
- The tested OpenAI-compatible model adapter and budgets.
- Detached worker, locks/leases, status, cancellation, resume.
- Exporters (md/pdf/docx/bib/csl/csv).
- Portable agent skill.

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
