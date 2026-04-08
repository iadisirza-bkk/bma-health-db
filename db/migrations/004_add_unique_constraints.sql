-- Migration 004: Add composite unique constraints to prevent duplicate rows on re-import
-- BMA Health Database

BEGIN;

-- Use (patient_id, visit_date, facility_code) as natural key for each table

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_visits_natural
    ON raw_visits (patient_id, visit_date, facility_code);

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_vitalsigns_natural
    ON raw_vitalsigns (patient_id, visit_date, facility_code);

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_homevisit_natural
    ON raw_homevisit (patient_id, visit_date, facility_code);

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_homehealth_natural
    ON raw_homehealth (patient_id, visit_date, facility_code);

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_lab_results_natural
    ON raw_lab_results (patient_id, visit_date, facility_code);

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_lab_extended_natural
    ON raw_lab_extended (patient_id, visit_date, facility_code);

COMMIT;
