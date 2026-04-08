-- Migration 005: PDPA Compliance — Data Retention + Erasure
-- Implements Thailand PDPA Section 24 (storage limitation) and Section 33 (right to erasure)

BEGIN;

-- Track data retention policy
CREATE TABLE IF NOT EXISTS data_retention_policy (
    id SERIAL PRIMARY KEY,
    table_name VARCHAR(50) NOT NULL UNIQUE,
    retention_years INTEGER NOT NULL DEFAULT 7,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Insert default retention policies (7 years per Thai MOH guidelines)
INSERT INTO data_retention_policy (table_name, retention_years, description) VALUES
    ('raw_patients', 7, 'Patient records — 7 years per MOH guidelines'),
    ('raw_visits', 7, 'Visit history'),
    ('raw_vitalsigns', 7, 'Vital sign measurements'),
    ('raw_homevisit', 7, 'Home visit records'),
    ('raw_homehealth', 7, 'Home health assessments'),
    ('raw_lab_results', 7, 'Laboratory results'),
    ('raw_lab_extended', 7, 'Extended lab screening'),
    ('import_history', 3, 'Import audit trail')
ON CONFLICT (table_name) DO NOTHING;

-- Track erasure requests (PDPA Section 33)
CREATE TABLE IF NOT EXISTS erasure_requests (
    id BIGSERIAL PRIMARY KEY,
    idcard_hash VARCHAR(64) NOT NULL,
    request_date TIMESTAMPTZ DEFAULT NOW(),
    processed_date TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, processing, completed, rejected
    reason TEXT,
    processed_by VARCHAR(50),
    tables_affected TEXT[],
    rows_deleted INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_erasure_requests_status ON erasure_requests(status);
CREATE INDEX idx_erasure_requests_hash ON erasure_requests(idcard_hash);

-- Function to execute erasure (delete all data for a patient)
CREATE OR REPLACE FUNCTION execute_patient_erasure(p_idcard_hash VARCHAR(64))
RETURNS INTEGER AS $$
DECLARE
    v_patient_id BIGINT;
    v_total_deleted INTEGER := 0;
    v_count INTEGER;
BEGIN
    -- Find patient
    SELECT id INTO v_patient_id FROM raw_patients WHERE idcard_hash = p_idcard_hash;
    IF v_patient_id IS NULL THEN
        RETURN 0;
    END IF;

    -- Delete from all related tables (cascade)
    DELETE FROM raw_lab_extended WHERE patient_id = v_patient_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_total_deleted := v_total_deleted + v_count;

    DELETE FROM raw_lab_results WHERE patient_id = v_patient_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_total_deleted := v_total_deleted + v_count;

    DELETE FROM raw_homehealth WHERE patient_id = v_patient_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_total_deleted := v_total_deleted + v_count;

    DELETE FROM raw_homevisit WHERE patient_id = v_patient_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_total_deleted := v_total_deleted + v_count;

    DELETE FROM raw_vitalsigns WHERE patient_id = v_patient_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_total_deleted := v_total_deleted + v_count;

    DELETE FROM raw_visits WHERE patient_id = v_patient_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_total_deleted := v_total_deleted + v_count;

    -- Delete patient record last
    DELETE FROM raw_patients WHERE id = v_patient_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    v_total_deleted := v_total_deleted + v_count;

    RETURN v_total_deleted;
END;
$$ LANGUAGE plpgsql;

-- Function to purge expired data based on retention policy
CREATE OR REPLACE FUNCTION purge_expired_data()
RETURNS TABLE(table_name TEXT, rows_deleted BIGINT) AS $$
DECLARE
    v_policy RECORD;
    v_count BIGINT;
    v_cutoff_date TIMESTAMPTZ;
BEGIN
    FOR v_policy IN SELECT * FROM data_retention_policy LOOP
        v_cutoff_date := NOW() - (v_policy.retention_years || ' years')::INTERVAL;

        -- Each raw table has a created_at or visit_date column
        CASE v_policy.table_name
            WHEN 'raw_patients' THEN
                DELETE FROM raw_patients WHERE updated_at < v_cutoff_date;
            WHEN 'raw_visits' THEN
                DELETE FROM raw_visits WHERE created_at < v_cutoff_date;
            WHEN 'raw_vitalsigns' THEN
                DELETE FROM raw_vitalsigns WHERE visit_date < v_cutoff_date;
            WHEN 'raw_homevisit' THEN
                DELETE FROM raw_homevisit WHERE visit_date < v_cutoff_date;
            WHEN 'raw_homehealth' THEN
                DELETE FROM raw_homehealth WHERE visit_date < v_cutoff_date;
            WHEN 'raw_lab_results' THEN
                DELETE FROM raw_lab_results WHERE visit_date < v_cutoff_date;
            WHEN 'raw_lab_extended' THEN
                DELETE FROM raw_lab_extended WHERE visit_date < v_cutoff_date;
            WHEN 'import_history' THEN
                DELETE FROM import_history WHERE started_at < v_cutoff_date;
            ELSE
                CONTINUE;
        END CASE;

        GET DIAGNOSTICS v_count = ROW_COUNT;
        IF v_count > 0 THEN
            table_name := v_policy.table_name;
            rows_deleted := v_count;
            RETURN NEXT;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

COMMIT;
