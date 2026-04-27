-- =============================================================================
-- Update mv_visit_resolved to include risk flags pivoted from EAV
-- This makes the API queries (overview/zones/districts) much simpler
-- =============================================================================

-- Drop dependent MVs first (will be recreated by next migration 101 re-apply)
DROP MATERIALIZED VIEW IF EXISTS public.mv_disease_district CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_kpi_tier1 CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_demographics CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_lab_distribution CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_mental_health CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_lifestyle CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.mv_visit_resolved CASCADE;

-- Recreate with risk flag pivot
CREATE MATERIALIZED VIEW public.mv_visit_resolved AS
WITH home_dc AS (
  SELECT pa.patient_id,
         pa.district_code AS home_district_code,
         pa.province_code AS home_province_code
  FROM private.patient_address pa
  WHERE pa.effective_to IS NULL
    AND pa.address_type = 'home'
),
visit_with_dc AS (
  SELECT
    v.id AS visit_id,
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
  WHERE v.cancel_status = 0
),
risk_pivot AS (
  SELECT
    vm.visit_id,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'risk_dm')            AS risk_dm,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'risk_hpt')           AS risk_hpt,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'risk_cvd')           AS risk_cvd,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'risk_bmi')           AS risk_bmi,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'risk_stroke')        AS risk_stroke,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'found_dm')           AS found_dm,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'found_hpt')          AS found_hpt,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'found_cvd')          AS found_cvd,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'found_obesity')      AS found_obesity,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'found_dyslipidemia') AS found_dyslipidemia,
    bool_or(vm.value_boolean) FILTER (WHERE vd.variable_key = 'found_stroke')       AS found_stroke
  FROM private.visit_measurement vm
  JOIN private.variable_definition vd ON vd.id = vm.variable_id
  WHERE vd.variable_key IN (
    'risk_dm','risk_hpt','risk_cvd','risk_bmi','risk_stroke',
    'found_dm','found_hpt','found_cvd','found_obesity',
    'found_dyslipidemia','found_stroke'
  )
  GROUP BY vm.visit_id
)
SELECT
  vw.*,
  COALESCE(rp.risk_dm,            FALSE) AS risk_dm,
  COALESCE(rp.risk_hpt,           FALSE) AS risk_hpt,
  COALESCE(rp.risk_cvd,           FALSE) AS risk_cvd,
  COALESCE(rp.risk_bmi,           FALSE) AS risk_bmi,
  COALESCE(rp.risk_stroke,        FALSE) AS risk_stroke,
  COALESCE(rp.found_dm,           FALSE) AS found_dm,
  COALESCE(rp.found_hpt,          FALSE) AS found_hpt,
  COALESCE(rp.found_cvd,          FALSE) AS found_cvd,
  COALESCE(rp.found_obesity,      FALSE) AS found_obesity,
  COALESCE(rp.found_dyslipidemia, FALSE) AS found_dyslipidemia,
  COALESCE(rp.found_stroke,       FALSE) AS found_stroke
FROM visit_with_dc vw
LEFT JOIN risk_pivot rp ON rp.visit_id = vw.visit_id;

CREATE UNIQUE INDEX uq_mv_visit_resolved
  ON public.mv_visit_resolved (visit_id);
CREATE INDEX idx_mv_visit_resolved_dist
  ON public.mv_visit_resolved (home_district_code, visit_date);
CREATE INDEX idx_mv_visit_resolved_src
  ON public.mv_visit_resolved (source_code, bucket);
CREATE INDEX idx_mv_visit_resolved_patient
  ON public.mv_visit_resolved (patient_id, visit_date);
