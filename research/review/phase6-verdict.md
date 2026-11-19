# Phase 6 (Worker) — OPUS Reviewer Verdict

Repo: `D:/prime-agent-harness/paperCLI` · branch `main` · Phase 6 = 6 commits
(`cabd391`,`aeae92a`,`6f9bb2d`,`779fcf7`,`3f83225`,`4d671e4`), HEAD `4d671e4` == origin/main, clean tree.
Reviewed by execution in a **fresh `uv venv --python 3.12` (CPython 3.12.8) `.vJ`**
(`uv pip install -e . pytest`), the full offline suite, live CLI probes through the
documented fake-worker seam (`PAPERS_WORKER_MODULE=papercli.jobs.fake_worker_child`)
in scratch project dirs, cross-process lease races, in-process heartbeat/staleness and
budget probes, and a secret-value grep over every durable surface. No raw secret values
printed (masked by name only). Phases 1–5 not re-litigated; their carried defects
(D-P5-0/1/2/3) were checked for consumption.

## VERDICT: **PASS**

Phase 6 introduces **zero blockers and zero majors**. All 13 checklist items verified
by execution: 256 passed / 0 failed in a fresh venv, detached launch through native
process APIs with an exclusive per-run lock, heartbeat-age (never PID-alone) staleness,
idempotent resume with changed-brief refusal, cancel → labeled partial bundle,
pre-dispatch budget reservations with partial close, bounded 429 backoff,
no-double-spend concurrency, 0 secret leaks, honest exports with deferred (not faked)
renderers, no Sci-Hub. Three **MINOR** defects listed below; none gates Phase 6.

## Test results (real counts, fresh venv)

| Suite | Result |
|---|---|
| Full non-network, fresh `.vJ` (`pytest tests -m "not network" -q`) | **256 passed, 0 failed** (256 progress dots, zero F/E markers, ~47 s) |
| Phase-1–5 regression spot-check (Phase-2 secret-scan defect tests, Phase-4 task-submission/research, Phase-5 adapter/rate-limit/reasoning/citations/defect-fix tests + Phase-6 acceptance/detach re-run) | **111 passed, 0 failed** |
| `ci/secret_scan.py` (tree + full git history) | **exit 0, clean** |

## Per-checklist table

| # | Item | Status | Evidence (verified by execution) |
|---|------|--------|-----------------------------------|
| 1 | Full non-network suite green in fresh 3.12 venv; no P1–5 regression | PASS | 256 passed / 0 failed in fresh `.vJ`; separate spot-check of P2/P4/P5 + P6 suites: 111 passed / 0 failed |
| 2a | Detached launch: envelope ACCEPTED + lock acquired + handshake file | PASS | Live `papers review run --plan plan.yaml --detach` (fake-worker seam): exit 0, `accepted=true`, `state=running`, `handshake=true`, `worker_pid` set; on disk: `run.lock` (live holder pid), `handshake.json` (`accepted=true`, launch nonce), `heartbeat.json` all present |
| 2b | Second concurrent launch REJECTED (exclusive lock) | PASS | Second `--detach` on the same running run: exit 2, `RUN_LOCK_HELD` ("a live worker holds the lock (pid …); cancel it or wait, then resume") |
| 2c | Native process APIs + arg array; NO shell; accepted ≠ completed | PASS | `detach.py` `_spawn_default_child`: `subprocess.Popen(args)` with `args` an ARG ARRAY (exe `-m papercli.jobs.worker --db-path … --run-id …`), `creationflags=CREATE_NEW_PROCESS_GROUP` on Windows / `start_new_session` on POSIX; zero `shell=True`/shell-string anywhere in `jobs/`+`cli/`; the accepted envelope reports `state=running` (run row CAS `planned/queued → running` only) — never `completed` |
| 2d | No host sleep-setting changes | PASS | grep of `src` for powercfg/SetWaitWakeToRDN/SetSystemPowerState/keep-awake: zero; spec-12's keep-awake note appears only in the handoff doc; no webhooks in `src` |
| 3 | Heartbeat staleness: kill/age → STALE-possibly-interrupted, never running-from-PID | PASS | (a) Live worker: `status` → `stale=false`, `lock.held=true`, fresh heartbeat age. (b) `taskkill` the worker + age heartbeat 900 s: `status` → `stale=true`, `lock.stale=true`, reason "holder pid is not running", age ≈ 900 s. (c) In-process: a LIVE pid whose heartbeat is 900 s old → `lock_held` stale=true "heartbeat stale (900s > 60s)" while `pid_alive(pid)=True` — staleness is derived from heartbeat AGE, a PID is never trusted alone; `STALE_AFTER_SECONDS=60`. Dead pid + fresh heartbeat also flagged stale (holder check is an additional signal, age is the primary one) |
| 4a | Resume re-dispatches ONLY unfinished; succeeded hashes unchanged; no duplicate work | PASS | Killed run (1 succeeded + 2 pending + stale lock/heartbeat): `papers review resume --detach` → `resumed=true, launched=true`, note "stale state detected"; the succeeded task's `result_hash` `bb7ef6aa97…` byte-identical before/after, task count 3→3 (no new rows), pending tasks re-dispatched to `succeeded`, run closed `completed` |
| 4b | Changed brief/config REFUSED with new-revision guidance | PASS | `resume --detach --brief changed-brief.yaml` (different question) → exit 2, `BRIEF_CHANGED`, message + `details.guidance` both carry NEW-revision guidance ("requires a NEW run revision — start one with `papers review plan`"); prior evidence untouched. Same-brief resume accepted (exit 0) |
| 5 | Cancel: stop dispatch + terminate bounded child work + checkpoint + partial bundle, labeled terminal state | PASS | Cancel on a live fake worker: `worker_running=true`, `cancel-request` file written; worker honored it at the next dispatch boundary, closed `cancelled`, wrote `run-summary.json` (state `cancelled`), released the lock. No-live-worker cancel: leases re-queued + run marked `cancelled` directly. Cancel on a closed run: exit 2 `RUN_ALREADY_CLOSED`. (In-suite cancel probe: `task_deferred` + `run_closed` log events, all waits ≤ 5 s bounded) |
| 6a | Budget exhaustion mid-run → dispatch stops, closure slice reserved, PARTIAL close, honest summary | PASS | `max_model_calls=1` run: `shared_budget.exhausted=true`, `partial_synthesis_reserved=true`, both checkpointed in `counters.worker.budget`; run closed `partial` (never `completed`); on-disk `run-summary.json` carries `state=partial`, `budget.exhausted=true`, the full honest limit set. Wall-deadline variant: anchored deadline checkpointed, a fresh worker restores the SAME deadline, stops dispatch, leaves the extra task `pending`, closes `partial` with `deadline_reached=true` |
| 6b | Budgets enforced BEFORE dispatch via reservations; hard token/call ceilings with null $ cap | PASS | `ModelBudget.reserve` runs pre-dispatch in the adapter (`reservation is None` → `budget_exhausted` result, no HTTP call); verified in-process: with `max_estimated_cost_usd=None` and `max_input_tokens=1000` the 2nd 600-token reserve is REFUSED (None) and post-reconcile `exhausted=true`, `allow_dispatch()=False`, while the `partial_synthesis` slice remains reservable (fail-closed on normal, closure path always allowed) |
| 6c | 45-rpm limiter SEPARATE from provider rate limits | PASS | `RateLimiter` (default 45 rpm, capacity 45) is a distinct object owned by the model adapter; the provider HTTP layer carries its own `RetryPolicy` + per-provider pacing (arXiv 1/3 s etc.) and does not import the model limiter — two independent gates |
| 7 | Model 429 → bounded exponential backoff into retry_wait via scheduler state; no storm; provider 429/Retry-After honored | PASS | Suite `test_model_429_bounded_backoff_not_storm`: exactly `max_re_prompts` backoff sleeps, HTTP hits = 1 + max_re_prompts (bounded); a drained limiter → `retry_wait` outcome with ZERO new HTTP calls. Worker level: the 429 task parks in `retry_wait` with `attempt=1`, `next_attempt_at` set (deferred through scheduler state, not a blocking hot-path sleep — the idle poll is bounded `_MAX_IDLE_POLLS` then closes partial), and the honest summary's failures block surfaces it. Provider layer (Phase 2, unchanged): `RetryPolicy` honors Retry-After (capped 15 s) within bounded exponential backoff; permanent 4xx never retried; a surviving 429 raises as a retryable rate-limit error (exit-5 class) |
| 8 | Concurrency: two claimers → exactly ONE acquires; no double-spend | PASS | Cross-process probe (two independent Python processes, each its own store connection, racing one lease): exactly 1 winner (claim+start+complete), loser's commit blocked with `TaskQueueError`, final row `succeeded` with a single `result_hash`. Two threads against one shared last-unit budget (`max_model_calls=1`): exactly 1 grant + 1 refusal, `exhausted=true`, 0 open reservations. Task leases are the SQLite conditional-UPDATE exclusion; the per-run lock rejects sequential second launches (2b) |
| 9 | Secrets: 0 secret values in handshake/heartbeat/worker.log/run-summary/dumps; CI scan clean | PASS | Grep of the entire `.papercli/storage` tree (SQLite db, run dirs, `handshake.json`, `heartbeat.json`, `worker.log.jsonl`, `run-summary.json`, all structured dumps) against the real `.env` values (PAPERS_EMAIL, OPENALEX_API_KEY, PAPERS_LLM_API_KEY, PAPERS_LLM_BASE_URL — values masked, checked in-process only): **all absent**; the endpoint IS visible in the summary (spec 16) and the key only as its env-var reference name. `python ci/secret_scan.py` → exit 0 "clean: no key-shaped values in tree or history" |
| 10 | Exports: partial/failed run → valid structured bundle; md/pdf/docx DEFERRED to Phase 7; render failures can't destroy Markdown/evidence | PASS | `papers review export <cancelled-run> --formats md,pdf,docx,bib,csl,csv` → exit 0, envelope `state=cancelled` (honest partial state reported); all 9 Phase-6 dumps written and JSON-valid (`evidence.json`, `claims.json`, `references.csl.json`, `papers-registry.json`, `screening.json`, `search-log.jsonl`, `evidence.csv`, `screening.csv`, `run-summary.json`); run-summary artifacts record `review.md/pdf/docx` as `generation_status="missing"` with "deferred to Phase 7" notes — never claimed complete, never silently missing. Phase 7 owns the renderers; Phase 6 cannot fake or destroy the canonical structured source. (One sub-gap: `bib` — see D-P6-1) |
| 11 | No Sci-Hub in the tracked tree | PASS | `git grep -in sci-hub` over tracked files: hits only in `ATTRIBUTION.md` ("no Sci-Hub paths", migration framing), the handoff spec itself, `docs/03-migration-note.md` (permitted descriptive note), and prior verdicts. `src/` and `tests/`: **zero** functional adapter/flag/env/config mentions |
| 12 | Windows-safety: no POSIX-only calls; subprocess arg arrays | PASS | `git grep` over `jobs/`, `cli/`, `exports/`: no `os.fork`/`os.setsid`/`pty`/`os.system`/`shell=True`/`/bin/`/`posix_spawn`; `subprocess` appears only in `detach.py` (arg-array `Popen` + `CREATE_NEW_PROCESS_GROUP`) and `documents/parser.py` (Phase-3 arg-array isolated PDF-parse child, out of Phase-6 scope); `fcntl` is POSIX-gated behind `os.name == "posix"` |
| 13 | Ruling: exit code of `review export` on a partial run | **RULED — see below** | exit-0-for-a-successful-export is the correct reading; **no code change required** |

## Ruling on the batch-flagged export exit-code question

**Exit 0 for a successful `review export` of a partial/cancelled run is CORRECT; no code
change is required.** Spec-6's table reserves exit 6 for "batch or **foreground** run
completed partially" — i.e. it describes the *run* command's own execution (`review run`
without `--detach`), which the code in fact does (`main.py:756` exits 6 on a partial/failed
foreground closure). `review export` is a distinct command operating on an already-closed
run: the bundle write succeeded, so the command succeeded (exit 0), and spec-6's explicit
rule — "Distinguish command success from review quality: a successful `status` call can
describe a failed or partial run" — applies to export by the same token. The partial state
is not hidden: the envelope carries `state=cancelled/partial/failed` plus `written`/
`deferred` inventories, and `run-summary.json` reports the honest state, budget
exhaustion/deficit flags, failures, and deferred-renderer inventory. A distinct exit code
for export would conflate the two planes the spec deliberately separates; the machine-
readable partial state is already surfaced exactly where spec-6 says review quality belongs
(envelope data, not the exit code).

## Notes (accepted, not defects)

- **`cancelled` foreground close exits 0** (`main.py:756` exits 6 only on
  `partial`/`failed`). Acceptable: a user-directed cancel is an intentional closure, not
  "completed partially" — the terminal state is honestly in the envelope
  (`state=cancelled`, `worker_exit_code=6`). The `RunPartial`/`RunPaused` error classes in
  `cli/errors.py` are currently unused scaffolding (Phase 7 / paused-run surface); no
  behavior depends on them.
- **Phase-5 carried defects are consumed**: D-P5-0 (evidence `evidence_id` alias) is now
  present in `evidence_for_run` so the Phase-6 `evidence.json` dump conforms to the
  published schema; D-P5-1 (deficit flag) is threaded into `run-summary.json`'s `budget`
  block (`budget_deficit` + the "billed, never absorbed" note); D-P5-2 (`wait_seconds`
  purity) is fixed — verified as a no-side-effect query; D-P5-3 (phantom `reservation`
  docstring key, hard-coded `retries_waited`) is fixed — real `retries_waited` now flows
  through the 429 backoff path.

## Defects

| ID | file:line | Severity | Finding + one-line fix |
|----|-----------|----------|------------------------|
| D-P6-1 | `src/papercli/exports/run_summary.py:510` (`write_structured_dumps` deferred set) + `:350` (`RENDERED`) | **MINOR** | Requested `bib` (in the default `md,pdf,docx,bib,csl,csv` and the spec-14 bundle's `references.bib`) is neither written nor recorded as deferred — it is the one requested format that silently disappears from both `written` and `deferred`, against spec-14's "no silent missing 'completed' artifacts". Fix: add `"bib"` to the deferred/`RENDERED` inventory sets (recorded as Phase-7 deferred) so every requested format is either written or explicitly accounted for. |
| D-P6-2 | `src/papercli/jobs/detach.py:325-326` | **MINOR (cosmetic)** | Doubled `@staticmethod` decorator on `RunLockGuard._check_and_reclaim` (harmless — the classmethod resolves — but a smell that suggests a copy/paste slip in a lock-critical path). Fix: delete one decorator. |
| D-P6-3 | `src/papercli/jobs/detach.py` (`RunLockGuard.acquire`, marker write ~:347-370) | **MINOR (narrow race, Windows-only)** | The per-run lock is an advisory marker (write-then-double-check) on Windows; POSIX is closed by `flock`, but two children racing a FRESH launch can interleave write→check→write→check so both keep the lock (TOCTOU window). Sequential second launches are correctly rejected (verified), and task leases + budget still prevent task double-commits, so worst case is a bounded duplicate-worker window, not state corruption. Fix: make the marker acquisition exclusive on Windows — `open(lock, "xb")` (O_EXCL) and write, catching `FileExistsError` → then reclaim-if-stale — so only one child ever wins the marker. |

No blockers. No Phase-6-introduced majors. Three minors (one a spec-14 honesty gap worth
a one-line inventory fix before Phase 7 builds the renderers on top; two cosmetic/race-
hardening). The concurrency, budgets, staleness, resume, cancel, and export behaviors that
gate Phase 6 all verified PASS by execution.

## Verification environment / assumptions

- Fresh `.vJ` venv: `uv venv --python 3.12` → CPython 3.12.8 (NOT 3.14 — no prebuilt
  pyyaml wheel on this box, per task constraint); `uv pip install -e . pytest`; pyyaml
  6.0.2 + jsonschema + httpx from the project pins.
- Live CLI probes used the documented offline seam `PAPERS_WORKER_MODULE=
  papercli.jobs.fake_worker_child` (never the production default; the production child is
  `python -m papercli.jobs.worker`) so the real `papers review run/status/cancel/resume/
  export` surfaces were exercised without a model endpoint or network.
- Secret grep used the real `.env` values in-process only; nothing secret is printed in
  this verdict (names + masked prefixes at most).
- Scratch dirs (`C:\Users\Jayanth\papercli-p6-probe\*`, `.vJ`, `.suite_*.txt`,
  `.spot.txt`) are outside the repo or git-ignored; the repo tree was left clean apart
  from this verdict file.
