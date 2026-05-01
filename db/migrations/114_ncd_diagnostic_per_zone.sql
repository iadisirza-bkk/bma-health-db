-- =============================================================================
-- Migration 114 — NCD Diagnostic Report per zone
-- =============================================================================
-- Per-zone version of mv_ncd_diagnostic_report (migration 113), so the
-- frontend hover tooltip can show 4-axis breakdown specific to the zone the
-- user is hovering. 8 zones × 11 diseases = 88 rows.
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_ncd_diagnostic_zone CASCADE;
CREATE MATERIALIZED VIEW public.mv_ncd_diagnostic_zone AS
WITH base AS (
  SELECT
    z.zone_code,
    vr.patient_id,
    bool_or(vr.risk_dm)            AS risk_dm,
    bool_or(vr.risk_hpt)           AS risk_hpt,
    bool_or(vr.risk_cvd)           AS risk_cvd,
    bool_or(vr.risk_bmi)           AS risk_bmi,
    bool_or(vr.found_dm)           AS found_dm,
    bool_or(vr.found_hpt)          AS found_hpt,
    bool_or(vr.found_cvd)          AS found_cvd,
    bool_or(vr.found_dyslipidemia) AS found_dyslipidemia,
    bool_or(vr.found_obesity)      AS found_obesity,
    bool_or(vr.found_stroke)       AS found_stroke
  FROM public.mv_visit_resolved vr
  JOIN private.geo_district g ON g.dcode = vr.home_district_code
  JOIN ref_health_zones z ON z.zone_code = g.zone_code
  WHERE vr.bucket = 'bkk' AND vr.is_dedup_kept
  GROUP BY z.zone_code, vr.patient_id
),
labs AS (
  SELECT le.patient_id,
    MAX(lm.value_number) FILTER (WHERE vd.variable_key='fbs')               AS fbs,
    MAX(lm.value_number) FILTER (WHERE vd.variable_key='total_cholesterol') AS chol,
    MAX(lm.value_number) FILTER (WHERE vd.variable_key='egfrrs')            AS egfr,
    MAX(lm.value_number) FILTER (WHERE vd.variable_key='sgot')              AS sgot,
    MAX(lm.value_number) FILTER (WHERE vd.variable_key='sgpt')              AS sgpt,
    MAX(lm.value_number) FILTER (WHERE vd.variable_key='hmgb')              AS hgb
  FROM private.lab_event le
  JOIN private.lab_measurement lm ON lm.lab_id = le.id
  JOIN private.variable_definition vd ON vd.id = lm.variable_id
  WHERE le.cancel_status = 0 AND lm.value_number IS NOT NULL
  GROUP BY le.patient_id
),
vitals AS (
  SELECT ve.patient_id,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key='sbp') AS sbp,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key='dbp') AS dbp,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key='bmi') AS bmi_direct,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key='height_cm') AS height_cm,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key='weight_kg') AS weight_kg
  FROM private.visit_event ve
  JOIN private.visit_measurement vm ON vm.visit_id = ve.id
  JOIN private.variable_definition vd ON vd.id = vm.variable_id
  WHERE ve.cancel_status = 0 AND vm.value_number IS NOT NULL
  GROUP BY ve.patient_id
),
patients AS (
  SELECT
    b.zone_code, b.patient_id,
    b.risk_dm, b.risk_hpt, b.risk_cvd, b.risk_bmi,
    b.found_dm, b.found_hpt, b.found_cvd, b.found_dyslipidemia, b.found_obesity, b.found_stroke,
    l.fbs, l.chol, l.egfr, l.sgot, l.sgpt, l.hgb,
    v.sbp, v.dbp,
    COALESCE(v.bmi_direct,
      CASE WHEN v.height_cm > 50 AND v.weight_kg > 5
           THEN v.weight_kg / ((v.height_cm/100.0)*(v.height_cm/100.0)) END
    ) AS bmi,
    p.sex_code
  FROM base b
  LEFT JOIN labs l ON l.patient_id = b.patient_id
  LEFT JOIN vitals v ON v.patient_id = b.patient_id
  LEFT JOIN private.patient p ON p.id = b.patient_id
),
zone_disease_grid AS (
  -- 11 diseases × N zones = full grid
  SELECT z.zone_code, d.*
  FROM ref_health_zones z
  CROSS JOIN (VALUES
    ('diabetes',          'โรคเบาหวาน',           'FBS ≥ 126 mg/dL'),
    ('hypertension',      'โรคความดันโลหิตสูง',    'SBP ≥ 140 OR DBP ≥ 90'),
    ('dyslipidemia',      'โรคไขมันในเลือดสูง',    'Cholesterol ≥ 200 mg/dL'),
    ('obesity',           'โรคอ้วน',              'BMI ≥ 23 kg/m²'),
    ('kidney',            'โรคไตเรื้อรัง',         'eGFR < 60 mL/min/1.73m²'),
    ('liver',             'โรคตับ',               'SGOT ≥ 120 OR SGPT ≥ 120 U/L'),
    ('anemia',            'ภาวะโลหิตจาง',          'Hemoglobin < 13/12 g/dL (M/F)'),
    ('cardiovascular',    'โรคหัวใจและหลอดเลือด',  'EKG ผิดปกติ'),
    ('stroke',            'โรคหลอดเลือดสมอง',     '— (clinical only)'),
    ('cervical_cancer',   'มะเร็งปากมดลูก',        'ผลตรวจ ผิดปกติ'),
    ('colorectal_cancer', 'มะเร็งลำไส้',          'ผลตรวจ ผิดปกติ')
  ) AS d(disease_key, disease_name_th, lab_threshold)
)
SELECT
  zd.zone_code,
  zd.disease_key,
  zd.disease_name_th,
  zd.lab_threshold,
  -- ① คนเสี่ยง
  CASE zd.disease_key
    WHEN 'diabetes'        THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.risk_dm)::bigint
    WHEN 'hypertension'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.risk_hpt)::bigint
    WHEN 'obesity'         THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.risk_bmi)::bigint
    WHEN 'cardiovascular'  THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.risk_cvd)::bigint
    ELSE NULL
  END AS at_risk,
  -- ② คนป่วย (FOUND_*)
  CASE zd.disease_key
    WHEN 'diabetes'        THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_dm)::bigint
    WHEN 'hypertension'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_hpt)::bigint
    WHEN 'dyslipidemia'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_dyslipidemia)::bigint
    WHEN 'obesity'         THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_obesity)::bigint
    WHEN 'cardiovascular'  THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_cvd)::bigint
    WHEN 'stroke'          THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_stroke)::bigint
    ELSE NULL
  END AS sick_clinical,
  -- ③ ใหม่จากตรวจ (FOUND_* AND lab not abnormal)
  CASE zd.disease_key
    WHEN 'diabetes'        THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_dm AND (p.fbs IS NULL OR p.fbs<126))::bigint
    WHEN 'hypertension'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_hpt AND NOT (p.sbp>=140 OR p.dbp>=90))::bigint
    WHEN 'dyslipidemia'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_dyslipidemia AND (p.chol IS NULL OR p.chol<200))::bigint
    WHEN 'obesity'         THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_obesity AND (p.bmi IS NULL OR p.bmi<23))::bigint
    WHEN 'cardiovascular'  THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_cvd)::bigint
    WHEN 'stroke'          THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_stroke)::bigint
    ELSE NULL
  END AS new_clinical,
  -- ④ แบ่งตามเกณฑ์ สนพ. (lab abnormal AND NOT FOUND_*)
  CASE zd.disease_key
    WHEN 'diabetes'        THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.fbs>=126 AND NOT COALESCE(p.found_dm,FALSE))::bigint
    WHEN 'hypertension'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND (p.sbp>=140 OR p.dbp>=90) AND NOT COALESCE(p.found_hpt,FALSE))::bigint
    WHEN 'dyslipidemia'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.chol>=200 AND NOT COALESCE(p.found_dyslipidemia,FALSE))::bigint
    WHEN 'obesity'         THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.bmi>=23 AND NOT COALESCE(p.found_obesity,FALSE))::bigint
    WHEN 'kidney'          THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.egfr<60 AND p.egfr>0)::bigint
    WHEN 'liver'           THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND (p.sgot>=120 OR p.sgpt>=120))::bigint
    WHEN 'anemia'          THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code
                                  AND ((p.sex_code='M' AND p.hgb<13)
                                    OR (p.sex_code='F' AND p.hgb<12)
                                    OR (p.sex_code IS NULL AND p.hgb<12)))::bigint
    ELSE NULL
  END AS by_snp_criteria
FROM zone_disease_grid zd;

CREATE UNIQUE INDEX uq_mv_ncd_diagnostic_zone
  ON public.mv_ncd_diagnostic_zone (zone_code, disease_key);
GRANT SELECT ON public.mv_ncd_diagnostic_zone TO bma_api_reader;
