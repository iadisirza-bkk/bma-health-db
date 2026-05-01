-- Migration 115: Add columns to import_history needed by /api/admin/upload-excel.
-- The new endpoint streams xlsx/zip uploads, persists a sha256 hash for tamper
-- detection, and may pause for operator confirmation when validation emits
-- warnings. The staged tmpdir must outlive the warning gate so the operator
-- can still decide to proceed; we record its path on the row and let the
-- periodic janitor wipe it when the row stays in pending_confirm > 2h.

ALTER TABLE import_history
    ADD COLUMN IF NOT EXISTS sha256          CHAR(64),
    ADD COLUMN IF NOT EXISTS size_bytes      BIGINT,
    ADD COLUMN IF NOT EXISTS kind            VARCHAR(8),    -- 'xlsx' | 'zip'
    ADD COLUMN IF NOT EXISTS tmpdir_path     TEXT,
    ADD COLUMN IF NOT EXISTS uploaded_path   TEXT,
    ADD COLUMN IF NOT EXISTS validate_status VARCHAR(10),   -- pass|warning|fail
    ADD COLUMN IF NOT EXISTS validate_report TEXT;

COMMENT ON COLUMN import_history.sha256 IS
    'SHA-256 of the uploaded file bytes (xlsx or zip). Used for tamper detection.';
COMMENT ON COLUMN import_history.kind IS
    'Upload kind: xlsx (admin-shaped XLSX, demuxed via xlsx_to_bmi100.py) or zip (BMI_100 layout).';
COMMENT ON COLUMN import_history.tmpdir_path IS
    'Staged BMI_100/{source}/*.csv extraction dir. Cleaned on success/cancel/timeout.';
COMMENT ON COLUMN import_history.validate_status IS
    'pass (rc=0), warning (rc=2 / pending_confirm), or fail (rc=1).';

-- Status values now also include 'pending_confirm' and 'validation_failed'.
-- The CHECK constraint (if any) is loose VARCHAR(20) so no schema change needed.

CREATE INDEX IF NOT EXISTS idx_import_history_pending_confirm
    ON import_history(started_at)
    WHERE status = 'pending_confirm';
