-- Migration 011: Support multi-source stack import (Portal + App1 + App2)
--
-- Changes:
--   1. Add data_source tag to every raw table ('portal'/'app1'/'app2')
--   2. Drop UNIQUE(idcard_hash) — same person can now appear once per source
--   3. New natural key = (idcard_hash, data_source)
--   4. Downgrade child-table UNIQUE(patient_id, visit_date, facility_code) to plain
--      INDEX — same visit may arrive from 2 systems, count both (duplicate-visit
--      reports are a required deliverable)
--   5. Add *_src columns for App2 pre-computed values (interpretation, labels)
--   6. Add ETL-derived columns harmonized across all sources
--      (BMI, age_group, PHQ-9 total, ST-5 total, MAP, pulse pressure, eGFR stage)
--
-- Design note: NO imputation anywhere. Every out-of-range or unparseable input
-- becomes NULL. *_src columns preserve what the source system provided.
-- Plain-name derived columns recompute from raw whenever raw is present.

BEGIN;

-- ============================================================================
-- 1. Drop UNIQUE on idcard_hash — allow multi-source same-person
-- ============================================================================

ALTER TABLE raw_patients
    DROP CONSTRAINT IF EXISTS raw_patients_idcard_hash_key;

-- ============================================================================
-- 2. Add data_source tag + bookkeeping columns
-- ============================================================================

ALTER TABLE raw_patients
    ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'portal'
        CHECK (data_source IN ('portal', 'app1', 'app2')),
    ADD COLUMN IF NOT EXISTS import_batch_id BIGINT;

ALTER TABLE raw_visits
    ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'portal'
        CHECK (data_source IN ('portal', 'app1', 'app2')),
    ADD COLUMN IF NOT EXISTS import_batch_id BIGINT;

ALTER TABLE raw_vitalsigns
    ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'portal'
        CHECK (data_source IN ('portal', 'app1', 'app2')),
    ADD COLUMN IF NOT EXISTS import_batch_id BIGINT;

ALTER TABLE raw_homevisit
    ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'portal'
        CHECK (data_source IN ('portal', 'app1', 'app2')),
    ADD COLUMN IF NOT EXISTS import_batch_id BIGINT;

ALTER TABLE raw_homehealth
    ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'portal'
        CHECK (data_source IN ('portal', 'app1', 'app2')),
    ADD COLUMN IF NOT EXISTS import_batch_id BIGINT;

ALTER TABLE raw_lab_results
    ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'portal'
        CHECK (data_source IN ('portal', 'app1', 'app2')),
    ADD COLUMN IF NOT EXISTS import_batch_id BIGINT;

ALTER TABLE raw_lab_extended
    ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'portal'
        CHECK (data_source IN ('portal', 'app1', 'app2')),
    ADD COLUMN IF NOT EXISTS import_batch_id BIGINT;

-- ============================================================================
-- 3. New natural key: (idcard_hash, data_source) per patient
-- ============================================================================

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_patients_hash_source
    ON raw_patients (idcard_hash, data_source);

-- ============================================================================
-- 4. Downgrade natural-key UNIQUE indexes on child tables to plain INDEX
--    (stack mode — duplicate visits across systems are intentional)
-- ============================================================================

DROP INDEX IF EXISTS uq_raw_visits_natural;
DROP INDEX IF EXISTS uq_raw_vitalsigns_natural;
DROP INDEX IF EXISTS uq_raw_homevisit_natural;
DROP INDEX IF EXISTS uq_raw_homehealth_natural;
DROP INDEX IF EXISTS uq_raw_lab_results_natural;
DROP INDEX IF EXISTS uq_raw_lab_extended_natural;

CREATE INDEX IF NOT EXISTS idx_raw_visits_natural
    ON raw_visits (patient_id, visit_date, facility_code);
CREATE INDEX IF NOT EXISTS idx_raw_vitalsigns_natural
    ON raw_vitalsigns (patient_id, visit_date, facility_code);
CREATE INDEX IF NOT EXISTS idx_raw_homevisit_natural
    ON raw_homevisit (patient_id, visit_date, facility_code);
CREATE INDEX IF NOT EXISTS idx_raw_homehealth_natural
    ON raw_homehealth (patient_id, visit_date, facility_code);
CREATE INDEX IF NOT EXISTS idx_raw_lab_results_natural
    ON raw_lab_results (patient_id, visit_date, facility_code);
CREATE INDEX IF NOT EXISTS idx_raw_lab_extended_natural
    ON raw_lab_extended (patient_id, visit_date, facility_code);

-- ============================================================================
-- 5. data_source indexes (for per-source queries, data quality reports,
--    cross-system duplicate report)
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_raw_patients_data_source
    ON raw_patients (data_source);
CREATE INDEX IF NOT EXISTS idx_raw_visits_data_source
    ON raw_visits (data_source);
CREATE INDEX IF NOT EXISTS idx_raw_vitalsigns_data_source
    ON raw_vitalsigns (data_source);
CREATE INDEX IF NOT EXISTS idx_raw_homevisit_data_source
    ON raw_homevisit (data_source);
CREATE INDEX IF NOT EXISTS idx_raw_homehealth_data_source
    ON raw_homehealth (data_source);
CREATE INDEX IF NOT EXISTS idx_raw_lab_results_data_source
    ON raw_lab_results (data_source);
CREATE INDEX IF NOT EXISTS idx_raw_lab_extended_data_source
    ON raw_lab_extended (data_source);

-- ============================================================================
-- 6. *_src columns — preserve App2's pre-computed interpretations / labels
-- ============================================================================

-- raw_patients
ALTER TABLE raw_patients
    ADD COLUMN IF NOT EXISTS age_group_src TEXT;
    -- App2: "15-34 ปี", "35-44 ปี", "45-59 ปี", "60 ปีขึ้นไป"

-- raw_vitalsigns
ALTER TABLE raw_vitalsigns
    ADD COLUMN IF NOT EXISTS bmi_src        DECIMAL(5,2),   -- App2 pre-computed BMI
    ADD COLUMN IF NOT EXISTS bmi_group_src  TEXT,           -- "ปกติสมส่วน"/"อ้วนระดับ 1"/...
    ADD COLUMN IF NOT EXISTS bp_group_src   TEXT,           -- "ปกติ"/"ความดันสูง"/...
    ADD COLUMN IF NOT EXISTS st5_group_src  TEXT,           -- "ความเครียดน้อย"/...
    ADD COLUMN IF NOT EXISTS scr2q_group_src TEXT,          -- depression screen label
    ADD COLUMN IF NOT EXISTS vsact_src      TEXT,           -- vision self-care
    ADD COLUMN IF NOT EXISTS drscn_src      TEXT,           -- DR screening
    ADD COLUMN IF NOT EXISTS selfour_src    TEXT;           -- self-care label

-- raw_lab_results — App2 has text interpretations + 3 numeric lab values
ALTER TABLE raw_lab_results
    ADD COLUMN IF NOT EXISTS hemoglobin_src   DECIMAL(5,1), -- App2 LAB_HEMOGLOBIN
    ADD COLUMN IF NOT EXISTS cholesterol_src  DECIMAL(6,1), -- App2 LAB_CHOLESTERAL
    ADD COLUMN IF NOT EXISTS egfr_src         DECIMAL(6,2), -- App2 LAB_EGFR
    ADD COLUMN IF NOT EXISTS cbc_interp_src   TEXT,
    ADD COLUMN IF NOT EXISTS bldsg_interp_src TEXT,
    ADD COLUMN IF NOT EXISTS ua_interp_src    TEXT,
    ADD COLUMN IF NOT EXISTS chltr_interp_src TEXT,
    ADD COLUMN IF NOT EXISTS liver_interp_src TEXT,
    ADD COLUMN IF NOT EXISTS uric_interp_src  TEXT,
    ADD COLUMN IF NOT EXISTS cv_interp_src    TEXT, -- cervical cancer screening
    ADD COLUMN IF NOT EXISTS cl_interp_src    TEXT, -- colorectal screening
    ADD COLUMN IF NOT EXISTS egfr_interp_src  TEXT,
    ADD COLUMN IF NOT EXISTS chest_interp_src TEXT,
    ADD COLUMN IF NOT EXISTS ekg_interp_src   TEXT;

-- raw_homehealth
ALTER TABLE raw_homehealth
    ADD COLUMN IF NOT EXISTS exercise_src      TEXT,
    ADD COLUMN IF NOT EXISTS vaccine_covid_src TEXT,
    ADD COLUMN IF NOT EXISTS health_use_src    TEXT,
    ADD COLUMN IF NOT EXISTS smoke_src         TEXT,
    ADD COLUMN IF NOT EXISTS alcohol_src       TEXT,
    ADD COLUMN IF NOT EXISTS food_freq_src     TEXT,
    ADD COLUMN IF NOT EXISTS water_freq_src    TEXT,
    ADD COLUMN IF NOT EXISTS noodle_freq_src   TEXT;

-- raw_homevisit
ALTER TABLE raw_homevisit
    ADD COLUMN IF NOT EXISTS edu_src         TEXT,
    ADD COLUMN IF NOT EXISTS occupation_src  TEXT,
    ADD COLUMN IF NOT EXISTS hometype_src    TEXT,
    ADD COLUMN IF NOT EXISTS privilege_src   TEXT,
    ADD COLUMN IF NOT EXISTS work_journey_src TEXT,
    ADD COLUMN IF NOT EXISTS homeland_src    TEXT,
    ADD COLUMN IF NOT EXISTS religion_src    TEXT;

-- ============================================================================
-- 7. ETL-derived columns — harmonized across all sources (compute from raw)
-- ============================================================================

-- raw_vitalsigns: BP-group, BMI-group (code form), MAP, pulse pressure, mental totals
ALTER TABLE raw_vitalsigns
    ADD COLUMN IF NOT EXISTS bmi_group      SMALLINT,  -- 1=ผอม 2=ปกติ 3=เกิน 4=อ้วน 5=อ้วนมาก
    ADD COLUMN IF NOT EXISTS bp_group       SMALLINT,  -- 1=ปกติ 2=สูงกว่าปกติ 3=เสี่ยงสูง 4=สูงมาก
    ADD COLUMN IF NOT EXISTS map_bp         DECIMAL(5,1), -- Mean Arterial Pressure = DBP + (SBP-DBP)/3
    ADD COLUMN IF NOT EXISTS pulse_pressure SMALLINT,  -- SBP - DBP
    ADD COLUMN IF NOT EXISTS phq9_total     SMALLINT,  -- sum Q1-Q9
    ADD COLUMN IF NOT EXISTS st5_total      SMALLINT,  -- sum Q1-Q5
    ADD COLUMN IF NOT EXISTS phq9_severity  SMALLINT,  -- 0=ปกติ 1=เล็ก 2=กลาง 3=ค่อนข้าง 4=รุนแรง
    ADD COLUMN IF NOT EXISTS st5_severity   SMALLINT;  -- 0=น้อย 1=ปานกลาง 2=สูง 3=รุนแรง 4=รุนแรงมาก

-- raw_lab_results: clinical staging
ALTER TABLE raw_lab_results
    ADD COLUMN IF NOT EXISTS egfr_stage   TEXT,         -- G1/G2/G3a/G3b/G4/G5
    ADD COLUMN IF NOT EXISTS anemia_class TEXT;         -- microcytic/normocytic/macrocytic

-- Indexes on derived columns (for dashboard filtering)
CREATE INDEX IF NOT EXISTS idx_raw_vitalsigns_bmi_group ON raw_vitalsigns (bmi_group);
CREATE INDEX IF NOT EXISTS idx_raw_vitalsigns_bp_group  ON raw_vitalsigns (bp_group);
CREATE INDEX IF NOT EXISTS idx_raw_vitalsigns_phq9_sev  ON raw_vitalsigns (phq9_severity);
CREATE INDEX IF NOT EXISTS idx_raw_lab_results_egfr_stg ON raw_lab_results (egfr_stage);

-- ============================================================================
-- 8. Cross-system duplicate view (for reports)
-- ============================================================================

CREATE OR REPLACE VIEW v_cross_system_duplicates AS
SELECT
    idcard_hash,
    COUNT(DISTINCT data_source) AS n_systems,
    STRING_AGG(DISTINCT data_source, ',' ORDER BY data_source) AS systems,
    COUNT(*) AS n_patient_rows
FROM raw_patients
GROUP BY idcard_hash
HAVING COUNT(DISTINCT data_source) > 1;

-- Source-level summary (how many rows from each system)
CREATE OR REPLACE VIEW v_source_row_counts AS
SELECT 'raw_patients'      AS table_name, data_source, COUNT(*) AS n FROM raw_patients      GROUP BY data_source
UNION ALL
SELECT 'raw_visits'        AS table_name, data_source, COUNT(*) AS n FROM raw_visits        GROUP BY data_source
UNION ALL
SELECT 'raw_vitalsigns'    AS table_name, data_source, COUNT(*) AS n FROM raw_vitalsigns    GROUP BY data_source
UNION ALL
SELECT 'raw_homevisit'     AS table_name, data_source, COUNT(*) AS n FROM raw_homevisit     GROUP BY data_source
UNION ALL
SELECT 'raw_homehealth'    AS table_name, data_source, COUNT(*) AS n FROM raw_homehealth    GROUP BY data_source
UNION ALL
SELECT 'raw_lab_results'   AS table_name, data_source, COUNT(*) AS n FROM raw_lab_results   GROUP BY data_source
UNION ALL
SELECT 'raw_lab_extended'  AS table_name, data_source, COUNT(*) AS n FROM raw_lab_extended  GROUP BY data_source
ORDER BY table_name, data_source;

COMMIT;
