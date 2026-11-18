# Phase 3 (Storage/Docs) — OPUS Reviewer Verdict

Repo: `D:/prime-agent-harness/paperCLI` · branch `main` · Phase 3 = 6 commits
(`5f1cc8d`,`8eaab26`,`26b1ae3`,`de5d583`,`c9f9c1c`,`a1dfeb8`) on top of
`61a5f59`, ending at `a1dfeb8`.

Every checklist item was verified by **execution** in a fresh venv
(`uv venv` CPython 3.12.8 + `uv pip install -e .`), live CLI runs in scratch
project dirs, scratch-DB pragma inspection, and git-history/working-tree
greps — not by reading worker claims. Phase 2 was not re-litigated (its
verdict, D-P2-1..3, is already fixed in `61a5f59`). No raw secret values are
printed anywhere in this document (masked/length-only).

## VERDICT: **PASS**

Zero blockers. 119/119 offline tests green (125/125 including the 6
`@pytest.mark.network` Phase-2 smoke tests). Wrong-paper guard works both
directions. Evidence excerpts resolve byte-exact to the stored index.
Child-process isolation is real (arg-array, no shell, both limits enforced).
Secret scan still effective. No Sci-Hub. Three minor follow-up notes, no
major defects.

## Checklist

| # | Item | Status | Evidence |
|---|------|--------|----------|
| 1 | Full pytest suite (expect 119 pass, 0 fail; Phase 1+2 no regression) | PASS | Fresh venv `python -m pytest tests -rA`: **125 passed, 0 failed** = 119 non-network + 6 `@pytest.mark.network` (Phase-2 smoke). Deselecting the network file: **119 passed, 6 deselected**. Phase-1 (`test_cli_exit_codes`,`test_envelope`,`test_schemas`,`test_redaction`,`test_config_precedence`) and Phase-2 (`test_phase2_defect_fixes`,`test_providers_offline`) files all pass. No regression. |
| 2 | Migrations idempotent + all new tables/columns | PASS | Scratch DB via `PaperStore`: `apply_migrations` 2nd/3rd run = `[]` (idempotent). PRAGMA confirms: `tasks` +{`idempotency_key`,`payload_json`,`protocol_revision`,`max_attempts`,`next_attempt_at`,`last_error`,`created_at`,`updated_at`}; new table `task_submissions`; `evidence` +{`methods_json`,`population_or_system`}; `documents` +{`parse_warnings_json`,`page_count`,`scanned_flag`,`identity_status`,`identity_json`}; new table `document_text` + `idx_document_text_key`(unique)+`idx_document_text_doc`. |
| 3 | TaskQueue lease lifecycle + distinct rejection codes | PASS | `claim→leased→running(start)→succeeded(complete, result_hash set)`; `fail→retry_wait(attempt=1)`; expired lease `release_expired()=1→pending→re-claimable by new worker`. `submit()` returns well-formed codes: `accepted`, `duplicate`(returns `prior_result`), `rejected_stale`(foreign worker / expired lease), `rejected_source`(payload input_hash mismatch), `rejected_state`(pending/terminal), `rejected_invalid`(unknown task) — each with a `reason`. CLI `task next/submit` are exit-2 `NOT_IMPLEMENTED` stubs (queue is a library protocol this phase; see defect D-3). |
| 4 | Wrong-paper guard (acceptance 'Identity') both directions | PASS | CLI: `papers ingest wrong_paper.pdf --run <run with doi 10.9999/rev.2023>` → **exit 4**, envelope `RETRIEVAL_UNAVAILABLE`, `details.reason`="embedded DOI 10.1234/other != requested DOI 10.9999/rev.2023", title_overlap=0.25; **no `documents` row created**. `papers ingest sample_paper.pdf` → exit 0, `data.identity_status="matched"`, 3 sections stored. |
| 5 | Evidence extraction byte-exact (acceptance 'Extraction') | PASS | `EvidenceIndex.resolve_excerpt("The effect size was 0.42.")` → stored row `(page=3,section=Results,para=0)`; excerpt is a verbatim substring of `row.text`. Nonexistent excerpt (`"the effect was 0.99"`) → `None` + `record_evidence` emits a disclosed `extraction_warnings` entry (never silent). No near-match/truncation drift. |
| 6 | Scanned/missing text (acceptance 'Missing text') | PASS (with D-1 note) | `scanned_sample.pdf` → `scanned=True`, `scanned_flag=1` in DB, warnings="page 2: scanned page (embedded image); OCR is out of scope and no text was extracted" + "scanned/empty pages detected; OCR is out of scope and those pages have no extractable text". `read`/`ingest` envelopes carry `scanned:true` + disclosure → the paper is *flagged, not represented as fully read*. See D-1: the automatic `access_level` downgrade to `partial_full_text`/`abstract_only` is not yet computed (signal present for a later phase). |
| 7 | Child-process isolation (spec sec.16) | PASS | `parser.parse_pdf_isolated`: `cmd=[python,_parse_worker.py,tf.pdf,max_bytes]` → `subprocess.run(cmd, capture_output=True, timeout=…)` — **arg-array, no `shell=True`** (grep-confirmed). Content limit: 4105-byte payload with `max_bytes=1024` → `ParseError "content exceeds limit"`. Runtime limit: `timeout=1e-7` → `ParseError "PDF parse timed out"` in 0.025 s (child killed, parent survives). No pathological 50 MB fixture shipped, so both limit code-paths were verified directly (timeout path via a deliberately tiny timeout; size path via oversize bytes). |
| 8 | `read` CLI envelope + page-range + max-chars enforced | PASS | `papers read sample_paper.pdf --pages 1-3 --max-chars 18000` → exit 0, one envelope `{status:ok,schema_version:1.0,data:{kind:pdf,page_count:3,requested_pages:[1,2,3],text,locations,scanned},warnings:[]}`. `--pages 3` → locations only page 3. `--max-chars 50` → `characters=43`, warning "output capped at 50 chars; 2 location(s) omitted". Both limits actually enforced. |
| 9 | No Phase-2 secret-redaction regression; new paths don't write credential URLs | PASS | `mask_url_creds` still defined/used in `http_layer.py` (cache keys + error text). New `documents/ingest.py` writes only `source_url="local:{filename}"` (no URL, no credentials). Git diff of the 6 commits' **added** lines grepped for all 4 `.env` secret values (raw + `@`→`%40` percent-encoded + `urllib.quote` form): **0 hits** for PAPERS_EMAIL, OPENALEX_API_KEY, PAPERS_LLM_API_KEY, PAPERS_LLM_BASE_URL. |
| 10 | No Sci-Hub functional mention | PASS | `src/` grep (case-insensitive `sci[-_ ]?hub`): **0 hits**. Only `ATTRIBUTION.md:11` "no Sci-Hub paths" — a descriptive build-decision note, which the task allows. |
| 11a | stdlib extractor limits (CID/ligatures/multi-column) | **accepted-with-followup** | A parse-quality signal *does* exist: `parse_warnings_json` + `scanned_flag` + per-page warnings (CID/ToUnicode out-of-scope → un-mappable glyph runs yield no output and the page is flagged; empty pages flagged; scanned pages flagged). The `parse_quality` column exists but is currently set to the format kind ("pdf"/"html"/"xml"), not a degraded-extraction grade. So a later phase *can* downgrade `access_level` / flag under-extraction from the stored warnings. See D-3. Not a major defect because the disclosure signal is present and honest. |
| 11b | no-paper-id default `identity_status='skipped'` | **confirmed** | `identity.check_identity` docstring documents `skipped`; `ingest_local_document` sets `identity_status="skipped"` when no requested identity is available and the guard is not run. The `papers ingest` envelope **carries** `data.identity_status` (verified live: a run with no paper → `identity_status="skipped"`), so an agent can distinguish "guard not run" (`skipped`) from "guard passed" (`matched`). Defect-free. |
| 11c | `secret_scan.py` binary-fixture fix | **confirmed** | `python ci/secret_scan.py .` on the clean tree → **exit 0** ("secret scan clean: no key-shaped values in tree or history"). Planting a fake `PAPERS_LLM_API_KEY=sk-…` in a tracked file → **exit 1** with a head-only match ("pattern 'openai-skill' matched (sk-fak…)"). The `git log -p` output is read in binary and decoded with `replace` (commit `a1dfeb8`) so binary PDF fixtures in history can't crash the scan. Scan remains effective. |

## Defects

All three are **minor** (follow-ups / accepted scoping). No blocker, no major.

- **D-1 (minor)** — `src/papercli/storage/db.py:193` (`record_document`) and the ingest path always store `access_status="retrieved_full_text"` for local docs, including scanned/partial ones; there is no mapping from `scanned_flag`→capped `access_level` (`partial_full_text`/`abstract_only`). The scanned flag + OCR-disclosure warning are emitted (so nothing is *silently* represented as fully read), but the automatic access-level downgrade the "Missing text" acceptance wording asks for is not implemented. Fix: when `scanned` is True (or some page has empty text), cap the derived `access_level` to `partial_full_text` and record it on the document/evidence.
- **D-2 (minor)** — spec sec.6 lists `papers task next/submit` as the CLI contract, but Phase 3 ships the lease-based `TaskQueue` **library** protocol only; the CLI `task` commands are exit-2 `NOT_IMPLEMENTED` stubs. The queue itself is complete and tested (check 3). Fix: wire the CLI `task next/submit` to `TaskQueue`/`submit()` in the next phase. Accepted scoping, not a defect in what was built.
- **D-3 (minor)** — `parse_quality` column is populated with the document *kind* ("pdf"/"xml"/"html"), not a degraded-extraction grade; under-extraction of CID/ligature/multi-column PDFs is signalled only implicitly via generic per-page `parse_warnings` entries. The hook for a later phase to flag/downgrade exists (`parse_warnings_json` + `scanned_flag` + `parse_quality`), but no explicit quality level yet. Fix (follow-up): emit a named degraded-extraction grade (e.g. `partial`/`low`) into `parse_quality` when warnings indicate CID/ligature/multi-column gaps.

## Risk rulings

- **(a) stdlib extractor limits** → **accepted-with-followup.** The parse-quality/warning signal *exists* (`parse_warnings_json`, `scanned_flag`, per-page empty/scanned/CID warnings, `parse_quality` column), so a later phase can downgrade `access_level` or flag degraded extraction. Not a MAJOR defect — the disclosure is present and honest; only the explicit quality grade is missing (D-3).
- **(b) no-paper-id `identity_status='skipped'`** → **confirmed.** Documented (identity.py docstring) and the ingest result envelope carries `identity_status`, so "guard not run" is distinguishable from "guard passed". No defect.
- **(c) secret_scan.py binary-fixture fix** → **confirmed.** Exit 0 on the clean tree; exit 1 on a planted fake secret; git-history output is decoded with `replace` so binary fixtures can't crash the scan. The scan remains effective.

## Notes / assumptions

- "119 tests" in the worker report = the offline (non-network) suite count; the full suite is 125 (119 + 6 `@pytest.mark.network` Phase-2 smoke tests that all pass in a live-venv run). No discrepancy.
- Child-process runtime limit was verified by forcing a near-zero timeout on the real fixture (deterministic, no pathological 50 MB fixture shipped); content-size limit verified by oversize bytes. Both limit code-paths abort cleanly and never hang the parent.
- No source files were modified and nothing was committed; all runs used scratch dirs/DBs and a scratch venv. Secret values are referenced by name/length only.
