# Phase 8 (Acceptance + Release) — OPUS Reviewer Verdict (final gate)

- **Reviewer:** OPUS REVIEWER (gate judge), Phase 8 of PaperCLI
- **Scope under gate:** Phase 8 = `e610614` (pilot brief + validated plan + doctor record),
  `b86eb0c` (acceptance DELTA tests), `d42755d` (release v0.2.0 + docs final).
  Phases 1–7 are already gated and were **not** re-litigated.
- **Method:** every worker claim was re-verified **by running** in a fresh
  `uv venv --python 3.12` (`.vJ8`), plus a clean-wheel install venv (`.vJ8clean`).
  Python 3.14 was not used (known box quirk: no prebuilt pyyaml/pypandoc wheels;
  pypandoc-binary pinned to 1.14). No overnight run was launched (that is the
  orchestrator's job, post-gate).

## VERDICT: PASS

Zero blockers, zero majors, zero minors. All nine gate items verified by running.
The release is honest and complete against spec 18/19/21: a working, unattended,
evidence-backed pipeline shipped with source + a reproducible install package +
an example brief/config + the portable skill + validation results + a limitations list
that makes no accuracy or exhaustive-coverage claim.

## Per-checklist results

| # | Check | Status | Evidence (by running) |
|---|-------|--------|----------------------|
| 1 | Full non-network suite in fresh `.vJ8` | **PASS** | `pytest tests -m "not network"` → **290 passed / 0 failed / 0 error** (`[100%]`, no `F`/`E`/`s` markers). Matches the worker's claimed 290 (282 baseline + 8 delta). No Phase 1–7 regression. |
| 2 | Pilot brief + plan schema-valid; limits == guardrails; question clean; Run row | **PASS** | `examples/brief-robotics.yaml` vs `brief` schema → **0 errors**; `examples/review-plan-robotics.yaml` vs `plan` schema → **0 errors**. Both `limits` dicts dict-equal the agreed guardrails (wall 360, queries 30, discovered 1000, full-text 60, model-calls 300, in 1000000, out 150000, USD null). No `Γöé`/`ΓÇö`/`Γ`/`Ã`/`Â`/`â`/`ï` mojibake in brief or plan. Plan: 20 query variants across `arxiv`/`crossref`/`openalex`/`s2`, expected_records 1000, full-text cap 60, protocol_hash `c10220eb45e765665b…`, `brief_ref.run_id` = `run-7dee3084-32ce-4f5f-b4e5-d09bceccf14f`. Live DB run row `run-7dee3084-…` → `state=planned`, `protocol_revision=1`, hash `c10220eb45e76566…`, limits_json == guardrails. |
| 3 | Doctor preflight record | **PASS** | `examples/doctor-preflight-record.json` → `summary` 7/7 pass, 0 warn, 0 fail, healthy; `model_endpoint` = pass, `http_status` 200, `network_checked` true, adapter `openai_compatible`. **Secrets redacted:** all four `.env` values (PAPERS_EMAIL, OPENALEX_API_KEY, PAPERS_LLM_API_KEY, PAPERS_LLM_BASE_URL) are **absent** from both the committed doctor JSON and the plan YAML (value-substring check = 0 hits). Endpoint host visibility is the only allowed exposure. |
| 4 | 8 delta tests are real + pass | **PASS** | `pytest tests/test_phase8_acceptance.py` → **8 passed**. Failure-injection matrix exercises every §19 row by **driving the real subsystems** (not tautologies): (a) a provider-429 OpenAlex branch is stored `completeness≠complete` + error disclosed and named in `coverage-and-limitations.md`; (b) wrong-paper PDF → `IngestRejected` with zero papers committed, and an inconclusive-identity document is flagged in the audit's `wrong_paper_identity.flagged`; (c) scanned PDF is flagged (`scanned=True`), capped below `full_text` (counted under `read_partial`), OCR disclosure present; (d) fabricated key `ghostpaper2099` surfaces in `unregistered_keys_used` + audit disclosures; (e) an interrupted run is re-dispatched **only** for pending/retriable work — succeeded task result_hashes stay byte-identical, no duplicate work, and a clean-completed resume is a no-op. Metrics honesty: a completed run carries all six counts + a no-exhaustive-coverage disclosure, and a partial run is asserted `state != "completed"` and its outputs never contain "all retractions ruled out" / "universal accuracy". |
| 5 | Fresh-install acceptance (§19 "Fresh install") | **PASS** | In a clean `.vJ8clean` venv (wheel install): `papers --version` → **0.2.0**; `papers doctor --json --skip-network` → status `ok`, 5 pass + 2 honest warnings (model-endpoint skipped-by-flag; pdf/docx exporters missing because the bare wheel omits the `[exports]` extra — md/bib/csl/csv still pass); a tiny fixture brief → `papers review plan` → `status=ok`, `run_state=planned`, and the written plan validates against the `plan` schema with **0 errors**. |
| 6 | Packaging honesty | **PASS** | `dist/agent_papercli-0.2.0.tar.gz` (sdist) + `dist/agent_papercli-0.2.0-py3-none-any.whl` (pure-Python, `py3-none-any`) both present. Wheel METADATA: `Version: 0.2.0`; `python-docx==1.2.0` + `pypandoc-binary==1.14` are declared with the `extra == "exports"` marker — i.e. an **extra, not a base dependency** (base `Requires-Dist` = httpx/pydantic/jsonschema/pyyaml/tomli only). Bare-wheel install warns about the two renderer deps (honest, not a defect). RELEASE.md carries the breaking-change note (Sci-Hub removed, exit 3 reserved, migration guidance — spec 17) and the version/schema note (package 0.2.0, schema 1.0). No doc claims the `[exports]` renderers are in the base wheel. |
| 7 | Launch sequence documented for a zero-wiring overnight start | **PASS** | README "Launch the overnight pilot" (L332) gives the exact 4-command sequence: (1) `papers review plan --brief examples/brief-robotics.yaml --out examples/review-plan-robotics.yaml`, (2) `papers review run --plan examples/review-plan-robotics.yaml --detach`, (3) `papers review status <RUN_ID>` / `papers review resume <RUN_ID> --detach`, (4) `papers review export <RUN_ID> --formats md,pdf,docx,bib,csl,csv`. A spec-4/12 blockquote states the host must stay **awake, powered, and network-connected** for the whole run and that a reboot is recovered **only** by an explicit `resume` (never automatic). `RELEASE.md` + `examples/README.md` mirror the same sequence + requirements. |
| 8 | No functional Sci-Hub; no POSIX-only calls; Windows-safe; secrets clean | **PASS** | Whole-tree `git grep -i sci-hub`: 30 hits, **all descriptive** (removal/migration notes, spec text, test assertions checking *absence*); **zero** functional mention in `src/`, `tests/`, or config. Phase-8 files (test_phase8_acceptance.py, RELEASE.md, examples/*) contain **no** POSIX-only constructs (`shlex`, `/bin/`, `os.fork/exec`, `SIGKILL`, `shell=True`, `os.system`); the test's subprocess calls use argument arrays. `ci/secret_scan.py` → exit 0 ("secret scan clean: no key-shaped values in tree or history"). `.env` values grepped across `git log --all -p` → 0 hits for email/OpenAlex key/LLM key; the LLM **base URL** host (`api.brocode…`) appears only in a prior-phase verdict (a host, no embedded credential — allowed). |
| 9 | Honest-completion check (spec 18/21) | **PASS** | No release doc claims "the literature review is proven accurate" or a universal accuracy % (regex sweep of RELEASE.md/README/docs → 0 accuracy-claim hits). RELEASE.md presents the robotics pilot as a real-topic acceptance run the owner inspects and lists all five required honest limitations: deterministic-vs-model-driven, keyless-S2 graceful degrade, renderer preflight, host-awake/connected, reboot=explicit-resume, plus "no universal-accuracy or exhaustive-coverage claim" and "cost figures are estimates". The pilot is correctly **deferred** to the orchestrator's overnight run (Live-pilot + Overnight-soak rows are gated on this release, not covered by the offline delta). |

## Defects

**None** (0 blocker / 0 major / 0 minor).

No new defects were found in the Phase-8 batches `e610614`/`b86eb0c`/`d42755d`.
The one prior MINOR doc-completeness note (host-stay-awake not restated inline in the
README launch section) has already been fixed by the orchestrator's follow-up commit
`5814966` and is now satisfied: the README launch section (L345) inlines the
"keep the host awake, powered, and network-connected … reboot is recovered only by an
explicit `papers review resume`" requirement, so spec 4/12 is stated in-place.

## READY FOR THE OVERNIGHT PILOT: **YES**

The gate passes with zero defects. The orchestrator may launch the pilot with **zero
new wiring** — the brief + plan are committed under `examples/` and the Run row
`run-7dee3084-32ce-4f5f-b4e5-d09bceccf14f` is already `state=planned`, rev 1,
protocol_hash `c10220eb45e76566…` (hash + limits embedded in the committed plan).

**Exact launch sequence (from a clean 3.12 venv with the package installed):**

1. `papers review plan --brief examples/brief-robotics.yaml --out examples/review-plan-robotics.yaml`
2. `papers review run --plan examples/review-plan-robotics.yaml --detach`
3. `papers review status <RUN_ID>` / `papers review resume <RUN_ID> --detach`   (only if it stalled / the host rebooted)
4. `papers review export <RUN_ID> --formats md,pdf,docx,bib,csl,csv`

**Host requirements (spec 4/12):** the host must stay **powered, awake, and
network-connected** for the entire detached run — a sleeping or offline host stalls the
worker. A reboot is recovered **only** by the explicit `papers review resume <RUN_ID>
--detach` (stale-lock / dead-holder detection), never automatically. Build/install on
**Python 3.12, not 3.14**; to get PDF/DOCX in the morning bundle install the
`[exports]` extra (a bare install still yields md/bib/csl/csv + structured dumps and
marks the two renderers `missing` with a reason).
