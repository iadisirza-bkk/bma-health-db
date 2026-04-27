-- Migration 017: Add district_code to raw_patients
--
-- Problem: ~43K Portal patients exist in raw_patients but never appear in
-- raw_vitalsigns (they're registered but haven't been screened yet).
-- Their district is stored only in raw_homevisit (or implied by facility),
-- so any per-zone aggregation that joins via raw_vitalsigns drops them.
--
-- Solution: store the resolved district at the patient level too. Backfill
-- with priority:
--   1. raw_vitalsigns.district_code (latest visit)
--   2. raw_homevisit.home_district  (latest visit, must be in 1001..1050)
--   3. ref_facility_districts via the patient's first facility
--
-- This makes raw_patients.district_code authoritative for "which district
-- does this person belong to?" — independent of which child tables exist.

BEGIN;

ALTER TABLE raw_patients
    ADD COLUMN IF NOT EXISTS district_code VARCHAR(4);

CREATE INDEX IF NOT EXISTS idx_raw_patients_district_code
    ON raw_patients (district_code);

-- ─── Priority 1: most recent vitalsigns district ──────────────────────────
WITH d AS (
    SELECT DISTINCT ON (patient_id)
        patient_id, district_code
    FROM raw_vitalsigns
    WHERE district_code IS NOT NULL
      AND district_code ~ '^10[0-5][0-9]$'
    ORDER BY patient_id, visit_date DESC NULLS LAST
)
UPDATE raw_patients p
   SET district_code = d.district_code
  FROM d
 WHERE p.id = d.patient_id
   AND p.district_code IS NULL;

-- ─── Priority 2: most recent homevisit home_district ──────────────────────
WITH d AS (
    SELECT DISTINCT ON (patient_id)
        patient_id, home_district::text AS dcode
    FROM raw_homevisit
    WHERE home_district IS NOT NULL
      AND home_district BETWEEN 1001 AND 1050
    ORDER BY patient_id, visit_date DESC NULLS LAST
)
UPDATE raw_patients p
   SET district_code = d.dcode
  FROM d
 WHERE p.id = d.patient_id
   AND p.district_code IS NULL;

-- ─── Priority 3: facility code → district mapping ─────────────────────────
-- Use the most recent facility_code from any child table.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'ref_facility_districts'
    ) THEN
        WITH facility_per_patient AS (
            SELECT DISTINCT ON (patient_id) patient_id, facility_code
            FROM (
                SELECT patient_id, facility_code, visit_date
                  FROM raw_vitalsigns WHERE facility_code IS NOT NULL
                UNION ALL
                SELECT patient_id, facility_code, visit_date
                  FROM raw_homevisit  WHERE facility_code IS NOT NULL
                UNION ALL
                SELECT patient_id, facility_code, visit_date
                  FROM raw_homehealth WHERE facility_code IS NOT NULL
            ) all_visits
            ORDER BY patient_id, visit_date DESC NULLS LAST
        )
        UPDATE raw_patients p
           SET district_code = fd.district_code
          FROM facility_per_patient fp
          JOIN ref_facility_districts fd ON fd.facility_code = fp.facility_code
         WHERE p.id = fp.patient_id
           AND p.district_code IS NULL;
    END IF;
END $$;

COMMIT;

-- ─── Report after migration ───────────────────────────────────────────────
SELECT data_source,
       COUNT(*)                           AS total_patients,
       COUNT(district_code)               AS with_district,
       ROUND(100.0 * COUNT(district_code) / COUNT(*), 1) AS pct_filled,
       COUNT(*) - COUNT(district_code)    AS still_missing
FROM raw_patients
GROUP BY data_source ORDER BY data_source;
