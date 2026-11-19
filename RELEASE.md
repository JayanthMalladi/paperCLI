# PaperCLI v0.2.0 — Release

Phase 8 (Acceptance + Release). The final build phase: real-topic pilot
brief, failure injection, acceptance tests, and the package release. The
overnight run itself is launched by the orchestrator **after** this gate
(spec 18 step 8) — it is NOT launched here.

## Package / schema versions

- **Package (distribution) version: `agent-papercli` `0.2.0`** (up from
  the greenfield baseline `0.1.0`; the earlier deliverable
  `papercli-0.1.0.zip` is the pre-rebuild legacy artifact, spec 17).
- **Schema version: `1.0`** (the nine published JSON Schemas in
  `src/papercli/agent_contract/schemas/` are versioned independently of
  the package and are unchanged).
- Python: `>=3.10` (tested + pinned on 3.12; see the 3.14 quirk below).
- Runtime deps are pinned in `pyproject.toml`
  (httpx==0.28.1, pydantic==2.14.2, jsonschema==4.26.0, pyyaml==6.0.2,
  tomli for 3.10). The `[exports]` extra adds python-docx==1.2.0 +
  pypandoc-binary==1.14 (the PDF/DOCX renderers); the `[dev]` extra adds
  pytest + coverage.

## Building + installing the distribution

**Honest outcome: the wheel build SUCCEEDED in this environment.**
`uv build` produced both artifacts from `pyproject.toml`:

```
dist/agent_papercli-0.2.0.tar.gz          # sdist
dist/agent_papercli-0.2.0-py3-none-any.whl # pure-Python wheel (py3-none-any)
```

`uv build` uses an isolated, uv-managed build environment (setuptools +
wheel), so no global `build`/`wheel` install is required. No build step
failed. The two install paths that are known-good:

1. **Editable (dev, what the `.v8` build venv uses):**
   ```
   uv venv --python 3.12 .venv
   uv pip install -e ".[exports,dev]" --python .venv/Scripts/python.exe
   ```
   Then run via `.venv/Scripts/papers` (or `python -m papercli`).
2. **The built wheel into a clean venv (verified):**
   ```
   uv venv --python 3.12 .clean
   uv pip install "dist/agent_papercli-0.2.0-py3-none-any.whl" --python .clean/Scripts/python.exe
   .clean/Scripts/papers --version     # -> "papers 0.2.0 (agent-papercli)"
   .clean/Scripts/papers doctor --json --skip-network   # healthy
   ```
   A bare wheel install omits the `[exports]` extra, so `doctor` reports
   the PDF/DOCX exporter deps as `warn` (md/bib/csl/csv + structured
   dumps still work). Add `.[exports]` to build the binary renderers.

`pip install -e .` (the documented fallback) is equivalent to path 1 and
is the working install path regardless of whether the wheel is used.
The known env quirk: build/test on **Python 3.12, not 3.14** — pyyaml /
pypandoc-binary wheels are missing on 3.14 in this environment
(pypandoc-binary is pinned to 1.14).

## Release notes (v0.2.0)

- Phases 1–8 complete: the full pipeline (providers → storage →
  research → reasoning → worker → export/skill) + the Phase-8 acceptance
  delta + the pilot brief/plan/preflight record.
- The portable skill (`papers skill export`), stdio MCP server
  (`papers mcp serve`), and the rendered exporter bundle
  (md/bib/csl/csv/pdf/docx + coverage-and-limitations.md +
  citation-audit.json) are in.
- CI: clean-venv `pytest`, the secret-scan (tree + full git history), and
  `.gitignore` guards.

## Breaking-change note (spec 17 — intentional)

- **Sci-Hub is removed completely** — no runtime adapter, config key,
  flag, env var, or doc example. This is an intentional breaking change
  versus the legacy `agent-papercli` 0.1.0.
- **Exit code 3 is reserved** (removed legacy verification code); it is
  never repurposed silently (see `src/papercli/cli/errors.py`).
- Old inputs/flags that reference the removed path return **actionable
  migration guidance (exit 2)**, not a silent reinterpretation
  (`docs/03-migration-note.md`).
- Existing immutable PDFs can still be imported; old provenance is
  preserved and identity checks are labeled accurately.
- New versions documented: package `0.2.0`, schema `1.0`.
- Do not claim existing tests prove the new pipeline (spec 17): the
  Phase-8 delta (batch 2) is the acceptance evidence.

## Honest limitations

- **Deterministic vs model-driven.** Discovery, dedup, screening,
  evidence extraction, the citations audit, and the run-summary counts
  are DETERMINISTIC. When a model endpoint is configured, synthesis +
  critique + finding extraction are MODEL-DRIVEN; without one, the
  pipeline still produces a deterministic evidence-backed bundle and
  marks the model tasks unrun (never a false "completed" review).
- **Keyless Semantic Scholar degrades gracefully.** S2 runs keyless with
  a smaller daily budget; a 429/403/miss is a recorded skip, never a run
  failure. No SEMANTIC_SCHOLAR / NCBI key is required (or present in
  `.env`).
- **Renderer preflight.** PDF/DOCX rendering depends on `pypandoc` /
  `python-docx` (the `[exports]` extra) + a LaTeX engine for PDF. A
  missing dependency marks ONLY that artifact `missing` with a reason;
  the md/bib/csl/csv + structured dumps are never destroyed. `doctor`
  preflight reports availability up front.
- **The host must stay awake + connected for a detached run.** A
  `--detach` worker is a real OS process; the machine must not sleep /
  lose network mid-run. Reboot recovery is an EXPLICIT `papers review
  resume <RUN_ID>` (stale-lock / dead-holder detection), not automatic.
- **No universal-accuracy or exhaustive-coverage claim.** Metrics are
  honest tallies of the saved registries; a found record is not a
  full-text read; the retraction check is an availability check, not a
  ruling (spec 10/19).
- **Cost figures are estimates** unless authoritative provider usage is
  recorded; an unknown model price is never treated as free. A null
  dollar cap is allowed only when the hard token/call ceilings are set.

## Launch the overnight pilot (one command per step)

Zero new wiring — the brief + plan are committed under `examples/`
(batch 1). From a clean 3.12 venv with the package installed
(path 1 or 2 above):

```
# 1) (re)validate the brief into the protocol + planned Run
papers review plan --brief examples/brief-robotics.yaml --out examples/review-plan-robotics.yaml
# 2) launch the detached overnight worker
papers review run --plan examples/review-plan-robotics.yaml --detach
# 3) watch it / recover it in the morning
papers review status <RUN_ID>
papers review resume <RUN_ID> --detach      # only if it stalled / host rebooted
# 4) export the bundle
papers review export <RUN_ID> --formats md,pdf,docx,bib,csl,csv
```

The committed plan maps to Run
`run-7dee3084-32ce-4f5f-b4e5-d09bceccf14f`
(protocol hash `c10220eb45e76566…`, state `planned`, revision 1):
20 planned query variants across arxiv/crossref/openalex/s2, expected
record volume 1000, full-text cap 60, guardrails
wall=360 min, queries=30, discovered=1000, full-text=60,
model-calls=300, in-tokens=1,000,000, out-tokens=150,000, USD=null
(the documented example ceilings, not promised throughput). A changed
brief or config creates a NEW run revision — it never mutates the prior
one (spec 5).

## Acceptance (spec 19)

Batch 2 added the DELTA rows only (the others were covered by Phases
2–7): the failure-injection matrix (provider 429 / keyless-S2 flag,
wrong-paper reject, inconclusive flag, scanned cap, fabricated key,
interrupt-then-resume), metrics honesty (completed counts + partial
never-"completed"), and the fresh-install row. Full non-network suite =
290 passed (282 baseline + 8 delta). The live-pilot / overnight-soak
rows are the orchestrator's overnight run, gated on this release.
