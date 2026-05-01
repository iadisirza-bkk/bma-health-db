-- =============================================================================
-- Migration 209 — Translate mv_summary_mental onto mv_visit_resolved
-- =============================================================================
-- Original (110): per-district mental health %s (PHQ-9 ≥ 10 moderate,
--   ST-5 ≥ 7 high stress, depression-2Q positive). Pulled from EAV
--   visit_measurement variables phq9_q*, st5_q*, depression_2q_*.
-- New: mv_visit_resolved already carries phq9_total + st5_total per visit
--   (clean.py pre-computed). For the 2Q depression score we read scr2q1/scr2q2
--   directly from bma_med.app1_vitalsignslf + portal_vitalsignslf (smallint
--   in app1, TEXT in portal). Output column shape preserved.
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_mental CASCADE;

CREATE MATERIALIZED VIEW public.mv_summary_mental AS
WITH patient_district AS (
  SELECT DISTINCT patient_id, home_district_code
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND cancel_status IS DISTINCT FROM 1
    AND home_district_code IS NOT NULL
),
-- Per-visit PHQ-9 / ST-5 totals (already on mv_visit_resolved).
visit_scores AS (
  SELECT vr.home_district_code AS district_code,
         vr.patient_id,
         vr.phq9_total,
         vr.st5_total
  FROM public.mv_visit_resolved vr
  WHERE vr.is_dedup_kept = TRUE
    AND vr.cancel_status IS DISTINCT FROM 1
    AND vr.home_district_code IS NOT NULL
),
-- Depression-2Q from app1+portal vitalsignslf (scr2q1, scr2q2).
dep2q_app1 AS (
  SELECT v.patient_id,
         GREATEST(COALESCE(v.scr2q1, 0), COALESCE(v.scr2q2, 0))::numeric AS dep2q_max
  FROM bma_med.app1_vitalsignslf v
  WHERE COALESCE(v.dup_pid_vstdate, 0) = 0
    AND v.patient_id IS NOT NULL
    AND (v.scr2q1 IS NOT NULL OR v.scr2q2 IS NOT NULL)
),
dep2q_portal AS (
  SELECT v.patient_id,
         GREATEST(
           COALESCE(NULLIF(v.scr2q1::text, '')::numeric, 0),
           COALESCE(NULLIF(v.scr2q2::text, '')::numeric, 0)
         ) AS dep2q_max
  FROM bma_med.portal_vitalsignslf v
  WHERE COALESCE(v.dup_pid_vstdate, 0) = 0
    AND v.patient_id IS NOT NULL
    AND (v.scr2q1 IS NOT NULL OR v.scr2q2 IS NOT NULL)
),
dep2q AS (
  SELECT patient_id, MAX(dep2q_max) AS dep2q_max
  FROM (
    SELECT * FROM dep2q_app1
    UNION ALL
    SELECT * FROM dep2q_portal
  ) u
  GROUP BY patient_id
),
combined AS (
  SELECT vs.district_code, vs.patient_id,
         vs.phq9_total, vs.st5_total, d.dep2q_max
  FROM visit_scores vs
  LEFT JOIN dep2q d ON d.patient_id = vs.patient_id
)
SELECT
  district_code,
  COUNT(DISTINCT patient_id) AS total_screened,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE dep2q_max >= 1)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_depression_risk,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE phq9_total >= 10)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_phq9_moderate,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE st5_total >= 7)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_high_stress
FROM combined
GROUP BY district_code
HAVING COUNT(DISTINCT patient_id) >= 5             -- k-anonymity gate
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_summary_mental
  ON public.mv_summary_mental (district_code);

GRANT SELECT ON public.mv_summary_mental
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_summary_mental IS
  'Per-district mental health %s: PHQ-9 ≥ 10 moderate-or-worse, ST-5 ≥ 7 high '
  'stress, depression-2Q positive (scr2q1 OR scr2q2 ≥ 1). Reads pre-computed '
  'phq9_total/st5_total from mv_visit_resolved + scr2q1/scr2q2 from '
  'bma_med.*vitalsignslf. k-anonymity gate at >= 5 distinct patients.';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_summary_mental;
