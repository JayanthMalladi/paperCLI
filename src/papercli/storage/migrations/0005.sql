-- 0005.sql -- Phase 4 defect fix: scope the task-submission payload index
-- to ACCEPTED rows (D-P4-2 / D-P4-3).
--
-- 0002 created a UNIQUE index on (task_id, payload_hash) spanning ALL
-- statuses, which the submit layer had to satisfy by DELETING the
-- rejected audit rows before recording an acceptance -- destroying the
-- audit trail of explicitly-rejected submissions (spec section 6 requires
-- rejecting invalid / stale / duplicated / source-inconsistent submissions
-- EXPLICITLY and keeping them auditable).
--
-- 0001-0004 are never edited; this migration supersedes 0002's all-status
-- index instead of rewriting it:
--   1. DROP the all-status unique index;
--   2. CREATE a PARTIAL unique index on accepted rows only.
--
-- Semantics after 0005:
-- * accepted rows stay unique per (task_id, payload_hash) -- a repeated
--   identical submission still dedupes to the prior result;
-- * rejected audit rows for the same payload MAY coexist (they are audit
--   records, never results), and the submit layer dedupes identical
--   rejected re-sends instead of re-inserting (no IntegrityError crash).

DROP INDEX IF EXISTS idx_task_submissions_payload;

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_submissions_accepted_payload
    ON task_submissions(task_id, payload_hash)
    WHERE status = 'accepted';
