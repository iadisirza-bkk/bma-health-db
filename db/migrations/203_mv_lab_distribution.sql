-- =============================================================================
-- Migration 203 — Translate mv_lab_distribution onto bma_med.*labhealth
-- =============================================================================
-- Original (101): district × source × lab_marker × value_bin (20-bucket
--   WIDTH_BUCKET on EAV lab_measurement.value_number, range from
--   variable_definition.valid_min/valid_max).
-- New: each lab marker is a typed column on bma_med.app1_labhealth /
--   bma_med.portal_labhealth; reshape via UNPIVOT (one row per (patient_id,
--   visit_date, lab_marker, value)). Hard-code the typical valid ranges
--   inline since variable_definition no longer exists. Markers covered:
--   hmgb, fbs, cholest, ldl, hdl, trigly, crtinine, egfrrs, sgot, sgpt,
--   uricacid, hmtc.
--   Portal lab columns are TEXT — wrap with NULLIF + safe-cast.
--   District lookup via mv_visit_resolved patient_id → home_district_code.
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_lab_distribution CASCADE;

CREATE MATERIALIZED VIEW public.mv_lab_distribution AS
WITH
-- 1. Numeric helper: TEXT → NUMERIC (NULL on parse failure)
-- (Inlined below as NULLIF + ::numeric for portal_*; app1_* is already num.)
patient_district AS (
  SELECT DISTINCT patient_id, home_district_code, source_code
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND cancel_status IS DISTINCT FROM 1
),
-- 2. Long-format lab values from app1.
--    Some columns (hmgb, fbs, cholest, ldl, hdl, trigly, crtinine, egfrrs,
--    hmtc) are already DOUBLE PRECISION; uricacid/sgot/sgpt are TEXT.
--    `NULLIF(x::text, '')::numeric` works for both (cast num → text → num,
--    null on empty string, no parse error on numeric-source columns).
lab_app1 AS (
  SELECT 'app1'::text AS source_code, l.patient_id, m.lab_marker, m.value, m.vmin, m.vmax
  FROM bma_med.app1_labhealth l
  CROSS JOIN LATERAL (VALUES
    ('hmgb',     NULLIF(l.hmgb::text,     '')::numeric,    5.0,  20.0),
    ('hmtc',     NULLIF(l.hmtc::text,     '')::numeric,   20.0,  60.0),
    ('fbs',      NULLIF(l.fbs::text,      '')::numeric,   40.0, 500.0),
    ('cholest',  NULLIF(l.cholest::text,  '')::numeric,  100.0, 400.0),
    ('hdl',      NULLIF(l.hdl::text,      '')::numeric,   10.0, 150.0),
    ('ldl',      NULLIF(l.ldl::text,      '')::numeric,   10.0, 300.0),
    ('trigly',   NULLIF(l.trigly::text,   '')::numeric,   20.0, 800.0),
    ('crtinine', NULLIF(l.crtinine::text, '')::numeric,    0.1,  10.0),
    ('egfrrs',   NULLIF(l.egfrrs::text,   '')::numeric,    5.0, 200.0),
    ('sgot',     NULLIF(l.sgot::text,     '')::numeric,    5.0, 500.0),
    ('sgpt',     NULLIF(l.sgpt::text,     '')::numeric,    5.0, 500.0),
    ('uricacid', NULLIF(l.uricacid::text, '')::numeric,    1.0,  20.0)
  ) AS m(lab_marker, value, vmin, vmax)
  WHERE COALESCE(l.dup_pid_vstdate, 0) = 0
    AND l.patient_id IS NOT NULL
    AND m.value IS NOT NULL
),
-- 3. Long-format lab values from portal (most columns TEXT — same cast pattern).
lab_portal AS (
  SELECT 'portal'::text AS source_code, l.patient_id, m.lab_marker, m.value, m.vmin, m.vmax
  FROM bma_med.portal_labhealth l
  CROSS JOIN LATERAL (VALUES
    ('hmgb',     NULLIF(l.hmgb::text,     '')::numeric,    5.0,  20.0),
    ('hmtc',     NULLIF(l.hmtc::text,     '')::numeric,   20.0,  60.0),
    ('fbs',      NULLIF(l.fbs::text,      '')::numeric,   40.0, 500.0),
    ('cholest',  NULLIF(l.cholest::text,  '')::numeric,  100.0, 400.0),
    ('hdl',      NULLIF(l.hdl::text,      '')::numeric,   10.0, 150.0),
    ('ldl',      NULLIF(l.ldl::text,      '')::numeric,   10.0, 300.0),
    ('trigly',   NULLIF(l.trigly::text,   '')::numeric,   20.0, 800.0),
    ('crtinine', NULLIF(l.crtinine::text, '')::numeric,    0.1,  10.0),
    ('egfrrs',   NULLIF(l.egfrrs::text,   '')::numeric,    5.0, 200.0),
    ('sgot',     NULLIF(l.sgot::text,     '')::numeric,    5.0, 500.0),
    ('sgpt',     NULLIF(l.sgpt::text,     '')::numeric,    5.0, 500.0),
    ('uricacid', NULLIF(l.uricacid::text, '')::numeric,    1.0,  20.0)
  ) AS m(lab_marker, value, vmin, vmax)
  WHERE COALESCE(l.dup_pid_vstdate, 0) = 0
    AND l.patient_id IS NOT NULL
    AND m.value IS NOT NULL
),
labs AS (
  SELECT * FROM lab_app1
  UNION ALL
  SELECT * FROM lab_portal
)
SELECT
  COALESCE(pd.home_district_code, '__null__')             AS district_code,
  labs.source_code,
  labs.lab_marker,
  WIDTH_BUCKET(labs.value, labs.vmin, labs.vmax, 20)      AS value_bin,
  COUNT(*)                                                AS n
FROM labs
LEFT JOIN patient_district pd
       ON pd.patient_id  = labs.patient_id
      AND pd.source_code = labs.source_code
GROUP BY pd.home_district_code, labs.source_code, labs.lab_marker, value_bin
HAVING COUNT(*) >= 5                              -- k-anon: ≥5 measurements
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_lab_distribution
  ON public.mv_lab_distribution (district_code, source_code, lab_marker, value_bin);

GRANT SELECT ON public.mv_lab_distribution
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_lab_distribution IS
  'Lab marker distributions (binned) per district × source. Reshapes typed '
  'wide columns from bma_med.app1_labhealth + portal_labhealth into long '
  'format then bins via WIDTH_BUCKET (20 bins, range hard-coded per marker). '
  'Portal lab columns are TEXT — cast safely. k-anon gate >= 5 per bin.';

-- Refresh after data load:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_lab_distribution;
