# User guide (Phase 7: install -> run -> read the bundle -> connect an agent)

## 1. Install

```
git clone https://github.com/JayanthMalladi/paperCLI
cd paperCLI
python -m venv .venv
.venv\Scripts\activate            # Windows (POSIX: source .venv/bin/activate)
pip install -e .
papers init
papers doctor --json
```

`papers doctor` is a preflight, not a gate: on a fresh unconfigured
project it exits 0 and reports warnings (missing contact email, no
model endpoint, exporter dependencies). Fix what the specific task
needs; a fresh install needs nothing to run the deterministic layers.

* **Exporters (Phase 7):** the `[exports]` extra installs
  `python-docx` + `pypandoc-binary` (which bundles the pandoc binary).
  On Python 3.14 these have no prebuilt wheels in some environments --
  the build/test venv is 3.12 (`uv venv --python 3.12`). PDF
  rendering additionally needs a LaTeX engine on PATH (pdflatex or
  xelatex; MiKTeX/TeX Live). If a renderer dependency is missing,
  `papers review export` still writes every other bundle file and
  records the missing one with a reason -- install
  `pip install -e ".[exports]"` and re-run export to fill it in.

## 2. Quickstart

```
papers init --env               # .papercli/config.toml + brief template + .env example
papers doctor --json            # preflight
```

1. Fill in `.papercli/config.toml`: `contact.email` (recommended --
   Crossref mailto) and, for model-driven synthesis, the `model`
   block (`base_url`, `model`, `api_key_env`).
2. Put real secret VALUES only in the local `.env` (git-ignored). The
   config stores environment-variable *references*, never values.
3. `papers doctor --json` confirms readiness; `papers config show
   --redact` shows the effective config with secrets masked.

## 3. Brief authoring

A brief is YAML (`examples/sample-brief.yaml` is the working
template). Required: `schema_version`, `question`; the essential-scope
preflight additionally requires a non-empty `purpose` and `scope`.
Recommended: `limits` (the budgets), `inclusion_criteria`,
`exclusion_criteria`, `themes_to_investigate` (organizes the
synthesis; without themes the draft is question-driven only),
`seed_identifiers` (known DOIs/arXiv ids to pull in first).

`papers review plan --brief brief.yaml --out review-plan.yaml`
validates the brief into a research protocol (database-specific query
variants, assumptions, resource estimate) and creates the planned
Run. A changed brief or config produces a new protocol REVISION --
never a silent mutation.

## 4. Run: foreground, detached, resume, cancel

```
papers review run --plan review-plan.yaml            # foreground
papers review run --plan review-plan.yaml --detach   # detached overnight
```

* **A successful detached launch means the worker ACCEPTED the job
  and holds its lock -- it is NOT a completion claim.** The actual
  state comes from:
  * `papers review status RUN_ID` -- state, task progress, heartbeat
    (running vs stale), budget usage. The worker is process-
    independent: a durable handshake + heartbeat, never a PID check.
  * `papers review logs RUN_ID [--follow]` -- worker log, one JSON
    envelope per line.
* **Interruption:** if the worker dies (kill, power loss), the run is
  checkpointed task-by-task; completed work is never re-done.
  `papers review resume RUN_ID --detach` re-launches it explicitly
  (pass `--brief` for the changed-brief check).
* **`papers review cancel RUN_ID`** stops new dispatch, terminates
  bounded in-flight work where safe, and writes the checkpoint +
  partial bundle (state `cancelled`).
* **Budgets:** at the wall-clock deadline or a budget ceiling the
  worker reserves the closure slice for a partial synthesis + status
  bundle. A budget DEFICIT (usage past a reservation) is disclosed in
  the run summary, never silently absorbed.

## 5. Interpreting the output bundle (every spec-14 file)

`papers review export RUN_ID --formats md,pdf,docx,bib,csl,csv`
writes the bundle into the run directory; `run-summary.json` carries
the per-file artifact inventory (a renderer that could not run is
`generation_status=missing` WITH a reason + an envelope warning; the
other files are never blocked or destroyed).

| File | What to read |
|---|---|
| `review.md` | The canonical narrative review: question + methodology, thematic synthesis (claims carry registry citation keys; unsupported/uncertain claims are prominently marked), contradictions, limitations, reproducibility block (software/model versions + search dates), references |
| `review.pdf` / `review.docx` | Rendered views of the SAME review.md (pandoc; shell escape disabled; python-docx fallback for docx) |
| `references.bib` | BibTeX bibliography generated ONLY from the bibliography registry (no invented entries; CSL-JSON-compatible fields) |
| `references.csl.json` | The same registry as CSL-JSON -- one canonical source, no drift from the .bib |
| `evidence.csv` | Findings, methods, limitations, access level, exact evidence locations (page/section/paragraph) |
| `screening.csv` | Screening decisions + exclusion reasons (criterion ids, rationale, reviewer) |
| `search-log.jsonl` | One envelope per recorded search: exact query, provider, filters, cursor/pagination, executed_at, result count, completeness, and the recorded error when a provider failed |
| `citation-audit.json` | The deterministic citation + claim-support audit: claim labels (supported / partially_supported / unsupported / uncertain / inference), citation-key validation against the registry, duplicate/orphan references, wrong-paper identity verdicts, the correction/retraction metadata check WITH its source + limits |
| `coverage-and-limitations.md` | Honest disclosure: searches that did not complete, papers with no full-text document, unresolved uncertain screenings, bibliographic gaps. **No exhaustive-coverage claim is made, ever** |
| `run-summary.json` | Run state (completed / partial / failed / cancelled -- never a false "completed"), limits, usage (cost figures are ESTIMATES unless authoritative provider usage was recorded; an unknown price is never treated as free), counts (discovered/dedup/screened/retrieved/read/cited), budget block (deficit + partial-synthesis reservation flags), failures, per-branch completeness, the artifact inventory, reproducibility metadata (the endpoint the model calls used is visible; the API key only as its env-var reference name) |

Plus the structured dumps every renderer reads from: `evidence.json`,
`claims.json`, `papers-registry.json`, `screening.json`.

**Read order for a finished run:** `run-summary.json` (state +
failures + budget) -> `coverage-and-limitations.md` (what is NOT
covered) -> `citation-audit.json` (which claims are actually
supported) -> `review.md` (the synthesis, with unresolved claims
marked) -> `references.bib` (for the bibliography style of your
choice, CSL/citeproc-compatible).

## 6. Agent-driven mode (zero credentials for the model)

Without a model endpoint configured, the deterministic layers still
run end-to-end: discovery, dedup, deterministic screening,
retrieval, evidence extraction into the registry. The model-driven
tasks are reported skipped (never faked). The agent supplies the
reasoning instead, through the lease-based task protocol:

```
papers task next --run RUN_ID --worker AGENT_ID    # lease the next task
# ... produce the result JSON for that task ...
papers task submit TASK_ID --input result.json
```

Submissions are validated explicitly: invalid, stale, duplicated, or
source-inconsistent payloads are REJECTED (and kept as audit rows);
an idempotent resubmission with the same payload hash returns the
prior result. The same protocol is what the detached worker uses,
so agent-driven and standalone runs share one queue and one
idempotency space.

## 7. Connecting an external agent session (MCP)

`papers mcp serve` (or `uv run papers mcp serve`) runs the stdio MCP
server: 18 tools wrapping the CLI commands 1:1, every result the
CLI's JSON envelope. See **docs/04-mcp-connection.md** for the
handshake + Python snippet.

## 8. Security notes (spec 16)

* TLS verification stays ON. The guarded HTTP layer validates URLs
  and rejects unsupported schemes and embedded credentials; it
  blocks loopback / private / link-local targets BY DEFAULT
  (including redirects and DNS resolution) -- there is an explicit
  local-development policy for when that is genuinely needed.
* Content sizes and parsing runtimes are bounded; heavy PDF parsing
  runs in an isolated child process.
* **No generated document content is ever executed** -- no code or
  shell fragments found in papers run; LaTeX/HTML export is
  sanitized (shell escape off, user content escaped).
* Raw third-party content stays outside trusted instruction channels
  (documents are data, not instructions).
* The selected reasoning endpoint is VISIBLE in config and in the
  run summary; the API key is stored only as its environment-
  variable reference name, never its value. `config show --redact`
  and `doctor` mask secrets in their output.
* External notifications / webhooks are DISABLED by default and
  there is no built-in path to enable them.

## 9. Honest limitations

* **Deterministic vs model-driven.** Discovery, dedup, the
  deterministic screening layer, retrieval, evidence indexing, the
  citation/claim ledgers, the audit, and the exporters are
  DETERMINISTIC code. Screening of uncertain records, finding
  extraction, synthesis, and critique are MODEL-DRIVEN when a model
  endpoint is configured -- their quality depends on that endpoint,
  and their prompt versions are recorded for reproducibility.
* **Keyless Semantic Scholar degrades, it does not fail:** 429/403 /
  misses become recorded skips (the search is marked incomplete for
  that branch), never a run failure and never a "complete" claim.
* **Renderer preflight:** pdf/docx require the `[exports]` extra
  (pypandoc bundles the pandoc binary; pdf additionally needs a
  LaTeX engine on PATH). A missing dependency records the artifact
  `missing` with the exact reason -- install the extra + an engine
  and re-export.
* **Retractions:** the audit records correction/retraction metadata
  AVAILABILITY (its source + limits) -- it is an availability check,
  never a ruling that no retractions exist.
* **Coverage:** a discovered record is not a full-text read; a
  provider failure is an incomplete search; the bundle discloses
  both and makes no exhaustive-coverage claim.
