-- Migration 016: pre-aggregated disease control metrics.
-- BEFORE: kpi.py:control_rates full-scanned raw_vitalsigns + raw_lab_results
--         on every request (~2-5s today, 30s+ at production scale).
-- AFTER:  one O(50) lookup against this materialized view.
--
-- Columns are intentionally named to match the metrics the KPI router
-- expects, so the application can switch with no math.

CREATE MATERIALIZED VIEW IF NOT EXISTS summary_disease_control AS
SELECT
    COALESCE(v.district_code, '__none__') AS district_code,
    -- DM control: FBS < 126 mg/dL among patients flagged found_dm.
    COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm) AS dm_diagnosed,
    COUNT(DISTINCT v.patient_id)
        FILTER (WHERE v.found_dm AND l.fbs IS NOT NULL) AS dm_with_lab,
    COUNT(DISTINCT v.patient_id)
        FILTER (WHERE v.found_dm AND l.fbs IS NOT NULL AND l.fbs < 126) AS dm_controlled,

    -- HPT control: SBP < 140 AND DBP < 90 among patients flagged found_hpt.
    COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt) AS hpt_diagnosed,
    COUNT(DISTINCT v.patient_id)
        FILTER (WHERE v.found_hpt AND v.sbp IS NOT NULL AND v.dbp IS NOT NULL) AS hpt_with_bp,
    COUNT(DISTINCT v.patient_id)
        FILTER (WHERE v.found_hpt
                AND v.sbp IS NOT NULL AND v.dbp IS NOT NULL
                AND v.sbp < 140 AND v.dbp < 90) AS hpt_controlled,

    -- Sanity check: any lab data at all in this district?
    COUNT(DISTINCT v.patient_id) FILTER (WHERE l.fbs IS NOT NULL) AS lab_patients
FROM raw_vitalsigns v
LEFT JOIN raw_lab_results l ON l.patient_id = v.patient_id
    AND l.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__');

-- REFRESH CONCURRENTLY needs a unique index
CREATE UNIQUE INDEX IF NOT EXISTS idx_uq_summary_disease_control
    ON summary_disease_control(district_code);

COMMENT ON MATERIALIZED VIEW summary_disease_control IS
    'Disease control rates per district (DM via FBS<126, HPT via SBP<140 AND DBP<90). Refresh after every ETL.';
