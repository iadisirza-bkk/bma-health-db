-- =============================================================================
-- Migration 113 — NCD Diagnostic Report MV
-- =============================================================================
-- For doctor-facing report: per-disease 4-axis breakdown
--   1. คนเสี่ยง (at_risk)            — risk_* flag from screening criteria
--   2. คนป่วย (sick_clinical)        — found_* flag (clinical/self-report)
--   3. คนใหม่จากการตรวจ              — found_* AND lab not abnormal
--                                       (clinically detected, lab didn't catch)
--   4. คนใหม่จากผลแลป (new_from_lab) — lab criterion met AND NOT found_*
--                                       (lab caught what self-report missed)
--
-- Lab thresholds (ราชวิทยาลัย/MOPH):
--   DM            FBS ≥ 126 mg/dL
--   HPT           SBP ≥ 140 OR DBP ≥ 90 mmHg
--   Dyslipidemia  Cholesterol ≥ 200 mg/dL
--   Obesity       BMI ≥ 23 kg/m²
--   CKD           eGFR < 60 mL/min/1.73m²
--   Liver         SGOT ≥ 120 OR SGPT ≥ 120 U/L
--   Anemia        Hemoglobin < 13 g/dL (M) / < 12 g/dL (F)
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_ncd_diagnostic_report CASCADE;
CREATE MATERIALIZED VIEW public.mv_ncd_diagnostic_report AS
WITH base AS (
  -- Per-patient screening flags (any visit triggers TRUE)
  SELECT
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
  WHERE vr.bucket = 'bkk' AND vr.is_dedup_kept
  GROUP BY vr.patient_id
),
labs AS (
  -- Latest (max numeric) lab value per patient
  SELECT
    le.patient_id,
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
  -- Latest vital + computed BMI per patient
  SELECT
    ve.patient_id,
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
    b.patient_id,
    b.risk_dm, b.risk_hpt, b.risk_cvd, b.risk_bmi,
    b.found_dm, b.found_hpt, b.found_cvd, b.found_dyslipidemia, b.found_obesity, b.found_stroke,
    l.fbs, l.chol, l.egfr, l.sgot, l.sgpt, l.hgb,
    v.sbp, v.dbp,
    -- Use direct BMI if present, else compute from height+weight
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
totals AS (SELECT COUNT(*) AS total_screened FROM patients)
SELECT * FROM (VALUES
  -- (disease_key, name_th, lab_threshold_label, at_risk, sick_clinical, new_clinical, new_from_lab, has_lab_data)
  (
    'diabetes', 'โรคเบาหวาน', 'FBS ≥ 126 mg/dL',
    (SELECT COUNT(*) FROM patients WHERE risk_dm)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_dm)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_dm AND (fbs IS NULL OR fbs < 126))::bigint,
    (SELECT COUNT(*) FROM patients WHERE fbs >= 126 AND NOT COALESCE(found_dm, FALSE))::bigint,
    TRUE
  ),
  (
    'hypertension', 'โรคความดันโลหิตสูง', 'SBP ≥ 140 OR DBP ≥ 90',
    (SELECT COUNT(*) FROM patients WHERE risk_hpt)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_hpt)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_hpt AND NOT (sbp >= 140 OR dbp >= 90))::bigint,
    (SELECT COUNT(*) FROM patients WHERE (sbp >= 140 OR dbp >= 90) AND NOT COALESCE(found_hpt, FALSE))::bigint,
    TRUE
  ),
  (
    'dyslipidemia', 'โรคไขมันในเลือดสูง', 'Cholesterol ≥ 200 mg/dL',
    NULL::bigint,  -- no risk_dyslipidemia flag
    (SELECT COUNT(*) FROM patients WHERE found_dyslipidemia)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_dyslipidemia AND (chol IS NULL OR chol < 200))::bigint,
    (SELECT COUNT(*) FROM patients WHERE chol >= 200 AND NOT COALESCE(found_dyslipidemia, FALSE))::bigint,
    TRUE
  ),
  (
    'obesity', 'โรคอ้วน', 'BMI ≥ 23 kg/m²',
    (SELECT COUNT(*) FROM patients WHERE risk_bmi)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_obesity)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_obesity AND (bmi IS NULL OR bmi < 23))::bigint,
    (SELECT COUNT(*) FROM patients WHERE bmi >= 23 AND NOT COALESCE(found_obesity, FALSE))::bigint,
    TRUE
  ),
  (
    'kidney', 'โรคไตเรื้อรัง', 'eGFR < 60 mL/min/1.73m²',
    NULL::bigint,
    NULL::bigint,
    NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE egfr < 60 AND egfr > 0)::bigint,
    TRUE
  ),
  (
    'liver', 'โรคตับ', 'SGOT ≥ 120 OR SGPT ≥ 120 U/L',
    NULL::bigint,
    NULL::bigint,
    NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE sgot >= 120 OR sgpt >= 120)::bigint,
    TRUE
  ),
  (
    'anemia', 'ภาวะโลหิตจาง', 'Hemoglobin < 13 g/dL (ชาย) / < 12 g/dL (หญิง)',
    NULL::bigint,
    NULL::bigint,
    NULL::bigint,
    (SELECT COUNT(*) FROM patients
       WHERE (sex_code = 'M' AND hgb < 13)
          OR (sex_code = 'F' AND hgb < 12)
          OR (sex_code IS NULL AND hgb < 12)  -- conservative when sex unknown
    )::bigint,
    TRUE
  ),
  (
    'cardiovascular', 'โรคหัวใจและหลอดเลือด', 'EKG ผิดปกติ (clinical exam, not numeric)',
    (SELECT COUNT(*) FROM patients WHERE risk_cvd)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_cvd)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_cvd)::bigint,  -- no separate lab; same as sick
    NULL::bigint,
    FALSE
  ),
  (
    'stroke', 'โรคหลอดเลือดสมอง', '— (clinical only)',
    NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_stroke)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_stroke)::bigint,
    NULL::bigint,
    FALSE
  ),
  (
    'cervical_cancer', 'มะเร็งปากมดลูก', 'ผลตรวจมะเร็งปากมดลูก ผิดปกติ (text)',
    NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint, FALSE
  ),
  (
    'colorectal_cancer', 'มะเร็งลำไส้', 'ผลตรวจมะเร็งลำไส้ ผิดปกติ (text)',
    NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint, FALSE
  )
) AS t(
  disease_key, disease_name_th, lab_threshold,
  at_risk, sick_clinical, new_clinical, new_from_lab, has_lab_threshold
);

-- Add the total_screened denominator as a separate row for context
-- (or we can include it in every row; both work for the report)
ALTER MATERIALIZED VIEW public.mv_ncd_diagnostic_report
  OWNER TO bma_dba_admin;

-- Add a unique index so REFRESH MATERIALIZED VIEW CONCURRENTLY works
CREATE UNIQUE INDEX uq_mv_ncd_diagnostic_report
  ON public.mv_ncd_diagnostic_report (disease_key);

GRANT SELECT ON public.mv_ncd_diagnostic_report TO bma_api_reader;
