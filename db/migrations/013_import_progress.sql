-- Migration 013: Track in-flight import progress for the admin UI.
-- The history page now polls this every few seconds to show a live progress
-- bar instead of the user staring at a blank "running" badge.

ALTER TABLE import_history
    ADD COLUMN IF NOT EXISTS progress_step VARCHAR(120),
    ADD COLUMN IF NOT EXISTS progress_pct  SMALLINT
        CHECK (progress_pct IS NULL OR (progress_pct BETWEEN 0 AND 100));

COMMENT ON COLUMN import_history.progress_step IS
    'Free-text label of the current step (e.g., "portal/vitalsignslf"). NULL when not running.';
COMMENT ON COLUMN import_history.progress_pct IS
    'Approximate completion percentage 0..100 — updated as the importer advances.';
