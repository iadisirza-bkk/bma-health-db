-- Migration 002: Create materialized views for district-level summaries
-- BMA Health Database
-- NOTE: All GROUP BY columns use COALESCE in the SELECT to guarantee NOT NULL,
--       enabling plain unique indexes required by REFRESH CONCURRENTLY.

BEGIN;

-- ============================================================
-- 1. summary_district_disease
--    Disease prevalence counts and risk percentages by district
-- ============================================================

CREATE MATERIALIZED VIEW summary_district_disease AS
SELECT
  d.dcode AS district_code,
  d.name_th AS district_name,
  d.zone_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)     AS risk_dm_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt)    AS risk_hpt_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd)    AS risk_cvd_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi)    AS risk_bmi_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm)    AS found_dm_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt)   AS found_hpt_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_cvd)   AS found_cvd_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_stroke) AS found_stroke_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity) AS found_obesity_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS found_dyslipidemia_count,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)  / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_risk_dm,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_risk_hpt,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_risk_cvd,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_found_dm,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_found_hpt,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_cvd) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_found_cvd,
  NOW() AS refreshed_at
FROM ref_districts d
LEFT JOIN raw_vitalsigns v ON v.district_code = d.dcode AND v.cancel_status IS DISTINCT FROM 1
LEFT JOIN raw_patients p ON v.patient_id = p.id
GROUP BY d.dcode, d.name_th, d.zone_code;

-- district_code comes from ref_districts.dcode (PK, never NULL) → plain index OK
CREATE UNIQUE INDEX idx_uq_summary_district_disease
  ON summary_district_disease(district_code);

-- ============================================================
-- 2. summary_district_risk_factors
--    Risk factor breakdown by district, sex, age group
-- ============================================================

CREATE MATERIALIZED VIEW summary_district_risk_factors AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COALESCE(p.sex, -1)                  AS sex,
  COALESCE(p.age_group, '__none__')    AS age_group,
  COALESCE(v.smoking, -1)              AS smoking,
  COALESCE(v.alcohol, -1)              AS alcohol,
  COALESCE(h.exercise, -1)             AS exercise,
  COUNT(*) AS patient_count,
  AVG(v.sbp) AS avg_sbp,
  AVG(v.dbp) AS avg_dbp,
  AVG(v.weight_kg) AS avg_weight_kg,
  AVG(v.waist_cm) AS avg_waist_cm,
  AVG(CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) AS avg_bmi
FROM raw_vitalsigns v
JOIN raw_patients p ON v.patient_id = p.id
LEFT JOIN raw_homehealth h ON v.patient_id = h.patient_id AND h.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY
  COALESCE(v.district_code, '__none__'),
  COALESCE(p.sex, -1),
  COALESCE(p.age_group, '__none__'),
  COALESCE(v.smoking, -1),
  COALESCE(v.alcohol, -1),
  COALESCE(h.exercise, -1);

-- All columns guaranteed NOT NULL by COALESCE → plain index works
CREATE UNIQUE INDEX idx_uq_summary_district_risk_factors
  ON summary_district_risk_factors(district_code, sex, age_group, smoking, alcohol, exercise);

-- ============================================================
-- 3. summary_district_lab
--    Average lab values and clinical thresholds per district
-- ============================================================

CREATE MATERIALIZED VIEW summary_district_lab AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT l.patient_id) AS total_lab_patients,
  AVG(l.hemoglobin) AS avg_hemoglobin,
  AVG(l.hematocrit) AS avg_hematocrit,
  AVG(l.fbs) AS avg_fbs,
  AVG(l.cholesterol) AS avg_cholesterol,
  AVG(l.triglyceride) AS avg_triglyceride,
  AVG(l.hdl) AS avg_hdl,
  AVG(l.ldl) AS avg_ldl,
  AVG(l.creatinine) AS avg_creatinine,
  AVG(l.egfr) AS avg_egfr,
  AVG(l.uric_acid) AS avg_uric_acid,
  AVG(l.sgot) AS avg_sgot,
  AVG(l.sgpt) AS avg_sgpt,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.hemoglobin < 12) / NULLIF(COUNT(*) FILTER (WHERE l.hemoglobin IS NOT NULL), 0), 2) AS pct_anemia,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.egfr < 60) / NULLIF(COUNT(*) FILTER (WHERE l.egfr IS NOT NULL), 0), 2) AS pct_ckd,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.cbc_result = 2) / NULLIF(COUNT(*), 0), 2) AS pct_cbc_abnormal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE l.liver_result = 2) / NULLIF(COUNT(*), 0), 2) AS pct_liver_abnormal
FROM raw_vitalsigns v
JOIN raw_lab_results l ON v.patient_id = l.patient_id AND l.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_summary_district_lab
  ON summary_district_lab(district_code);

-- ============================================================
-- 4. summary_district_mental
--    Mental health screening summary per district
-- ============================================================

CREATE MATERIALIZED VIEW summary_district_mental AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
    WHERE v.depression_2q_1 >= 1 OR v.depression_2q_2 >= 1
  ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_depression_risk,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
    WHERE (COALESCE(v.phq9_q1,0) + COALESCE(v.phq9_q2,0) + COALESCE(v.phq9_q3,0)
         + COALESCE(v.phq9_q4,0) + COALESCE(v.phq9_q5,0) + COALESCE(v.phq9_q6,0)
         + COALESCE(v.phq9_q7,0) + COALESCE(v.phq9_q8,0) + COALESCE(v.phq9_q9,0)) >= 10
  ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_phq9_moderate,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
    WHERE (COALESCE(v.st5_q1,0) + COALESCE(v.st5_q2,0) + COALESCE(v.st5_q3,0)
         + COALESCE(v.st5_q4,0) + COALESCE(v.st5_q5,0)) >= 7
  ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_high_stress
FROM raw_vitalsigns v
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_summary_district_mental
  ON summary_district_mental(district_code);

-- ============================================================
-- 5. summary_district_demographics
--    Education, occupation, privilege, housing breakdown
-- ============================================================

CREATE MATERIALIZED VIEW summary_district_demographics AS
SELECT
  COALESCE(hv.current_district, -1) AS district_code,
  COUNT(DISTINCT hv.patient_id) AS total_respondents,
  COUNT(*) FILTER (WHERE hv.education = 1) AS edu_none,
  COUNT(*) FILTER (WHERE hv.education = 2) AS edu_primary,
  COUNT(*) FILTER (WHERE hv.education = 3) AS edu_secondary,
  COUNT(*) FILTER (WHERE hv.education = 4) AS edu_high_school,
  COUNT(*) FILTER (WHERE hv.education = 5) AS edu_vocational,
  COUNT(*) FILTER (WHERE hv.education = 6) AS edu_bachelor,
  COUNT(*) FILTER (WHERE hv.education >= 7) AS edu_postgrad,
  COUNT(*) FILTER (WHERE hv.occupation = 1) AS occ_government,
  COUNT(*) FILTER (WHERE hv.occupation = 2) AS occ_private,
  COUNT(*) FILTER (WHERE hv.occupation = 3) AS occ_self_employed,
  COUNT(*) FILTER (WHERE hv.occupation = 4) AS occ_agriculture,
  COUNT(*) FILTER (WHERE hv.occupation = 5) AS occ_unemployed,
  COUNT(*) FILTER (WHERE hv.occupation = 6) AS occ_student,
  COUNT(*) FILTER (WHERE hv.occupation = 7) AS occ_retired,
  COUNT(*) FILTER (WHERE hv.health_privilege = 1) AS priv_ucs,
  COUNT(*) FILTER (WHERE hv.health_privilege = 2) AS priv_sso,
  COUNT(*) FILTER (WHERE hv.health_privilege = 3) AS priv_csmbs,
  COUNT(*) FILTER (WHERE hv.health_privilege = 4) AS priv_other,
  COUNT(*) FILTER (WHERE hv.home_type = 1) AS house_owned,
  COUNT(*) FILTER (WHERE hv.home_type = 2) AS house_rented,
  COUNT(*) FILTER (WHERE hv.home_type = 3) AS house_condo,
  COUNT(*) FILTER (WHERE hv.home_type = 4) AS house_other
FROM raw_homevisit hv
WHERE hv.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(hv.current_district, -1);

CREATE UNIQUE INDEX idx_uq_summary_district_demographics
  ON summary_district_demographics(district_code);

-- ============================================================
-- Refresh function for all materialized views
-- ============================================================

CREATE OR REPLACE FUNCTION refresh_all_summaries() RETURNS void AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_disease;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_risk_factors;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_lab;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_mental;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_demographics;
END;
$$ LANGUAGE plpgsql;

COMMIT;
