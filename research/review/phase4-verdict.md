# Phase 4 (Research) — OPUS Reviewer Verdict

Repo: `D:/prime-agent-harness/paperCLI` · branch `main` · Phase 4 = 3 commits
(`22681fb`,`db930ef`,`f2aa37b`) on top of `a1dfeb8`, HEAD `f2aa37b` == origin/main,
clean tree. Reviewed by execution in a **fresh `uv venv --python 3.12` (CPython
3.12.8) `.vJ`** (`uv pip install -e .` → pyyaml 6.0.2, jsonschema, httpx present),
live CLI runs in scratch project dirs, in-process DB inspection, and live OpenAlex
API probes. Phase 3 was not re-litigated (its D-1/D-2/D-3 minors are what this
phase was asked to resolve). No raw secret values printed (masked by name only).

## VERDICT: **FAIL**

Zero blockers. **3 MAJOR defects**, 2 MINOR. Two of the three worker-flagged
items — **(a)** the OpenAlex citation filter and **(b)** the task-submission
duplicate-guard scoping — are ruled **not acceptable** as shipped. Non-network
suite is green (144 pass) and the 6 `@network` Phase-2 smoke tests pass online,
so *regression* is clean; the FAIL is driven by the three MAJOR functional/audit
defects below, not by test failures. PASS requires zero blockers/majors and all
three flagged items ruled acceptable or fixed — that bar is not met.

## Test results (real counts, fresh venv)

| Suite | Result |
|---|---|
| Non-network (`pytest tests -p no:cacheprovider --deselect tests/test_provider_smoke_network.py`) | **144 passed, 6 deselected, 0 failed** (Phase-3 119 offline + Phase-4 25) |
| `tests/test_phase4_research.py` alone | 25 passed |
| `@network` Phase-2 smoke (`test_provider_smoke_network.py`, online) | **6 passed** (unpaywall/crossref/openalex±key/arxiv/s2-skip) |

Full non-network+network = 150 passing. No Phase-1/2/3 regression.

## Checklist (per spec sec. 7 / 9 / 11 / 19 acceptance rows)

| # | Item | Status | Evidence |
|---|------|--------|----------|
| 1 | `review plan` valid protocol YAML + Run `planned` + protocol_hash + config snapshot + defaults recorded | PASS | Envelope `run_state=planned`, `protocol_hash` on the run row, `config-snapshots/config-<16>.json` content-addressed (reused when identical). Minimal brief → `defaults_applied` lists all 9 filled optional fields, each recorded in the saved YAML (`review_type`, `languages`, `include_preprints`, `citation_style`, …). |
| 2 | missing required field → exit 2 naming the field | PASS | Every top-level omission (`question`/`purpose`/`scope`/`outputs`/`limits`/`schema_version`) and every `limits.*` omission returned exit 2 `CONFIG_REQUIRED` naming the *correct* field (14 cases). |
| 3 | changed brief → new revision, old run immutable | PASS | Re-plan same question → revision N+1, new run row; a changed question restarts at revision 1 for that question. Snapshotted all run rows before a further plan → all prior rows **byte-identical** after (plan_json/protocol_hash/revision unchanged). |
| 4 | null monetary cap valid only when hard token/call limits set | PASS | null `max_estimated_cost_usd` + hard `max_model_calls`&`max_input_tokens` → exit 0; either call or input ceiling null → exit 2 `BriefError` naming `limits.max_estimated_cost_usd` ("null monetary cap requires hard … ceilings (spec section 7)"). A non-null $ cap with null ceilings → exit 0 (correct: cap present). |
| 5 | Screening: two-stage decision records + spec field set | PASS | Persisted `screenings` rows carry decision, criterion_ids, rationale, evidence_basis (abstract/metadata/full_text), reviewer (`deterministic/papercli-screening@1.0`), prompt_version (`deterministic-v1`), decided_at. |
| 6 | uncertainty queue + bounded recheck + unresolved reported | PASS | Ambiguous records → `uncertainty` row `queued`; exactly one deterministic recheck pass (`rechecks=1`); resolvable (year out of range) → `resolved`; the rest → `unresolved` and reported in the summary `unresolved_ids`. |
| 7 | dedup: normalized-identifier-first, fuzzy as LINKS w/ confidence, never silent merge; preprint↔report provenance | PASS | Same normalized DOI (across case/URL) → `duplicate`/`preprint_of_report` link conf 1.0; fuzzy title/author/year pair → `candidate_duplicate` conf 0.7 (<0.9); **both paper rows retained** (no merge/delete); provider provenance preserved on each. |
| 8 | Evidence: deterministic extraction, byte-exact excerpts, access-level never-infer gate, scanned cap + grade | PASS (note D-P4-5) | full_text doc → 3 rows, every `supporting_excerpt` a contiguous slice of a stored `document_text` row; `metadata_only`/`abstract_only` docs → **ZERO** rows (`gated=true`, disclosure "never inferred from metadata"); `scanned_sample.pdf` → `parse_quality=scanned`, `access_level` capped to `partial_full_text`, disclosed on the evidence rows. |
| 9 | Expansion: bounded to `max_discovered_records`, provenance chains, failing branch INCOMPLETE not complete | PASS (but see D-P4-1) | Fixture adapters: cap defers overflow (`added=1 deferred=2` at cap 1); each branch persists a `searches` row with `seed→via:provider:method:query/cursor/date` chain + provenance on candidate papers; a failing OpenAlex branch → `completeness=incomplete`, `complete=False`; a keyless S2 skip → `skipped`, also `complete=False`. |
| 10 | Task CLI (D-2): `task next` lease / `task:null`; `task submit` distinct reject codes + idempotency | PASS (but see D-P4-2/3) | `task next` leases in order (worker A→task1, B→task2, C→`task:null` ok); submit returns distinct `accepted`/`duplicate`(prior_result)/`rejected_source`/`rejected_stale`/`rejected_invalid`/`rejected_state` with exit codes; `evidence list` ok envelope. **However** the duplicate-guard has two defects (below). |
| 11 | No regression: non-network suite green | PASS | 144 passed / 0 failed (above). |
| 12 | 6 @network smoke tests | PASS (online) | 6/6 passed; would be reported skipped+why only if offline. |

## Flagged items — explicit rulings

### (a) `expansion._openalex_branch` filter name/direction — **NOT acceptable (MAJOR, D-P4-1)**

The branch hard-codes `method="citations"` (`expansion.py:236`, `:238`) and builds the
filter `citations:{work}` (`expansion.py:101`). `research/provider-docs/openalex.md`
does **not** document a `citations` filter (its confirmed filter list names only
`cited_by_count`; the fuller filtering doc lists `cites`, `cited_by`,
`referenced_works`, `referenced_works_count`, `has_references`). I probed the live
API keyless:

| filter | result |
|---|---|
| `filter=citations:W…` | **400** "citations is not a valid field" |
| `filter=references:W…` | **400** "references is not a valid field" |
| `filter=cites:W…` / `referenced_works:W…` | 200, count ≈ 280 (works this work **references**, backward) |
| `filter=cited_by:W…` | 200, count ≈ 31 (works that **cite** this work, forward) |

So the shipped `citations:` filter **does not exist in OpenAlex** — against the real
API the OpenAlex expansion branch always 400s → `completeness=incomplete` → it adds
*zero* candidates in a production run (only the keyless-skipped S2 branch also adds
zero). The intended direction is also contradictory: the docstring says "cited_by or
references" but the code exposes neither. This is exactly the "wrong/undefined
citation-graph direction = wrong expansion set" case the task calls out. **MAJOR.**
Fix: call a real filter and pick one documented direction — `cited_by:{work}` for
forward/citing works (typical expansion) or `cites:`/`referenced_works:{work}` for
backward references — and update the `method` label to match.

### (b) `jobs/tasks.py` duplicate-guard scoping — **NOT acceptable (MAJOR, D-P4-2 + D-P4-3)**

The fix scopes the *duplicate lookup* to `status=accepted` (so a rejected row doesn't
shadow a fresh claim — good) and, to satisfy the `0002 UNIQUE(task_id,payload_hash)`
index, **`DELETE`s the rejected audit rows on an accept** (`tasks.py:488`). Two
problems, both verified:

* **D-P4-2 (MAJOR):** the DELETE destroys the audit record of explicitly-rejected
  submissions. Spec sec.6 requires rejecting "invalid, stale, duplicated, or
  source-inconsistent submissions **explicitly**" — an explicitly-rejected submit
  should stay auditable (who/why/when it was rejected). Deleting it on a later
  accept of the same payload loses that trail. **MAJOR** per the task's own
  criterion ("if deleting it destroys the audit trail, that is a MAJOR defect").
* **D-P4-3 (MAJOR):** `_record_rejected` (`tasks.py:429`) blindly INSERTs. A
  *second* submission of an **identical rejected payload** (same
  `task_id`+`payload_hash`) re-INSERTs into the all-status UNIQUE index →
  **unhandled `IntegrityError` → CLI exit 1 `INTERNAL_ERROR`** instead of a clean
  reject. Reproduced both in-process and via `papers task submit`: first bad
  submit → `TASK_SUBMIT_SOURCE_MISMATCH` (exit 2), second identical → exit 1 crash.
  Fix: make the unique index partial (accepted only) or add `status` to it, keep
  rejected rows, and make the rejected insert idempotent (INSERT OR IGNORE /
  pre-check). Idempotent **accepted** resubmit (returns prior result) and genuine
  **stale** rejection both still work (verified) — the defects are the audit-row
  deletion and the rejected-payload re-insert crash.

### (c) `plan._brief_field_from_error` regex field naming — **Acceptable as shipped (MINOR, D-P4-4)**

Regex-parsing jsonschema error strings is brittle in principle, but I tested every
omission edge case (each top-level required field, and each `limits.*` field one by
one, plus a two-field-missing case): **every** case named the correct missing field
via exit 2 `CONFIG_REQUIRED` (`details.field`). So it is *correct* for the current
schemas. **MINOR** (brittleness, first-error-only) with a concrete hardening path:
walk `Draft202012Validator.iter_errors()` and read `err.path`/`err.schema_path`
directly instead of string-matching `e.message` (`plan.py:103-124`).

## Defect list (file:line · severity · one-line fix)

* **D-P4-1 (MAJOR)** — `src/papercli/research/expansion.py:101,236,238` — OpenAlex
  branch uses the non-existent `citations:{work}` filter (400 live; direction
  undefined vs the docstring's "cited_by or references"); live expansion adds zero
  candidates. Fix: use a real filter and one documented direction (`cited_by:`
  forward / `cites:` or `referenced_works:` backward) and align the `method` label.
* **D-P4-2 (MAJOR)** — `src/papercli/jobs/tasks.py:488` — accepted-submission path
  `DELETE`s non-accepted audit rows, destroying the audit trail of explicitly
  rejected submits (spec sec.6). Fix: keep rejected rows (partial index on accepted
  only, or add `status` to uniqueness) instead of deleting.
* **D-P4-3 (MAJOR)** — `src/papercli/jobs/tasks.py:429-432` — `_record_rejected`
  blindly INSERTs; a repeated identical *rejected* payload hits the 0002 all-status
  UNIQUE index → unhandled `IntegrityError` → exit 1 `INTERNAL_ERROR`. Fix: make the
  rejected insert idempotent and scope the UNIQUE index to accepted rows.
* **D-P4-4 (MINOR)** — `src/papercli/research/plan.py:103-124` — exit-2 field naming
  regex-parses jsonschema messages; correct today for all 14 omission cases but
  brittle + first-error-only. Fix: read `err.path`/`err.schema_path` from
  `iter_errors()` directly.
* **D-P4-5 (MINOR)** — `src/papercli/research/evidence.py` — `supporting_excerpt` is
  `text.strip()[:MAX_EXCERPT_CHARS]` (stripped + truncated contiguous slice,
  truncation disclosed) rather than a literal byte copy of the stored line; still
  resolves to the stored source text, so acceptable, noted for strictness.

## Notes / assumptions

- "Fresh venv" requirement honored: built `.vJ` on CPython 3.12.8 and installed the
  project there; all CLI/DB work ran against scratch projects
  (`D:/tmp/papercli-p4`, `D:/tmp/p4scratch`) so the repo tree stayed clean
  (`git status --porcelain` empty). No source file was modified.
- Live OpenAlex probes were keyless single-entity/filtered GETs (free tier), used
  only to settle item (a); no credentials were sent or printed.
- The MAJORs are in shipped, tested code but are not covered by the offline suite
  (the expansion tests use fixture adapters that fake `search_works`, so the invalid
  filter never 400s in tests; the task-submission tests never re-submit an identical
  *rejected* payload, so the crash is not exercised). Recommend adding those two
  cases to the suite alongside the fixes.
