# Attribution

PaperCLI is a **greenfield build**. Upstream projects were inspected as
design references only — no code was forked wholesale and no upstream
package is a runtime dependency.

## Greenfield decision B

Decision **B (greenfield)** was taken at build start. There is no old
PaperCLI 0.1.0 package to migrate from in this tree: the repo contains no
legacy source, no Sci-Hub paths, and no prior `papercli-0.1.0.zip`
artifact to reuse. Concepts that proved sound (download, hashing, page
extraction, JSON output, cache) will be re-implemented and validated in
the later phases rather than carried over from a legacy codebase.

## Upstream design references

These repositories were reviewed for patterns and are recorded here with
their pinned commits and licenses. Reuse is **patterns, not code** — ideas
are not copyrightable, and any incidental similarity is to independently
derived architecture, not copied source.

| Upstream | Pinned commit | License | What we reference |
|---|---|---|---|
| [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) | `ec59a29a03d5c40f73ab40878df04860bc00e876` | MIT | Seven-stage literature-review workflow; the deliberate split between a *live* search stage and a *deterministic offline* record pipeline (dedupe/filter/rank/format); explicit, bounded DOI/citation verification that checks registration and metadata but **not** claim support. |
| [google-research/paper-orchestra](https://github.com/google-research/paper-orchestra) | `d9bae997e793a015ed38bb1605be9eb58c00284f` | Apache-2.0 | Separation of planning / discovery / synthesis / critique through typed intermediate artifacts (pydantic); bounded concurrency; bibliography + citation-map generation. |

### Anti-patterns deliberately NOT inherited

- Printing API keys in plaintext (observed in an upstream utility at import
  time). PaperCLI redacts every secret from logs, errors, JSON, and exports.
- In-memory task tracking with no persistence. PaperCLI uses a durable
  SQLite-backed task state so interrupted runs can resume.
- Silently discarded exceptions. PaperCLI surfaces failures in the JSON
  envelope with stable codes and exit codes.

Full upstream review notes live in `research/upstream/` (read-only).

## Licensing notes

MIT and Apache-2.0 both permit the kind of pattern-level borrowing done
here. No NOTICE files were present in either upstream repo, so no NOTICE
attribution is required beyond what is recorded above.
