-- 0002.sql — Phase 3: task queue, evidence minimum fields, document parse quality.
--
-- Extends 0001 (which is never edited). Every ALTER either adds a nullable
-- column or one with a *constant* default, so databases created by 0001
-- (or fresh databases) migrate in place. Single-writer SQLite: one
-- transaction per migration, as the Phase-1 runner already does.

-- ---------------------------------------------------------------------------
-- runs: full timestamp pair (spec section 11 "timestamps").
-- ---------------------------------------------------------------------------
ALTER TABLE runs ADD COLUMN created_at TEXT;

-- ---------------------------------------------------------------------------
-- tasks: lease/claim protocol + idempotency (spec sections 6 and 11).
--
-- Idempotency key = task type + input/content hash + protocol revision.
-- Re-enqueueing the same key inside a run returns the prior task; the
-- partial unique index enforces that.
-- ---------------------------------------------------------------------------
ALTER TABLE tasks ADD COLUMN idempotency_key TEXT;
ALTER TABLE tasks ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN protocol_revision TEXT NOT NULL DEFAULT '1';
ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE tasks ADD COLUMN next_attempt_at TEXT;
ALTER TABLE tasks ADD COLUMN last_error TEXT;
ALTER TABLE tasks ADD COLUMN created_at TEXT;
ALTER TABLE tasks ADD COLUMN updated_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
    ON tasks(run_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_run_state ON tasks(run_id, state);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(state, lease_expires_at);

-- Submissions carry the lease-based external-agent protocol (spec section 6:
-- reject invalid/stale/duplicated submissions explicitly; an idempotent
-- repeated submission with the same payload hash returns the prior result).
CREATE TABLE IF NOT EXISTS task_submissions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT NOT NULL,
    worker TEXT,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,            -- accepted | duplicate | rejected
    result_hash TEXT,
    result_json TEXT,
    rejection_reason TEXT,
    submitted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_submissions_payload
    ON task_submissions(task_id, payload_hash);
CREATE INDEX IF NOT EXISTS idx_task_submissions_task ON task_submissions(task_id);

-- ---------------------------------------------------------------------------
-- evidence: complete the spec section 9 minimum record. 0001 already has
-- evidence_id (id), paper_id, document_id, access_level, location_json,
-- supporting_excerpt, finding, claim_type, limitations_json,
-- warnings_json; 0002 adds the two missing fields.
-- ---------------------------------------------------------------------------
ALTER TABLE evidence ADD COLUMN methods_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE evidence ADD COLUMN population_or_system TEXT;
CREATE INDEX IF NOT EXISTS idx_evidence_document ON evidence(document_id);
CREATE INDEX IF NOT EXISTS idx_evidence_paper ON evidence(paper_id);

-- ---------------------------------------------------------------------------
-- documents: parse-quality fields (spec section 9: preserve extraction
-- warnings and flag scanned/empty pages; OCR is out of scope and its
-- absence must be disclosed).
-- ---------------------------------------------------------------------------
ALTER TABLE documents ADD COLUMN parse_warnings_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE documents ADD COLUMN page_count INTEGER;
ALTER TABLE documents ADD COLUMN scanned_flag INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_documents_paper ON documents(paper_id);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);

-- ---------------------------------------------------------------------------
-- document_text: stable page/section/paragraph -> exact stored source text.
-- Every evidence excerpt must resolve to one of these rows (acceptance test
-- 'Extraction'). Single document, single writer: plain UNIQUE key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_text (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id),
    page INTEGER NOT NULL DEFAULT 1,
    section TEXT NOT NULL DEFAULT '',
    paragraph INTEGER NOT NULL DEFAULT 1,
    text TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_document_text_key
    ON document_text(document_id, page, section, paragraph);
CREATE INDEX IF NOT EXISTS idx_document_text_doc ON document_text(document_id);
