# Phase 5 (Reasoning) — OPUS Reviewer Verdict

Repo: `D:/prime-agent-harness/paperCLI` · branch `main` · Phase 5 = 4 commits
(`7a4109f`,`63c1608`,`704aa6d`,`da1c2e0`), HEAD `da1c2e0` == origin/main, clean tree.
Reviewed by execution in a **fresh `uv venv --python 3.12` (CPython 3.12.8) `.vJ`**
(`uv pip install -e .` → pyyaml 6.0.2, jsonschema, httpx present), offline
adapter/reasoning probes via `httpx.MockTransport`, live CLI runs in scratch project
dirs, in-process DB inspection, and one re-run of the live-model smoke against the
configured endpoint. No raw secret values printed (masked by name/prefix only).
Phases 1–4 were not re-litigated.

## VERDICT: **PASS**

Phase 5 introduces **zero blockers and zero majors of its own**. The offline suite is
green in a fresh venv (190 passed, 0 failed), the agent-driven no-model path works,
no secret leaks, the live smoke round-trips, and all three worker-flagged items are
ruled below. One **pre-existing MAJOR** conformance gap (the Phase-3 evidence accessor
/ Phase-1 `evidence.json`) is disclosed and attributed — it is **not** a Phase-5
regression and is out of Phase-5 gating scope ("do not re-litigate Phases 1–4"), but
it is real and Phase 6 + external agents will hit it, so it must be fixed before
Phase 6. Three Phase-5-scope **MINOR** defects are listed.

## Test results (real counts, fresh venv)

| Suite | Result |
|---|---|
| Non-network, fresh `.vJ` (`pytest tests -m "not network"`) | **190 passed, 7 deselected (network), 0 failed** (46.7 s) |
| `@network` marked tests (deselected offline) | 7 (1 Phase-5 live smoke + 6 Phase-2 provider smokes) |
| Phase-2 secret-scan test + Phase-4 task-submission tests (spot re-run) | pass (no Phase-1/2/4 regression) |
| Live model smoke re-run by reviewer (real endpoint, max_tokens ~50) | **1 passed in 2.26 s** — real round-trip + a `model_calls` row; secret absent |

## Per-checklist table

| # | Item | Status | Evidence (verified by execution) |
|---|------|--------|-----------------------------------|
| 1 | Full non-network suite green in fresh 3.12 venv; no P1–4 regression | PASS | 190 passed / 0 failed / 7 deselected; `test_secrets_and_storage.py` + `test_phase4_research.py` + `test_cli_exit_codes.py` re-ran green |
| 2a | Lenient JSON: fenced ```json parses | PASS | `extract_json` round-trips a fenced reply → `{"decision":"include"}` |
| 2b | Malformed → re-prompt; 2 re-prompts still bad → FAILED recorded, no exception | PASS | 3 garbage replies → `ok=False`, `outcome=failed`, `attempts=3`, no raise |
| 2c | Post-hoc jsonschema rejects structurally-valid-but-wrong-shape | PASS | `{"decision":5}` and missing-required both yield non-empty error lists |
| 2d | 45-rpm bucket math (10 rapid → 6th+ throttled) | PASS | default cap=45: first 45 rapid granted, 46th throttled wait≈1.333 s (=60/45). Scaled cap=5 @60rpm: first 5 granted, 6th–10th throttled with positive bounded waits. Refill math exact (0.75 tok/s). Note: under the default 45-capacity, 10 rapid calls all pass — "6th+ throttled" maps to a 5-token capacity, which is what the worker test models. Bucket math is correct. |
| 2e | 429 → bounded exponential backoff into retry/failed, no storm | PASS | persistent 429 → ≤3 HTTP calls, 2 backoff sleeps (2 s,4 s capped to 1 s), `outcome=failed` with 429 noted; no unbounded loop |
| 2f | Unknown price → `cost=null` + `pricing_known=false` | PASS | unlisted model → `estimated_cost_usd=None`, `pricing_known=False`; persisted row `estimated_cost_usd` is null (not zero) |
| 2g | `model_calls` row has model, prompt hash+version, protocol revision, tokens, usage/cost, outcome | PASS | persisted row carries all 12 spec-11 fields (endpoint + `api_key_env` ref visible; secret value absent from the row blob) |
| 2h | No secret value can appear in a prompt | PASS | probe value set via `api_key_env` → absent from the full request body and every message; present only in the `Authorization` header; absent from the persisted row |
| 2i | Budget: reserve-before-dispatch + reconcile vs returned usage; on exhaustion stop dispatch AND reserve partial-synthesis | PASS | `reserve` blocks when a dimension can't hold; `reconcile` bills FULL actual usage (deficit over reservation is charged, not absorbed); on exhaustion `allow_dispatch()=False`, normal `reserve`→None, and a `partial_synthesis` slice is reserved (`partial_synthesis_reserved=True`, capped at remaining budget) |
| 3a | `screen_uncertain`: one-pass bounded recheck; persisted records carry the full spec-9 field set; no-model → `skipped_no_model`, run completes deterministically | PASS | persisted screening row has decision, criterion_ids, rationale, evidence_basis, reviewer=`model/<id>`, prompt_version, timestamp; validates against the published `screening` schema with 0 errors; `rechecks` capped at 1, leftovers reported unresolved; `cfg=None` → `skipped_no_model` with the queue left queued |
| 3b | `extract_findings`: access-level gate real; numeric finding without units/context rejected/flagged | PASS | `abstract_only` + `metadata_only` docs → `documents_gated=2`, **zero** findings persisted; a numeric finding with no units/context → `extraction_warnings` populated (`numeric finding without units`, `...without population/task context`) |
| 3c | `synthesize`: theme-organized; unregistered citation keys DROPPED in code + flagged; correction/retraction check recorded WITH limits; no exhaustive-coverage claim | PASS | a fabricated key is dropped from the persisted claim and listed in `dropped_citation_keys`; only registry keys survive; `correction_check` carries `limits`; no positive exhaustive-coverage / "all retractions ruled out" prose in the draft |
| 3d | `critique`: all 5 labels reachable; DOI resolution alone NEVER sets supported; unsupported removed/narrowed/prominently-marked in 1 bounded pass; critique+revision records persisted | PASS | all 5 labels (`supported`,`partially_supported`,`unsupported`,`uncertain`,`inference`) persist; a `supported` claim with zero linked evidence is downgraded to `uncertain` + disclosed in the deterministic audit (DOI alone never sets supported); `unsupported` is marked unresolved in exactly one revision pass; `audits` + `revision` records persisted |
| 4 | Worker-flagged item: reply-shape vs persisted-wrapper schema conformance | **RULED — see below** | screening wrapper conforms; **evidence wrapper has a pre-existing `evidence_id` conformance gap** |
| 5 | `papers citations audit --run R` CLI | PASS | valid run → exit 0, `ok` envelope with claim-audit labels + key/dup/orphan/wrong-paper checks + coverage disclosure; unknown run → exit 2 `CONFIG_REQUIRED` error envelope |
| 6 | Agent-driven mode, NO model configured: deterministic path runs, model tasks → `skipped_no_model` | PASS | with `PAPERS_LLM_*` blanked: `review plan` → `planned`; deterministic `ScreeningPipeline.screen_records` decides + queues; `evidence list` + `citations audit` run; `screen_uncertain`/`extract_findings`/`synthesize` → `skipped_no_model`; `critique` → `succeeded` (0 claims). No model-driven task raises |
| 7 | Secrets: endpoint+model visible in `config show`, key value masked; no secret in model_calls/summaries | PASS | `config show` exposes `base_url` + `model` + `api_key_env`; the key value is masked (probe value absent from output); model-call rows carry no secret value |
| 8 | Worker note (b): OpenAlex network smoke keyless isolation | **NOTE (pre-existing, not a defect)** | `test_provider_smoke_network.py` was last touched in Phase 2 (`82e1326`), NOT in the Phase-5 range; the without-key OpenAlex test already strips `OPENALEX_API_KEY` and treats provider failure as `incomplete` |
| 9 | Live model smoke re-run | PASS | re-ran `test_live_model_smoke_records_call_row` against `https://api.brocode.live/v1` → 1 passed; real round-trip + a `model_calls` row; endpoint visible, secret absent |
| 10 | No Sci-Hub; no POSIX-only calls; Windows-safe | PASS | no `sci-hub`/`scihub`/`sci_hub` in `src`/`tests`; no `os.fork`/`os.setsid`/`signal.`/`pty`/`os.system`/`subprocess`/`shell=True` in the new Phase-5 modules; CRLF is pre-existing repo-wide, not a Phase-5 introduction |

## Rulings on the three worker-flagged items

1. **Budget-deficit disclosure — ACCEPTABLE for Phase 5 (MINOR tracking for Phase 6).**
   The budget accounting is honest: `ModelBudget.reconcile` charges the **full** actual
   usage against the pool, so a deficit over the reservation is *billed, not silently
   absorbed* (verified: `remaining` shrinks by the full actuals). The `deficit` flag is
   computed and returned by `reconcile`, but `ModelAdapter._reconcile`
   (`src/papercli/models/adapter.py:454-458`) **discards that return**, and no
   user-facing surface (model-call row, task result, run summary) currently carries it —
   and the Phase-1 `run_summary` schema has no deficit field yet. Since run-summary
   assembly is a **Phase-6** deliverable, I rule this acceptable: nothing is
   under-charged. Phase 6's run-summary builder must thread `reconcile`'s `deficit`
   through to `usage`/`counts`, and the adapter should stop dropping the flag. Tracked
   as MINOR-1 below (adapter side).

2. **Reply-shape vs persisted-wrapper validation — CONFIRMED GAP, PRE-EXISTING (MAJOR, out of Phase-5 scope).**
   - **Screening**: the persisted wrapper **conforms**. The task validates the model
     *reply* (decision object) against the screening schema's decision fields, then
     persists a row with `paper_id`/`stage` added; `validate(row,"screening")` → 0
     errors (required `paper_id`,`stage`,`decision` all present as real columns).
   - **Evidence**: the persisted/surfaced record does **NOT** conform to the Phase-1
     published `evidence` schema. `PaperStore.evidence_for_run`
     (`src/papercli/storage/db.py:314-327`) returns rows keyed `id`, but
     `evidence.json` requires a distinct top-level key `evidence_id`
     (`src/papercli/agent_contract/schemas/evidence.json:8`). Empirically:
     `validate(evidence_list_row, "evidence")` → `["missing required key: 'evidence_id'"]`.
     The asymmetry is structural: the `screenings` table has a real `paper_id` column
     (so it conforms), whereas the `evidence` table's primary key is `id` and there is
     no `evidence_id` column. **Root cause is pre-existing** — `evidence_for_run`
     landed in Phase 3 (`5f1cc8d`), the `evidence.json` requirement in Phase 1
     (`5a1d243`); Phase 5's `extract_findings` persists correctly via `record_evidence`
     and its own result carries `evidence_ids` — it did **not** introduce or worsen
     this. Impact: an external agent (or the Phase-6 worker) that reads `papers
     evidence list` and validates against the published `evidence` schema gets a hard
     failure. **Ruling: MAJOR, pre-existing (Phase-3 accessor / Phase-1 schema), NOT a
     Phase-5 regression; disclosed and must be fixed before Phase 6. It does not block
     Phase 5** (per "Phases 1–4 are gated; do not re-litigate"). One-line fix: map the
     evidence primary key to `evidence_id` when surfacing (add `d["evidence_id"]=d["id"]`
     in `evidence_for_run` / `_cmd_evidence_list`), or align the schema; the minimal
     fix is the key alias in the accessor.

3. **Capability-probe-skip on injected client — ACCEPTABLE (offline determinism preserved).**
   When a client is injected, `ModelAdapter.__init__` skips the capability probe and
   sets `capability={"probed": False, ...}` (`src/papercli/models/adapter.py:213-216`);
   the probe runs only against a real self-owned client and is report-only (never
   crashes). This is exactly right: the injected mock's response sequence must not be
   consumed by a probe call, otherwise the offline suite would lose determinism.
   Verified: the full offline suite is green (0 failures) and injected-client runs are
   deterministic. **Not a defect.**

## Defects

| ID | file:line | Severity | Finding + one-line fix |
|----|-----------|----------|------------------------|
| D-P5-0 | `src/papercli/storage/db.py:314-327` (`evidence_for_run`) + `src/papercli/agent_contract/schemas/evidence.json:8` | **MAJOR (pre-existing, Phase-3/Phase-1; NOT a Phase-5 regression)** | Surfaced evidence records are keyed `id` and lack the schema-required `evidence_id`, so they fail the published `evidence` schema. Fix: alias `d["evidence_id"]=d["id"]` in `evidence_for_run` (and keep it through `papers evidence list`) before Phase 6. Disclosed for Phase 6; does not gate Phase 5. |
| D-P5-1 | `src/papercli/models/adapter.py:454-458` (`_reconcile`) | **MINOR** | `reconcile`'s returned `deficit` flag is discarded, so a budget deficit is never surfaced (run-summary is Phase-6). Fix: propagate `budget.reconcile(...)`'s `deficit` into the model-call row / task result so Phase 6's run-summary can disclose it. |
| D-P5-2 | `src/papercli/models/rate_limit.py:126-128` (`RateLimiter.wait_seconds`) | **MINOR (latent)** | Docstring claims "without consuming one" but the method calls `self.acquire()`, which decrements the token bucket — a side-effecting "query". Unused in shipped code (no bare `wait_seconds()` callers), so latent. Fix: compute the wait from `self.tokens`/`refill_per_second` without `acquire()`, or delete the method. |
| D-P5-3 | `src/papercli/models/adapter.py:301` + `:464` | **MINOR** | `complete_json`'s docstring lists a ``reservation`` return key that no return path includes, and `_fail_result` hard-codes `retries_waited: 0` even on the 429 backoff path where backoffs actually occurred. Fix: drop the phantom `reservation` from the docstring and pass the real `retries_waited` into `_fail_result`. |

No blockers. Zero Phase-5-introduced majors. One pre-existing major (D-P5-0)
disclosed and attributed. Three Phase-5-scope minors (D-P5-1..3).

## Live-smoke result (reviewer's own run)

Re-ran `tests/test_phase5_rate_limit.py::test_live_model_smoke_records_call_row`
once, with the documented `.env` endpoint (`PAPERS_LLM_BASE_URL` / `PAPERS_LLM_API_KEY`,
key value masked, not printed): endpoint `https://api.brocode.live/v1`, model fallback
`claude-sonnet-5-5`. Result: **1 passed in 2.26 s** — a tiny real call
(`max_tokens ~50`) round-trips as `success` and records a `model_calls` row whose
`endpoint` is the selected endpoint and which carries **no secret value**. This confirms
the worker's "live smoke RAN" claim. Kept live calls to a single round-trip (well under
the 60 rpm endpoint limit).

## Notes / assumptions

- "10 rapid calls → 6th+ throttled" in the checklist maps to the worker's
  capacity-5 scaled limiter; the default 45-capacity bucket passes all 10 (correct
  token-bucket behavior). No defect — bucket math verified exact.
- `critique` with no claims returns `succeeded` (0 claims) rather than
  `skipped_no_model`; that is the no-model path with an empty ledger and is correct
  (deterministic, no force-decided labels).
- CRLF line endings are pre-existing across the repo (many Phase-2/3/4 files); not a
  Phase-5 introduction and not Windows-unsafe.
- Working tree kept clean: only the verdict file is committed/pushed; scratch probes and
  the `.vJ` venv are git-ignored/untracked and were removed from the tree.
