-- Migration 003: Create import_history table for tracking CSV import jobs.

CREATE TABLE IF NOT EXISTS import_history (
    id BIGSERIAL PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    table_name VARCHAR(50) NOT NULL,
    file_type VARCHAR(20) NOT NULL,
    rows_imported INTEGER DEFAULT 0,
    rows_skipped INTEGER DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, running, success, error
    error_message TEXT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_seconds DECIMAL(10,2),
    uploaded_by VARCHAR(50) DEFAULT 'admin'
);

CREATE INDEX IF NOT EXISTS idx_import_history_status ON import_history(status);
CREATE INDEX IF NOT EXISTS idx_import_history_started ON import_history(started_at DESC);
