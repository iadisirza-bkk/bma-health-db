-- =============================================================================
-- Migration 211 — NCD Diagnostic Report per zone (8 zones × 11 diseases = 88)
-- =============================================================================
-- Original (114): per-zone version of mv_ncd_diagnostic_report, joined to
--   private.geo_district + ref_health_zones for the zone_code lookup.
-- New: read from mv_visit_resolved + bma_med.*labhealth + ref_districts +
--   ref_health_zones. msd_* columns on labhealth provide the lab-criterion
--   counts directly (no threshold computation needed).
-- Output column shape preserved: (zone_code, disease_key, disease_name_th,
--   lab_threshold, at_risk, sick_clinical, new_clinical, by_snp_criteria).
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_ncd_diagnostic_zone CASCADE;

CREATE MATERIALIZED VIEW public.mv_ncd_diagnostic_zone AS
WITH base AS (
  SELECT
    rd.zone_code,
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
    MAX(vr.sbp) AS sbp,
    MAX(vr.dbp) AS dbp,
    MAX(vr.bmi) AS bmi
  FROM public.mv_visit_resolved vr
  JOIN ref_districts rd ON rd.dcode = vr.home_district_code
  WHERE vr.is_dedup_kept = TRUE
    AND vr.cancel_status IS DISTINCT FROM 1
    AND vr.home_district_code BETWEEN '1001' AND '1050'
  GROUP BY rd.zone_code, vr.patient_id
),
lab_app1 AS (
  SELECT l.patient_id,
         (l.msd_dm        = 1)::int AS m_dm,
         (l.msd_hyperchol = 1)::int AS m_hyperchol,
         (l.msd_kidney    = 1)::int AS m_kidney,
         (l.msd_liver     = 1)::int AS m_liver,
         (l.msd_anemia    = 1)::int AS m_anemia,
         (l.msd_cervical  = 1)::int AS m_cervical,
         (l.msd_colon     = 1)::int AS m_colon
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
         (l.msd_colon     = 1)::int AS m_colon
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
         MAX(m_colon)     AS m_colon
  FROM (
    SELECT * FROM lab_app1
    UNION ALL
    SELECT * FROM lab_portal
  ) u
  GROUP BY patient_id
),
patients AS (
  SELECT
    b.zone_code, b.patient_id,
    b.risk_dm, b.risk_hpt, b.risk_cvd, b.risk_bmi,
    b.found_dm, b.found_hpt, b.found_cvd, b.found_dyslipidemia,
    b.found_obesity, b.found_stroke,
    b.sbp, b.dbp, b.bmi,
    l.m_dm, l.m_hyperchol, l.m_kidney, l.m_liver, l.m_anemia,
    l.m_cervical, l.m_colon
  FROM base b
  LEFT JOIN labs l ON l.patient_id = b.patient_id
),
zone_disease_grid AS (
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
  -- (1) คนเสี่ยง
  CASE zd.disease_key
    WHEN 'diabetes'        THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.risk_dm)::bigint
    WHEN 'hypertension'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.risk_hpt)::bigint
    WHEN 'obesity'         THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.risk_bmi)::bigint
    WHEN 'cardiovascular'  THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.risk_cvd)::bigint
    ELSE NULL
  END AS at_risk,
  -- (2) คนป่วย (FOUND_*)
  CASE zd.disease_key
    WHEN 'diabetes'        THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_dm)::bigint
    WHEN 'hypertension'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_hpt)::bigint
    WHEN 'dyslipidemia'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_dyslipidemia)::bigint
    WHEN 'obesity'         THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_obesity)::bigint
    WHEN 'cardiovascular'  THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_cvd)::bigint
    WHEN 'stroke'          THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_stroke)::bigint
    ELSE NULL
  END AS sick_clinical,
  -- (3) ใหม่จากตรวจ (FOUND_* AND lab/threshold not abnormal)
  CASE zd.disease_key
    WHEN 'diabetes'        THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_dm AND COALESCE(p.m_dm, 0) = 0)::bigint
    WHEN 'hypertension'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_hpt AND NOT (p.sbp>=140 OR p.dbp>=90))::bigint
    WHEN 'dyslipidemia'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_dyslipidemia AND COALESCE(p.m_hyperchol, 0) = 0)::bigint
    WHEN 'obesity'         THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_obesity AND (p.bmi IS NULL OR p.bmi<23))::bigint
    WHEN 'cardiovascular'  THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_cvd)::bigint
    WHEN 'stroke'          THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.found_stroke)::bigint
    ELSE NULL
  END AS new_clinical,
  -- (4) แบ่งตามเกณฑ์ สนพ. (msd_* lab criterion AND NOT FOUND_*)
  CASE zd.disease_key
    WHEN 'diabetes'        THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.m_dm = 1 AND NOT COALESCE(p.found_dm, FALSE))::bigint
    WHEN 'hypertension'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND (p.sbp>=140 OR p.dbp>=90) AND NOT COALESCE(p.found_hpt, FALSE))::bigint
    WHEN 'dyslipidemia'    THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.m_hyperchol = 1 AND NOT COALESCE(p.found_dyslipidemia, FALSE))::bigint
    WHEN 'obesity'         THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.bmi>=23 AND NOT COALESCE(p.found_obesity, FALSE))::bigint
    WHEN 'kidney'          THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.m_kidney = 1)::bigint
    WHEN 'liver'           THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.m_liver = 1)::bigint
    WHEN 'anemia'          THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.m_anemia = 1)::bigint
    WHEN 'cervical_cancer' THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.m_cervical = 1)::bigint
    WHEN 'colorectal_cancer' THEN (SELECT COUNT(*) FROM patients p WHERE p.zone_code=zd.zone_code AND p.m_colon = 1)::bigint
    ELSE NULL
  END AS by_snp_criteria
FROM zone_disease_grid zd
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_ncd_diagnostic_zone
  ON public.mv_ncd_diagnostic_zone (zone_code, disease_key);

GRANT SELECT ON public.mv_ncd_diagnostic_zone
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_ncd_diagnostic_zone IS
  'NCD diagnostic 4-axis breakdown per health zone (8 × 11 = 88 rows). '
  'Reads msd_* SMALLINT criteria from bma_med.*labhealth + risk_*/found_* '
  'from mv_visit_resolved. Joined via ref_districts.zone_code.';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_ncd_diagnostic_zone;
