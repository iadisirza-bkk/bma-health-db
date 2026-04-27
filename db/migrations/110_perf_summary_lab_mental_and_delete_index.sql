-- =============================================================================
-- Migration 110 — Perf round 2: lab/mental MVs + missing FK index
-- =============================================================================
-- Background:
-- - /summary/lab took 7.5s on cold cache (computed on every request from EAV).
-- - /summary/mental-health took 4.7s (same pattern).
-- - Bundle DELETE for `portal` source took 150s+ (and sometimes timed out).
--   Root cause: patient_address.reported_by_visit_id has ON DELETE SET NULL,
--   but no index → seq scan of patient_address per row deleted in visit_event.
-- =============================================================================

-- ─── 1. mv_summary_lab — pre-aggregated lab averages per district ─────────

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_lab CASCADE;
CREATE MATERIALIZED VIEW public.mv_summary_lab AS
WITH lab_by_district AS (
  SELECT pa.district_code, le.patient_id,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='hemoglobin')        AS hemoglobin,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='hematocrit')        AS hematocrit,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='fbs')               AS fbs,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='total_cholesterol') AS cholesterol,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='triglyceride')      AS triglyceride,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='hdl')               AS hdl,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='ldl')               AS ldl,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='creatinine')        AS creatinine,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='egfr')              AS egfr,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='uric_acid')         AS uric_acid,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='sgot')              AS sgot,
    AVG(lm.value_number) FILTER (WHERE vd.variable_key='sgpt')              AS sgpt
  FROM private.lab_event le
  JOIN private.lab_measurement lm ON lm.lab_id = le.id
  JOIN private.variable_definition vd ON vd.id = lm.variable_id
  LEFT JOIN private.patient_address pa
    ON pa.patient_id = le.patient_id
   AND pa.address_type = 'home' AND pa.effective_to IS NULL
  WHERE le.cancel_status = 0 AND lm.value_number IS NOT NULL
  GROUP BY pa.district_code, le.patient_id
)
SELECT
  district_code,
  COUNT(DISTINCT patient_id)        AS total_lab_patients,
  ROUND(AVG(hemoglobin), 2)         AS avg_hemoglobin,
  ROUND(AVG(hematocrit), 2)         AS avg_hematocrit,
  ROUND(AVG(fbs), 2)                AS avg_fbs,
  ROUND(AVG(cholesterol), 2)        AS avg_cholesterol,
  ROUND(AVG(triglyceride), 2)       AS avg_triglyceride,
  ROUND(AVG(hdl), 2)                AS avg_hdl,
  ROUND(AVG(ldl), 2)                AS avg_ldl,
  ROUND(AVG(creatinine), 2)         AS avg_creatinine,
  ROUND(AVG(egfr), 2)               AS avg_egfr,
  ROUND(AVG(uric_acid), 2)          AS avg_uric_acid,
  ROUND(AVG(sgot), 2)               AS avg_sgot,
  ROUND(AVG(sgpt), 2)               AS avg_sgpt,
  ROUND(100.0 * COUNT(*) FILTER (WHERE hemoglobin < 12)
              / NULLIF(COUNT(*) FILTER (WHERE hemoglobin IS NOT NULL), 0), 2) AS pct_anemia,
  ROUND(100.0 * COUNT(*) FILTER (WHERE egfr < 60)
              / NULLIF(COUNT(*) FILTER (WHERE egfr IS NOT NULL), 0), 2)       AS pct_ckd
FROM lab_by_district
WHERE district_code IS NOT NULL
GROUP BY district_code;
CREATE UNIQUE INDEX uq_mv_summary_lab ON public.mv_summary_lab (district_code);
GRANT SELECT ON public.mv_summary_lab TO bma_api_reader;


-- ─── 2. mv_summary_mental — pre-aggregated PHQ-9/ST-5/2Q stats ────────────

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_mental CASCADE;
CREATE MATERIALIZED VIEW public.mv_summary_mental AS
WITH per_visit AS (
  SELECT vr.home_district_code AS district_code,
         vr.patient_id, vr.visit_id,
    SUM(vm.value_number) FILTER (WHERE vd.variable_key SIMILAR TO 'phq9_q[1-9]') AS phq9_total,
    SUM(vm.value_number) FILTER (WHERE vd.variable_key SIMILAR TO 'st5_q[1-5]')  AS st5_total,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key='depression_2q_1')         AS dep1,
    MAX(vm.value_number) FILTER (WHERE vd.variable_key='depression_2q_2')         AS dep2
  FROM mv_visit_resolved vr
  JOIN private.visit_measurement vm ON vm.visit_id = vr.visit_id
  JOIN private.variable_definition vd ON vd.id = vm.variable_id
  WHERE vd.variable_key SIMILAR TO 'phq9_q[1-9]|st5_q[1-5]|depression_2q_[12]'
  GROUP BY vr.home_district_code, vr.patient_id, vr.visit_id
)
SELECT
  district_code,
  COUNT(DISTINCT patient_id) AS total_screened,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE dep1 >= 1 OR dep2 >= 1)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_depression_risk,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE phq9_total >= 10)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_phq9_moderate,
  ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE st5_total >= 7)
              / NULLIF(COUNT(DISTINCT patient_id), 0), 2) AS pct_high_stress
FROM per_visit
WHERE district_code IS NOT NULL
GROUP BY district_code;
CREATE UNIQUE INDEX uq_mv_summary_mental ON public.mv_summary_mental (district_code);
GRANT SELECT ON public.mv_summary_mental TO bma_api_reader;


-- ─── 3. Index for FK ON DELETE SET NULL cascade ──────────────────────────
-- Without this, deleting one visit_event row triggers a seq scan of
-- patient_address (51K rows) to find references via reported_by_visit_id.
-- Bulk delete on 489K visit_event rows = 25B row checks = 18+ min stuck.
-- With this index + the manual visit_measurement-first delete order in
-- api/admin.py:_delete_for_sources, total bundle DELETE drops from 150s+
-- (timing out) to ~45s.

CREATE INDEX IF NOT EXISTS idx_patient_address_reported_by_visit
  ON private.patient_address(reported_by_visit_id)
  WHERE reported_by_visit_id IS NOT NULL;

ANALYZE private.patient_address;
