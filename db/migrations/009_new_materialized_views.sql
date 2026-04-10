-- Migration 009: New materialized views for screening tests, chronic history, family history
-- These aggregate data from raw tables that were imported but never analyzed.

-- =========================================================================
-- 1. summary_screening_tests — EKG, chest X-ray, vision, DR screening rates
-- =========================================================================

DROP MATERIALIZED VIEW IF EXISTS summary_screening_tests;

CREATE MATERIALIZED VIEW summary_screening_tests AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,

  -- EKG: 0 = not done, 1 = normal, 2 = abnormal (from CSV CHEST/EKG encoding)
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.ekg IS NOT NULL AND v.ekg > 0) AS ekg_done,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.ekg = 1) AS ekg_normal,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.ekg = 2) AS ekg_abnormal,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.ekg IS NOT NULL AND v.ekg > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_ekg_done,

  -- Chest X-ray
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.chest_xray IS NOT NULL AND v.chest_xray > 0) AS xray_done,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.chest_xray = 1) AS xray_normal,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.chest_xray = 2) AS xray_abnormal,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.chest_xray IS NOT NULL AND v.chest_xray > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_xray_done,

  -- Vision screening
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.vision IS NOT NULL AND v.vision > 0) AS vision_done,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.vision = 1) AS vision_normal,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.vision = 2) AS vision_abnormal,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.vision IS NOT NULL AND v.vision > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_vision_done,

  -- DR (Diabetic Retinopathy) screening
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.dr_screening IS NOT NULL AND v.dr_screening > 0) AS dr_done,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.dr_screening = 1) AS dr_normal,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.dr_screening = 2) AS dr_abnormal,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.dr_screening IS NOT NULL AND v.dr_screening > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_dr_done

FROM raw_vitalsigns v
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_screening_tests_dc
  ON summary_screening_tests (district_code);


-- =========================================================================
-- 2. summary_chronic_history — Known conditions, treatment, vaccination
-- =========================================================================

DROP MATERIALIZED VIEW IF EXISTS summary_chronic_history;

CREATE MATERIALIZED VIEW summary_chronic_history AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT h.patient_id) AS total_respondents,

  -- Known chronic conditions (from homehealth — self-reported history)
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_dm IS NOT NULL AND h.history_dm > 0) AS history_dm,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_hpt IS NOT NULL AND h.history_hpt > 0) AS history_hpt,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_stroke IS NOT NULL AND h.history_stroke > 0) AS history_stroke,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_heart IS NOT NULL AND h.history_heart > 0) AS history_heart,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_kidney IS NOT NULL AND h.history_kidney > 0) AS history_kidney,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_dyslipidemia IS NOT NULL AND h.history_dyslipidemia > 0) AS history_dyslipidemia,

  -- Treatment adherence
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.dm_treatment IS NOT NULL AND h.dm_treatment > 0) AS dm_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.hpt_treatment IS NOT NULL AND h.hpt_treatment > 0) AS hpt_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.dyslipidemia_treatment IS NOT NULL AND h.dyslipidemia_treatment > 0) AS dyslipidemia_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.heart_treatment IS NOT NULL AND h.heart_treatment > 0) AS heart_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.kidney_treatment IS NOT NULL AND h.kidney_treatment > 0) AS kidney_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.stroke_treatment IS NOT NULL AND h.stroke_treatment > 0) AS stroke_on_treatment,

  -- Vaccination
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.vaccine_covid IS NOT NULL AND h.vaccine_covid > 0) AS vaccinated_covid,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.vaccine_influenza IS NOT NULL AND h.vaccine_influenza > 0) AS vaccinated_influenza,

  -- Exercise (from homehealth)
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.exercise = 1) AS exercise_regular,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.exercise = 2) AS exercise_sometimes,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.exercise = 3 OR h.exercise IS NULL) AS exercise_never

FROM raw_homehealth h
JOIN raw_vitalsigns v ON h.patient_id = v.patient_id AND v.cancel_status IS DISTINCT FROM 1
WHERE h.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_chronic_history_dc
  ON summary_chronic_history (district_code);


-- =========================================================================
-- 3. summary_family_history — Family disease history
-- =========================================================================

DROP MATERIALIZED VIEW IF EXISTS summary_family_history;

CREATE MATERIALIZED VIEW summary_family_history AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_respondents,

  -- Family DM (from vitalsigns.family_dm)
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.family_dm IS NOT NULL AND v.family_dm > 0) AS family_dm_count,

  -- Parent history (from homehealth)
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_dm IS NOT NULL AND h.parent_dm) AS parent_dm,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_hpt IS NOT NULL AND h.parent_hpt) AS parent_hpt,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_stroke IS NOT NULL AND h.parent_stroke) AS parent_stroke,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_heart_attack IS NOT NULL AND h.parent_heart_attack) AS parent_heart,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_kidney IS NOT NULL AND h.parent_kidney) AS parent_kidney,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_gout IS NOT NULL AND h.parent_gout) AS parent_gout,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_emphysema IS NOT NULL AND h.parent_emphysema) AS parent_emphysema,

  -- Percentage with any family history
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.family_dm > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_family_dm

FROM raw_vitalsigns v
LEFT JOIN raw_homehealth h ON v.patient_id = h.patient_id AND h.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_family_history_dc
  ON summary_family_history (district_code);
