-- =============================================================================
-- Migration 212 — Promote summary_chronic_history + summary_family_history
--                 from stub views (returning zeros) to real materialized views.
--                 Plus add mv_dm_classification (4-condition Venn per request).
-- =============================================================================
-- Background:
--   Migration 200 dropped the legacy summary_chronic_history and
--   summary_family_history MVs (defined in mig 009 + 012) and replaced
--   them with stub VIEWs that returned zeros — see mig 200 line 792 with
--   the TODO(promote) comment. This migration fulfils that TODO using the
--   bma_med.app1_homehealth + portal_homehealth tables that the v3
--   architecture made canonical.
--
-- New objects:
--   public.summary_chronic_history       — self-reported chronic disease + treatment
--   public.summary_family_history        — parent_* checkboxes + pct_parent_*
--   public.mv_dm_classification          — 4-bit DM classification per district
--                                          (the user-requested Venn)
--
-- Inputs:
--   public.mv_visit_resolved             — patient → district mapping (deduped)
--   bma_med.app1_homehealth              — App1 source self-history + family checkboxes
--   bma_med.portal_homehealth            — Portal source (different polarity!)
--   bma_med.app1_labhealth               — App1 lab values (FBS / prefpg / bldsugar)
--   bma_med.portal_labhealth             — Portal lab values
--
-- Polarity handling:
--   App1   stores DM/HPT/STROKE/CHLTR/HRT/KIDNEY as SMALLINT
--          where 1 = เป็น (HAS) — count `= 1`.
--   Portal stores them as TEXT
--          where '1' = HAS, NULL/0 = doesn't — count IN ('1','true','TRUE').
--   Same convention for parent checkboxes (pdm/phpt/phrtm/...).
--
-- k-anonymity: applied at mv_dm_classification (HAVING n>=5) since per-pattern
--              per-district cells can be small. The two summary_* MVs aggregate
--              to one row per district (50 rows) and don't need cell suppression.
--
-- Refresh time estimate: ~30-90 sec each on the 642K-person dataset.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Drop the stub views from migration 200
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS public.summary_chronic_history CASCADE;
DROP VIEW IF EXISTS public.summary_family_history  CASCADE;

-- ---------------------------------------------------------------------------
-- 2. summary_chronic_history — self-reported chronic-disease + treatment
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW public.summary_chronic_history AS
WITH
hx_app1 AS (
  -- App1 cols are NUMERIC. App1 schema does NOT have `dmrs` — treatment
  -- status only available from Portal. Set dm_on_treatment FALSE here so
  -- the UNION below combines cleanly; Portal contributes the real signal.
  SELECT patient_id,
         bool_or(dm     = 1) AS hx_dm,
         bool_or(hpt    = 1) AS hx_hpt,
         bool_or(stroke = 1) AS hx_stroke,
         bool_or(chltr  = 1) AS hx_chltr,
         bool_or(hrt    = 1) AS hx_hrt,
         bool_or(kidney = 1) AS hx_kidney,
         FALSE                AS dm_on_treatment
  FROM bma_med.app1_homehealth
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
hx_portal AS (
  SELECT patient_id,
         bool_or(dm     IN ('1','true','TRUE')) AS hx_dm,
         bool_or(hpt    IN ('1','true','TRUE')) AS hx_hpt,
         bool_or(stroke IN ('1','true','TRUE')) AS hx_stroke,
         bool_or(chltr  IN ('1','true','TRUE')) AS hx_chltr,
         bool_or(hrt    IN ('1','true','TRUE')) AS hx_hrt,
         bool_or(kidney IN ('1','true','TRUE')) AS hx_kidney,
         bool_or(dmrs   IS NOT NULL AND dmrs <> '' AND dmrs <> '0') AS dm_on_treatment
  FROM bma_med.portal_homehealth
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
hx_all AS (
  SELECT patient_id,
         bool_or(hx_dm)             AS hx_dm,
         bool_or(hx_hpt)            AS hx_hpt,
         bool_or(hx_stroke)         AS hx_stroke,
         bool_or(hx_chltr)          AS hx_chltr,
         bool_or(hx_hrt)            AS hx_hrt,
         bool_or(hx_kidney)         AS hx_kidney,
         bool_or(dm_on_treatment)   AS dm_on_treatment
  FROM (SELECT * FROM hx_app1 UNION ALL SELECT * FROM hx_portal) u
  GROUP BY patient_id
),
visits_dedup AS (
  SELECT DISTINCT patient_id, home_district_code
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND home_district_code IS NOT NULL
)
SELECT
  v.home_district_code                                              AS district_code,
  COUNT(DISTINCT v.patient_id)                                       AS total_respondents,
  -- Match the stub's column names so any SELECT * caller keeps working
  COUNT(DISTINCT v.patient_id) FILTER (WHERE h.hx_dm)                AS hx_dm_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE h.hx_hpt)               AS hx_hpt_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE h.hx_stroke)            AS hx_stroke_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE h.hx_chltr)             AS hx_chltr_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE h.hx_hrt)               AS hx_hrt_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE h.hx_kidney)            AS hx_kidney_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE h.dm_on_treatment)      AS dm_on_treatment_count,
  -- back-compat zeros for family columns the stub had — real values are
  -- now in summary_family_history; kept here so old SELECTs don't break
  0::bigint AS family_dm_count,
  0::bigint AS family_hpt_count,
  0::bigint AS family_stroke_count,
  0::bigint AS family_hrt_count
FROM visits_dedup v
LEFT JOIN hx_all h ON h.patient_id = v.patient_id
GROUP BY v.home_district_code
WITH NO DATA;

CREATE UNIQUE INDEX uq_summary_chronic_history
  ON public.summary_chronic_history (district_code);

GRANT SELECT ON public.summary_chronic_history
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.summary_chronic_history IS
  'Self-reported chronic disease history + treatment status, per home district. '
  'Sources: bma_med.{app1,portal}_homehealth. Refresh after mv_visit_resolved.';


-- ---------------------------------------------------------------------------
-- 3. summary_family_history — parent_* checkboxes + percentages
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW public.summary_family_history AS
WITH
fam_app1 AS (
  SELECT patient_id,
         bool_or(pdm     = 1) AS p_dm,
         bool_or(phpt    = 1) AS p_hpt,
         bool_or(pstroke = 1) AS p_stroke,
         bool_or(phrtm   = 1) AS p_heart,
         bool_or(pkidney = 1) AS p_kidney,
         bool_or(pgout   = 1) AS p_gout,
         bool_or(pepm    = 1) AS p_emphysema
  FROM bma_med.app1_homehealth
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
fam_portal AS (
  SELECT patient_id,
         bool_or(pdm     IN ('1','true','TRUE')) AS p_dm,
         bool_or(phpt    IN ('1','true','TRUE')) AS p_hpt,
         bool_or(pstroke IN ('1','true','TRUE')) AS p_stroke,
         bool_or(phrtm   IN ('1','true','TRUE')) AS p_heart,
         bool_or(pkidney IN ('1','true','TRUE')) AS p_kidney,
         bool_or(pgout   IN ('1','true','TRUE')) AS p_gout,
         bool_or(pepm    IN ('1','true','TRUE')) AS p_emphysema
  FROM bma_med.portal_homehealth
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
fam_all AS (
  SELECT patient_id,
         bool_or(p_dm)        AS p_dm,
         bool_or(p_hpt)       AS p_hpt,
         bool_or(p_stroke)    AS p_stroke,
         bool_or(p_heart)     AS p_heart,
         bool_or(p_kidney)    AS p_kidney,
         bool_or(p_gout)      AS p_gout,
         bool_or(p_emphysema) AS p_emphysema
  FROM (SELECT * FROM fam_app1 UNION ALL SELECT * FROM fam_portal) u
  GROUP BY patient_id
),
visits_dedup AS (
  SELECT DISTINCT patient_id, home_district_code
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND home_district_code IS NOT NULL
)
SELECT
  v.home_district_code                                              AS district_code,
  COUNT(DISTINCT v.patient_id)                                       AS total_respondents,
  -- Counts (used by sidebar + chat tools)
  COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_dm)                 AS parent_dm_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_hpt)                AS parent_hpt_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_stroke)             AS parent_stroke_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_heart)              AS parent_heart_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_kidney)             AS parent_kidney_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_gout)               AS parent_gout_count,
  COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_emphysema)          AS parent_emphysema_count,
  -- Back-compat alias (older callers used this name)
  COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_dm)                 AS family_dm_count,
  -- Percentages (consumed by report_data_collector.py:932-937)
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_dm)
                 / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2)       AS pct_parent_dm,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_hpt)
                 / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2)       AS pct_parent_hpt,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_stroke)
                 / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2)       AS pct_parent_stroke,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_heart)
                 / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2)       AS pct_parent_heart,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_kidney)
                 / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2)       AS pct_parent_kidney,
  ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (WHERE f.p_gout)
                 / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2)       AS pct_parent_gout
FROM visits_dedup v
LEFT JOIN fam_all f ON f.patient_id = v.patient_id
GROUP BY v.home_district_code
WITH NO DATA;

CREATE UNIQUE INDEX uq_summary_family_history
  ON public.summary_family_history (district_code);

GRANT SELECT ON public.summary_family_history
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.summary_family_history IS
  'Family-history (parent_*) checkbox counts + percentages, per home district. '
  'Sources: bma_med.{app1,portal}_homehealth. Refresh after mv_visit_resolved.';


-- ---------------------------------------------------------------------------
-- 4. mv_dm_classification — 4-condition DM Venn (per district + bitstring)
-- ---------------------------------------------------------------------------
-- Conditions:
--   c1_risk     = riskdm flag at screening (mv_visit_resolved.risk_dm)
--   c2_diag    = self/doctor confirmed (mv_visit_resolved.found_dm)
--   c3_family   = parent_dm checkbox (any source)
--   c4_fpg     = FPG >= 126 mg/dL (ADA 2024 / สำนักการแพทย์)
--                preferring prefpg → fbs → bldsugar
-- Pattern: 4-char bitstring 'c1c2c3c4', e.g. '1010' = risk + family only.
-- k-anon: HAVING COUNT(*) >= 5 drops rare cells; the unflagged '0000'
--         pattern dominates and is preserved.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW public.mv_dm_classification AS
WITH
fam AS (
  SELECT patient_id, bool_or(p_dm) AS p_dm
  FROM (
    SELECT patient_id, bool_or(pdm = 1) AS p_dm
    FROM bma_med.app1_homehealth
    WHERE patient_id IS NOT NULL
    GROUP BY patient_id
    UNION ALL
    SELECT patient_id, bool_or(pdm IN ('1','true','TRUE')) AS p_dm
    FROM bma_med.portal_homehealth
    WHERE patient_id IS NOT NULL
    GROUP BY patient_id
  ) u
  GROUP BY patient_id
),
labs AS (
  -- FPG-based DM cutoff (≥126 mg/dL, ADA 2024 / สำนักการแพทย์).
  -- prefpg is NOT present in either lab table (verified Step 1) — fallback
  -- chain is fbs → bldsugar only. App1 cols are NUMERIC (no regex guard
  -- needed); Portal cols are TEXT (regex guards non-numeric tokens like
  -- Thai "ปกติ"/"เสี่ยง" that may have leaked in via ETL).
  SELECT patient_id, bool_or(fpg_high) AS fpg_high
  FROM (
    -- App1 (numeric)
    SELECT patient_id,
           (COALESCE(fbs, bldsugar) >= 126) AS fpg_high
    FROM bma_med.app1_labhealth
    WHERE patient_id IS NOT NULL
    UNION ALL
    -- Portal (text — regex-guarded cast)
    SELECT patient_id,
           (COALESCE(
              CASE WHEN fbs      ~ '^[0-9]+(\.[0-9]+)?$' THEN fbs::numeric      END,
              CASE WHEN bldsugar ~ '^[0-9]+(\.[0-9]+)?$' THEN bldsugar::numeric END
            ) >= 126) AS fpg_high
    FROM bma_med.portal_labhealth
    WHERE patient_id IS NOT NULL
  ) u
  GROUP BY patient_id
),
v_per_patient AS (
  SELECT
    patient_id,
    home_district_code,
    bool_or(risk_dm)  AS risk_dm,
    bool_or(found_dm) AS found_dm
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND home_district_code IS NOT NULL
  GROUP BY patient_id, home_district_code
),
patient_flags AS (
  SELECT
    v.patient_id,
    v.home_district_code,
    v.risk_dm                    AS c1_risk,
    v.found_dm                   AS c2_diag,
    COALESCE(f.p_dm,    FALSE)   AS c3_family,
    COALESCE(l.fpg_high, FALSE)  AS c4_fpg
  FROM v_per_patient v
  LEFT JOIN fam  f ON f.patient_id = v.patient_id
  LEFT JOIN labs l ON l.patient_id = v.patient_id
)
SELECT
  home_district_code AS district_code,
  CONCAT(c1_risk::int, c2_diag::int, c3_family::int, c4_fpg::int) AS pattern,
  COUNT(*) AS n_patients
FROM patient_flags
GROUP BY home_district_code, pattern
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE INDEX idx_mv_dm_classification_dist
  ON public.mv_dm_classification (district_code);
CREATE INDEX idx_mv_dm_classification_pattern
  ON public.mv_dm_classification (pattern);

GRANT SELECT ON public.mv_dm_classification
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_dm_classification IS
  '4-bit DM classification per district. pattern[0]=risk_dm, pattern[1]=found_dm '
  '(self+doctor), pattern[2]=parent_dm, pattern[3]=FPG>=126. k-anon: cells with '
  'n<5 dropped. Refresh after mv_visit_resolved.';


-- ---------------------------------------------------------------------------
-- 5. Initial REFRESH (non-CONCURRENT — first build needs full scan).
--    Subsequent refreshes can use CONCURRENTLY because the unique indexes
--    are in place.
-- ---------------------------------------------------------------------------
REFRESH MATERIALIZED VIEW public.summary_chronic_history;
REFRESH MATERIALIZED VIEW public.summary_family_history;
REFRESH MATERIALIZED VIEW public.mv_dm_classification;

COMMIT;

-- =============================================================================
-- END migration 212
-- =============================================================================
