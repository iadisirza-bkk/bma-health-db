-- =============================================================================
-- Migration 207 — Translate mv_summary_zones (rollup of mv_summary_districts)
-- =============================================================================
-- Original (106): 8 health-zone rollups built by SUMming mv_summary_districts.
-- New schema is structurally the same — just rebuild on top of the new
-- mv_summary_districts. Output column shape preserved.
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_zones CASCADE;

CREATE MATERIALIZED VIEW public.mv_summary_zones AS
SELECT
  z.zone_code, z.name_th, z.name_en,
  COUNT(DISTINCT d.dcode)                       AS district_count,
  COALESCE(SUM(s.total_screened), 0)            AS total_screened,
  COALESCE(SUM(s.total_visits), 0)              AS total_visits,
  COALESCE(SUM(s.risk_dm_count), 0)             AS diabetes,
  COALESCE(SUM(s.risk_hpt_count), 0)            AS hypertension,
  COALESCE(SUM(s.risk_cvd_count), 0)            AS cardiovascular,
  COALESCE(SUM(s.risk_bmi_count), 0)            AS obesity,
  COALESCE(SUM(s.found_dyslipidemia_count), 0)  AS dyslipidemia,
  COALESCE(SUM(s.found_stroke_count), 0)        AS stroke
FROM ref_health_zones z
LEFT JOIN ref_districts d            ON d.zone_code = z.zone_code
LEFT JOIN public.mv_summary_districts s ON s.district_code = d.dcode
GROUP BY z.zone_code, z.name_th, z.name_en
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_summary_zones
  ON public.mv_summary_zones (zone_code);

GRANT SELECT ON public.mv_summary_zones
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_summary_zones IS
  'Health-zone rollup of mv_summary_districts (8 rows). Pure SUM over the '
  'district MV — refresh AFTER mv_summary_districts.';

-- Refresh after data load (run AFTER mv_summary_districts):
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_summary_zones;
