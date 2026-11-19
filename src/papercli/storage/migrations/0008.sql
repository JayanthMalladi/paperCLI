-- 0008.sql -- Phase 7: persist the recorded search error on the searches
-- row.
--
-- 0001 created the searches table without an error column; record_search
-- accepted an ``error`` argument but silently dropped it, so a provider
-- failure was visible only through the completeness flag. The spec-14
-- search-log.jsonl ("exact queries, pagination, timestamps,
-- results/coverage") and the Phase-7 coverage-and-limitations.md need
-- the recorded error text; persisting it is a nullable-with-constant-
-- default ALTER, so existing databases migrate in place.

ALTER TABLE searches ADD COLUMN error TEXT;
