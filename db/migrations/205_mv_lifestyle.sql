-- =============================================================================
-- Migration 205 — Translate mv_lifestyle onto bma_med.* (vitalsignslf+homehealth)
-- =============================================================================
-- Original (101): district × source × variable_key × value persons,
--   where variable_key ∈ {smoking, alcohol, exercise} pulled from EAV
--   visit_measurement.value_text.
-- New: smoking/alcohol live on bma_med.*vitalsignslf (smoke, alcohal as
--   DOUBLE PRECISION codes), exercise on bma_med.*homehealth (excercise:
--   DOUBLE PRECISION on app1, TEXT on portal). Reshape with UNION + UNPIVOT.
--   district resolved via mv_visit_resolved patient_id join.
-- Output column shape: (district_code, source_code, variable_key, value, persons)
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_lifestyle CASCADE;

CREATE MATERIALIZED VIEW public.mv_lifestyle AS
WITH patient_district AS (
  SELECT DISTINCT patient_id, source_code, home_district_code
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND cancel_status IS DISTINCT FROM 1
),
-- Smoking + alcohol from app1_vitalsignslf (numeric codes)
ls_app1_vital AS (
  SELECT 'app1'::text AS source_code, v.patient_id,
         m.variable_key, m.value
  FROM bma_med.app1_vitalsignslf v
  CROSS JOIN LATERAL (VALUES
    ('smoking',  NULLIF(v.smoke::text,  '')),
    ('alcohol',  NULLIF(v.alcohal::text,''))
  ) AS m(variable_key, value)
  WHERE COALESCE(v.dup_pid_vstdate, 0) = 0
    AND v.patient_id IS NOT NULL
    AND m.value IS NOT NULL
),
-- Smoking + alcohol from portal_vitalsignslf
ls_portal_vital AS (
  SELECT 'portal'::text AS source_code, v.patient_id,
         m.variable_key, m.value
  FROM bma_med.portal_vitalsignslf v
  CROSS JOIN LATERAL (VALUES
    ('smoking',  NULLIF(v.smoke::text,  '')),
    ('alcohol',  NULLIF(v.alcohal::text,''))
  ) AS m(variable_key, value)
  WHERE COALESCE(v.dup_pid_vstdate, 0) = 0
    AND v.patient_id IS NOT NULL
    AND m.value IS NOT NULL
),
-- Exercise from homehealth (app1 numeric, portal TEXT)
ls_app1_home AS (
  SELECT 'app1'::text AS source_code, h.patient_id,
         'exercise'::text AS variable_key,
         NULLIF(h.excercise::text, '') AS value
  FROM bma_med.app1_homehealth h
  WHERE COALESCE(h.dup_pid_vstdate, 0) = 0
    AND h.patient_id IS NOT NULL
    AND h.excercise IS NOT NULL
),
ls_portal_home AS (
  SELECT 'portal'::text AS source_code, h.patient_id,
         'exercise'::text AS variable_key,
         NULLIF(h.excercise, '') AS value
  FROM bma_med.portal_homehealth h
  WHERE COALESCE(h.dup_pid_vstdate, 0) = 0
    AND h.patient_id IS NOT NULL
    AND h.excercise IS NOT NULL
),
patient_lifestyle AS (
  SELECT * FROM ls_app1_vital
  UNION ALL SELECT * FROM ls_portal_vital
  UNION ALL SELECT * FROM ls_app1_home
  UNION ALL SELECT * FROM ls_portal_home
)
SELECT
  COALESCE(pd.home_district_code, '__null__')             AS district_code,
  pl.source_code,
  pl.variable_key,
  pl.value,
  COUNT(DISTINCT pl.patient_id)                           AS persons
FROM patient_lifestyle pl
LEFT JOIN patient_district pd
       ON pd.patient_id  = pl.patient_id
      AND pd.source_code = pl.source_code
GROUP BY pd.home_district_code, pl.source_code, pl.variable_key, pl.value
HAVING COUNT(DISTINCT pl.patient_id) >= 5          -- k-anonymity gate
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_lifestyle
  ON public.mv_lifestyle (district_code, source_code, variable_key, value);

GRANT SELECT ON public.mv_lifestyle
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_lifestyle IS
  'Lifestyle factor distributions: persons by (district × source × variable × value). '
  'Variables: smoking, alcohol (from *vitalsignslf), exercise (from *homehealth). '
  'k-anonymity gate at >= 5 distinct patients per cell.';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_lifestyle;
