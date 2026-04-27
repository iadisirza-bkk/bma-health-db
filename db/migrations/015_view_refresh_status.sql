-- Migration 012: Track materialized-view refresh status per import.
-- Background: a successful CSV import can leave materialized views stale if
-- REFRESH MATERIALIZED VIEW CONCURRENTLY fails. Previously this only logged
-- a warning. We now record the outcome on import_history so the admin UI
-- can flag stale data and operators know to re-run the refresh.

ALTER TABLE import_history
    ADD COLUMN IF NOT EXISTS view_refresh_status VARCHAR(20),
    ADD COLUMN IF NOT EXISTS view_refresh_error  TEXT;

COMMENT ON COLUMN import_history.view_refresh_status IS
    'NULL=not attempted, success=refreshed, failed=stale (see view_refresh_error), skipped';

CREATE INDEX IF NOT EXISTS idx_import_history_view_refresh
    ON import_history(view_refresh_status)
    WHERE view_refresh_status = 'failed';
