-- =============================================================================
-- Migration 107 — Backfill private.patient.sex_code + repair summary_bmi_waist
-- =============================================================================
--
-- Background
-- ----------
-- The v3 ETL (etl/import_csv_v3.py:259) used to write `sex_code` to
-- private.patient using a too-strict filter:
--
--     sex = str(r.get('MALE', '')).strip() or None
--     if sex and sex not in ('1', '2'):
--         sex = None
--
-- The actual MALE column values across the three sources are:
--
--   Portal      : '1' = ชาย, '2' = หญิง
--   App1        : '1' = ชาย, '2' = หญิง   (in practice many rows use '10'/'20')
--   App2 (EAV)  : raw text 'ชาย' / 'หญิง' / 'LGBTQ+' (also '10'/'20' encodings)
--
-- Result: 709,662 / 709,662 patients ended up with sex_code = NULL, which in
-- turn meant `summary_bmi_waist.male_waist_risk` and `female_waist_risk`
-- were always 0 (they need sex to bucket the >90 / >80-cm waist counts).
--
-- This migration:
--   1. Backfills `private.patient.sex_code` from existing data WITHOUT
--      re-importing CSVs (the raw_* tables have already been truncated).
--   2. Re-creates `public.summary_bmi_waist` so that sex is sourced from
--      the patient row first (canonical 'M'/'F'), and only falls back to the
--      App2 EAV text representation when no patient-row code exists.
--   3. Re-establishes the unique index on (district_code, sex) so that
--      CONCURRENTLY refresh keeps working.
--
-- The companion code change is etl/import_csv_v3.py — the regex was widened
-- to accept '1'|'10'|'M'|'ชาย' → 'M' and '2'|'20'|'F'|'หญิง' → 'F'.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Step 1: backfill private.patient.sex_code from EAV (App2 only)
-- -----------------------------------------------------------------------------
-- Logic:
--   • Look up MALE measurements in private.visit_measurement, grouped by
--     patient (via private.visit_event), and pick a deterministic
--     representative value per patient.
--   • Only App2 stores MALE in the EAV (Portal/App1 wrote it directly to the
--     patient row, but those values were nuked by the bug). For Portal/App1,
--     no in-DB source remains — they will only be recovered on the next CSV
--     re-import (which is now safe because the ETL code is fixed).
--   • Map raw text → 'M' / 'F'. Unknown values (incl. 'LGBTQ+') stay NULL.
-- -----------------------------------------------------------------------------

WITH male_per_patient AS (
  -- One representative MALE value per patient. We pick the most common
  -- non-blank value to be robust against typos / contradictory records;
  -- ties broken by alphabetical order for determinism.
  SELECT patient_id, value_text
  FROM (
    SELECT
      ve.patient_id,
      vm.value_text,
      COUNT(*) AS n,
      ROW_NUMBER() OVER (
        PARTITION BY ve.patient_id
        ORDER BY COUNT(*) DESC, vm.value_text
      ) AS rn
    FROM private.visit_measurement vm
    JOIN private.variable_definition vd ON vd.id = vm.variable_id
    JOIN private.visit_event ve         ON ve.id = vm.visit_id
    WHERE vd.csv_column_name = 'MALE'
      AND vm.value_text IS NOT NULL
      AND TRIM(vm.value_text) <> ''
    GROUP BY ve.patient_id, vm.value_text
  ) ranked
  WHERE rn = 1
),
mapped AS (
  SELECT
    patient_id,
    CASE TRIM(value_text)
      WHEN '1'    THEN 'M'
      WHEN '10'   THEN 'M'
      WHEN 'M'    THEN 'M'
      WHEN 'ชาย'  THEN 'M'
      WHEN '2'    THEN 'F'
      WHEN '20'   THEN 'F'
      WHEN 'F'    THEN 'F'
      WHEN 'หญิง'  THEN 'F'
      ELSE NULL
    END AS sex_code
  FROM male_per_patient
)
UPDATE private.patient p
   SET sex_code   = m.sex_code,
       updated_at = NOW()
  FROM mapped m
 WHERE p.id = m.patient_id
   AND p.sex_code IS NULL          -- only fill blanks; don't overwrite
   AND m.sex_code IS NOT NULL;     -- skip rows we couldn't map (e.g. 'LGBTQ+')

-- -----------------------------------------------------------------------------
-- Step 2: rebuild public.summary_bmi_waist so sex resolves from patient row
-- -----------------------------------------------------------------------------
-- The previous definition (created circa migration 105 follow-up) derived
-- sex purely from EAV `value_text='ชาย'/'หญิง'`. That works for App2 data
-- (where sex *is* in the EAV) but fails for App1/Portal (where sex is on
-- private.patient.sex_code). Combined with the bug in step 1, no Portal /
-- App1 visit ever got a non-'unknown' sex bucket — so the male/female
-- waist-risk counters were always 0.
--
-- New definition:
--   • Sex comes from private.patient.sex_code first (now backfilled).
--   • Falls back to App2 EAV value_text for the few patients without a
--     normalised sex_code on the row (defensive — should be a no-op once
--     the ETL fix is in place).
--   • Resulting bucket is the same 4-state value that downstream consumers
--     already expect: 'male' | 'female' | 'lgbtq' | 'unknown'.
--
-- The shape of the MV (columns + (district_code, sex) unique key) is
-- preserved verbatim so /summary/* endpoints continue to work unchanged.
-- -----------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS public.summary_bmi_waist;

CREATE MATERIALIZED VIEW public.summary_bmi_waist AS
WITH visit_sex AS (
  -- Per-visit sex bucket. patient.sex_code wins; EAV text is only used as
  -- a last-resort fallback for patients whose row sex_code is NULL.
  SELECT
    vr.visit_id,
    vr.home_district_code AS district_code,
    COALESCE(
      CASE p.sex_code WHEN 'M' THEN 'male'
                      WHEN 'F' THEN 'female'
                      ELSE NULL END,
      MAX(CASE
            WHEN vd.variable_key = 'sex' AND vm.value_text = 'ชาย'    THEN 'male'
            WHEN vd.variable_key = 'sex' AND vm.value_text = 'หญิง'    THEN 'female'
            WHEN vd.variable_key = 'sex' AND vm.value_text = 'LGBTQ+' THEN 'lgbtq'
            ELSE NULL
          END),
      'unknown'
    ) AS sex,
    MAX(CASE WHEN vd.variable_key = 'bmi'       THEN vm.value_number END) AS bmi_direct,
    MAX(CASE WHEN vd.variable_key = 'wstl'      THEN vm.value_number END) AS waist,
    MAX(CASE WHEN vd.variable_key = 'height_cm' THEN vm.value_number END) AS height,
    MAX(CASE WHEN vd.variable_key = 'weight_kg' THEN vm.value_number END) AS weight
  FROM public.mv_visit_resolved vr
  JOIN private.patient            p  ON p.id = vr.patient_id
  JOIN private.visit_measurement  vm ON vm.visit_id = vr.visit_id
  JOIN private.variable_definition vd ON vd.id = vm.variable_id
  WHERE vr.bucket = 'bkk'
    AND vr.is_dedup_kept
    AND vd.variable_key IN ('bmi', 'wstl', 'height_cm', 'weight_kg', 'sex')
  GROUP BY vr.visit_id, vr.home_district_code, p.sex_code
),
with_bmi AS (
  -- Derive BMI either from direct BMI measurement or from height/weight.
  SELECT
    district_code,
    sex,
    waist,
    height,
    weight,
    COALESCE(
      bmi_direct,
      CASE WHEN height > 50 AND weight > 5
           THEN weight / ((height / 100.0) * (height / 100.0))
           ELSE NULL END
    ) AS bmi
  FROM visit_sex
  WHERE district_code IS NOT NULL
),
per_sex AS (
  SELECT
    district_code,
    sex,
    COUNT(*) FILTER (WHERE bmi IS NOT NULL)                                     AS total_measured,
    COUNT(*) FILTER (WHERE bmi < 18.5)                                          AS bmi_underweight,
    COUNT(*) FILTER (WHERE bmi >= 18.5 AND bmi < 23)                            AS bmi_normal,
    COUNT(*) FILTER (WHERE bmi >= 23   AND bmi < 25)                            AS bmi_overweight,
    COUNT(*) FILTER (WHERE bmi >= 25   AND bmi < 30)                            AS bmi_obese,
    COUNT(*) FILTER (WHERE bmi >= 30)                                           AS bmi_severely_obese,
    (AVG(bmi) FILTER (WHERE bmi BETWEEN 10  AND 80))::numeric(5,2)              AS avg_bmi,
    COUNT(*) FILTER (WHERE waist IS NOT NULL)                                   AS total_waist_measured,
    (AVG(waist) FILTER (WHERE waist BETWEEN 30 AND 200))::numeric(5,2)          AS avg_waist,
    COUNT(*) FILTER (WHERE sex = 'male'   AND waist > 90)                       AS male_waist_risk,
    COUNT(*) FILTER (WHERE sex = 'female' AND waist > 80)                       AS female_waist_risk,
    (AVG(height) FILTER (WHERE height BETWEEN 50 AND 250))::numeric(5,2)        AS avg_height,
    (AVG(weight) FILTER (WHERE weight BETWEEN 5  AND 300))::numeric(5,2)        AS avg_weight
  FROM with_bmi
  GROUP BY district_code, sex
),
all_sexes AS (
  -- 'all' rollup (per district, sex='all') for endpoints that just want the
  -- district-level number without a split.
  SELECT
    district_code,
    'all'::text AS sex,
    COUNT(*) FILTER (WHERE bmi IS NOT NULL)                                     AS total_measured,
    COUNT(*) FILTER (WHERE bmi < 18.5)                                          AS bmi_underweight,
    COUNT(*) FILTER (WHERE bmi >= 18.5 AND bmi < 23)                            AS bmi_normal,
    COUNT(*) FILTER (WHERE bmi >= 23   AND bmi < 25)                            AS bmi_overweight,
    COUNT(*) FILTER (WHERE bmi >= 25   AND bmi < 30)                            AS bmi_obese,
    COUNT(*) FILTER (WHERE bmi >= 30)                                           AS bmi_severely_obese,
    (AVG(bmi) FILTER (WHERE bmi BETWEEN 10  AND 80))::numeric(5,2)              AS avg_bmi,
    COUNT(*) FILTER (WHERE waist IS NOT NULL)                                   AS total_waist_measured,
    (AVG(waist) FILTER (WHERE waist BETWEEN 30 AND 200))::numeric(5,2)          AS avg_waist,
    COUNT(*) FILTER (WHERE sex = 'male'   AND waist > 90)                       AS male_waist_risk,
    COUNT(*) FILTER (WHERE sex = 'female' AND waist > 80)                       AS female_waist_risk,
    (AVG(height) FILTER (WHERE height BETWEEN 50 AND 250))::numeric(5,2)        AS avg_height,
    (AVG(weight) FILTER (WHERE weight BETWEEN 5  AND 300))::numeric(5,2)        AS avg_weight
  FROM with_bmi
  GROUP BY district_code
)
SELECT * FROM per_sex
UNION ALL
SELECT * FROM all_sexes;

-- Recreate the unique index — required for `REFRESH MATERIALIZED VIEW
-- CONCURRENTLY public.summary_bmi_waist` (used by public.refresh_all_mvs()).
CREATE UNIQUE INDEX uq_summary_bmi_waist
  ON public.summary_bmi_waist (district_code, sex);

COMMIT;
