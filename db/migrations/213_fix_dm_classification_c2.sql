-- =============================================================================
-- Migration 213 — Fix mv_dm_classification.c2 (เป็นโรค DM) polarity
-- =============================================================================
-- Background:
--   mig 212's mv_dm_classification.c2 read mv_visit_resolved.found_dm,
--   which is computed in mig 200 as:
--       COALESCE(av.found_dm, FALSE) OR COALESCE(sh.hx_dm, FALSE)
--   where sh.hx_dm uses `bool_or(NULLIF(dm, 0) IS NOT NULL)` for App1.
--
--   App1 polarity is 1=เป็น / 2=ไม่เป็น / 3=ไม่เคยตรวจ — but
--   `NULLIF(dm,0) IS NOT NULL` returns TRUE for ALL three values, so
--   anyone who answered "no" or "never tested" is counted as "has DM".
--   Result: c2_diag inflated to 84.6% of cohort (351,774 / 415,849).
--
--   Real "เป็น DM" should mean: doctor confirmed at visit OR patient
--   self-reported having DM (not just answering the question).
--
-- Fix: rebuild mv_dm_classification with c2 derived directly from
--   bma_med.{app1,portal}_vitalsignslf.dm  (doctor exam — 1=positive)
--   bma_med.{app1,portal}_homehealth.dm    (self-report — 1=เป็น)
--   App1 cols are NUMERIC → `dm = 1`
--   Portal cols are TEXT  → `dm IN ('1','true','TRUE')`
--   Combined per patient with bool_or; OR across the two sources.
--
-- Other 3 conditions (c1 risk, c3 family, c4 fpg) unchanged from mig 212.
-- This migration only touches mv_dm_classification, NOT the two summary_*
-- MVs (those use the same patterns but for different reporting columns).
-- =============================================================================

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS public.mv_dm_classification CASCADE;

CREATE MATERIALIZED VIEW public.mv_dm_classification AS
WITH
-- ---------------------------------------------------------------------------
-- c2: per-patient "เป็น DM" (doctor exam OR self-report, correct polarity)
-- ---------------------------------------------------------------------------
diag_app1 AS (
  SELECT patient_id, bool_or(dm = 1) AS diag
  FROM (
    SELECT patient_id, dm FROM bma_med.app1_vitalsignslf WHERE patient_id IS NOT NULL
    UNION ALL
    SELECT patient_id, dm FROM bma_med.app1_homehealth   WHERE patient_id IS NOT NULL
  ) u
  GROUP BY patient_id
),
diag_portal AS (
  SELECT patient_id, bool_or(dm IN ('1','true','TRUE')) AS diag
  FROM (
    SELECT patient_id, dm FROM bma_med.portal_vitalsignslf WHERE patient_id IS NOT NULL
    UNION ALL
    SELECT patient_id, dm FROM bma_med.portal_homehealth   WHERE patient_id IS NOT NULL
  ) u
  GROUP BY patient_id
),
diag_all AS (
  SELECT patient_id, bool_or(diag) AS c2_diag
  FROM (SELECT * FROM diag_app1 UNION ALL SELECT * FROM diag_portal) u
  GROUP BY patient_id
),

-- ---------------------------------------------------------------------------
-- c3: per-patient family DM (parent_dm checkbox, any source) — unchanged
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- c4: per-patient FPG ≥ 126 (any visit, fbs → bldsugar fallback) — unchanged
-- ---------------------------------------------------------------------------
labs AS (
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

-- ---------------------------------------------------------------------------
-- c1: per-patient risk_dm (from screening). risk_dm in mv_visit_resolved
-- is unaffected by the c2 polarity bug — it comes straight from riskdm.
-- ---------------------------------------------------------------------------
v_per_patient AS (
  SELECT
    patient_id,
    home_district_code,
    bool_or(risk_dm) AS risk_dm
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND home_district_code IS NOT NULL
  GROUP BY patient_id, home_district_code
),

-- ---------------------------------------------------------------------------
-- Combine all 4 flags per patient
-- ---------------------------------------------------------------------------
patient_flags AS (
  SELECT
    v.patient_id,
    v.home_district_code,
    v.risk_dm                    AS c1_risk,
    COALESCE(d.c2_diag,  FALSE)  AS c2_diag,
    COALESCE(f.p_dm,     FALSE)  AS c3_family,
    COALESCE(l.fpg_high, FALSE)  AS c4_fpg
  FROM v_per_patient v
  LEFT JOIN diag_all d ON d.patient_id = v.patient_id
  LEFT JOIN fam      f ON f.patient_id = v.patient_id
  LEFT JOIN labs     l ON l.patient_id = v.patient_id
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
  '4-bit DM classification per district (mig 213 — c2 polarity-corrected). '
  'pattern[0]=risk_dm (riskdm=1 at screening), '
  'pattern[1]=found_dm (vitalsignslf.dm=1 OR homehealth.dm=1 — doctor or self), '
  'pattern[2]=family_dm (parent_dm checkbox, any source), '
  'pattern[3]=fpg_high (FBS or bldsugar >= 126). '
  'k-anon: per-district per-pattern cells with n<5 dropped. '
  'Refresh after mv_visit_resolved.';

REFRESH MATERIALIZED VIEW public.mv_dm_classification;

COMMIT;

-- =============================================================================
-- END migration 213
-- =============================================================================
