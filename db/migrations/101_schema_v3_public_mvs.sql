-- =============================================================================
-- Schema v3 — Public Materialized Views (k-anonymized aggregates)
-- =============================================================================
-- These are the ONLY tables the API/Frontend can read.
-- All MVs:
--   - aggregate enough to ensure n ≥ K_ANON (= 5)
--   - HAVING clause suppresses small cells
--   - REFRESH CONCURRENTLY (no read lock)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Helper view (intermediate; used by other MVs)
-- visit_resolved: per-visit with home_district + bucket pre-computed
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_visit_resolved AS
WITH home_dc AS (
  SELECT pa.patient_id,
         pa.district_code AS home_district_code,
         pa.province_code AS home_province_code
  FROM private.patient_address pa
  WHERE pa.effective_to IS NULL
    AND pa.address_type = 'home'
)
SELECT
  v.id              AS visit_id,
  v.patient_id,
  v.visit_date,
  v.source_code,
  v.facility_code,
  v.cancel_status,
  v.is_dedup_kept,
  h.home_district_code,
  h.home_province_code,
  CASE
    WHEN h.home_district_code BETWEEN '1001' AND '1050' THEN 'bkk'
    WHEN h.home_district_code IS NULL                   THEN 'unknown'
    ELSE                                                     'non_bkk'
  END AS bucket
FROM private.visit_event v
LEFT JOIN home_dc h ON h.patient_id = v.patient_id
WHERE v.cancel_status = 0;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_visit_resolved
  ON public.mv_visit_resolved (visit_id);
CREATE INDEX IF NOT EXISTS idx_mv_visit_resolved_dist
  ON public.mv_visit_resolved (home_district_code, visit_date);
CREATE INDEX IF NOT EXISTS idx_mv_visit_resolved_src
  ON public.mv_visit_resolved (source_code, bucket);

-- -----------------------------------------------------------------------------
-- Tier 1 KPI — daily project-wide totals
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_kpi_tier1 AS
SELECT
  COALESCE(home_district_code, '__null__') AS district_code,
  source_code,
  bucket,
  COUNT(DISTINCT patient_id) AS persons,
  COUNT(*) FILTER (WHERE is_dedup_kept) AS visits
FROM public.mv_visit_resolved
GROUP BY home_district_code, source_code, bucket
HAVING COUNT(DISTINCT patient_id) >= 5;          -- k-anonymity ≥ 5

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_kpi_tier1
  ON public.mv_kpi_tier1 (district_code, source_code, bucket);

-- -----------------------------------------------------------------------------
-- Disease prevalence per district × source
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_disease_district AS
WITH disease_vars AS (
  -- Variables tagged as disease risk/found flags
  SELECT id, variable_key
  FROM private.variable_definition
  WHERE variable_key IN ('risk_dm','risk_hpt','risk_cvd','risk_bmi','risk_stroke',
                          'risk_dyslipidemia',
                          'found_dm','found_hpt','found_cvd','found_obesity',
                          'found_dyslipidemia','found_stroke')
    AND deprecated_at IS NULL
),
visit_disease AS (
  SELECT
    vr.home_district_code,
    vr.source_code,
    vr.patient_id,
    dv.variable_key
  FROM public.mv_visit_resolved vr
  JOIN private.visit_measurement vm ON vm.visit_id = vr.visit_id
  JOIN disease_vars dv ON dv.id = vm.variable_id
  WHERE vm.value_boolean = TRUE
    AND vr.bucket = 'bkk'
)
SELECT
  home_district_code AS district_code,
  source_code,
  variable_key AS disease_key,
  COUNT(DISTINCT patient_id) AS persons_at_risk
FROM visit_disease
GROUP BY home_district_code, source_code, variable_key
HAVING COUNT(DISTINCT patient_id) >= 5;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_disease_district
  ON public.mv_disease_district (district_code, source_code, disease_key);

-- -----------------------------------------------------------------------------
-- Demographics pivot — sex × age_band × district
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_demographics AS
SELECT
  COALESCE(vr.home_district_code, '__null__') AS district_code,
  vr.source_code,
  COALESCE(p.sex_code, 'unknown') AS sex_code,
  CASE
    WHEN p.birth_year IS NULL THEN 'unknown'
    WHEN EXTRACT(YEAR FROM CURRENT_DATE) - p.birth_year < 20 THEN 'lt20'
    WHEN EXTRACT(YEAR FROM CURRENT_DATE) - p.birth_year < 35 THEN '20_34'
    WHEN EXTRACT(YEAR FROM CURRENT_DATE) - p.birth_year < 50 THEN '35_49'
    WHEN EXTRACT(YEAR FROM CURRENT_DATE) - p.birth_year < 65 THEN '50_64'
    ELSE                                                          '65plus'
  END AS age_band,
  COUNT(DISTINCT vr.patient_id) AS persons
FROM public.mv_visit_resolved vr
JOIN private.patient p ON p.id = vr.patient_id
WHERE NOT p.is_erased
GROUP BY vr.home_district_code, vr.source_code, p.sex_code, age_band
HAVING COUNT(DISTINCT vr.patient_id) >= 5;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_demographics
  ON public.mv_demographics (district_code, source_code, sex_code, age_band);

-- -----------------------------------------------------------------------------
-- Lab distribution (binned)
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_lab_distribution AS
SELECT
  COALESCE(vr.home_district_code, '__null__') AS district_code,
  vr.source_code,
  vd.variable_key AS lab_marker,
  WIDTH_BUCKET(
    lm.value_number,
    GREATEST(vd.valid_min, 0),
    GREATEST(vd.valid_max, 1),
    20
  ) AS value_bin,
  COUNT(*) AS n
FROM private.lab_event le
JOIN private.lab_measurement lm ON lm.lab_id = le.id
JOIN private.variable_definition vd ON vd.id = lm.variable_id
JOIN public.mv_visit_resolved vr ON vr.patient_id = le.patient_id
WHERE vd.domain = 'lab'
  AND lm.value_number IS NOT NULL
  AND vd.valid_min IS NOT NULL
  AND vd.valid_max IS NOT NULL
  AND le.cancel_status = 0
GROUP BY vr.home_district_code, vr.source_code, vd.variable_key, value_bin
HAVING COUNT(*) >= 5;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_lab_distribution
  ON public.mv_lab_distribution (district_code, source_code, lab_marker, value_bin);

-- -----------------------------------------------------------------------------
-- Mental health (PHQ-9 / ST5 / 2Q binned)
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_mental_health AS
WITH phq9 AS (
  SELECT vm.visit_id, SUM(vm.value_number)::int AS total
  FROM private.visit_measurement vm
  JOIN private.variable_definition vd ON vd.id = vm.variable_id
  WHERE vd.variable_key SIMILAR TO 'phq9_q[1-9]'
  GROUP BY vm.visit_id
),
st5 AS (
  SELECT vm.visit_id, SUM(vm.value_number)::int AS total
  FROM private.visit_measurement vm
  JOIN private.variable_definition vd ON vd.id = vm.variable_id
  WHERE vd.variable_key SIMILAR TO 'st5_q[1-5]'
  GROUP BY vm.visit_id
),
combined AS (
  SELECT
    vr.home_district_code,
    vr.source_code,
    vr.patient_id,
    p9.total AS phq9_total,
    s5.total AS st5_total
  FROM public.mv_visit_resolved vr
  LEFT JOIN phq9 p9 ON p9.visit_id = vr.visit_id
  LEFT JOIN st5  s5 ON s5.visit_id = vr.visit_id
)
SELECT
  COALESCE(home_district_code, '__null__') AS district_code,
  source_code,
  CASE
    WHEN phq9_total IS NULL THEN 'unknown'
    WHEN phq9_total < 5  THEN 'minimal'
    WHEN phq9_total < 10 THEN 'mild'
    WHEN phq9_total < 15 THEN 'moderate'
    WHEN phq9_total < 20 THEN 'mod_severe'
    ELSE                       'severe'
  END AS phq9_band,
  CASE
    WHEN st5_total IS NULL THEN 'unknown'
    WHEN st5_total <= 4    THEN 'low'
    WHEN st5_total <= 7    THEN 'moderate'
    WHEN st5_total <= 9    THEN 'high'
    ELSE                        'severe'
  END AS st5_band,
  COUNT(DISTINCT patient_id) AS persons
FROM combined
GROUP BY home_district_code, source_code, phq9_band, st5_band
HAVING COUNT(DISTINCT patient_id) >= 5;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_mental_health
  ON public.mv_mental_health (district_code, source_code, phq9_band, st5_band);

-- -----------------------------------------------------------------------------
-- Lifestyle (smoking, alcohol, exercise rates)
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_lifestyle AS
WITH lifestyle_vars AS (
  SELECT id, variable_key
  FROM private.variable_definition
  WHERE variable_key IN ('smoking','alcohol','exercise')
    AND deprecated_at IS NULL
),
patient_lifestyle AS (
  SELECT DISTINCT
    vr.home_district_code,
    vr.source_code,
    vr.patient_id,
    lv.variable_key,
    vm.value_text AS value
  FROM public.mv_visit_resolved vr
  JOIN private.visit_measurement vm ON vm.visit_id = vr.visit_id
  JOIN lifestyle_vars lv ON lv.id = vm.variable_id
  WHERE vm.value_text IS NOT NULL
)
SELECT
  COALESCE(home_district_code, '__null__') AS district_code,
  source_code,
  variable_key,
  value,
  COUNT(DISTINCT patient_id) AS persons
FROM patient_lifestyle
GROUP BY home_district_code, source_code, variable_key, value
HAVING COUNT(DISTINCT patient_id) >= 5;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_lifestyle
  ON public.mv_lifestyle (district_code, source_code, variable_key, value);

-- -----------------------------------------------------------------------------
-- Data dictionary — frontend reads to know what's available
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_data_dictionary AS
SELECT
  vd.variable_key,
  vd.domain,
  vd.sub_domain,
  vd.data_type,
  vd.unit,
  vd.description_th,
  vd.description_en,
  vd.tier,
  ARRAY_AGG(DISTINCT vd.source_code ORDER BY vd.source_code) AS available_in_sources,
  ARRAY_AGG(DISTINCT vd.csv_column_name ORDER BY vd.csv_column_name) AS csv_column_names,
  COUNT(*) AS source_count
FROM private.variable_definition vd
WHERE vd.deprecated_at IS NULL
  AND NOT COALESCE(vd.is_pii, FALSE)
GROUP BY vd.variable_key, vd.domain, vd.sub_domain, vd.data_type,
         vd.unit, vd.description_th, vd.description_en, vd.tier;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_data_dictionary
  ON public.mv_data_dictionary (variable_key);
CREATE INDEX IF NOT EXISTS idx_mv_data_dictionary_domain
  ON public.mv_data_dictionary (domain, tier);

-- -----------------------------------------------------------------------------
-- Reference views (pass-through, no aggregation needed — these are tiny)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.v_districts AS
SELECT dcode, province_code, zone_code, name_th, name_en, population, is_bangkok
FROM private.geo_district;

CREATE OR REPLACE VIEW public.v_health_zones AS
SELECT zone_code, name_th, name_en, facilitator, mentor, area_manager_count
FROM private.geo_health_zone;

CREATE OR REPLACE VIEW public.v_facilities AS
SELECT code, name_th, name_en, facility_type, district_code, zone_code,
       latitude, longitude, address, telephone, ct_id, ct_name
FROM private.facility WHERE active;

CREATE OR REPLACE VIEW public.v_data_sources AS
SELECT source_code, name_th, name_en
FROM private.data_source WHERE active;

-- -----------------------------------------------------------------------------
-- Refresh function — call from pg_cron daily
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.refresh_all_mvs() RETURNS TABLE (
  view_name TEXT, status TEXT, duration_ms INTEGER, error_message TEXT
) AS $$
DECLARE
  v_name TEXT;
  v_start TIMESTAMP;
  v_err TEXT;
BEGIN
  -- mv_visit_resolved is the source for several other MVs.
  -- Refresh it FIRST, then refresh the rest alphabetically.
  -- Parentheses around each SELECT prevent the trailing ORDER BY from
  -- sorting the entire UNION (which would push mv_visit_resolved to the end
  -- alphabetically and cause downstream MVs to read stale data).
  FOR v_name IN
    (SELECT 'mv_visit_resolved' AS n)
    UNION ALL
    (SELECT mv.matviewname
       FROM pg_matviews mv
      WHERE mv.schemaname = 'public'
        AND mv.matviewname <> 'mv_visit_resolved'
      ORDER BY mv.matviewname)
  LOOP
    v_start := clock_timestamp();
    BEGIN
      EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY public.%I', v_name);
      view_name := v_name;
      status := 'ok';
      duration_ms := EXTRACT(MILLISECOND FROM clock_timestamp() - v_start)::INT;
      error_message := NULL;
      RETURN NEXT;
    EXCEPTION WHEN OTHERS THEN
      view_name := v_name;
      status := 'error';
      duration_ms := EXTRACT(MILLISECOND FROM clock_timestamp() - v_start)::INT;
      GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT;
      error_message := v_err;
      RETURN NEXT;
    END;
  END LOOP;
  RETURN;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

COMMENT ON FUNCTION public.refresh_all_mvs IS
  'Refresh all public.mv_* in dependency order. Returns one row per view with timing/error.';

-- Track last refresh
CREATE TABLE IF NOT EXISTS public.mv_refresh_log (
  id              BIGSERIAL PRIMARY KEY,
  refreshed_at    TIMESTAMPTZ DEFAULT NOW(),
  view_name       VARCHAR(100),
  status          VARCHAR(20),
  duration_ms     INTEGER,
  error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_mv_refresh_log
  ON public.mv_refresh_log (refreshed_at DESC);
