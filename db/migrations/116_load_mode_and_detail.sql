-- Migration 116: Add load_mode + detail JSONB columns to import_history.
-- The /api/admin/upload-excel route now accepts a load_mode toggle:
--   * 'replace' (default) — TRUNCATE bma_med data tables before export, run
--                          the whole sequence in one transaction so a failed
--                          export rolls back to the prior data set.
--   * 'append'           — preserve existing rows; rely on UPSERT/merge in
--                          export.py for de-dup.
--
-- `detail` is a free-form JSONB blob the pipeline uses for structured
-- metadata that doesn't deserve its own column: pre_truncate_counts,
-- the chosen load_mode, future audit-style fields. We deliberately keep it
-- JSONB rather than splitting into more columns because the shape varies
-- per import-mode and we want to evolve it without further migrations.

ALTER TABLE import_history
    ADD COLUMN IF NOT EXISTS load_mode VARCHAR(10),       -- 'replace' | 'append'
    ADD COLUMN IF NOT EXISTS detail    JSONB;

COMMENT ON COLUMN import_history.load_mode IS
    'Upload load mode: replace (truncate-then-load, default) or append (UPSERT/merge).';
COMMENT ON COLUMN import_history.detail IS
    'Structured pipeline metadata (pre_truncate_counts, mode notes, etc).';
