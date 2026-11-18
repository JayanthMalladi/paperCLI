-- 0004.sql -- Phase 4: research protocol runs, screening, dedup links, expansion.
--
-- Extends 0001/0002/0003 (which are never edited). Every ALTER either adds
-- a nullable column or one with a *constant* default, so existing
-- databases migrate in place. Single-writer SQLite: one transaction per
-- migration, as the runner already does.

-- ---------------------------------------------------------------------------
-- runs: store the validated protocol + revision so a changed brief/config
-- produces a NEW run revision, never a silent mutation (spec section 5).
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN plan_json TEXT;
ALTER TABLE runs ADD COLUMN protocol_revision INTEGER NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------------------
-- papers: per-record access level. The screening/evidence layers read this
-- to apply the "never infer full-text findings from metadata" gate
-- (spec section 9: full_text / partial_full_text / abstract_only /
-- metadata_only / unavailable).
-- ---------------------------------------------------------------------------
ALTER TABLE papers ADD COLUMN access_level TEXT;

-- ---------------------------------------------------------------------------
-- documents: the D-1/D-3 fix. parse_quality now holds the degraded-
-- extraction GRADE (good|partial|poor|scanned), and access_level is the
-- capped access level derived from grade + access_status (scanned/partial
-- documents are never represented as fully read; spec section 19
-- "Missing text"). access_status stays the retrieval verdict
-- (retrieved_full_text / retrieved_html_landing / unavailable).
-- ---------------------------------------------------------------------------
ALTER TABLE documents ADD COLUMN access_level TEXT;

-- ---------------------------------------------------------------------------
-- screenings: complete the spec section 9 decision-record fields
-- (evidence basis + uncertainty-queue status; reviewer + prompt_version +
-- decided_at already exist from 0001).
-- ---------------------------------------------------------------------------
ALTER TABLE screenings ADD COLUMN evidence_basis TEXT;
ALTER TABLE screenings ADD COLUMN queue_status TEXT;

-- ---------------------------------------------------------------------------
-- uncertainty queue: records the deterministic layer cannot classify enter
-- here; a bounded recheck (max 1 deterministic pass) may resolve some, the
-- rest are reported unresolved (spec section 9).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS uncertainty (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    paper_id TEXT NOT NULL REFERENCES papers(id),
    stage TEXT NOT NULL,
    reason TEXT,
    rechecks INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | resolved | unresolved
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_uncertainty_run ON uncertainty(run_id, status);

-- ---------------------------------------------------------------------------
-- paper links: dedup + version relations. Normalized identifiers link with
-- high confidence; fuzzy title/authors/year signals link as CANDIDATES
-- only -- never a silent merge; provenance of every linked record is kept
-- (spec section 9).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_links (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    source_paper_id TEXT NOT NULL REFERENCES papers(id),
    target_paper_id TEXT NOT NULL REFERENCES papers(id),
    relation TEXT NOT NULL,       -- duplicate | preprint_of_report | report_of_preprint | related_study
    confidence REAL NOT NULL DEFAULT 0.0,
    signals_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_links_pair
    ON paper_links(run_id, source_paper_id, target_paper_id, relation);
CREATE INDEX IF NOT EXISTS idx_paper_links_target ON paper_links(target_paper_id);
