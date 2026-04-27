-- Migration 012: Add data_source dimension to all 13 materialized views
--
-- Each view now carries `data_source` ('portal'/'app1'/'app2') as the first
-- column, GROUPed BY that source. This lets API filter by source directly
-- (`WHERE data_source = 'portal'`) without touching raw tables.
--
-- Aggregation strategy for source='all':
--   - For raw counts → SUM(count_col) GROUP BY district_code (additive)
--   - For percentages → recompute from counts in API (not from pre-computed %)
--
-- Per-source percentages pre-stored here are valid within-source only.
--
-- Each view drops + recreates to avoid messy ALTERs. All still populated
-- (REFRESHed) at end of this migration. Runtime ~5-15 minutes for 446K rows.

BEGIN;

-- ============================================================================
-- Drop all 13 matviews (CASCADE safe — no downstream views depend on them)
-- ============================================================================

DROP MATERIALIZED VIEW IF EXISTS summary_district_disease          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_district_risk_factors     CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_district_lab              CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_district_mental           CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_district_demographics     CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_disease_age_sex           CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_lab_disease_cross         CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_bmi_waist                 CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_facility                  CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_comorbidity               CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_screening_tests           CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_chronic_history           CASCADE;
DROP MATERIALIZED VIEW IF EXISTS summary_family_history            CASCADE;

-- ============================================================================
-- 1. summary_district_disease (per source)
-- ============================================================================

CREATE MATERIALIZED VIEW summary_district_disease AS
SELECT
  COALESCE(v.data_source, '__none__') AS data_source,
  d.dcode AS district_code,
  d.name_th AS district_name,
  d.zone_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)  AS risk_dm_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) AS risk_hpt_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd) AS risk_cvd_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi) AS risk_bmi_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm) AS found_dm_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt) AS found_hpt_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_cvd) AS found_cvd_count,
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
GROUP BY COALESCE(v.data_source, '__none__'), d.dcode, d.name_th, d.zone_code;

CREATE UNIQUE INDEX idx_uq_summary_district_disease
  ON summary_district_disease(data_source, district_code);

-- ============================================================================
-- 2. summary_district_risk_factors
-- ============================================================================

CREATE MATERIALIZED VIEW summary_district_risk_factors AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
  COALESCE(v.district_code, '__none__') AS district_code,
  COALESCE(p.sex, -1)                   AS sex,
  COALESCE(p.age_group, '__none__')     AS age_group,
  COALESCE(v.smoking, -1)               AS smoking,
  COALESCE(v.alcohol, -1)               AS alcohol,
  COALESCE(h.exercise, -1)              AS exercise,
  COUNT(*) AS patient_count,
  AVG(v.sbp) AS avg_sbp,
  AVG(v.dbp) AS avg_dbp,
  AVG(v.weight_kg) AS avg_weight_kg,
  AVG(v.waist_cm) AS avg_waist_cm,
  AVG(COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END)) AS avg_bmi
FROM raw_vitalsigns v
JOIN raw_patients p ON v.patient_id = p.id
LEFT JOIN raw_homehealth h ON v.patient_id = h.patient_id AND h.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY
  COALESCE(v.data_source, '__none__'),
  COALESCE(v.district_code, '__none__'),
  COALESCE(p.sex, -1),
  COALESCE(p.age_group, '__none__'),
  COALESCE(v.smoking, -1),
  COALESCE(v.alcohol, -1),
  COALESCE(h.exercise, -1);

CREATE UNIQUE INDEX idx_uq_summary_district_risk_factors
  ON summary_district_risk_factors(data_source, district_code, sex, age_group, smoking, alcohol, exercise);

-- ============================================================================
-- 3. summary_district_lab
-- ============================================================================

CREATE MATERIALIZED VIEW summary_district_lab AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
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
GROUP BY COALESCE(v.data_source, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_summary_district_lab
  ON summary_district_lab(data_source, district_code);

-- ============================================================================
-- 4. summary_district_mental
-- ============================================================================

CREATE MATERIALIZED VIEW summary_district_mental AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
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
GROUP BY COALESCE(v.data_source, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_summary_district_mental
  ON summary_district_mental(data_source, district_code);

-- ============================================================================
-- 5. summary_district_demographics
-- ============================================================================

CREATE MATERIALIZED VIEW summary_district_demographics AS
SELECT
  COALESCE(hv.data_source, '__none__') AS data_source,
  COALESCE(hv.current_district, -1)    AS district_code,
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
GROUP BY COALESCE(hv.data_source, '__none__'), COALESCE(hv.current_district, -1);

CREATE UNIQUE INDEX idx_uq_summary_district_demographics
  ON summary_district_demographics(data_source, district_code);

-- ============================================================================
-- 6. summary_disease_age_sex
-- ============================================================================

CREATE MATERIALIZED VIEW summary_disease_age_sex AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
  COALESCE(v.district_code, '__none__') AS district_code,
  COALESCE(p.sex, -1)                   AS sex,
  COALESCE(p.age_group, '__none__')     AS age_group,
  COUNT(DISTINCT v.patient_id) AS total_screened,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) AS risk_dm,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) AS risk_hpt,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd) AS risk_cvd,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi) AS risk_bmi,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm) AS found_dm,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt) AS found_hpt,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_cvd) AS found_cvd,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_stroke) AS found_stroke,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity) AS found_obesity,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS found_dyslipidemia
FROM raw_vitalsigns v
JOIN raw_patients p ON v.patient_id = p.id
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY
  COALESCE(v.data_source, '__none__'),
  COALESCE(v.district_code, '__none__'),
  COALESCE(p.sex, -1),
  COALESCE(p.age_group, '__none__');

CREATE UNIQUE INDEX idx_uq_disease_age_sex
  ON summary_disease_age_sex(data_source, district_code, sex, age_group);

-- ============================================================================
-- 7. summary_lab_disease_cross
-- ============================================================================

CREATE MATERIALIZED VIEW summary_lab_disease_cross AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
  COALESCE(v.district_code, '__none__') AS district_code,

  AVG(l.fbs) FILTER (WHERE v.found_dm) AS avg_fbs_dm_positive,
  AVG(l.fbs) FILTER (WHERE NOT v.found_dm OR v.found_dm IS NULL) AS avg_fbs_dm_negative,
  COUNT(*) FILTER (WHERE l.fbs IS NOT NULL AND v.found_dm) AS n_fbs_dm_positive,
  COUNT(*) FILTER (WHERE l.fbs IS NOT NULL AND (NOT v.found_dm OR v.found_dm IS NULL)) AS n_fbs_dm_negative,

  AVG(v.sbp) FILTER (WHERE v.found_hpt) AS avg_sbp_hpt_positive,
  AVG(v.sbp) FILTER (WHERE NOT v.found_hpt OR v.found_hpt IS NULL) AS avg_sbp_hpt_negative,
  AVG(v.dbp) FILTER (WHERE v.found_hpt) AS avg_dbp_hpt_positive,
  AVG(v.dbp) FILTER (WHERE NOT v.found_hpt OR v.found_hpt IS NULL) AS avg_dbp_hpt_negative,

  AVG(l.cholesterol) FILTER (WHERE v.found_dyslipidemia) AS avg_chol_dyslip_positive,
  AVG(l.cholesterol) FILTER (WHERE NOT v.found_dyslipidemia OR v.found_dyslipidemia IS NULL) AS avg_chol_dyslip_negative,
  AVG(l.ldl) FILTER (WHERE v.found_dyslipidemia) AS avg_ldl_dyslip_positive,
  AVG(l.ldl) FILTER (WHERE NOT v.found_dyslipidemia OR v.found_dyslipidemia IS NULL) AS avg_ldl_dyslip_negative,

  AVG(l.egfr) AS avg_egfr_all,
  COUNT(*) FILTER (WHERE l.egfr IS NOT NULL AND l.egfr < 60) AS n_ckd,
  COUNT(*) FILTER (WHERE l.egfr IS NOT NULL AND l.egfr >= 60) AS n_no_ckd,

  AVG(l.hemoglobin) AS avg_hemoglobin_all,
  COUNT(*) FILTER (WHERE l.hemoglobin IS NOT NULL AND l.hemoglobin < 12) AS n_anemia,

  COUNT(DISTINCT v.patient_id) AS total_patients

FROM raw_vitalsigns v
LEFT JOIN raw_lab_results l ON v.patient_id = l.patient_id AND l.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.data_source, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_lab_disease_cross
  ON summary_lab_disease_cross(data_source, district_code);

-- ============================================================================
-- 8. summary_bmi_waist
-- ============================================================================

CREATE MATERIALIZED VIEW summary_bmi_waist AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
  COALESCE(v.district_code, '__none__') AS district_code,
  COALESCE(p.sex, -1)                   AS sex,
  COUNT(*) FILTER (WHERE v.bmi IS NOT NULL OR (v.height_cm > 0 AND v.weight_kg > 0)) AS total_measured,

  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) < 18.5) AS bmi_underweight,
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) >= 18.5
    AND COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) < 23) AS bmi_normal,
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) >= 23
    AND COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) < 25) AS bmi_overweight,
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) >= 25
    AND COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) < 30) AS bmi_obese,
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) >= 30) AS bmi_severely_obese,

  AVG(COALESCE(v.bmi, CASE WHEN v.height_cm > 0 AND v.weight_kg > 0
    THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END)) AS avg_bmi,

  COUNT(*) FILTER (WHERE v.waist_cm IS NOT NULL AND v.waist_cm > 0) AS total_waist_measured,
  AVG(v.waist_cm) FILTER (WHERE v.waist_cm > 0) AS avg_waist,
  COUNT(*) FILTER (WHERE v.waist_cm >= 90 AND p.sex = 10) AS male_waist_risk,
  COUNT(*) FILTER (WHERE v.waist_cm >= 80 AND p.sex = 20) AS female_waist_risk,

  AVG(v.height_cm) FILTER (WHERE v.height_cm > 0) AS avg_height,
  AVG(v.weight_kg) FILTER (WHERE v.weight_kg > 0) AS avg_weight

FROM raw_vitalsigns v
JOIN raw_patients p ON v.patient_id = p.id
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.data_source, '__none__'), COALESCE(v.district_code, '__none__'), COALESCE(p.sex, -1);

CREATE UNIQUE INDEX idx_uq_bmi_waist
  ON summary_bmi_waist(data_source, district_code, sex);

-- ============================================================================
-- 9. summary_facility
-- ============================================================================

CREATE MATERIALIZED VIEW summary_facility AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
  COALESCE(v.facility_code, '__none__') AS facility_code,
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) AS risk_dm,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) AS risk_hpt,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm) AS found_dm,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt) AS found_hpt,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity) AS found_obesity,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS found_dyslipidemia,
  COUNT(DISTINCT l.patient_id) AS lab_completed,
  MIN(v.visit_date) AS first_screening,
  MAX(v.visit_date) AS last_screening
FROM raw_vitalsigns v
LEFT JOIN raw_lab_results l ON v.patient_id = l.patient_id AND l.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.data_source, '__none__'), COALESCE(v.facility_code, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_facility
  ON summary_facility(data_source, facility_code, district_code);

-- ============================================================================
-- 10. summary_comorbidity
-- ============================================================================

CREATE MATERIALIZED VIEW summary_comorbidity AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND NOT COALESCE(v.found_hpt, false) AND NOT COALESCE(v.found_cvd, false)) AS dm_only,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt AND NOT COALESCE(v.found_dm, false) AND NOT COALESCE(v.found_cvd, false)) AS hpt_only,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity AND NOT COALESCE(v.found_dm, false) AND NOT COALESCE(v.found_hpt, false)) AS obesity_only,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_hpt) AS dm_and_hpt,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_obesity) AS dm_and_obesity,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_dyslipidemia) AS dm_and_dyslipidemia,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt AND v.found_obesity) AS hpt_and_obesity,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt AND v.found_dyslipidemia) AS hpt_and_dyslipidemia,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_cvd AND v.found_stroke) AS cvd_and_stroke,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_cvd) AS dm_and_cvd,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_hpt AND v.found_dyslipidemia) AS metabolic_syndrome,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_hpt AND v.found_obesity) AS dm_hpt_obesity,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE
    (CASE WHEN v.found_dm THEN 1 ELSE 0 END +
     CASE WHEN v.found_hpt THEN 1 ELSE 0 END +
     CASE WHEN v.found_cvd THEN 1 ELSE 0 END +
     CASE WHEN v.found_obesity THEN 1 ELSE 0 END +
     CASE WHEN v.found_dyslipidemia THEN 1 ELSE 0 END +
     CASE WHEN v.found_stroke THEN 1 ELSE 0 END) >= 2
  ) AS multi_disease_count,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE
    NOT COALESCE(v.found_dm, false) AND NOT COALESCE(v.found_hpt, false) AND
    NOT COALESCE(v.found_cvd, false) AND NOT COALESCE(v.found_obesity, false) AND
    NOT COALESCE(v.found_dyslipidemia, false) AND NOT COALESCE(v.found_stroke, false)
  ) AS no_disease

FROM raw_vitalsigns v
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.data_source, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_comorbidity
  ON summary_comorbidity(data_source, district_code);

-- ============================================================================
-- 11. summary_screening_tests
-- ============================================================================

CREATE MATERIALIZED VIEW summary_screening_tests AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.ekg IS NOT NULL AND v.ekg > 0) AS ekg_done,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.ekg = 1) AS ekg_normal,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.ekg = 2) AS ekg_abnormal,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.ekg IS NOT NULL AND v.ekg > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_ekg_done,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.chest_xray IS NOT NULL AND v.chest_xray > 0) AS xray_done,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.chest_xray = 1) AS xray_normal,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.chest_xray = 2) AS xray_abnormal,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.chest_xray IS NOT NULL AND v.chest_xray > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_xray_done,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.vision IS NOT NULL AND v.vision > 0) AS vision_done,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.vision = 1) AS vision_normal,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.vision = 2) AS vision_abnormal,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.vision IS NOT NULL AND v.vision > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_vision_done,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.dr_screening IS NOT NULL AND v.dr_screening > 0) AS dr_done,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.dr_screening = 1) AS dr_normal,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.dr_screening = 2) AS dr_abnormal,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.dr_screening IS NOT NULL AND v.dr_screening > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_dr_done

FROM raw_vitalsigns v
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.data_source, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_summary_screening_tests_dc
  ON summary_screening_tests (data_source, district_code);

-- ============================================================================
-- 12. summary_chronic_history
-- ============================================================================

CREATE MATERIALIZED VIEW summary_chronic_history AS
SELECT
  COALESCE(h.data_source, '__none__')   AS data_source,
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT h.patient_id) AS total_respondents,

  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_dm IS NOT NULL AND h.history_dm > 0) AS history_dm,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_hpt IS NOT NULL AND h.history_hpt > 0) AS history_hpt,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_stroke IS NOT NULL AND h.history_stroke > 0) AS history_stroke,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_heart IS NOT NULL AND h.history_heart > 0) AS history_heart,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_kidney IS NOT NULL AND h.history_kidney > 0) AS history_kidney,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.history_dyslipidemia IS NOT NULL AND h.history_dyslipidemia > 0) AS history_dyslipidemia,

  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.dm_treatment IS NOT NULL AND h.dm_treatment > 0) AS dm_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.hpt_treatment IS NOT NULL AND h.hpt_treatment > 0) AS hpt_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.dyslipidemia_treatment IS NOT NULL AND h.dyslipidemia_treatment > 0) AS dyslipidemia_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.heart_treatment IS NOT NULL AND h.heart_treatment > 0) AS heart_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.kidney_treatment IS NOT NULL AND h.kidney_treatment > 0) AS kidney_on_treatment,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.stroke_treatment IS NOT NULL AND h.stroke_treatment > 0) AS stroke_on_treatment,

  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.vaccine_covid IS NOT NULL AND h.vaccine_covid > 0) AS vaccinated_covid,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.vaccine_influenza IS NOT NULL AND h.vaccine_influenza > 0) AS vaccinated_influenza,

  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.exercise = 1) AS exercise_regular,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.exercise = 2) AS exercise_sometimes,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.exercise = 3 OR h.exercise IS NULL) AS exercise_never

FROM raw_homehealth h
JOIN raw_vitalsigns v ON h.patient_id = v.patient_id AND v.cancel_status IS DISTINCT FROM 1
WHERE h.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(h.data_source, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_summary_chronic_history_dc
  ON summary_chronic_history (data_source, district_code);

-- ============================================================================
-- 13. summary_family_history
-- ============================================================================

CREATE MATERIALIZED VIEW summary_family_history AS
SELECT
  COALESCE(v.data_source, '__none__')   AS data_source,
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_respondents,

  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.family_dm IS NOT NULL AND v.family_dm > 0) AS family_dm_count,

  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_dm IS NOT NULL AND h.parent_dm) AS parent_dm,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_hpt IS NOT NULL AND h.parent_hpt) AS parent_hpt,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_stroke IS NOT NULL AND h.parent_stroke) AS parent_stroke,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_heart_attack IS NOT NULL AND h.parent_heart_attack) AS parent_heart,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_kidney IS NOT NULL AND h.parent_kidney) AS parent_kidney,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_gout IS NOT NULL AND h.parent_gout) AS parent_gout,
  COUNT(DISTINCT h.patient_id) FILTER (WHERE h.parent_emphysema IS NOT NULL AND h.parent_emphysema) AS parent_emphysema,

  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE v.family_dm > 0)
    / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_family_dm

FROM raw_vitalsigns v
LEFT JOIN raw_homehealth h ON v.patient_id = h.patient_id AND h.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.data_source, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_summary_family_history_dc
  ON summary_family_history (data_source, district_code);

COMMIT;

-- ============================================================================
-- Populate views outside the main transaction (REFRESH is atomic per-view)
-- ============================================================================

REFRESH MATERIALIZED VIEW summary_district_disease;
REFRESH MATERIALIZED VIEW summary_district_risk_factors;
REFRESH MATERIALIZED VIEW summary_district_lab;
REFRESH MATERIALIZED VIEW summary_district_mental;
REFRESH MATERIALIZED VIEW summary_district_demographics;
REFRESH MATERIALIZED VIEW summary_disease_age_sex;
REFRESH MATERIALIZED VIEW summary_lab_disease_cross;
REFRESH MATERIALIZED VIEW summary_bmi_waist;
REFRESH MATERIALIZED VIEW summary_facility;
REFRESH MATERIALIZED VIEW summary_comorbidity;
REFRESH MATERIALIZED VIEW summary_screening_tests;
REFRESH MATERIALIZED VIEW summary_chronic_history;
REFRESH MATERIALIZED VIEW summary_family_history;
