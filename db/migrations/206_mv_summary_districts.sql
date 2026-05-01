-- =============================================================================
-- Migration 206 — Translate mv_summary_districts onto mv_visit_resolved
-- =============================================================================
-- Original (106): per-district KPI rollup. 50 rows (one per BKK district).
--   - 30-day dedup window for visit count
--   - Patient-level OR-aggregation across visits for risk_*/found_* counts
--   - Joined ref_districts (dcode → name_th, zone_code)
-- New: mv_visit_resolved already carries risk_*/found_* booleans + is_dedup_kept,
--   so the 30-day window logic collapses to `is_dedup_kept = TRUE`.
-- Output column shape: identical to migration 106 so /summary/districts
-- frontend doesn't change.
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_districts CASCADE;

CREATE MATERIALIZED VIEW public.mv_summary_districts AS
WITH patient_flags AS (
  SELECT
    home_district_code, patient_id,
    bool_or(risk_dm)            AS has_risk_dm,
    bool_or(risk_hpt)           AS has_risk_hpt,
    bool_or(risk_cvd)           AS has_risk_cvd,
    bool_or(risk_bmi)           AS has_risk_bmi,
    bool_or(risk_stroke)        AS has_risk_stroke,
    bool_or(found_dm)           AS has_found_dm,
    bool_or(found_hpt)          AS has_found_hpt,
    bool_or(found_cvd)          AS has_found_cvd,
    bool_or(found_obesity)      AS has_found_obesity,
    bool_or(found_dyslipidemia) AS has_found_dyslipidemia,
    bool_or(found_stroke)       AS has_found_stroke
  FROM public.mv_visit_resolved
  WHERE home_district_code IS NOT NULL
    AND cancel_status IS DISTINCT FROM 1
  GROUP BY home_district_code, patient_id
),
visit_counts AS (
  SELECT home_district_code, COUNT(*) AS total_visits
  FROM public.mv_visit_resolved
  WHERE home_district_code IS NOT NULL
    AND is_dedup_kept = TRUE
    AND cancel_status IS DISTINCT FROM 1
  GROUP BY home_district_code
)
SELECT
  d.dcode                                                    AS district_code,
  d.name_th                                                  AS district_name,
  d.zone_code,
  COUNT(DISTINCT pf.patient_id)                              AS total_screened,
  COALESCE(vc.total_visits, 0)                               AS total_visits,
  COUNT(*) FILTER (WHERE pf.has_risk_dm)                     AS risk_dm_count,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pf.has_risk_dm)
              / NULLIF(COUNT(pf.patient_id), 0), 2)          AS pct_risk_dm,
  COUNT(*) FILTER (WHERE pf.has_risk_hpt)                    AS risk_hpt_count,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pf.has_risk_hpt)
              / NULLIF(COUNT(pf.patient_id), 0), 2)          AS pct_risk_hpt,
  COUNT(*) FILTER (WHERE pf.has_risk_cvd)                    AS risk_cvd_count,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pf.has_risk_cvd)
              / NULLIF(COUNT(pf.patient_id), 0), 2)          AS pct_risk_cvd,
  COUNT(*) FILTER (WHERE pf.has_risk_bmi)                    AS risk_bmi_count,
  ROUND(100.0 * COUNT(*) FILTER (WHERE pf.has_risk_bmi)
              / NULLIF(COUNT(pf.patient_id), 0), 2)          AS pct_risk_bmi,
  COUNT(*) FILTER (WHERE pf.has_found_obesity)               AS found_obesity_count,
  COUNT(*) FILTER (WHERE pf.has_found_dyslipidemia)          AS found_dyslipidemia_count,
  COUNT(*) FILTER (WHERE pf.has_found_stroke)                AS found_stroke_count
FROM ref_districts d
LEFT JOIN patient_flags pf ON pf.home_district_code = d.dcode
LEFT JOIN visit_counts vc  ON vc.home_district_code = d.dcode
GROUP BY d.dcode, d.name_th, d.zone_code, vc.total_visits
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_summary_districts
  ON public.mv_summary_districts (district_code);

GRANT SELECT ON public.mv_summary_districts
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_summary_districts IS
  'District-level KPI rollup (50 BKK rows). Joins ref_districts to '
  'patient-level OR-aggregated risk_*/found_* flags from mv_visit_resolved. '
  'Visit counts use is_dedup_kept = TRUE (replaces the original 30-day dedup '
  'window logic).';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_summary_districts;
