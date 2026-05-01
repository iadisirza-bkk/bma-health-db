-- =============================================================================
-- Migration 204 — Translate mv_mental_health onto bma_med.*vitalsignslf
-- =============================================================================
-- Original (101): district × source × phq9_band × st5_band person count.
--   phq9_total / st5_total were SUM()s over EAV phq9_q1..q9 / st5_q1..q5.
-- New: bma_med.*vitalsignslf already carries phq9_total + st5_total as
--   pre-computed DOUBLE PRECISION columns (clean.py output). Read them
--   directly via mv_visit_resolved which already exposes phq9_total / st5_total
--   per visit.
-- Output column shape: (district_code, source_code, phq9_band, st5_band, persons).
-- Bands match the original 101 cuts exactly.
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_mental_health CASCADE;

CREATE MATERIALIZED VIEW public.mv_mental_health AS
SELECT
  COALESCE(home_district_code, '__null__')                AS district_code,
  source_code,
  CASE
    WHEN phq9_total IS NULL THEN 'unknown'
    WHEN phq9_total < 5     THEN 'minimal'
    WHEN phq9_total < 10    THEN 'mild'
    WHEN phq9_total < 15    THEN 'moderate'
    WHEN phq9_total < 20    THEN 'mod_severe'
    ELSE                         'severe'
  END                                                     AS phq9_band,
  CASE
    WHEN st5_total IS NULL  THEN 'unknown'
    WHEN st5_total <= 4     THEN 'low'
    WHEN st5_total <= 7     THEN 'moderate'
    WHEN st5_total <= 9     THEN 'high'
    ELSE                         'severe'
  END                                                     AS st5_band,
  COUNT(DISTINCT patient_id)                              AS persons
FROM public.mv_visit_resolved
WHERE cancel_status IS DISTINCT FROM 1
  AND is_dedup_kept = TRUE
GROUP BY home_district_code, source_code, phq9_band, st5_band
HAVING COUNT(DISTINCT patient_id) >= 5             -- k-anonymity gate
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_mental_health
  ON public.mv_mental_health (district_code, source_code, phq9_band, st5_band);

GRANT SELECT ON public.mv_mental_health
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_mental_health IS
  'PHQ-9 + ST-5 banding per district × source. Bands match original 101 cuts: '
  'PHQ-9 (<5/<10/<15/<20/severe) and ST-5 (<=4/<=7/<=9/severe). '
  'Reads pre-computed phq9_total / st5_total from mv_visit_resolved.';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_mental_health;
