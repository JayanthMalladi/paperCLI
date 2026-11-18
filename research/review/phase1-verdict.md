# Phase 1 (Foundation) — OPUS Reviewer Verdict

Repo: D:/prime-agent-harness/paperCLI · branch `main` · commits 6393afd..637025c
Reviewer ran every checklist item by execution (fresh venv install, live CLI,
pytest, git-history greps, schema validation), not by reading claims.
Secret values are masked throughout this document.

## VERDICT: PASS

Zero blockers. Items 1–6 verified by the reviewer personally. Two **major**
spec deviations were found but neither blocks Phase 2; all are listed below
for the coding worker to fix in a follow-up commit.

## Checklist

| # | Item | Status | Evidence |
|---|------|--------|----------|
| 1 | Fresh install | verified-ok | Clean venv (`uv venv` + `uv pip install -e ".[dev]"`, CPython 3.12.8): build + install clean; `papers --version` → `papers 0.1.0`, `papers init` → exit 0, `papers doctor --json` → exit 0 on a fresh config with 4 warns/0 fails (`summary.healthy=true`). Report-not-gate matches spec §13: doctor returns exit 0 on completion; only an unexpected crash exits 1. |
| 2 | JSON envelope | issue (1 deviation) | `init`, `config show --redact`, `doctor --json`, `schema --list`, `search "x"` (exit 2, error code `NOT_IMPLEMENTED`) all emit exactly one parseable envelope on stdout; diagnostics (traceback) on stderr. **Deviation:** bare `papers schema` prints a bare `{"envelope":…,"brief":…}` dict and `papers schema --json` prints `{"schemas":…}` — neither has `schema_version`/`status`/`warnings` (spec §6: "Default stdout is one JSON envelope"; the published envelope schema even says "emitted by every papers command"). See defect D1. |
| 3 | Exit codes | verified-ok | 0 on success (init/config/doctor/schema/version); 2 on NOT_IMPLEMENTED (all 17 stubs) and on schema-validation failure; 1 on internal failure (triggered `schema --validate --input <missing>` → `INTERNAL_ERROR`, exit 1, traceback on stderr). No exit 3 anywhere in src/tests (`grep "exit(3)"` → 0 hits); 3 documented as reserved only in the `EXIT_CODES` table. |
| 4 | Redaction | verified-ok | Fake values (one `*_API_KEY`, one email, one LLM key) written to a scratch project `.env` and exported in the child process env; ran init, config show --redact, doctor, schema, search: **zero** raw-value hits in stdout+stderr. Git history (`git log --all -p`): repo `.env`'s email value `au…om` → 0 matches; OpenAlex key `Sq…cb` → 0 matches; LLM key `sk…zk` → 0 matches. `.env` was never committed and is `.gitignore`-covered (`git check-ignore` rc=0). Note: `PAPERS_LLM_BASE_URL` value `ht…ve` **is** in history via `research/provider-docs/llm-endpoint-probe.json` — it is a URL, not a credential; acceptable, but worth noting for the secret-scan patterns. The redaction test (`test_redaction.py`) asserts on real subprocess output, not a tautology. |
| 5 | No Sci-Hub | verified-ok | `git grep` over the whole tracked tree: zero functional mentions. Only allowed descriptive/migration-note hits: `docs/03-migration-note.md` (permitted), `ATTRIBUTION.md:11` ("no Sci-Hub paths", migration framing), and the spec file itself. No adapter, flag, env var, or config key. |
| 6 | Schemas | verified-ok | 9 schemas under `agent_contract/schemas/` (envelope, brief, plan, tasks, screening, evidence, claims, bibliography, run_summary); all pass `Draft202012Validator.check_schema`. `papers schema --list` surfaces all 9 with required fields. A live doctor envelope was validated programmatically against `envelope.json` → 0 errors. |
| 7 | Config precedence | verified-ok | End-to-end in a scratch project: user layer `network_workers=99` visible when project config silent; project `model.base_url` beats user; `PAPERS_MODEL_BASE_URL` env beats project; `cli_overrides` beats env. `sources` records the applied layers in order. `Config.snapshot()` returns `effective` (nonsecret) + `secret_refs` (env-var names only; verified no raw value in `json.dumps(snapshot)`), matching §13 "save effective nonsecret configuration". |
| 8 | Provider contract | verified-ok | `providers/base.py`: ABC `ProviderAdapter` with abstract `credential_status` + `preflight` (verified: is an ABC, abstractmethods = {credential_status, preflight}), `CredentialStatus` enum, frozen `ProviderInfo`. `providers/registry.py`: module-level `ProviderRegistry` with explicit `register()`/`get()`/`names()`; registry currently empty — Phase 2 adapters plug in by calling `register_provider` on import without core changes. |
| 9 | Storage | verified-ok | `storage/migrations.py` runner + `0001.sql` (10 tables covering all §11 entities). Verified idempotent: first run applies `[0001.sql]`, second run applies `[]`; `schema_migrations` tracks versions; one transaction per migration, `PRAGMA foreign_keys=ON`. Doctor's `db_migrations` check also applied cleanly in the scratch project. |
| 10 | CI | issue (scan no-op) | `.github/workflows/ci.yml`: clean-venv install + `pytest -q` + CLI smoke run + secret-scan job. **The key-pattern step is a no-op:** it embeds a PCRE lookahead `(?!…)` in `git grep -nE '…'` (POSIX ERE, which rejects it) and swallows the error with `\|\| true`, so the pattern step can never fail. Only the "committed .env files" and ".gitignore covers .env" steps actually run. Assessment: **insufficient** — see D2. |
| 11 | Attribution | verified-ok | `ATTRIBUTION.md` records greenfield decision B and both upstream pins (K-Dense `ec59a29…`, paper-orchestra `d9bae99…` with licenses); pins match `research/upstream/pinned-commits.txt` (commit 8da768f). `pyproject.toml` is MIT (`license = { text = "MIT" }` + OSI classifier). |
| 12 | Windows-safety | verified-ok | No `os.fork`, no `/bin/` paths, no `shell=True`, no shlex-based shell strings in `src/` (the only "shlex" hit is a docstring in `tests/conftest.py`). Tests drive the CLI via `subprocess.run` with argument arrays. `pathlib` + `sqlite3` throughout; migration DB paths absolute. |
| 13 | Test suite | verified-ok | `pytest -q` in a clean venv: **40 passed, 0 failed** (rc=0). Redaction tests run real CLI subprocesses and assert the fake secret values are absent from combined stdout+stderr — not tautological. Precedence tests isolate each layer; storage tests assert idempotent re-apply; envelope tests pin shape and closed-top-level-keys. |

## Defect list

- **D1 — MAJOR — `papers schema` default and `--json` output are not envelopes.**
  `src/papercli/cli/main.py:235-237` (`emit(out if not args.json else {"schemas": out})`).
  Spec §6: "Default stdout is one JSON envelope … Optional human formatting must be explicit."
  Fix: wrap both forms in the standard envelope, e.g. default → `ok(data={"schemas": out})` and drop/merge `--json` (or keep it emitting `ok(data=out)`); add a test asserting `papers schema` stdout parses with `status`/`schema_version` keys.
- **D2 — MINOR — CI secret-scan key-pattern step can never fail (no-op).**
  `.github/workflows/ci.yml:62` uses `git grep -nE 'sk-[A-Za-z0-9]{16,}|…|OPENALEX_API_KEY=(?!…)[A-Za-z0-9]{16,}|…'` — the PCRE lookahead is invalid under POSIX ERE, `git grep` errors out, and `|| true` swallows it, so `hits` is always empty. Fix: remove the lookahead (plain ERE `OPENALEX_API_KEY=[A-Za-z0-9]{16,}` is enough — a committed `KEY=<name>` self-reference is implausible), drop `|| true` so a regex error fails the step, and optionally add a negative/positive fixture assertion that the step catches a planted `sk-` token.
- **D3 — MINOR — `schema --validate` on a missing/unreadable `--input` file exits 1 (`INTERNAL_ERROR`) instead of 2 (input error).**
  `src/papercli/cli/main.py:211-215` (open/json.load) with the catch-all at `main.py:385-390`. Spec §6 exit table: 2 = "Input/configuration/preflight failure". Fix: catch `FileNotFoundError`/`json.JSONDecodeError` there and emit a `CONFIG_REQUIRED`-style error envelope with exit 2 (e.g. code `INPUT_FILE_NOT_FOUND`).
- **D4 — MINOR (hardening) — doctor masks secrets on the *serialized* envelope, so a secret containing JSON-escaped characters would leak in escaped form.**
  `src/papercli/cli/doctor.py` (end of `run_doctor`): `mask(json.dumps(envelope))` replaces raw values; if a secret value contains `"`/`\`, its JSON-escaped rendering won't match and survives into stdout. Current real-world values are alphanumeric, so no live leak — fix by redacting per-string-value in the envelope tree before serialization instead of on the serialized text.

## Observations (not defects)

- Bare `papers` (no subcommand) prints human help to stdout and exits 0; this is a gray zone vs "exactly one JSON envelope" but is consistent with the documented `--help`/`--version` human exceptions and is pinned by a test.
- Doctor has side effects (creates `.papercli/storage/`, applies migrations, writes+removes a probe file) — reasonable for a preflight, arguably part of the "writable storage" and "migrations" checks per §13.
- The repo `.env` holds live credentials and is git-ignored; history is clean for all four values except the base-URL host (URL, not a secret). Keep the CI .env-file check from D2's step intact.
