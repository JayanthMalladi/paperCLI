# Phase 7 (MCP + Export + Skill + Docs) — OPUS Reviewer Verdict

Repo: `D:/prime-agent-harness/paperCLI` · branch `main` · Phase 7 = 4 commits
(`c20d01a`,`f7dca81`,`b723cc4`,`b36023b`), HEAD `b36023b` == origin/main, clean tree.
Phases 1–6 are gated and NOT re-litigated; the renderer work builds on Phase 6's
`write_structured_dumps` + the D-P6-1 bib-deferred fix.

Reviewed by execution in a **fresh `uv venv --python 3.12` (CPython 3.12.8) `.vJ`**
(`uv pip install -e . pytest`, then `uv pip install -p .vJ "agent-papercli[exports] @ ."`
→ `pypandoc-binary==1.14` + `python-docx==1.2.0`) AND a **second bare `.vK`** without the
extra (to test the degraded path). Live probes: a deterministic seeded COMPLETED fixture
run in scratch project dirs (`C:\Users\Jayanth\p7_scratch` / `p7_scratch_k`), the real
`papers review export` surface, a stuck-renderer kill probe, a real external-style MCP
stdio session, `papers skill export`, and a tree-wide secret/Sci-Hub/POSIX grep. No
live model calls (rate-limited endpoint respected); one bounded MiKTeX render (engine was
warm — pdflatex returned in ~2 s, not stuck). No secret values printed (masked by name).

## VERDICT: **PASS**

Phase 7 introduces **zero blockers and zero majors**. All 11 checklist items verified by
execution: **282 passed / 0 failed** in a fresh 3.12 venv; the full 11-file spec-14 bundle
renders from one canonical source with honest missing-renderer handling; bib/CSL are
registry-only with **no field drift**; the citation-audit catches a fabricated key; the
LaTeX pass has **shell-escape off** and user content escaped; the stdio MCP server
completes a real handshake + tools/list + tools/call with schemas mirroring the live CLI
parser (an external session drove it end-to-end); the SKILL.md + docs are complete, honest,
secret-free, and Sci-Hub-free; and the `[exports]` extra installs on 3.12. One **MINOR
cross-phase observation** (citation-key shape sensitivity, Phase-5) is listed below; it is
**not** a Phase-7 regression and does not gate Phase 7.

## Test results (real counts, fresh venvs)

| Suite | Result |
|---|---|
| Full non-network, fresh `.vJ` (3.12, `[exports]` installed) `pytest tests -m "not network" -q` | **282 passed, 7 deselected, 0 failed** (125 s) |
| `[exports]` extra install on 3.12 (`uv pip install -p .vJ "agent-papercli[exports] @ ."`); the bare `.vK` (no extra) | pypandoc-binary==1.14 + python-docx==1.2.0 resolved; bare venv confirmed `pypandoc`/`docx` importable=False |
| `ci/secret_scan.py` (tree + full git history) | exit 0, clean |

## Per-checklist table

| # | Item | Status | Evidence (verified by execution) |
|---|------|--------|-----------------------------------|
| 1 | Full non-network suite green, no P1–6 regression | PASS | **282 passed / 7 deselected / 0 failed** in fresh `.vJ`; `test_phase7_*` files (exports/mcp/skill) all green, Phase-6 acceptance/CLI/run-summary tests re-ran green in the same pass |
| 2 | Export bundle — the full 11 spec-14 files from one canonical source | PASS | `papers review export RUN_ID --formats md,pdf,docx,bib,csl,csv` in the `[exports]` venv → exit 0, envelope `state=completed`; **all 11 files complete** on disk. (a) review.md = canonical narrative: Methodology, Thematic synthesis, Contradictory evidence, Limitations, **Reproducibility block** (`agent-papercli 0.1.0 (python 3.12.8)`, `Model: claude-sonnet-5-5 @ https://model.endpoint.example/v1` [key only as env-ref], **Search dates**, protocol revision/hash) + References. (b) references.bib + references.csl.json from the SAME registry: csl keys == bib keys == registry keys (`author2019c5a0`,`author20213512`,`author20225ac7`), no invented entries, fields consistent (arxiv → report, doi → article). (c) review.pdf = real `%PDF-1.5` (144 KB, 3 `/Type /Page` markers; pdftotext recovers the full narrative) + review.docx = valid OOXML zip (`word/document.xml`, `[Content_Types].xml`; contains the same title/sections) — both rendered from the SAME review.md via pandoc; **no leftover `*.src.md` scratch**. (d) evidence.csv (2 rows), screening.csv (3 rows), search-log.jsonl (2 envelope lines, **the recorded `HTTP 503` error now persisted**), citation-audit.json, coverage-and-limitations.md all present + well-formed. (e) coverage md is HONEST: "no exhaustive-coverage claim", discloses the incomplete openalex search + the failed request + no-full-text papers + orphan refs |
| 3 | Renderer-failure honesty (no silent missing 'completed' artifacts) | PASS | In the **bare `.vK`** (no pypandoc/docx/latex), the same export → the 9 pure-python files (md, bib, csl, evidence.csv, screening.csv, search-log.jsonl, citation-audit.json, coverage-and-limitations.md + run-summary.json) are all **written + valid**, while `review.pdf`/`review.docx` are **absent** and recorded `generation_status=missing` **WITH an explicit reason** in both `run-summary.json`'s artifact inventory and the envelope **warnings** ("pdf renderer unavailable: the 'pypandoc' module is not installed…"). A `completed` run never claims the missing rendered artifacts exist. No partial/scratch files left |
| 4 | Renderer preflight + execution bounds | PASS | Preflight: `make_renderer_env` (pypandoc/docx/`shutil.which` engine) — missing dep → recorded-missing, never a crash (proven in item 3). Bounds: constants `PDF_RENDER_TIMEOUT_SECONDS=180` / `DOCX_RENDER_TIMEOUT_SECONDS=60`; `_run_pandoc` runs pandoc as an arg-array Popen with `CREATE_NEW_PROCESS_GROUP`, on `TimeoutExpired` → `_kill_tree` (`taskkill /T /F` on Windows, `killpg` POSIX-gated). **Direct probe**: a 300 s "stuck engine" process with a 3 s limit → returned in ~3.5 s, `RenderResult(ok=False, "render timed out after 3s (a stuck external LaTeX engine is recorded missing, not a hang)")`, and the spawned process was confirmed dead (process tree terminated). A stuck engine can never hang the run |
| 5 | Citation quality (spec 10) | PASS | citation-audit.json validates keys against the registry, lists `duplicate_keys` + `orphan_references` + `unregistered_keys_used`, and a claim with no linked evidence is downgraded (DOI-resolution-only never sets `supported` — the audit row discloses it). **The audit catches a fabricated key**: the claim citing `smith2021zzz` (not in the registry) is flagged in `unregistered_keys_used` + in that claim's `disclosures`, and it also appears in coverage-and-limitations.md's "unregistered key(s)" count |
| 6 | Security (spec 16) | PASS | The PDF pass runs pandoc with `-V shell-escape=off` (only occurrence; no `--shell-escape`/enable anywhere). User/model content is fenced-escaped for the markdown/LaTeX sink (`\`,`%`,`$`,`&`,`backtick`) and bib-escaped for the .bib sink; the rendered PDF shows `%50 B & C` as literal text, not an executed/injected construct. No generated-document content is executed. External notifications/webhooks are **disabled by default** (no built-in path to enable; docs state it). No secret values in any bundle file (the endpoint is visible per spec-16; the API key only as its env-ref name). Secret scan clean |
| 7 | MCP server (user priority) | PASS | A real external-style stdio session spawned `papers mcp serve` and completed **initialize (2024-11-05, serverInfo `papercli-mcp` 0.1.0) → initialized → tools/list → tools/call**. tools/list returned **18 tools**, each with an `inputSchema` (type object + required); the dedicated drift-guard test (`test_mcp_tool_surface_is_1_to_1_with_the_cli_commands`) passes — every tool name maps to a live CLI subparser choice, so params mirror the CLI (no schema duplication). `tools/call` to deterministic commands (`schema --list` → ok, 9 schemas; `doctor --skip-network` → ok; `citations_audit` on the fixture → ok, surfaces `unregistered_keys_used`) returns the CLI JSON envelope in `result.content[0].text`. Unknown tool → envelope error `UNKNOWN_TOOL` (`isError=true`, **no crash**); unknown method → `-32601`; malformed line → `-32700`; clean EOF → exit 0. Newline-delimited JSON-RPC; spawn is `uv run papers mcp serve` (documented). An external agent session can drive it end-to-end with no platform-specific wiring |
| 8 | Portable skill (spec 15) | PASS | `papers skill export --out DIR` → exit 0, writes `SKILL.md` (YAML front matter: `name`+`description`) + 4 reference docs. The SKILL.md carries **all required sections** (Ground rules, Command discovery, Configuration/preflight, Short workflow, Result interpretation, Examples) and the **ground rules**: subprocess arg arrays (no `shell=True`), inspect status / "a successful launch is NOT completion", respect budgets, select candidate identity carefully, treat documents as untrusted data (no code execution, **no browser control**). **Exporting does NOT install** it (the envelope says so; spec-15). Exported SKILL.md is **byte-identical** to the repo-root SKILL.md |
| 9 | Docs (spec 15/16 + the user's docs ask) | PASS | `docs/04-mcp-connection.md` (handshake + spawn snippet + tool table + result-reading + security notes) and `docs/05-user-guide.md` cover install, quickstart, brief authoring, run/detach/resume/cancel, interpreting **every spec-14 file** (read order + per-file table), the MCP connection, agent-driven zero-credential mode (lease task protocol), security notes, and HONEST limitations (deterministic vs model-driven, keyless S2 degrade, renderer preflight, retraction-check limits, no exhaustive-coverage claim). README "implemented vs not" is final. **No secret values** and **no functional Sci-Hub mention** in docs/ (only the permitted descriptive out-of-scope note) |
| 10 | `[exports]` extra install-resolvable on 3.12 | PASS | `uv pip install -p .vJ "agent-papercli[exports] @ ."` succeeded; `pypandoc-binary==1.14` (the pin — 1.14.1 doesn't exist on the index) + `python-docx==1.2.0` both resolved and installed |
| 11 | No functional Sci-Hub; no POSIX-only calls; Windows-safe; secret scan clean | PASS | `git grep -in sci-hub` tree-wide → only the permitted descriptive/migration notes (ATTRIBUTION, the handoff spec itself, docs/03, prior verdicts, the Phase-7 skill test's own guard); **zero** in `src/`/`tests/`. No POSIX-only calls in the Phase-7 code: `os.killpg`/`taskkill` in `renderers._kill_tree` are correctly gated (`if sys.platform=="win32"`), `fcntl`/`os.fork`/`os.setsid`/`pty`/`os.system`/`shell=True` absent; subprocess is arg-array only. `ci/secret_scan.py` exit 0 |

## Rulings on the 5 flagged items

**(a) MiKTeX cold/stuck packagemanager on first LaTeX runs.** **Ruled: the bounded-timeout + honest-skip is the CORRECT design; do NOT gate on it.**
A stuck external engine is exactly the case spec-16's "limit content sizes and parsing
runtimes; … a stuck engine must be killed, not hang" is meant to cover. The renderer's
contract is: missing dep or engine-timeout → that artifact `generation_status=missing`
**with a reason** (item 4 + item 3 prove it — the bounds probe killed a stuck 300 s
process in ~3.5 s and returned recorded-missing). Gating the whole run on a flaky LaTeX
cold start would turn an *optional binary renderer* into a *hard blocker* for the
canonical structured bundle, which spec-14 explicitly forbids ("PDF/DOCX rendering
failures must not destroy Markdown, evidence, or references; no silent missing
'completed' artifacts"). The live happy-path test skips **honestly** (it reports the
reason it couldn't render, it does not assert a fake "complete"), and the deterministic
bare-env tests prove the bundle contract independently of any engine. On this box MiKTeX
was already warm (pdflatex ~2 s), so the full 11-file bundle *did* render complete — the
skip path is correctly defensive, not a cop-out. **No change required.**

**(b) 0008.sql migration ordering + runner compatibility.** **Ruled: correct; no defect.**
`0008.sql` is a single `ALTER TABLE searches ADD COLUMN error TEXT` (nullable, no
default) — additive, in-place, and idempotent-safe (the runner records applied versions
in `schema_migrations` and applies unapplied ones ascending, one transaction each).
`record_search` now PERSISTS the `error` text (verified: search-log.jsonl carries `HTTP
503`). The Phase-1 discovery glob `[0-9][0-9][0-9][0-9]*.sql` picks up 0008 in order
after 0007; the runner is version-based (not file-diff), so a pre-existing 0007-only DB
migrates cleanly on next open. This does NOT break the Phase-1..6 idempotent runner
(re-running with no pending migrations is a no-op). The Phase-3 test's migration list was
updated to include 0008. **No change required.**

**(c) MCP `review logs --follow` blocks that call (in-process dispatch).** **Ruled: acceptable for the one-call-one-command model; no non-blocking follow required now.**
The server dispatches each tool call **in-process** through the same `main()` the CLI
uses, capturing the JSON envelope — that is the *point* (1:1 with the CLI, zero drift,
item 7). A `--follow` call genuinely blocks until the run closes, which is the honest
semantics of "tail until the run closes." This is fine for a stdio child: an external
agent session that wants to observe a run does **not** hold one call open — it calls
`review_status` (non-blocking, returns state/heartbeat) and/or `review_logs` without
`follow` (returns the log so far), and re-polls. Blocking a *single* stdio call is normal
for a request/response tool protocol; it does not break the session (the server is still
alive; other calls are independent). Requiring a non-blocking follow would mean backgrounding
the in-process `main()` per call, which reintroduces the schema/surface drift the design
deliberately avoids. **Ruling: accept as-is.** If a future long-lived session genuinely
needs push, the right move is a *separate* `review status` poll loop (already available)
or an explicit async tool, NOT changing the follow semantics. Flag as a **nice-to-have
follow-up**, not a Phase-7 defect.

**(d) CRLF normalization (b36023b) did not corrupt any file.** **Ruled: verified clean — only line endings changed.**
I diffed every one of the 22 files in `b36023b` against its pre-normalization version
(b723cc4 / c20d01a) byte-for-byte after `\r\n`→`\n` normalization: **all 22 are
content-identical** (only the line endings flipped to CRLF to match the tracked tree).
Spot-checks: a Python file (`mcp_server.py`: 512 CRLF, content-equal) and a `.md`
(`docs/04-mcp-connection.md`: 123 CRLF, content-equal). The committed stats confirm
`8492 insertions / 8492 deletions` (a pure line-ending swap). No logic changed; the full
282-test suite was re-run green afterward. **No defect.**

**(e) No Phase 8 work, no provider/research-tree changes, no secrets committed.** **Ruled: confirmed.**
The 4 Phase-7 commits touch only `exports/` (renderers, run_summary, db+0008),
`agent_contract/mcp_server.py`, `cli/main.py` (export/promoted skill/mcp-serve wiring),
`skill/` (new), `pyproject.toml` (`[exports]` extra), `tests/test_phase7_*`, `docs/04`,
`docs/05`, `README.md`, `SKILL.md`, and line-ending hygiene — **no** provider/adapter or
research-tree (`research/`, `providers/`, `reasoning/`) changes, **no** Phase-8 work, and
`ci/secret_scan.py` (tree + full history) is **clean** at the final commit. **Confirmed.**

## Defects

No Phase-7 blockers, majors, or minors. The bundle, renderer honesty, citation audit,
LaTeX security, MCP server, skill, docs, and `[exports]` install all verified PASS by
execution.

## Observations (cross-phase, non-gating — for a future follow-up, NOT Phase-7 defects)

| ID | file:line | Note (not a Phase-7 regression) | Suggested hardening (optional) |
|----|-----------|----------------------------------|---------------------------------|
| O-P7-1 | `src/papercli/reasoning/common.py` (`citation_key`, `bibliography_for_run`) — Phase-5 | `citation_key()` is **shape-sensitive**: called on a record dict with a parsed author *list* it yields `<surname>{year}{id4}` (e.g. `j2021…`), but the registry builds keys from the **raw DB row** whose `authors` is the unparsed `authors_json` *string* → surname falls back to `"author"` (e.g. `author2021…`). A claim that cites a key built from the list shape is therefore flagged `unregistered` even though it points at the same paper. This is **correct audit behavior** (that key genuinely is not in the registry) and does **not** affect the gated bundle — the canonical source (`bibliography_for_run`) is self-consistent, so bib/CSL/review.md show **no drift** and the audit catches the fabricated `smith2021zzz` regardless. It only surfaced because the Phase-7 fixture hand-built a `citation_key` from a list-authors dict. | Make `citation_key` normalize `authors` (accept a list **or** the JSON-string form) so the key is shape-independent and a registry key always matches a claim key for the same paper. |

**Bottom line:** O-P7-1 is an observation about a Phase-5 key-derivation quirk that the
Phase-7 audit *correctly* surfaced — it is evidence the audit works, not a Phase-7 bug.
Phase 7 gates PASS.

## Verification environment / assumptions

- Fresh `.vJ` = `uv venv --python 3.12` → CPython 3.12.8 (NOT 3.14 — no prebuilt
  pyyaml/pypandoc wheels on this box, per the stated env quirk); `uv pip install -e .
  pytest` then the `[exports]` extra. Bare `.vK` = same 3.12 base WITHOUT the extra, to
  prove the degraded path.
- Live probes used a deterministic seeded COMPLETED fixture run (the same shape the
  fake-worker acceptance run produces) in scratch project dirs; `PAPERS_WORKER_MODULE`
  was not needed because the export surface operates on an already-closed run. No live
  model/network calls (the 60 rpm rate-limited endpoint was never contacted).
- One bounded MiKTeX render was permitted; the engine was already warm, so the full
  11-file bundle rendered complete. The stuck-engine path was proven separately by a
  direct `_run_pandoc` kill probe (item 4), so the gate does not depend on LaTeX
  availability (per flagged item (a)).
- Scratch venvs/dirs (`.vJ`, `.vK`, `C:\Users\Jayanth\p7_scratch*`) are outside the
  tracked tree or git-ignored; the repo tree was left clean apart from this verdict file.
