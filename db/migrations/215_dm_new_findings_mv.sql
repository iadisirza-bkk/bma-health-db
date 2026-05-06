-- =============================================================================
-- Migration 215 — mv_dm_new_findings
-- =============================================================================
-- Purpose: count patients who explicitly self-reported NOT having DM but
-- whose FPG ≥ 126 mg/dL on this screening — "เจอใหม่ในโครงการ".
--
-- Why a new MV:
--   `mv_dm_classification.c2_diag` (in mig 213) bundles BOTH self-report
--   AND doctor-confirm-at-visit, so `c2=0 AND c4=1` ("undiagnosed_dm" in
--   API named_groups) is too loose — it counts everyone who didn't say
--   "yes-DM" anywhere, including blank / "ไม่เคยตรวจ" responses.
--
--   The user-actionable metric is stricter: only patients whose
--   self-report column **explicitly says no DM** combined with a positive
--   FPG (≥126). That's the "newly found via screening" cohort.
--
-- Polarity (verified):
--   App1 homehealth.dm   numeric       1=เป็น, 2=ไม่เป็น, 3=ไม่เคยตรวจ
--     "self said no DM"  ⟺  dm = 2  (strict — not 3)
--   Portal homehealth.dm text checkbox '1'=ticked-yes, else=not ticked
--     "self said no DM"  ⟺  dm IS NOT NULL AND dm NOT IN ('1','true','TRUE')
--     (rationale: NULL means they didn't engage with the question;
--      a non-yes value is the closest analogue to "ติ้กว่าไม่เป็น")
--
-- Lab positive: COALESCE(fbs, bldsugar) ≥ 126 mg/dL.
--   App1 lab cols are numeric; Portal cols are text — regex-guard cast.
-- =============================================================================

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS public.mv_dm_new_findings CASCADE;

CREATE MATERIALIZED VIEW public.mv_dm_new_findings AS
WITH
-- ── Self-report status per patient ──────────────────────────────────────
self_app1 AS (
  SELECT patient_id,
         bool_or(dm = 1) AS self_yes,
         bool_or(dm = 2) AS self_no    -- strict: explicit "ไม่เป็น"
  FROM bma_med.app1_homehealth
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
self_portal AS (
  SELECT patient_id,
         bool_or(dm IN ('1','true','TRUE')) AS self_yes,
         bool_or(dm IS NOT NULL AND dm NOT IN ('1','true','TRUE')) AS self_no
  FROM bma_med.portal_homehealth
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
self_all AS (
  SELECT patient_id,
         bool_or(self_yes) AS self_yes,
         bool_or(self_no)  AS self_no
  FROM (SELECT * FROM self_app1 UNION ALL SELECT * FROM self_portal) u
  GROUP BY patient_id
),

-- ── Lab positive (FPG ≥ 126) per patient ────────────────────────────────
labs AS (
  SELECT patient_id, bool_or(fpg_high) AS fpg_high
  FROM (
    SELECT patient_id,
           (COALESCE(fbs, bldsugar) >= 126) AS fpg_high
    FROM bma_med.app1_labhealth
    WHERE patient_id IS NOT NULL
    UNION ALL
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

-- ── Patient → district (one row per patient via dedup-kept visits) ──────
v_per_patient AS (
  SELECT DISTINCT patient_id, home_district_code
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND home_district_code IS NOT NULL
),

per_patient AS (
  SELECT
    v.patient_id,
    v.home_district_code,
    COALESCE(s.self_yes, FALSE) AS self_yes,
    COALESCE(s.self_no,  FALSE) AS self_no,
    COALESCE(l.fpg_high, FALSE) AS fpg_high
  FROM v_per_patient v
  LEFT JOIN self_all s ON s.patient_id = v.patient_id
  LEFT JOIN labs     l ON l.patient_id = v.patient_id
)

SELECT
  home_district_code AS district_code,
  COUNT(*) AS total_patients,
  -- Strict: explicit self-no AND NEVER self-yes AND lab positive.
  -- Excludes ~10K patients who answered "yes" in one source but "no" in
  -- another (cross-form contradictions); those need clinical review, not
  -- inclusion in the actionable "newly found" cohort.
  COUNT(*) FILTER (WHERE self_no AND NOT self_yes AND fpg_high)
    AS newly_found_strict,
  -- Loose: NOT self-yes AND lab positive (includes blanks + ไม่เคยตรวจ
  -- + non-respondents — anyone who didn't claim DM and has FPG ≥ 126).
  COUNT(*) FILTER (WHERE NOT self_yes AND fpg_high)
    AS newly_found_loose,
  -- Lab positive total (denominator for "% of FPG+ that are newly found").
  COUNT(*) FILTER (WHERE fpg_high) AS fpg_positive_total
FROM per_patient
GROUP BY home_district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_dm_new_findings
  ON public.mv_dm_new_findings (district_code);

GRANT SELECT ON public.mv_dm_new_findings
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_dm_new_findings IS
  'Newly-found DM cohort per district: patients who explicitly self-reported '
  'NOT having DM but have FPG >= 126. "strict" uses explicit ไม่เป็น only; '
  '"loose" uses NOT-self-yes (includes blanks + ไม่เคยตรวจ). Refresh after '
  'mv_visit_resolved.';

REFRESH MATERIALIZED VIEW public.mv_dm_new_findings;

COMMIT;

-- =============================================================================
-- END migration 215
-- =============================================================================
