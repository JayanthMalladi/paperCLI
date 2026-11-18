-- 0006.sql -- Phase 5: model-call detail + claim-ledger location.
--
-- Extends 0001 (which is never edited). 0001's ``model_calls`` table
-- carries the spec section 11 minimum (model, prompt hash/version,
-- tokens, cost, outcome). Phase 5 adds the detail a budget audit needs:
--
--   * task_type + protocol_revision: the idempotency context (spec
--     section 11: reusing a retrieval cache must not silently reuse an
--     obsolete interpretation -- the prompt key includes the protocol
--     revision + prompt version);
--   * usage_json: authoritative token/cost capture (the dollar figure is
--     an ESTIMATE unless authoritative provider usage exists, spec
--     section 12; an unknown price records cost=null +
--     pricing_known=false -- never treated as free);
--   * endpoint + api_key_env: the selected reasoning endpoint is
--     VISIBLE (spec section 13/16); the secret is stored only as its
--     environment-variable reference, never its value.
--
-- 0001's ``claims`` table carries the claim-ledger minimum; Phase 5
-- adds the stable draft location the spec section 10 audit needs.
-- Every ALTER adds a nullable column or one with a *constant* default,
-- so existing databases migrate in place.

ALTER TABLE model_calls ADD COLUMN task_type TEXT;
ALTER TABLE model_calls ADD COLUMN protocol_revision TEXT NOT NULL DEFAULT '1';
ALTER TABLE model_calls ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE model_calls ADD COLUMN endpoint TEXT;
ALTER TABLE model_calls ADD COLUMN api_key_env TEXT;
ALTER TABLE model_calls ADD COLUMN created_at TEXT NOT NULL DEFAULT (datetime('now'));

ALTER TABLE claims ADD COLUMN location TEXT;
ALTER TABLE claims ADD COLUMN is_synthesis INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_model_calls_run ON model_calls(run_id, outcome);
CREATE INDEX IF NOT EXISTS idx_claims_run ON claims(run_id, audit_status);
