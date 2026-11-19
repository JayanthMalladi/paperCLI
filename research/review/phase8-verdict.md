# Phase 8 Reviewer Verdict (OPUS final gate)

- Reviewer: OPUS REVIEWER (final gate), Phase 8 of PaperCLI
- Tree: `main` @ `d42755d` == origin/main (tree clean at review start; Phase 8 = `e610614`, `b86eb0c`, `d42755d`); Phases 1-7 already gated
- Scope: bounded, targeted acceptance checks only. The previous judge's full-suite + wheel rebuilds were NOT re-run; the Phase-8 worker's recorded results are used as the baseline (290 non-network tests green; `uv build` -> sdist + `agent_papercli-0.2.0` pure-Python wheel installing into a clean 3.12 venv).
- Environment: reused the existing `.vJ8` venv (Python 3.12.8). No scratch venv was needed. Python 3.14 was not used.
- Baseline from worker (not re-run): **290 passed** full non-network suite. Committed log `p8_final.log` independently confirms it: exactly **290 test-result dots**, `[100%]`, `rc=0`.

## VERDICT: PASS

## Per-check results

| # | Check | Status | Evidence |
|---|-------|--------|----------|
| 1 | Pilot inputs schema-valid (brief + plan) | PASS | In-process jsonschema Draft-2020-12: `examples/brief-robotics.yaml` vs `src/papercli/agent_contract/schemas/brief.json` -> **0 errors**; `examples/review-plan-robotics.yaml` vs `plan.json` -> **0 errors** |
| 1b | Brief limits == agreed guardrails | PASS | Both brief AND plan carry `limits` exactly: wall_minutes=360, max_search_queries=30, max_discovered_records=1000, max_full_text_papers=60, max_model_calls=300, max_input_tokens=1000000, max_output_tokens=150000, max_estimated_cost_usd=null (dict-equal in both files) |
| 1c | Question mojibake-free | PASS | Grep of `Γöé` / `ΓÇö` / `Γ` in brief-robotics.yaml and review-plan-robotics.yaml -> **0 hits** in both |
| 2 | Run row planned / rev-1 / hash prefix | PASS | In-process PaperStore read (no launch): `run-7dee3084-32ce-4f5f-b4e5-d09bceccf14f` -> state=`planned`, protocol_revision=`1`, protocol_hash=`c10220eb45e76566…` (full: `c10220eb45e765665b20e2b7052be94550ab4e297a9b004ffa73afbde3cc7fbd`); embedded plan_json plan_id `plan-f601e32b-…` + same hash; run limits_json matches the guardrails |
| 3 | 8 delta tests in `tests/test_phase8_acceptance.py` | PASS | All **8 pass** in `.vJ8` (`8 dots [100%]`, rc=0, re-run twice with identical result). Skimmed all 8: the failure-injection matrix and metrics-honesty tests are **real, non-tautological assertions** — wrong-paper PDF rejected (`IngestRejected`, zero papers committed) + inconclusive identity flagged in the audit; scanned PDF flagged with OCR warning and access_level capped below full_text (counted under `read_partial`); fabricated citation key `ghostpaper2099` surfaces in `unregistered_keys_used` + audit disclosures; a 429 openalex branch stored as `incomplete` with error disclosed in run-summary + coverage md ("did NOT complete"); interrupted resume re-dispatches only pending work with succeeded result_hashes byte-stable and a clean-completed resume being a no-op; a partial run is never state `completed` and its outputs never contain "all retractions ruled out" / "universal accuracy"; completed-run metrics carry all six counts + a no-exhaustive-coverage disclosure and the retraction check's "NOT a ruling" limit |
| 4 | Bounded Phase 5/6 regression spot-check | PASS | `tests/test_phase6_acceptance.py` + `tests/test_phase5_rate_limit.py` in `.vJ8`: **25 passed, 1 skipped, 0 failed** — no regression. (Worker's 290-suite green remains the full-suite baseline; this is the confirmation spot-check) |
| 5 | Secret-history check | PASS | `ci/secret_scan.py` (tree + full history) exit **0** ("secret scan clean"); `git log --all -p` greps for the actual `.env` values of PAPERS_EMAIL, OPENALEX_API_KEY, PAPERS_LLM_API_KEY -> **0 hits** each (values kept in-memory, never printed; committed doctor JSON + plan YAML also 0 hits for all three values) |
| 6 | Packaging honesty (against committed artifacts, not rebuild) | PASS | `pyproject.toml` version `0.2.0` and the committed wheel metadata (`dist/agent_papercli-0.2.0-py3-none-any.whl`) both state `agent-papercli` 0.2.0; all nine JSON Schemas keep `schema_version` const `1.0` (plan schema verified); wheel METADATA shows `python-docx==1.2.0` and `pypandoc-binary==1.14` carry the `extra == "exports"` marker -> an EXTRA, NOT base-wheel dependencies (no unmarked base-Requires-Dist entries for either); RELEASE.md "Breaking-change note (spec 17)" is present (Sci-Hub removed completely + exit-3-reserved + migration-guidance note); "proven correct" / "universally accurate" / "guaranteed correct" greps -> **0 hits** across tracked docs (only the required NEGATION "No universal-accuracy or exhaustive-coverage claim" in RELEASE.md/run_summary/test docs, plus the README out-of-scope "A universal exhaustive-coverage guarantee ... never a claim" item) |
| 7 | Launch-readiness docs | PASS | `README.md` "Launch the overnight pilot (Phase 8, one command per step)" (line 332) gives the exact 4-command sequence: 1) `papers review plan --brief examples/brief-robotics.yaml --out examples/review-plan-robotics.yaml`, 2) `papers review run --plan examples/review-plan-robotics.yaml --detach`, 3) `papers review status <RUN_ID>` / `papers review resume <RUN_ID> --detach`, 4) `papers review export <RUN_ID> --formats md,pdf,docx,bib,csl,csv`; states "recover it in the morning if it stalled **or the host rebooted**" (step 3), maps the committed plan to `run-7dee3084-…` (hash `c10220eb45e76566…`, state planned) and points to RELEASE.md, which carries the explicit host requirement ("**The host must stay awake + connected for a detached run.** … machine must not sleep / lose network mid-run. **Reboot recovery is an EXPLICIT `papers review resume <RUN_ID>`**, not automatic" — RELEASE.md:105-108). `examples/README.md` mirrors the same 4-step sequence |

## Defects

None at blocker or major severity. One MINOR doc-completeness note (non-gating):

- `README.md:332-356` (Launch section): MINOR — the "host must stay awake / machine must not sleep" requirement lives in RELEASE.md (lines 105-108) and examples/README.md, while the README's launch section states only "recover in the morning if it stalled **or the host rebooted**" (step 3, which covers reboot->explicit-resume per spec 4/12) but does not restate the stay-awake/must-not-sleep requirement inline. One-line fix: add one sentence under the launch steps: "The host must stay awake + connected for a detached run; a reboot is recovered by the explicit `papers review resume` (spec 4/12; details in RELEASE.md)."

## Verifying worker claims vs re-running (no new expensive build)

- "290 non-network tests green": confirmed independently from committed `p8_final.log` (exactly 290 result-dots, rc=0). NOT re-run.
- "`uv build` produced sdist + `agent_papercli-0.2.0` pure-Python wheel": confirmed against committed artifacts — `dist/` holds `agent_papercli-0.2.0.tar.gz` (sdist) and `agent_papercli-0.2.0-py3-none-any.whl` (pure-Python, name/version/metadata verified from the wheel's own METADATA); `p8_wheel2.log` shows the clean-3.12-venv wheel install + `papers doctor` healthy + exporter-deps warnings (md/bib/csl/csv pass; pdf/docx warn) consistent with a bare install omitting `[exports]`. NOT re-run (per instruction; bounded gate).

## READY FOR OVERNIGHT PILOT: **YES**

Exact launch sequence (zero new wiring; brief + plan already committed under `examples/`; run row `run-7dee3084-32ce-4f5f-b4e5-d09bceccf14f` already `planned`, rev 1, hash `c10220eb45e76566…`):

1. `papers review plan --brief examples/brief-robotics.yaml --out examples/review-plan-robotics.yaml`
2. `papers review run --plan examples/review-plan-robotics.yaml --detach`
3. `papers review status <RUN_ID>` / `papers review resume <RUN_ID> --detach` (only if it stalled / host rebooted)
4. `papers review export <RUN_ID> --formats md,pdf,docx,bib,csl,csv`

Host requirements: the host must stay awake + connected for the whole detached run (no sleep, no network loss); reboot is recovered by the EXPLICIT `papers review resume <RUN_ID> --detach`, never automatic. Use a clean 3.12 venv (never 3.14 — no prebuilt pyyaml/pypandoc wheels on this box; pypandoc-binary pinned 1.14). To get PDF/DOCX in the bundle, install with the `[exports]` extra; a bare install still produces md/bib/csl/csv + structured dumps and marks the PDF/DOCX exporters `missing` with a reason.
