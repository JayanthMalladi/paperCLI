-- 0007.sql -- Phase 5: citation keys on the claim ledger.
--
-- 0006 added claims.location (stable draft location, spec section 10).
-- The citations audit validates that every citation key a claim uses
-- resolves in the run's bibliography registry (the model may SELECT
-- existing references but cannot fabricate entries, spec section 10).
-- To make that check durable -- and to surface unregistered /
-- fabricated keys in `papers citations audit` -- the claim row itself
-- carries the citation keys it cited.
--
-- Nullable-with-constant-default ALTER: existing databases migrate in
-- place; 0001-0006 are never edited.

ALTER TABLE claims ADD COLUMN citation_keys_json TEXT NOT NULL DEFAULT '[]';
