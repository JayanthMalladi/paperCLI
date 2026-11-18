# PaperOrchestra (google-research/paper-orchestra) — upstream review

Repo: https://github.com/google-research/paper-orchestra
Pinned commit: d9bae997e793a015ed38bb1605be9eb58c00284f (shallow clone, 2026-11-18)
License: **Apache-2.0** (LICENSE, standard full Apache 2.0 text, 11357 bytes; no NOTICE file in the repo;
GitHub API confirms spdx_id=Apache-2.0; every source file carries a "Copyright 2026 Google LLC" + Apache 2.0 header)
Files reviewed: methods/agents/literature_review_agent.py, methods/paper_writer.py,
utils/scholar_utils.py, paper_writing_cli.py

## 1. Architecture: how planning / discovery / synthesis / critique are separated

The pipeline is a 4-agent sequential flow orchestrated by `write_single_paper()` in
methods/paper_writer.py (each step's outputs copied into the next step's inputs; the whole thing
runs inside a retry loop, `max_n_tries = 3`, that restarts the full pipeline on exception):

1. **Planning — `OutlineAgent`** (methods/agents/outline_agent.py): one LLM call over
   idea.md + experimental_log.md + template.tex + guidelines.md -> `outline.json`.
   The outline JSON is the planning artifact: it carries `intro_related_work_plan`
   (introduction_strategy with search_directions, related_work_strategy with subsections,
   each with limitation_search_queries / sota_investigation_mission) plus a `section_plan`
   blueprint with `citation_hints` per subsection.
2. **Discovery — `HybridLiteratureAgent` Phase 1** (literature_review_agent.py):
   collects search tasks from the outline (`_collect_search_tasks`: intro macro-tasks, RW
   micro-tasks, targeted "must-have" citation-hint tasks), then runs a two-stage hybrid:
   (a) **parallel** candidate discovery — Gemini with the Google Search tool, 10-thread pool,
   returns `DiscoveryResult` candidates (10-15 per exploration task, exactly 1 for targeted);
   (b) **sequential, rate-limited** enrichment — Semantic Scholar title search (`s2_title_search`,
   1s sleep between queries) to pull authoritative metadata, with cutoff-date filtering and
   dedup by normalized title.
3. **Synthesis — `HybridLiteratureAgent` Phases 2-3**: generates bibtex + citation_map from the
   registry, injects `citation_candidates` back into the outline JSON, then one writer LLM call
   synthesizes Introduction + Related Work LaTeX (`WriterOutput.latex_code`) with a hard rule
   `min_cite_paper_count = int(len(papers)*0.9)`. `SectionWritingAgent` then writes the remaining
   body sections.
4. **Critique — `ContentRefinementAgent`**: up to `max_reflections=3` rounds of LLM "peer
   review" plus a VLM LaTeX-formatting review; numeric scores are extracted and compared to
   decide convergence; outputs the final compiled PDF.

Key separation properties: planning (outline JSON) and discovery (search tasks derived from it)
are decoupled through the typed outline artifact; discovery and synthesis are decoupled through
the bibtex/citation_map/updated-outline outputs; critique is a separate agent that consumes the
draft plus citation_map. There is no feedback loop from critique back into planning — a failed
section is only retried by restarting the entire pipeline (paper_writer.py retry loop).

## 2. Typed intermediate objects

Pydantic `BaseModel`s in literature_review_agent.py:

- `CandidatePaper` {title, year, reason} — raw discovery hit.
- `DiscoveryResult` {section_name, candidates: List[CandidatePaper]} — the LLM-discovery
  response schema (injected into prompts via `DiscoveryResult.model_json_schema()`).
- `PaperData` {citation_key, title, authors, venue, year, abstract, citation_count,
  found_in_section, reason, + optional journal/volume/pages/publication_date} — enriched record.
- `WriterOutput` {sections_written, latex_code} — synthesis output.

Untyped but central: the **outline JSON** (a plain dict) is the plan; `citation_map:
Dict[str, Dict]` (key -> title/authors/venue/year/abstract) is the per-key lookup used by the
section writers; `references.bib` is the generated bibliography. Intermediate artifacts are
persisted as files (outline.json, outline_v1.json, updated_template.tex, references.bib,
citation_map.json) — the object model is pydantic-validated at the discovery/synthesis boundary
but degrades to plain JSON on disk.

## 3. Task tracking: in-memory (confirmed)

**Confirmed in-memory, no persistence.** In `HybridLiteratureAgent.run()`
(literature_review_agent.py): `unique_papers_registry: Dict[str, PaperData] = {}` and
`processed_tasks = set()` are local variables; dedup state is per-process
(`registry_lock` around the registry, task ids `section:focus` added to the set in
`_execute_tasks_in_parallel`). If a run dies, nothing about discovered/enriched papers is
preserved — a resume re-runs discovery from scratch. (The only file-based tracking is at the
*bath level* in paper_writer.py: `paper_writing_log.json` records which whole papers in a batch
succeeded, for skipping completed papers on re-run — that is coarse result logging, not
task tracking.) For PaperCLI: if we want resumable search, we need to persist the registry
ourselves; upstream proves a minimal pattern (result log keyed by paper_id) but no task state.

## 4. API-key printing: confirmed

**Confirmed.** utils/scholar_utils.py **line 28** (module-import time):

```python
S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")   # line 27
print(f"Using S2 API Key: {S2_API_KEY}")                   # line 28
```

The full Semantic Scholar API key is printed in plaintext to stdout on import — a security
anti-pattern we must not copy (log the key name / presence, never the value).

## 5. License verdict (Apache-2.0)

- Google repos use Apache-2.0; here confirmed by the LICENSE file (full standard Apache 2.0 text,
  "Copyright 2026 Google LLC" headers on all source files) and the GitHub API license endpoint.
  No NOTICE file (so no NOTICE attribution obligations beyond the license header).
- **Borrowing ideas / patterns: freely allowed** — ideas are not copyrightable; Apache-2.0
  imposes no restriction on independent re-implementation.
- **Copying code: allowed with conditions** — Apache-2.0 is permissive: we may take code into
  PaperCLI provided we (a) retain the license/copyright notice, (b) state the changes made, and
  (c) distribute the Apache license with the combined work. Apache also carries a patent
  grant — a plus for a Google-authored codebase. For an open CLI, including Apache-2.0-licensed
  modules (or clearly-attributed pattern ports) is legal; mixing with a different license later
  is a decision to flag with the user.
- Recommendation: **copy architecture/patterns (4-agent split, typed discovery records, outline
  JSON as plan artifact, citation_map lookup, retry loop, batch result log); do not lift
  `scholar_utils.py` as-is** — its import-time key print (line 28) is a defect, and its fuzzy
  title-match logic (ratio > 70) is crude; port the *interface* (title+year -> metadata record,
  cutoff filtering) with our own implementation and attribution note.

## 6. Risks / limits observed (PaperOrchestra)

- Hard-coded Gemini model names (`gemini-3.1-pro-preview`, `gemini-3-flash-preview`) as defaults —
  provider-locked; our CLI must stay model-agnostic.
- Discovery depends on the Google Search tool inside Gemini; fallback is 3 retries then empty —
  whole task silently degrades to zero candidates.
- Semantic Scholar enrichment: 1s sleep per query is sequential and slow; fuzzy matching
  threshold (>70) with year-hint +10 bias can pick the wrong record; exceptions swallowed
  (`except: pass` in `_enrich_and_register_paper`) — silent data loss.
- Abstracts truncated at 1500 chars — context loss for synthesis.
- No critique -> planning feedback; critique failures only trigger full-pipeline restarts
  (3 tries), which is expensive.
- Cutoff-date filtering logic duplicated in two places (agent + scholar_utils `is_date_valid`)
  with slightly different semantics — drift risk.
- Thread-safe print lock + bare prints for all progress: no structured logging/task state.
- API key printed at import (see §4) — must not copy.
