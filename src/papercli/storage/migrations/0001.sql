-- 0001.sql — initial PaperCLI storage schema (Phase 1 foundation)
--
-- Establishes the durable entities the research pipeline will use. Later
-- phases add migrations 0002+; this file must not be edited once applied.

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    protocol_hash TEXT NOT NULL,
    config_snapshot_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    limits_json TEXT NOT NULL,
    counters_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    type TEXT NOT NULL,
    input_hash TEXT,
    result_hash TEXT,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    dependencies_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS searches (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    executed_at TEXT,
    cursor TEXT,
    result_count INTEGER,
    completeness TEXT
);

CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    canonical_ids_json TEXT NOT NULL DEFAULT '{}',
    title TEXT,
    authors_json TEXT,
    year INTEGER,
    metadata_provenance_json TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(id),
    source_url TEXT,
    content_hash TEXT,
    version TEXT,
    access_status TEXT,
    parse_quality TEXT,
    raw_path TEXT
);

CREATE TABLE IF NOT EXISTS screenings (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(id),
    stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    criteria_json TEXT NOT NULL DEFAULT '[]',
    rationale TEXT,
    reviewer TEXT,
    prompt_version TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(id),
    document_id TEXT REFERENCES documents(id),
    access_level TEXT NOT NULL,
    location_json TEXT,
    supporting_excerpt TEXT,
    finding TEXT,
    claim_type TEXT,
    limitations_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    draft_location TEXT,
    claim_text TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    audit_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    model TEXT NOT NULL,
    prompt_hash TEXT,
    prompt_version TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd REAL,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT,
    run_revision TEXT,
    generation_status TEXT
);
