-- =============================================================================
-- Drop legacy summary_* materialized views, replace with compat VIEWs
-- over private.* + public.mv_visit_resolved
-- =============================================================================
-- Goal: routers that still query summary_district_* keep working, but get
-- DYNAMIC data from the v3 schema (no separate MV storage / refresh needed).
--
-- Affected routers (kept working):
--   /api/v2/summary/lab            → summary_district_lab
--   /api/v2/summary/mental-health  → summary_district_mental
--   /api/v2/summary/demographics   → summary_district_demographics
--   /api/v2/summary/districts/{x}  → summary_district_disease + lab/mental/demo
--   /api/v2/summary/zones/{x}      → summary_district_disease + summary_facility
--   /api/v2/executive/*            → summary_district_disease
--
-- Dropped (endpoints using these will return empty until refactored):
--   summary_chronic_history, summary_family_history, summary_comorbidity,
--   summary_disease_age_sex, summary_disease_control, summary_lab_disease_cross,
--   summary_bmi_waist, summary_screening_tests, summary_district_risk_factors
-- =============================================================================

-- Step 1: drop all legacy MVs
DROP MATERIALIZED VIEW IF EXISTS
  summary_bmi_waist,
  summary_chronic_history,
  summary_comorbidity,
  summary_disease_age_sex,
  summary_disease_control,
  summary_district_demographics,
  summary_district_disease,
  summary_district_lab,
  summary_district_mental,
  summary_district_risk_factors,
  summary_facility,
  summary_family_history,
  summary_lab_disease_cross,
  summary_screening_tests
CASCADE;

-- =============================================================================
-- Compat VIEW 1: summary_district_disease  (used by /districts /zones /executive)
-- =============================================================================

CREATE OR REPLACE VIEW summary_district_disease AS
SELECT
  COALESCE(vr.source_code, '__none__'::text) AS data_source,
  d.dcode                                    AS district_code,
  d.name_th                                  AS district_name,
  d.zone_code,
  COUNT(DISTINCT vr.patient_id)              AS total_screened,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.risk_dm)            AS risk_dm_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.risk_hpt)           AS risk_hpt_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.risk_cvd)           AS risk_cvd_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.risk_bmi)           AS risk_bmi_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_dm)           AS found_dm_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_hpt)          AS found_hpt_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_cvd)          AS found_cvd_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_stroke)       AS found_stroke_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_obesity)      AS found_obesity_count,
  COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_dyslipidemia) AS found_dyslipidemia_count,
  ROUND(100.0 * COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.risk_dm)::numeric
              / NULLIF(COUNT(DISTINCT vr.patient_id), 0)::numeric, 2) AS pct_risk_dm,
  ROUND(100.0 * COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.risk_hpt)::numeric
              / NULLIF(COUNT(DISTINCT vr.patient_id), 0)::numeric, 2) AS pct_risk_hpt,
  ROUND(100.0 * COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.risk_cvd)::numeric
              / NULLIF(COUNT(DISTINCT vr.patient_id), 0)::numeric, 2) AS pct_risk_cvd,
  ROUND(100.0 * COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_dm)::numeric
              / NULLIF(COUNT(DISTINCT vr.patient_id), 0)::numeric, 2) AS pct_found_dm,
  ROUND(100.0 * COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_hpt)::numeric
              / NULLIF(COUNT(DISTINCT vr.patient_id), 0)::numeric, 2) AS pct_found_hpt,
  ROUND(100.0 * COUNT(DISTINCT vr.patient_id) FILTER (WHERE vr.found_cvd)::numeric
              / NULLIF(COUNT(DISTINCT vr.patient_id), 0)::numeric, 2) AS pct_found_cvd,
  NOW() AS refreshed_at
FROM ref_districts d
LEFT JOIN public.mv_visit_resolved vr
  ON vr.home_district_code = d.dcode
GROUP BY d.dcode, d.name_th, d.zone_code, vr.source_code;

GRANT SELECT ON summary_district_disease TO bma_api_reader;
GRANT SELECT ON summary_district_disease TO bma_etl_writer;

-- =============================================================================
-- Compat VIEW 2: summary_district_lab — lab averages per district
-- =============================================================================

CREATE OR REPLACE VIEW summary_district_lab AS
WITH lab_by_district AS (
  SELECT
    pa.district_code,
    le.patient_id,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'hemoglobin')   AS hemoglobin,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'hematocrit')   AS hematocrit,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'fbs')          AS fbs,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'total_cholesterol') AS cholesterol,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'triglyceride') AS triglyceride,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'hdl')          AS hdl,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'ldl')          AS ldl,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'creatinine')   AS creatinine,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'egfr')         AS egfr,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'uric_acid')    AS uric_acid,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'sgot')         AS sgot,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key = 'sgpt')         AS sgpt
  FROM private.lab_event le
  JOIN private.lab_measurement lm ON lm.lab_id = le.id
  JOIN private.variable_definition vd ON vd.id = lm.variable_id
  LEFT JOIN private.patient_address pa ON pa.patient_id = le.patient_id
    AND pa.address_type = 'home' AND pa.effective_to IS NULL
  WHERE le.cancel_status = 0 AND lm.value_number IS NOT NULL
  GROUP BY pa.district_code, le.patient_id
)
SELECT
  district_code,
  COUNT(DISTINCT patient_id)::bigint AS total_lab_patients,
  ROUND(AVG(hemoglobin)::numeric,    2) AS avg_hemoglobin,
  ROUND(AVG(hematocrit)::numeric,    2) AS avg_hematocrit,
  ROUND(AVG(fbs)::numeric,           2) AS avg_fbs,
  ROUND(AVG(cholesterol)::numeric,   2) AS avg_cholesterol,
  ROUND(AVG(triglyceride)::numeric,  2) AS avg_triglyceride,
  ROUND(AVG(hdl)::numeric,           2) AS avg_hdl,
  ROUND(AVG(ldl)::numeric,           2) AS avg_ldl,
  ROUND(AVG(creatinine)::numeric,    2) AS avg_creatinine,
  ROUND(AVG(egfr)::numeric,          2) AS avg_egfr,
  ROUND(AVG(uric_acid)::numeric,     2) AS avg_uric_acid,
  ROUND(AVG(sgot)::numeric,          2) AS avg_sgot,
  ROUND(AVG(sgpt)::numeric,          2) AS avg_sgpt,
  -- Rates
  ROUND(100.0 * COUNT(*) FILTER (WHERE hemoglobin < 12)
              / NULLIF(COUNT(*) FILTER (WHERE hemoglobin IS NOT NULL), 0), 2) AS pct_anemia,
  ROUND(100.0 * COUNT(*) FILTER (WHERE egfr < 60)
              / NULLIF(COUNT(*) FILTER (WHERE egfr IS NOT NULL), 0), 2)        AS pct_ckd
FROM lab_by_district
WHERE district_code IS NOT NULL
GROUP BY district_code;

GRANT SELECT ON summary_district_lab TO bma_api_reader;

-- =============================================================================
-- Compat VIEW 3: summary_district_mental — PHQ-9, ST5, depression
-- =============================================================================

CREATE OR REPLACE VIEW summary_district_mental AS
WITH per_visit AS (
  SELECT
    vr.home_district_code AS district_code,
    vr.patient_id,
    vr.visit_id,
    SUM(vm.value_number) FILTER (WHERE vd.variable_key SIMILAR TO 'phq9_q[1-9]') AS phq9_total,
    SUM(vm.value_number) FILTER (WHERE vd.variable_key SIMILAR TO 'st5_q[1-5]')  AS st5_total,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key = 'depression_2q_1')      AS dep1,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key = 'depression_2q_2')      AS dep2
  FROM public.mv_visit_resolved vr
  JOIN private.visit_measurement vm ON vm.visit_id = vr.visit_id
  JOIN private.variable_definition vd ON vd.id = vm.variable_id
  WHERE vd.variable_key SIMILAR TO 'phq9_q[1-9]|st5_q[1-5]|depression_2q_[12]'
  GROUP BY vr.home_district_code, vr.patient_id, vr.visit_id
)
SELECT
  district_code,
  COUNT(DISTINCT patient_id)::bigint AS total_screened,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE dep1 >= 1 OR dep2 >= 1)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_depression_risk,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE phq9_total >= 10)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_phq9_moderate,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE st5_total >= 7)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_high_stress
FROM per_visit
WHERE district_code IS NOT NULL
GROUP BY district_code;

GRANT SELECT ON summary_district_mental TO bma_api_reader;

-- =============================================================================
-- Compat VIEW 4: summary_district_demographics — edu/occ/privilege/housing
-- =============================================================================

CREATE OR REPLACE VIEW summary_district_demographics AS
SELECT
  vr.home_district_code AS district_code,
  COUNT(DISTINCT vr.patient_id)::bigint AS total_respondents,
  -- Education levels (variable_key = 'education')
  COUNT(DISTINCT vr.patient_id) FILTER (
    WHERE EXISTS (SELECT 1 FROM private.visit_measurement vm
                  JOIN private.variable_definition vd ON vd.id = vm.variable_id
                  WHERE vm.visit_id = vr.visit_id
                    AND vd.variable_key = 'education'
                    AND vm.value_text = '0')
  )::bigint AS edu_none,
  COUNT(DISTINCT vr.patient_id) FILTER (
    WHERE EXISTS (SELECT 1 FROM private.visit_measurement vm
                  JOIN private.variable_definition vd ON vd.id = vm.variable_id
                  WHERE vm.visit_id = vr.visit_id
                    AND vd.variable_key = 'education'
                    AND vm.value_text IN ('1','2'))
  )::bigint AS edu_primary,
  COUNT(DISTINCT vr.patient_id) FILTER (
    WHERE EXISTS (SELECT 1 FROM private.visit_measurement vm
                  JOIN private.variable_definition vd ON vd.id = vm.variable_id
                  WHERE vm.visit_id = vr.visit_id
                    AND vd.variable_key = 'education'
                    AND vm.value_text = '3')
  )::bigint AS edu_secondary,
  -- Placeholders for the rest (return 0 — full implementation later)
  0::bigint AS edu_high_school,
  0::bigint AS edu_vocational,
  0::bigint AS edu_bachelor,
  0::bigint AS edu_postgrad,
  0::bigint AS occ_government,
  0::bigint AS occ_private,
  0::bigint AS occ_self_employed,
  0::bigint AS occ_agriculture,
  0::bigint AS occ_unemployed,
  0::bigint AS occ_student,
  0::bigint AS occ_retired,
  0::bigint AS priv_ucs,
  0::bigint AS priv_sso,
  0::bigint AS priv_csmbs,
  0::bigint AS priv_other,
  0::bigint AS house_owned,
  0::bigint AS house_rented,
  0::bigint AS house_condo,
  0::bigint AS house_other
FROM public.mv_visit_resolved vr
WHERE vr.home_district_code IS NOT NULL
GROUP BY vr.home_district_code;

GRANT SELECT ON summary_district_demographics TO bma_api_reader;

-- =============================================================================
-- Compat VIEW 5: summary_facility — per-facility totals
-- =============================================================================

CREATE OR REPLACE VIEW summary_facility AS
SELECT
  ve.facility_code,
  ve.facility_code AS district_code,   -- legacy column kept (admin/zones queries)
  COUNT(DISTINCT ve.patient_id)::bigint AS total_screened,
  COUNT(DISTINCT ve.patient_id) FILTER (
    WHERE EXISTS (SELECT 1 FROM public.mv_visit_resolved vr
                  WHERE vr.visit_id = ve.id AND vr.risk_dm)
  )::bigint AS risk_dm,
  COUNT(DISTINCT ve.patient_id) FILTER (
    WHERE EXISTS (SELECT 1 FROM public.mv_visit_resolved vr
                  WHERE vr.visit_id = ve.id AND vr.risk_hpt)
  )::bigint AS risk_hpt,
  COUNT(DISTINCT ve.patient_id) FILTER (
    WHERE EXISTS (SELECT 1 FROM public.mv_visit_resolved vr
                  WHERE vr.visit_id = ve.id AND vr.found_obesity)
  )::bigint AS found_obesity,
  0::bigint AS lab_completed,
  MIN(ve.visit_date)::date AS first_screening,
  MAX(ve.visit_date)::date AS last_screening
FROM private.visit_event ve
WHERE ve.cancel_status = 0 AND ve.facility_code IS NOT NULL
GROUP BY ve.facility_code;

GRANT SELECT ON summary_facility TO bma_api_reader;

-- =============================================================================
-- Compat VIEW 6: summary_district_risk_factors — sex × age × smoke × exercise
-- (Empty stub — endpoints querying this return rows but with 0 counts)
-- =============================================================================

CREATE OR REPLACE VIEW summary_district_risk_factors AS
SELECT
  d.dcode AS district_code,
  NULL::int AS sex,
  NULL::text AS age_group,
  NULL::int AS smoking,
  NULL::int AS exercise,
  0::bigint AS patient_count,
  NULL::numeric AS avg_sbp,
  NULL::numeric AS avg_dbp,
  NULL::numeric AS avg_weight_kg,
  NULL::numeric AS avg_waist_cm,
  NULL::numeric AS avg_bmi
FROM ref_districts d
WHERE FALSE;  -- empty stub for now — requires more complex pivot

GRANT SELECT ON summary_district_risk_factors TO bma_api_reader;
