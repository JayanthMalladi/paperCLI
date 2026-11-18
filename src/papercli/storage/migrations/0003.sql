-- 0003.sql — Phase 3: wrong-paper identity check result on Document rows.
--
-- Documents that pass ingestion keep their identity-check verdict here
-- (acceptance test 'Identity'): a definite mismatch is rejected BEFORE
-- this row exists; an inconclusive check is ingested but flagged.

ALTER TABLE documents ADD COLUMN identity_status TEXT;
ALTER TABLE documents ADD COLUMN identity_json TEXT NOT NULL DEFAULT '{}';
