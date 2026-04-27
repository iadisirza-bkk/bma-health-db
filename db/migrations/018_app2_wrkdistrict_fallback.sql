-- Migration 018: Backfill App2 district from WRKDISTRICT when home DISTRICT was 9999
--
-- App2's source CSV has 4,692 rows where DISTRICT = 9999 ("ไม่ระบุ"). Our ETL
-- correctly normalizes these to NULL (per spec). However, ~86% of those
-- patients DO have a valid WRKDISTRICT (1001..1050) — the BMA work-district
-- code. For coverage/dashboard purposes the project treats "where do you
-- access services" as roughly equivalent to "your district" so falling back
-- to WRKDISTRICT recovers ~4,016 patients that would otherwise be lost.
--
-- This migration:
--   1. raw_homevisit.home_district  ←  work_district (App2 only, when home is NULL)
--   2. raw_vitalsigns.district_code ←  work_district (App2 only, when district is NULL)
--   3. raw_patients.district_code   ←  work_district (App2 only, when district is NULL)
--
-- Trade-off: WRKDISTRICT ≠ HOMEDISTRICT for cross-district commuters (e.g.
-- someone living in 1019 working in 1024 will be tagged 1024). This is
-- acceptable for screening-coverage analytics but should be flagged as
-- "inferred" if used for residence-based epidemiology. We leave the
-- separate `work_district` column intact so the original value is auditable.

BEGIN;

-- ── Step 1: raw_homevisit.home_district (App2) ────────────────────────────
-- Fixes a separate bug where import_app2() always wrote NULL for
-- home_district even when DISTRICT was valid in source. We now copy
-- work_district (which IS being populated) into the home slot when missing.
UPDATE raw_homevisit
SET home_district = work_district
WHERE data_source = 'app2'
  AND home_district IS NULL
  AND work_district BETWEEN 1001 AND 1050;

-- Report rows updated
DO $$
DECLARE n int;
BEGIN
  GET DIAGNOSTICS n = ROW_COUNT;
  RAISE NOTICE 'Step 1: raw_homevisit App2 home_district backfilled: % rows', n;
END $$;

-- ── Step 2: raw_vitalsigns.district_code (App2) ───────────────────────────
-- For each App2 patient with NULL district_code in vitalsigns, pull the
-- most-recent valid work_district from their homevisit history.
WITH wkd_per_patient AS (
    SELECT DISTINCT ON (patient_id)
        patient_id,
        work_district
    FROM raw_homevisit
    WHERE data_source = 'app2'
      AND work_district BETWEEN 1001 AND 1050
    ORDER BY patient_id, visit_date DESC NULLS LAST
)
UPDATE raw_vitalsigns v
SET district_code = w.work_district::text
FROM wkd_per_patient w
WHERE v.data_source = 'app2'
  AND v.patient_id = w.patient_id
  AND v.district_code IS NULL;

DO $$
DECLARE n int;
BEGIN
  GET DIAGNOSTICS n = ROW_COUNT;
  RAISE NOTICE 'Step 2: raw_vitalsigns App2 district_code backfilled: % rows', n;
END $$;

-- ── Step 3: raw_patients.district_code (App2) ─────────────────────────────
-- Patient-level resolution (used by Coverage banner + dashboards).
WITH wkd_per_patient AS (
    SELECT DISTINCT ON (patient_id)
        patient_id,
        work_district
    FROM raw_homevisit
    WHERE data_source = 'app2'
      AND work_district BETWEEN 1001 AND 1050
    ORDER BY patient_id, visit_date DESC NULLS LAST
)
UPDATE raw_patients p
SET district_code = w.work_district::text
FROM wkd_per_patient w
WHERE p.data_source = 'app2'
  AND p.id = w.patient_id
  AND p.district_code IS NULL;

DO $$
DECLARE n int;
BEGIN
  GET DIAGNOSTICS n = ROW_COUNT;
  RAISE NOTICE 'Step 3: raw_patients App2 district_code backfilled: % rows', n;
END $$;

COMMIT;

-- ── Verification ──────────────────────────────────────────────────────────
SELECT 'AFTER migration 018' AS phase, data_source,
       COUNT(*) AS total_patients,
       COUNT(district_code) AS with_district,
       ROUND(100.0 * COUNT(district_code) / COUNT(*), 1) AS pct_filled,
       COUNT(*) - COUNT(district_code) AS still_missing
FROM raw_patients
GROUP BY data_source ORDER BY data_source;
