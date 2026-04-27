-- =============================================================================
-- Migration 106 — Performance MVs for /summary/* endpoints
-- =============================================================================
-- Background: /summary/districts, /summary/overview, /summary/zones each ran
-- the unified-CTE 30-day dedup over 898K visits per call → 30-75 seconds.
--
-- Fix: pre-aggregate the per-district / per-zone / global numbers into MVs,
-- refreshed by public.refresh_all_mvs() after each ETL import. End result:
--   /summary/districts: 75s → 0.07s
--   /summary/overview:  38s → 0.04s
--   /summary/zones:     33s → 0.005s
--
-- Source-filtered queries (?sources=portal,...) still fall through to the
-- slow CTE path. If those become hot, add per-source MVs in a follow-up.
-- =============================================================================

-- ─── 1. mv_summary_districts (50 rows, one per BKK district) ───────────────

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_districts CASCADE;
CREATE MATERIALIZED VIEW public.mv_summary_districts AS
WITH visits_30d AS (
  SELECT visit_id, source_code, patient_id, visit_date, home_district_code
  FROM (
    SELECT visit_id, source_code, patient_id, visit_date, home_district_code,
           ROW_NUMBER() OVER (
             PARTITION BY source_code, patient_id, group_id ORDER BY visit_date DESC
           ) AS rn
    FROM (
      SELECT visit_id, source_code, patient_id, visit_date, home_district_code,
             SUM(CASE WHEN prev_day IS NULL OR (visit_date - prev_day) >= 30
                      THEN 1 ELSE 0 END)
               OVER (PARTITION BY source_code, patient_id ORDER BY visit_date) AS group_id
      FROM (
        SELECT visit_id, source_code, patient_id, visit_date, home_district_code,
               LAG(visit_date) OVER (PARTITION BY source_code, patient_id ORDER BY visit_date) AS prev_day
        FROM public.mv_visit_resolved
      ) lagged
    ) grouped
  ) ranked
  WHERE rn = 1
),
patient_flags AS (
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
  GROUP BY home_district_code, patient_id
),
visit_counts AS (
  SELECT home_district_code, COUNT(*) AS total_visits
  FROM visits_30d
  WHERE home_district_code IS NOT NULL
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
LEFT JOIN visit_counts vc ON vc.home_district_code = d.dcode
GROUP BY d.dcode, d.name_th, d.zone_code, vc.total_visits;

CREATE UNIQUE INDEX uq_mv_summary_districts ON public.mv_summary_districts (district_code);
GRANT SELECT ON public.mv_summary_districts TO bma_api_reader;


-- ─── 2. mv_summary_zones (8 rows) ─────────────────────────────────────────

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_zones CASCADE;
CREATE MATERIALIZED VIEW public.mv_summary_zones AS
SELECT
  z.zone_code, z.name_th, z.name_en,
  COUNT(DISTINCT d.dcode)               AS district_count,
  COALESCE(SUM(s.total_screened), 0)    AS total_screened,
  COALESCE(SUM(s.total_visits), 0)      AS total_visits,
  COALESCE(SUM(s.risk_dm_count), 0)     AS diabetes,
  COALESCE(SUM(s.risk_hpt_count), 0)    AS hypertension,
  COALESCE(SUM(s.risk_cvd_count), 0)    AS cardiovascular,
  COALESCE(SUM(s.risk_bmi_count), 0)    AS obesity,
  COALESCE(SUM(s.found_dyslipidemia_count), 0) AS dyslipidemia,
  COALESCE(SUM(s.found_stroke_count), 0)       AS stroke
FROM ref_health_zones z
LEFT JOIN ref_districts d ON d.zone_code = z.zone_code
LEFT JOIN public.mv_summary_districts s ON s.district_code = d.dcode
GROUP BY z.zone_code, z.name_th, z.name_en;

CREATE UNIQUE INDEX uq_mv_summary_zones ON public.mv_summary_zones (zone_code);
GRANT SELECT ON public.mv_summary_zones TO bma_api_reader;


-- ─── 3. mv_summary_global (1 row, includes bucket breakdown) ──────────────

DROP MATERIALIZED VIEW IF EXISTS public.mv_summary_global CASCADE;
CREATE MATERIALIZED VIEW public.mv_summary_global AS
WITH visits_30d AS (
  SELECT visit_id, source_code, patient_id, visit_date, bucket
  FROM (
    SELECT visit_id, source_code, patient_id, visit_date, bucket,
           ROW_NUMBER() OVER (
             PARTITION BY source_code, patient_id, group_id ORDER BY visit_date DESC
           ) AS rn
    FROM (
      SELECT visit_id, source_code, patient_id, visit_date, bucket,
             SUM(CASE WHEN prev_day IS NULL OR (visit_date - prev_day) >= 30
                      THEN 1 ELSE 0 END)
               OVER (PARTITION BY source_code, patient_id ORDER BY visit_date) AS group_id
      FROM (
        SELECT visit_id, source_code, patient_id, visit_date, bucket,
               LAG(visit_date) OVER (PARTITION BY source_code, patient_id ORDER BY visit_date) AS prev_day
        FROM public.mv_visit_resolved
      ) lagged
    ) grouped
  ) ranked
  WHERE rn = 1
),
flags AS (
  SELECT patient_id, bucket,
    bool_or(risk_dm)            AS has_risk_dm,
    bool_or(risk_hpt)           AS has_risk_hpt,
    bool_or(risk_cvd)           AS has_risk_cvd,
    bool_or(risk_bmi)           AS has_risk_bmi,
    bool_or(found_dyslipidemia) AS has_found_dyslipidemia,
    bool_or(found_stroke)       AS has_found_stroke
  FROM public.mv_visit_resolved
  GROUP BY patient_id, bucket
)
SELECT
  COUNT(DISTINCT f.patient_id)                                         AS total_screened,
  (SELECT COUNT(*) FROM visits_30d)                                    AS total_visits,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.bucket='bkk')           AS bkk_screened,
  (SELECT COUNT(*) FROM visits_30d WHERE bucket='bkk')                 AS bkk_visits,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.bucket='non_bkk')       AS non_bkk_screened,
  (SELECT COUNT(*) FROM visits_30d WHERE bucket='non_bkk')             AS non_bkk_visits,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.bucket='unknown')       AS unknown_screened,
  (SELECT COUNT(*) FROM visits_30d WHERE bucket='unknown')             AS unknown_visits,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_risk_dm)            AS diabetes,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_risk_hpt)           AS hypertension,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_risk_cvd)           AS cardiovascular,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_risk_bmi)           AS obesity,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_found_dyslipidemia) AS dyslipidemia,
  COUNT(DISTINCT f.patient_id) FILTER (WHERE f.has_found_stroke)       AS stroke,
  NOW() AS refreshed_at
FROM flags f;

CREATE UNIQUE INDEX uq_mv_summary_global ON public.mv_summary_global (refreshed_at);
GRANT SELECT ON public.mv_summary_global TO bma_api_reader;
