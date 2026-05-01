-- =============================================================================
-- Migration 208 — Translate mv_summary_global onto mv_visit_resolved
-- =============================================================================
-- Original (106): citywide 1-row rollup with bkk/non_bkk/unknown bucket
--   breakdown. 30-day dedup window for visit counts.
-- New: mv_visit_resolved doesn't carry the bucket column, but it can be
--   re-derived inline from home_district_code (1001..1050 → 'bkk').
--   is_dedup_kept replaces the 30-day window logic. Output column shape
--   identical (refreshed_at unique-index column included).
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_global CASCADE;

CREATE MATERIALIZED VIEW public.mv_summary_global AS
WITH bucketed AS (
  SELECT
    patient_id,
    is_dedup_kept,
    risk_dm, risk_hpt, risk_cvd, risk_bmi,
    found_dyslipidemia, found_stroke,
    CASE
      WHEN home_district_code BETWEEN '1001' AND '1050' THEN 'bkk'
      WHEN home_district_code IS NULL                   THEN 'unknown'
      ELSE                                                   'non_bkk'
    END AS bucket
  FROM public.mv_visit_resolved
  WHERE cancel_status IS DISTINCT FROM 1
),
flags AS (
  SELECT patient_id, bucket,
    bool_or(risk_dm)            AS has_risk_dm,
    bool_or(risk_hpt)           AS has_risk_hpt,
    bool_or(risk_cvd)           AS has_risk_cvd,
    bool_or(risk_bmi)           AS has_risk_bmi,
    bool_or(found_dyslipidemia) AS has_found_dyslipidemia,
    bool_or(found_stroke)       AS has_found_stroke
  FROM bucketed
  GROUP BY patient_id, bucket
),
visits AS (
  SELECT bucket, COUNT(*) AS n
  FROM bucketed
  WHERE is_dedup_kept = TRUE
  GROUP BY bucket
)
SELECT
  COUNT(DISTINCT f.patient_id)                                         AS total_screened,
  (SELECT COALESCE(SUM(n), 0) FROM visits)                             AS total_visits,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.bucket='bkk')           AS bkk_screened,
  (SELECT COALESCE(n, 0) FROM visits WHERE bucket='bkk')               AS bkk_visits,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.bucket='non_bkk')       AS non_bkk_screened,
  (SELECT COALESCE(n, 0) FROM visits WHERE bucket='non_bkk')           AS non_bkk_visits,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.bucket='unknown')       AS unknown_screened,
  (SELECT COALESCE(n, 0) FROM visits WHERE bucket='unknown')           AS unknown_visits,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_risk_dm)            AS diabetes,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_risk_hpt)           AS hypertension,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_risk_cvd)           AS cardiovascular,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_risk_bmi)           AS obesity,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_found_dyslipidemia) AS dyslipidemia,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_found_stroke)       AS stroke,
  NOW()                                                                AS refreshed_at
FROM flags f
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_summary_global
  ON public.mv_summary_global (refreshed_at);

GRANT SELECT ON public.mv_summary_global
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_summary_global IS
  'Citywide 1-row KPI with bkk/non_bkk/unknown bucket breakdown. Bucket '
  're-derived inline from home_district_code (1001..1050 → bkk). Visit counts '
  'use is_dedup_kept (replaces original 30-day dedup window).';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_summary_global;
