-- =============================================================================
-- Migration 201 — Translate mv_kpi_tier1 onto bma_med.* via mv_visit_resolved
-- =============================================================================
-- Original (101): per (district, source, bucket) persons + visit count from
--   private.visit_event + private.patient_address. The bucket was derived
--   from home_district_code BETWEEN '1001' AND '1050'.
-- New: read mv_visit_resolved (created by migration 200) which already
--   carries home_district_code, source_code, is_dedup_kept. Bucket is
--   re-derived inline. Output column shape preserved so the existing
--   /summary/overview / /summary/zones routers don't change.
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_kpi_tier1 CASCADE;

CREATE MATERIALIZED VIEW public.mv_kpi_tier1 AS
SELECT
  COALESCE(home_district_code, '__null__')                AS district_code,
  source_code,
  CASE
    WHEN home_district_code BETWEEN '1001' AND '1050' THEN 'bkk'
    WHEN home_district_code IS NULL                   THEN 'unknown'
    ELSE                                                   'non_bkk'
  END                                                     AS bucket,
  COUNT(DISTINCT patient_id)                              AS persons,
  COUNT(*) FILTER (WHERE is_dedup_kept)                   AS visits
FROM public.mv_visit_resolved
WHERE cancel_status IS DISTINCT FROM 1
GROUP BY home_district_code, source_code, bucket
HAVING COUNT(DISTINCT patient_id) >= 5             -- k-anonymity gate
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_kpi_tier1
  ON public.mv_kpi_tier1 (district_code, source_code, bucket);

GRANT SELECT ON public.mv_kpi_tier1
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_kpi_tier1 IS
  'Tier-1 KPI: persons + visits by (district × source × bucket). '
  'Derived from mv_visit_resolved; bucket re-computed from home_district_code. '
  'k-anonymity gate at >= 5 distinct patients per cell.';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_kpi_tier1;
