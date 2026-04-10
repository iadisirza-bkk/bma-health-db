-- Migration 006: Expanded materialized views for BMA Health DB
-- P0 + P1 requirements from stakeholder review

BEGIN;

-- ============================================================
-- V6: summary_disease_age_sex
-- Disease prevalence by age group × sex × district
-- Used by: /api/v2/epidemiology/age-group-prevalence
-- ============================================================

CREATE MATERIALIZED VIEW summary_disease_age_sex AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COALESCE(p.sex, -1) AS sex,
  COALESCE(p.age_group, '__none__') AS age_group,
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
  COALESCE(v.district_code, '__none__'),
  COALESCE(p.sex, -1),
  COALESCE(p.age_group, '__none__');

CREATE UNIQUE INDEX idx_uq_disease_age_sex
  ON summary_disease_age_sex(district_code, sex, age_group);

-- ============================================================
-- V7: summary_lab_disease_cross
-- Lab averages stratified by disease flag (true vs false)
-- Used by: /api/v2/epidemiology/disease-lab-crosstab
-- ============================================================

CREATE MATERIALIZED VIEW summary_lab_disease_cross AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,

  -- FBS by DM status
  AVG(l.fbs) FILTER (WHERE v.found_dm) AS avg_fbs_dm_positive,
  AVG(l.fbs) FILTER (WHERE NOT v.found_dm OR v.found_dm IS NULL) AS avg_fbs_dm_negative,
  COUNT(*) FILTER (WHERE l.fbs IS NOT NULL AND v.found_dm) AS n_fbs_dm_positive,
  COUNT(*) FILTER (WHERE l.fbs IS NOT NULL AND (NOT v.found_dm OR v.found_dm IS NULL)) AS n_fbs_dm_negative,

  -- SBP/DBP by HPT status
  AVG(v.sbp) FILTER (WHERE v.found_hpt) AS avg_sbp_hpt_positive,
  AVG(v.sbp) FILTER (WHERE NOT v.found_hpt OR v.found_hpt IS NULL) AS avg_sbp_hpt_negative,
  AVG(v.dbp) FILTER (WHERE v.found_hpt) AS avg_dbp_hpt_positive,
  AVG(v.dbp) FILTER (WHERE NOT v.found_hpt OR v.found_hpt IS NULL) AS avg_dbp_hpt_negative,

  -- Cholesterol by dyslipidemia status
  AVG(l.cholesterol) FILTER (WHERE v.found_dyslipidemia) AS avg_chol_dyslip_positive,
  AVG(l.cholesterol) FILTER (WHERE NOT v.found_dyslipidemia OR v.found_dyslipidemia IS NULL) AS avg_chol_dyslip_negative,
  AVG(l.ldl) FILTER (WHERE v.found_dyslipidemia) AS avg_ldl_dyslip_positive,
  AVG(l.ldl) FILTER (WHERE NOT v.found_dyslipidemia OR v.found_dyslipidemia IS NULL) AS avg_ldl_dyslip_negative,

  -- eGFR by CKD proxy (egfr < 60)
  AVG(l.egfr) AS avg_egfr_all,
  COUNT(*) FILTER (WHERE l.egfr IS NOT NULL AND l.egfr < 60) AS n_ckd,
  COUNT(*) FILTER (WHERE l.egfr IS NOT NULL AND l.egfr >= 60) AS n_no_ckd,

  -- Hemoglobin overall
  AVG(l.hemoglobin) AS avg_hemoglobin_all,
  COUNT(*) FILTER (WHERE l.hemoglobin IS NOT NULL AND l.hemoglobin < 12) AS n_anemia,

  COUNT(DISTINCT v.patient_id) AS total_patients

FROM raw_vitalsigns v
LEFT JOIN raw_lab_results l ON v.patient_id = l.patient_id AND l.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_lab_disease_cross
  ON summary_lab_disease_cross(district_code);

-- ============================================================
-- V8: summary_bmi_waist
-- BMI distribution + waist categories per district
-- Used by: /api/v2/promotion/bmi-distribution
-- ============================================================

CREATE MATERIALIZED VIEW summary_bmi_waist AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COALESCE(p.sex, -1) AS sex,
  COUNT(*) FILTER (WHERE v.bmi IS NOT NULL OR (v.height_cm > 0 AND v.weight_kg > 0)) AS total_measured,

  -- BMI categories (Asia-Pacific WHO) — use stored bmi column with fallback
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) < 18.5) AS bmi_underweight,
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) >= 18.5
    AND COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) < 23) AS bmi_normal,
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) >= 23
    AND COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) < 25) AS bmi_overweight,
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) >= 25
    AND COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) < 30) AS bmi_obese,
  COUNT(*) FILTER (WHERE COALESCE(v.bmi, CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) >= 30) AS bmi_severely_obese,

  -- BMI average — prefer stored column
  AVG(COALESCE(v.bmi, CASE WHEN v.height_cm > 0 AND v.weight_kg > 0
    THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END)) AS avg_bmi,

  -- Waist circumference (risk: male >= 90cm, female >= 80cm)
  COUNT(*) FILTER (WHERE v.waist_cm IS NOT NULL AND v.waist_cm > 0) AS total_waist_measured,
  AVG(v.waist_cm) FILTER (WHERE v.waist_cm > 0) AS avg_waist,
  COUNT(*) FILTER (WHERE v.waist_cm >= 90 AND p.sex = 10) AS male_waist_risk,
  COUNT(*) FILTER (WHERE v.waist_cm >= 80 AND p.sex = 20) AS female_waist_risk,

  -- Averages
  AVG(v.height_cm) FILTER (WHERE v.height_cm > 0) AS avg_height,
  AVG(v.weight_kg) FILTER (WHERE v.weight_kg > 0) AS avg_weight

FROM raw_vitalsigns v
JOIN raw_patients p ON v.patient_id = p.id
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__'), COALESCE(p.sex, -1);

CREATE UNIQUE INDEX idx_uq_bmi_waist
  ON summary_bmi_waist(district_code, sex);

-- ============================================================
-- V9: summary_facility
-- Screening performance per health facility
-- Used by: /api/v2/facility/performance
-- ============================================================

CREATE MATERIALIZED VIEW summary_facility AS
SELECT
  COALESCE(v.facility_code, '__none__') AS facility_code,
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) AS risk_dm,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) AS risk_hpt,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm) AS found_dm,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt) AS found_hpt,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity) AS found_obesity,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS found_dyslipidemia,
  -- Lab completion rate
  COUNT(DISTINCT l.patient_id) AS lab_completed,
  MIN(v.visit_date) AS first_screening,
  MAX(v.visit_date) AS last_screening
FROM raw_vitalsigns v
LEFT JOIN raw_lab_results l ON v.patient_id = l.patient_id AND l.cancel_status IS DISTINCT FROM 1
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.facility_code, '__none__'), COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_facility
  ON summary_facility(facility_code, district_code);

-- ============================================================
-- V10: summary_comorbidity
-- Disease co-occurrence matrix per district
-- Used by: /api/v2/epidemiology/multi-disease-matrix
-- ============================================================

CREATE MATERIALIZED VIEW summary_comorbidity AS
SELECT
  COALESCE(v.district_code, '__none__') AS district_code,
  COUNT(DISTINCT v.patient_id) AS total_screened,

  -- Single disease counts
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND NOT COALESCE(v.found_hpt, false) AND NOT COALESCE(v.found_cvd, false)) AS dm_only,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt AND NOT COALESCE(v.found_dm, false) AND NOT COALESCE(v.found_cvd, false)) AS hpt_only,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity AND NOT COALESCE(v.found_dm, false) AND NOT COALESCE(v.found_hpt, false)) AS obesity_only,

  -- Common pairs
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_hpt) AS dm_and_hpt,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_obesity) AS dm_and_obesity,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_dyslipidemia) AS dm_and_dyslipidemia,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt AND v.found_obesity) AS hpt_and_obesity,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_hpt AND v.found_dyslipidemia) AS hpt_and_dyslipidemia,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_cvd AND v.found_stroke) AS cvd_and_stroke,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_cvd) AS dm_and_cvd,

  -- Metabolic syndrome proxy (DM + HPT + dyslipidemia)
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_hpt AND v.found_dyslipidemia) AS metabolic_syndrome,

  -- Triple disease
  COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dm AND v.found_hpt AND v.found_obesity) AS dm_hpt_obesity,

  -- Multi-disease count
  COUNT(DISTINCT v.patient_id) FILTER (WHERE
    (CASE WHEN v.found_dm THEN 1 ELSE 0 END +
     CASE WHEN v.found_hpt THEN 1 ELSE 0 END +
     CASE WHEN v.found_cvd THEN 1 ELSE 0 END +
     CASE WHEN v.found_obesity THEN 1 ELSE 0 END +
     CASE WHEN v.found_dyslipidemia THEN 1 ELSE 0 END +
     CASE WHEN v.found_stroke THEN 1 ELSE 0 END) >= 2
  ) AS multi_disease_count,

  -- No disease
  COUNT(DISTINCT v.patient_id) FILTER (WHERE
    NOT COALESCE(v.found_dm, false) AND NOT COALESCE(v.found_hpt, false) AND
    NOT COALESCE(v.found_cvd, false) AND NOT COALESCE(v.found_obesity, false) AND
    NOT COALESCE(v.found_dyslipidemia, false) AND NOT COALESCE(v.found_stroke, false)
  ) AS no_disease

FROM raw_vitalsigns v
WHERE v.cancel_status IS DISTINCT FROM 1
GROUP BY COALESCE(v.district_code, '__none__');

CREATE UNIQUE INDEX idx_uq_comorbidity
  ON summary_comorbidity(district_code);

-- ============================================================
-- Update refresh function to include all views
-- ============================================================

CREATE OR REPLACE FUNCTION refresh_all_summaries() RETURNS void AS $$
BEGIN
  -- Original 5 views
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_disease;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_risk_factors;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_lab;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_mental;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_district_demographics;
  -- New 5 views (migration 006)
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_disease_age_sex;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_lab_disease_cross;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_bmi_waist;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_facility;
  REFRESH MATERIALIZED VIEW CONCURRENTLY summary_comorbidity;
END;
$$ LANGUAGE plpgsql;

COMMIT;
