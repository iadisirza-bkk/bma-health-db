-- Migration 013: Add rows_total + rows_processed to import_history for live progress
--
-- `progress_pct` + `progress_step` already exist (migration unknown / added ad-hoc).
-- This adds the per-file row counters that let the UI show "4,250 / 200,000 rows"
-- instead of just "5%" frozen for 30 minutes.

BEGIN;

ALTER TABLE import_history
    ADD COLUMN IF NOT EXISTS rows_total     INTEGER,
    ADD COLUMN IF NOT EXISTS rows_processed INTEGER DEFAULT 0;

-- For 2s polling, we want fast UPDATEs — the existing PK index is enough,
-- no new index needed (lookup is always by id).

COMMIT;
