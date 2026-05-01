-- =============================================================================
-- Migration 210 — NCD Diagnostic Report MV (citywide, 11 rows)
-- =============================================================================
-- Original (113): 4-axis breakdown per disease — at_risk, sick_clinical,
--   new_clinical (clinical found, lab missed), new_from_lab (lab caught,
--   self-report missed). Pulled from EAV visit/lab measurements + computed
--   thresholds (FBS≥126, SBP≥140 OR DBP≥90, Cholesterol≥200, BMI≥23,
--   eGFR<60, SGOT/SGPT≥120, Hb<13/12, EKG/CXR/cervical/colon).
-- New: clean.py already encoded MSD-Thailand criteria as msd_* SMALLINT
--   columns on bma_med.*vitalsignslf and *labhealth (1=positive, 0=negative,
--   NULL=unknown). Just SUM/COUNT them. Threshold logic preserved as
--   fallback for new_clinical via the typed numeric columns.
-- Output: identical 8-column shape (disease_key, name_th, lab_threshold,
--   at_risk, sick_clinical, new_clinical, new_from_lab, has_lab_threshold).
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_ncd_diagnostic_report CASCADE;

CREATE MATERIALIZED VIEW public.mv_ncd_diagnostic_report AS
WITH base AS (
  -- Per-patient screening flags (any visit triggers TRUE) — BKK only,
  -- matching original 113 filter on bucket = 'bkk'.
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
    bool_or(vr.found_stroke)       AS found_stroke,
    -- Latest vitals for fallback threshold checks
    MAX(vr.sbp) AS sbp,
    MAX(vr.dbp) AS dbp,
    MAX(vr.bmi) AS bmi
  FROM public.mv_visit_resolved vr
  WHERE vr.is_dedup_kept = TRUE
    AND vr.cancel_status IS DISTINCT FROM 1
    AND vr.home_district_code BETWEEN '1001' AND '1050'   -- bkk only
  GROUP BY vr.patient_id
),
-- Lab-MSD flags + raw values from app1+portal labhealth.
lab_app1 AS (
  SELECT l.patient_id,
         (l.msd_dm        = 1)::int AS m_dm,
         (l.msd_hyperchol = 1)::int AS m_hyperchol,
         (l.msd_kidney    = 1)::int AS m_kidney,
         (l.msd_liver     = 1)::int AS m_liver,
         (l.msd_anemia    = 1)::int AS m_anemia,
         (l.msd_cervical  = 1)::int AS m_cervical,
         (l.msd_colon     = 1)::int AS m_colon,
         l.fbs::numeric                 AS fbs,
         l.cholest::numeric             AS chol
  FROM bma_med.app1_labhealth l
  WHERE COALESCE(l.dup_pid_vstdate, 0) = 0
    AND l.patient_id IS NOT NULL
),
lab_portal AS (
  SELECT l.patient_id,
         (l.msd_dm        = 1)::int AS m_dm,
         (l.msd_hyperchol = 1)::int AS m_hyperchol,
         (l.msd_kidney    = 1)::int AS m_kidney,
         (l.msd_liver     = 1)::int AS m_liver,
         (l.msd_anemia    = 1)::int AS m_anemia,
         (l.msd_cervical  = 1)::int AS m_cervical,
         (l.msd_colon     = 1)::int AS m_colon,
         NULLIF(l.fbs,     '')::numeric AS fbs,
         NULLIF(l.cholest, '')::numeric AS chol
  FROM bma_med.portal_labhealth l
  WHERE COALESCE(l.dup_pid_vstdate, 0) = 0
    AND l.patient_id IS NOT NULL
),
labs AS (
  SELECT patient_id,
         MAX(m_dm)        AS m_dm,
         MAX(m_hyperchol) AS m_hyperchol,
         MAX(m_kidney)    AS m_kidney,
         MAX(m_liver)     AS m_liver,
         MAX(m_anemia)    AS m_anemia,
         MAX(m_cervical)  AS m_cervical,
         MAX(m_colon)     AS m_colon,
         MAX(fbs)         AS fbs,
         MAX(chol)        AS chol
  FROM (
    SELECT * FROM lab_app1
    UNION ALL
    SELECT * FROM lab_portal
  ) u
  GROUP BY patient_id
),
patients AS (
  SELECT
    b.patient_id,
    b.risk_dm, b.risk_hpt, b.risk_cvd, b.risk_bmi,
    b.found_dm, b.found_hpt, b.found_cvd, b.found_dyslipidemia,
    b.found_obesity, b.found_stroke,
    b.sbp, b.dbp, b.bmi,
    l.m_dm, l.m_hyperchol, l.m_kidney, l.m_liver, l.m_anemia,
    l.m_cervical, l.m_colon, l.fbs, l.chol
  FROM base b
  LEFT JOIN labs l ON l.patient_id = b.patient_id
)
SELECT * FROM (VALUES
  -- (disease_key, name_th, lab_threshold,
  --  at_risk, sick_clinical, new_clinical, new_from_lab, has_lab_threshold)
  (
    'diabetes', 'โรคเบาหวาน', 'FBS ≥ 126 mg/dL',
    (SELECT COUNT(*) FROM patients WHERE risk_dm)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_dm)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_dm AND COALESCE(m_dm, 0) = 0)::bigint,
    (SELECT COUNT(*) FROM patients WHERE m_dm = 1 AND NOT COALESCE(found_dm, FALSE))::bigint,
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
    NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_dyslipidemia)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_dyslipidemia AND COALESCE(m_hyperchol, 0) = 0)::bigint,
    (SELECT COUNT(*) FROM patients WHERE m_hyperchol = 1 AND NOT COALESCE(found_dyslipidemia, FALSE))::bigint,
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
    NULL::bigint, NULL::bigint, NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE m_kidney = 1)::bigint,
    TRUE
  ),
  (
    'liver', 'โรคตับ', 'SGOT ≥ 120 OR SGPT ≥ 120 U/L',
    NULL::bigint, NULL::bigint, NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE m_liver = 1)::bigint,
    TRUE
  ),
  (
    'anemia', 'ภาวะโลหิตจาง', 'Hemoglobin < 13 g/dL (ชาย) / < 12 g/dL (หญิง)',
    NULL::bigint, NULL::bigint, NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE m_anemia = 1)::bigint,
    TRUE
  ),
  (
    'cardiovascular', 'โรคหัวใจและหลอดเลือด', 'EKG ผิดปกติ (clinical exam, not numeric)',
    (SELECT COUNT(*) FROM patients WHERE risk_cvd)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_cvd)::bigint,
    (SELECT COUNT(*) FROM patients WHERE found_cvd)::bigint,
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
    'cervical_cancer', 'มะเร็งปากมดลูก', 'ผลตรวจมะเร็งปากมดลูก ผิดปกติ',
    NULL::bigint, NULL::bigint, NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE m_cervical = 1)::bigint,
    TRUE
  ),
  (
    'colorectal_cancer', 'มะเร็งลำไส้', 'ผลตรวจมะเร็งลำไส้ ผิดปกติ',
    NULL::bigint, NULL::bigint, NULL::bigint,
    (SELECT COUNT(*) FROM patients WHERE m_colon = 1)::bigint,
    TRUE
  )
) AS t(
  disease_key, disease_name_th, lab_threshold,
  at_risk, sick_clinical, new_clinical, new_from_lab, has_lab_threshold
)
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_ncd_diagnostic_report
  ON public.mv_ncd_diagnostic_report (disease_key);

GRANT SELECT ON public.mv_ncd_diagnostic_report
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_ncd_diagnostic_report IS
  'NCD diagnostic 4-axis breakdown (11 rows). Reads MSD-Thailand criteria '
  'pre-computed by clean.py as msd_* SMALLINT flags on bma_med.*labhealth + '
  '*vitalsignslf. BKK only (district 1001..1050).';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_ncd_diagnostic_report;
