# Build notes — Phase 1 (Foundation)

What this phase ships, what is intentionally deferred, and the design
decisions a reviewer should scrutinize.

## Scope

Phase 1 establishes the durable foundation: the machine contract,
configuration, JSON Schemas, the provider extension point, the storage
migration framework, the preflight doctor, and the test/CI scaffolding.
It stops just short of any live provider, document, research, worker, or
export logic — those are Phases 2–7.

## Design decisions (scrutinize these)

### 1. CLI framework: stdlib `argparse`, not click

`click` is deliberately **not** a dependency. The CLI is built on
`argparse` because:

- It is stdlib — no extra runtime dep to pin or break.
- The contract needs to be parseable and machine-inspectable; a hand-built
  parser gives full control over the envelope/exit-code behavior.
- All documented command arguments are declared up front so that every
  contract command parses and returns a clean `NOT_IMPLEMENTED` envelope
  instead of an argparse usage error.

### 2. TOML parsing: `tomllib` with a `tomli` fallback

Python 3.10 has no stdlib `tomllib`; 3.11+ does. The loader tries `tomllib`
first, then `tomli`. `tomli` is a conditional dependency
(`python_version < '3.11'`). No third-party TOML lib is required on 3.11+.

### 3. Secret handling: references, never values

Config files hold `*_env` references (e.g. `api_key_env = "OPENALEX_API_KEY"`).
Values are read from the process environment or a local, git-ignored
`.env` **at call time** and never persisted. `Config.snapshot()` saves the
effective non-secret config with secret fields kept as their variable-name
references. The doctor and `config show --redact` mask every tracked value
before emitting.

### 4. Doctor is a report, not a gate

`papers doctor --json` exits 0 whenever it completes, even with
warn/fail individual checks. A fresh unconfigured project runs green
(healthy = no hard failures). This is what makes the "fresh install
passes preflight" acceptance hold. An unexpected crash inside doctor
still exits 1.

### 5. Schema validation: structural fallback + optional `jsonschema`

Every schema validates structurally (required keys) with stdlib only, so
`papers schema` always works on a bare install. When the optional
`jsonschema` package is present, full draft-2020-12 validation runs.
`jsonschema` is pinned as a core dep for the tests.

### 6. Windows-first portability

- `pathlib` everywhere; no POSIX-only calls.
- Subprocess and shell use argument arrays only — no `shlex`, no shell
  interpolation.
- The test suite launches the CLI via `python -c` shims (no separate
  console install required) with argument arrays.

## Deferred to later phases

- **Phase 2 — Providers:** live Unpaywall/Crossref/OpenAlex/arXiv adapters,
  pagination, retries, provenance. The `ProviderAdapter` base + registry are
  in place; no network code yet.
- **Phase 3 — Storage/documents:** the migration runner and `0001.sql`
  entities exist; document hashing, page parsing, imports are not.
- **Phase 4 — Research:** protocol generation, screening, extraction,
  bounded search expansion.
- **Phase 5 — Reasoning:** the tested OpenAI-compatible model adapter,
  synthesis, critic, claim ledger, budgets.
- **Phase 6 — Worker:** detached execution, locks/leases, status,
  cancellation, recovery.
- **Phase 7 — Export/skill:** citations, md/pdf/docx/bib/csl/csv, and the
  portable skill.

## Provider facts (corrected)

See `docs/02-provider-facts.md` for the verified, corrected provider facts
used by the preflight and later adapters.
