-- =============================================================================
-- Migration 202 — Translate mv_demographics onto bma_med.* via mv_visit_resolved
-- =============================================================================
-- Original (101): district × source × sex_code × age_band person counts.
--   sex_code was TEXT ('M'/'F'/'unknown'), age_band derived from birth_year.
-- New: mv_visit_resolved has sex_code SMALLINT (10/20) + age_years INT;
--   keep the same output column shape but map sex_code 10 → 'M', 20 → 'F'.
--   age_band derived directly from age_years (snapshot at visit_date).
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_demographics CASCADE;

CREATE MATERIALIZED VIEW public.mv_demographics AS
SELECT
  COALESCE(home_district_code, '__null__')                AS district_code,
  source_code,
  CASE sex_code
    WHEN 10 THEN 'M'
    WHEN 20 THEN 'F'
    ELSE         'unknown'
  END                                                     AS sex_code,
  CASE
    WHEN age_years IS NULL  THEN 'unknown'
    WHEN age_years < 20     THEN 'lt20'
    WHEN age_years < 35     THEN '20_34'
    WHEN age_years < 50     THEN '35_49'
    WHEN age_years < 65     THEN '50_64'
    ELSE                         '65plus'
  END                                                     AS age_band,
  COUNT(DISTINCT patient_id)                              AS persons
FROM public.mv_visit_resolved
WHERE cancel_status IS DISTINCT FROM 1
  AND is_dedup_kept = TRUE
GROUP BY home_district_code, source_code, sex_code, age_band
HAVING COUNT(DISTINCT patient_id) >= 5             -- k-anonymity gate
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_demographics
  ON public.mv_demographics (district_code, source_code, sex_code, age_band);

GRANT SELECT ON public.mv_demographics
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_demographics IS
  'Demographic pivot: persons by (district × source × sex × age band). '
  'sex_code projected to legacy ''M''/''F''/''unknown'' text values; age band '
  'derived from mv_visit_resolved.age_years (snapshot at visit_date).';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_demographics;
