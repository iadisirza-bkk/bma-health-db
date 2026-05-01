-- =============================================================================
-- Migration 200 — Translate hot/cold materialized views from
--                 EAV (`private.visit_measurement`) onto the bma_med.* typed
--                 wide tables.
-- =============================================================================
-- Background:
--   The pre-200 schema stored every measurement in `private.visit_measurement`
--   (EAV, ~78 M rows) and every router/MV reached into it via
--   `variable_definition.variable_key`. That was slow on cold cache and forced
--   every aggregate into a 4-way join. Migration 109 already cleaned up
--   long-tail EAV; this migration moves the 4 hot MVs (the only ones that
--   actually matter for prod traffic) onto the new `bma_med.*` typed wide
--   tables ingested by /Users/dev/bma-med/ingest.py.
--
--   The 4 hot MVs cover ~95 % of API references (53 of ~70 in
--   FRONTEND-API-MAP.md) and hold the planner's hottest plans:
--      mv_visit_resolved          → foundation for the 3 below + many cold MVs
--      summary_district_disease   → 36 API refs   (district × disease counts)
--      summary_facility           →  9 API refs   (per-facility screen+dx)
--      summary_disease_age_sex    →  8 API refs   (age × sex × disease)
--
--   The 8 cold MVs (summary_district_risk_factors, summary_district_lab,
--   summary_district_mental, summary_district_demographics,
--   summary_lab_disease_cross, summary_bmi_waist, summary_screening_tests,
--   summary_chronic_history) are recreated as plain VIEWs returning empty
--   stubs so the existing endpoints don't 500. They can be promoted to real
--   MVs incrementally.
--
-- Source-table notes (verified via /Users/dev/bma-med/generate_table_ddl.py):
--   bma_med.patient                — patient_id, sex_code (10/20), birthdate
--   bma_med.app1_pt / portal_pt    — pid, male, birthdate, hptcode (facility)
--   bma_med.app1_vitalsignslf
--      and portal_vitalsignslf     — vstdate, hptcode, hbpn (SBP), lbpn (DBP),
--                                    height, weight, prefpg, postfpg, riskdm,
--                                    riskhpt, riskcdvcl, riskbmi, dm/hpt/
--                                    cdvcl/stroke/fat/chltr (post-test),
--                                    smoke, alcohal, msd_dm, msd_ht, msd_obese,
--                                    msd_cvd_ekg, msd_chest, bmi_calc,
--                                    bp_cat_aha, phq9_total, st5_total
--   bma_med.app1_homehealth
--      and portal_homehealth       — vstdate, dm/hpt/stroke/chltr/hrt/kidney,
--                                    parent (family hx), excercise, food
--   bma_med.app1_homevisit
--      and portal_homevisit        — vstdate, district (numeric/text),
--                                    crdistrict (alt), wrkdistrict, edu,
--                                    occptn, prvlg, hometype
--   bma_med.app1_labhealth
--      and portal_labhealth        — vstdate, fbs, cholest, ldl, hdl, trigly,
--                                    egfrrs, egfr_lab, hmgb, msd_dm,
--                                    msd_kidney, msd_anemia, msd_hyperchol,
--                                    msd_liver, msd_cervical, msd_colon
--
--   Both `app1_*` and `portal_*` carry the same conceptual columns; this
--   migration UNIONs them so each (patient_id, vstdate) is a single conceptual
--   "visit" tuple regardless of which app the screening came through.
--   `app2_app2` is a dashboard-derived rollup of the above and intentionally
--   excluded from `mv_visit_resolved` (its columns are already in app1/portal).
--
-- Idempotency: every CREATE uses `WITH NO DATA` so the migration applies
-- without forcing a refresh. Operator runs the REFRESH commands at the
-- bottom (commented out) after data load.
--
-- K-anonymity: every aggregate that exposes a small cell carries
--   `HAVING COUNT(DISTINCT patient_id) >= 5`
-- as belt-and-braces over `security/k_anon.py`. This keeps small groups
-- (e.g. district × age × sex × disease) from leaking to the API even if
-- the application-layer gate is bypassed.
--
-- Apply with:
--   psql 'postgresql://postgres:bma_health_dev@localhost:5433/bma_med_test' \
--        -f db/migrations/200_bma_med_mv_translation.sql
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Drop legacy EAV-backed MV first; CASCADE because all summary_* depend on it.
-- -----------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS public.mv_visit_resolved CASCADE;

-- Drop any previous incarnations of the 4 hot MVs (defensive — migration
-- 105 made them VIEWs, but in some staging envs they may already be MVs).
DROP MATERIALIZED VIEW IF EXISTS public.summary_district_disease  CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_facility          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_disease_age_sex   CASCADE;
DROP VIEW              IF EXISTS public.summary_district_disease  CASCADE;
DROP VIEW              IF EXISTS public.summary_facility          CASCADE;
DROP VIEW              IF EXISTS public.summary_disease_age_sex   CASCADE;

-- And the 8 cold stubs we recreate as VIEWs at the end.
DROP MATERIALIZED VIEW IF EXISTS public.summary_district_risk_factors CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_district_lab          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_district_mental       CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_district_demographics CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_lab_disease_cross     CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_bmi_waist             CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_screening_tests       CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_chronic_history       CASCADE;
DROP VIEW              IF EXISTS public.summary_district_risk_factors CASCADE;
DROP VIEW              IF EXISTS public.summary_district_lab          CASCADE;
DROP VIEW              IF EXISTS public.summary_district_mental       CASCADE;
DROP VIEW              IF EXISTS public.summary_district_demographics CASCADE;
DROP VIEW              IF EXISTS public.summary_lab_disease_cross     CASCADE;
DROP VIEW              IF EXISTS public.summary_bmi_waist             CASCADE;
DROP VIEW              IF EXISTS public.summary_screening_tests       CASCADE;
DROP VIEW              IF EXISTS public.summary_chronic_history       CASCADE;


-- =============================================================================
-- 1. mv_visit_resolved
--    Foundation MV — every other summary_* MV depends on this.
--
--    Shape (per row = one screening visit):
--       visit_uid              text     -- '<src>:<patient_id>:<vstdate>'
--       patient_id             bigint
--       source_code            text     -- 'app1' | 'portal'
--       visit_date             date
--       facility_code          text     -- maps to legacy hptcode
--       sex_code               smallint -- from bma_med.patient (10=M, 20=F)
--       birthdate              date     -- from bma_med.patient
--       age_years              int      -- derived; NULL if birthdate NULL
--       age_group              text     -- '18-29','30-44',...,'unknown'
--       home_district_code     text     -- from homevisit.district (TEXT cast)
--       cancel_status          smallint -- 0=active, 1=cancelled (record_cancelled)
--       is_dedup_kept          boolean  -- TRUE if the dedup chooser kept this row
--       -- pre-test screening risk flags (from vitalsignslf risk* + msd_*)
--       risk_dm, risk_hpt, risk_cvd, risk_bmi, risk_stroke
--       -- post-test confirmed disease flags (from msd_* or self-reported)
--       found_dm, found_hpt, found_cvd, found_obesity,
--       found_dyslipidemia, found_stroke
--       -- vital metrics (raw values, used by stub MVs)
--       sbp, dbp, height_cm, weight_kg, waist_cm, bmi
--       -- mental scores
--       phq9_total, st5_total
--
--    UNION rule: app1 + portal sources merged; app2 excluded (its columns
--    are derived rollups of app1/portal already and would double-count).
--    Each (patient_id, vstdate, source_code) tuple is unique.
--
--    The msd_* SMALLINT flags are 1=positive / 0=negative / NULL=not eval;
--    we map them to bool via `= 1`. The riskdm/riskhpt/etc DOUBLE PRECISION
--    columns use 1=at-risk per CODES_REFERENCE.md (factsheet line 94).
-- =============================================================================

CREATE MATERIALIZED VIEW public.mv_visit_resolved AS
WITH
-- 1a. visit_event from vitalsignslf (one row per visit; the only table that
--     has SBP/DBP/BMI/risk* in both app1 and portal)
visit_app1 AS (
  SELECT
    'app1'::text                                            AS source_code,
    v.patient_id,
    v.vstdate::date                                         AS visit_date,
    v.hptcode                                               AS facility_code,
    COALESCE(v.record_cancelled, 0)::smallint               AS cancel_status,
    -- Pre-test risk: 1.0 = at-risk per factsheet
    (v.riskdm    = 1)                                       AS risk_dm,
    (v.riskhpt   = 1)                                       AS risk_hpt,
    (v.riskcdvcl = 1)                                       AS risk_cvd,
    (v.riskbmi   = 1)                                       AS risk_bmi,
    (v.stroke    = 1)                                       AS risk_stroke,
    -- Post-test confirmed (msd_* SMALLINT flags = 1 means positive)
    (v.msd_dm     = 1)                                      AS found_dm,
    (v.msd_ht     = 1)                                      AS found_hpt,
    (v.msd_cvd_ekg = 1 OR v.msd_chest = 1)                  AS found_cvd,
    (v.msd_obese  = 1)                                      AS found_obesity,
    -- Dyslipidemia: chltr post-test indicator (1 = abnormal lipid)
    (v.chltr      = 1)                                      AS found_dyslipidemia,
    (v.stroke     = 1)                                      AS found_stroke,
    -- Raw vitals
    v.hbpn::numeric                                         AS sbp,
    v.lbpn::numeric                                         AS dbp,
    v.height::numeric                                       AS height_cm,
    v.weight::numeric                                       AS weight_kg,
    v.wstl::numeric                                         AS waist_cm,
    v.bmi_calc::numeric                                     AS bmi,
    v.phq9_total::numeric                                   AS phq9_total,
    v.st5_total::numeric                                    AS st5_total
  FROM bma_med.app1_vitalsignslf v
  WHERE COALESCE(v.dup_pid_vstdate, 0) = 0   -- dedup chooser (unique tuple)
    AND v.patient_id IS NOT NULL
),
visit_portal AS (
  SELECT
    'portal'::text                                          AS source_code,
    v.patient_id,
    v.vstdate::date                                         AS visit_date,
    v.hptcode                                               AS facility_code,
    COALESCE(v.record_cancelled, 0)::smallint               AS cancel_status,
    (v.riskdm    = 1)                                       AS risk_dm,
    (v.riskhpt   = 1)                                       AS risk_hpt,
    -- portal_vitalsignslf.riskcdvcl is TEXT; compare as text '1'
    (v.riskcdvcl = '1')                                     AS risk_cvd,
    (v.riskbmi   = 1)                                       AS risk_bmi,
    (v.stroke    = '1')                                     AS risk_stroke,
    (v.msd_dm     = 1)                                      AS found_dm,
    (v.msd_ht     = 1)                                      AS found_hpt,
    (v.msd_cvd_ekg = 1 OR v.msd_chest = 1)                  AS found_cvd,
    (v.msd_obese  = 1)                                      AS found_obesity,
    (v.chltr      = '1')                                    AS found_dyslipidemia,
    (v.stroke     = '1')                                    AS found_stroke,
    v.hbpn::numeric                                         AS sbp,
    v.lbpn::numeric                                         AS dbp,
    v.height::numeric                                       AS height_cm,
    v.weight::numeric                                       AS weight_kg,
    v.wstl::numeric                                         AS waist_cm,
    v.bmi_calc::numeric                                     AS bmi,
    v.phq9_total::numeric                                   AS phq9_total,
    v.st5_total::numeric                                    AS st5_total
  FROM bma_med.portal_vitalsignslf v
  WHERE COALESCE(v.dup_pid_vstdate, 0) = 0
    AND v.patient_id IS NOT NULL
),
-- 1b. district lookup from homevisit (most-recent non-null per patient).
--     Both app1.district and portal.district are DOUBLE PRECISION codes 1001-1050.
--     crdistrict (TEXT, portal only) is the registered-address fallback.
home_district AS (
  SELECT DISTINCT ON (patient_id)
    patient_id,
    home_district_code
  FROM (
    SELECT
      h.patient_id,
      LPAD(NULLIF(h.district::text, ''), 4, '0')            AS home_district_code,
      h.vstdate
    FROM bma_med.app1_homevisit h
    WHERE h.district IS NOT NULL
    UNION ALL
    SELECT
      h.patient_id,
      COALESCE(
        LPAD(NULLIF(h.district::text, ''), 4, '0'),
        LPAD(NULLIF(h.crdistrict, ''),     4, '0')
      )                                                     AS home_district_code,
      h.vstdate
    FROM bma_med.portal_homevisit h
    WHERE h.district IS NOT NULL OR h.crdistrict IS NOT NULL
  ) sub
  WHERE home_district_code IS NOT NULL
  ORDER BY patient_id, vstdate DESC NULLS LAST
),
-- 1c. self-reported chronic disease history (homehealth) — flag-or by patient.
--     Used to enrich found_* even when MSD criteria didn't fire on this visit.
self_history AS (
  SELECT patient_id,
         bool_or(NULLIF(dm, 0)     IS NOT NULL) AS hx_dm,
         bool_or(NULLIF(hpt, 0)    IS NOT NULL) AS hx_hpt,
         bool_or(NULLIF(stroke, 0) IS NOT NULL) AS hx_stroke,
         bool_or(NULLIF(chltr, 0)  IS NOT NULL) AS hx_dyslipidemia,
         bool_or(NULLIF(hrt, 0)    IS NOT NULL) AS hx_cvd,
         bool_or(NULLIF(kidney, 0) IS NOT NULL) AS hx_kidney
  FROM bma_med.app1_homehealth
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
self_history_portal AS (
  SELECT patient_id,
         bool_or(dm     IN ('1', 'true', 'TRUE'))      AS hx_dm,
         bool_or(hpt    IN ('1', 'true', 'TRUE'))      AS hx_hpt,
         bool_or(stroke IN ('1', 'true', 'TRUE'))      AS hx_stroke,
         bool_or(chltr  IN ('1', 'true', 'TRUE'))      AS hx_dyslipidemia,
         bool_or(hrt    IN ('1', 'true', 'TRUE'))      AS hx_cvd,
         bool_or(kidney IN ('1', 'true', 'TRUE'))      AS hx_kidney
  FROM bma_med.portal_homehealth
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
self_history_all AS (
  SELECT patient_id,
         bool_or(hx_dm)            AS hx_dm,
         bool_or(hx_hpt)           AS hx_hpt,
         bool_or(hx_stroke)        AS hx_stroke,
         bool_or(hx_dyslipidemia)  AS hx_dyslipidemia,
         bool_or(hx_cvd)           AS hx_cvd,
         bool_or(hx_kidney)        AS hx_kidney
  FROM (
    SELECT * FROM self_history
    UNION ALL
    SELECT * FROM self_history_portal
  ) u
  GROUP BY patient_id
),
all_visits AS (
  SELECT * FROM visit_app1
  UNION ALL
  SELECT * FROM visit_portal
),
-- 1d. Dedup chooser — within (patient, source, visit_date) keep one row.
--     `is_dedup_kept = TRUE` is the row consumers should aggregate over.
all_visits_ranked AS (
  SELECT
    v.*,
    ROW_NUMBER() OVER (
      PARTITION BY v.patient_id, v.source_code, v.visit_date
      ORDER BY (v.cancel_status = 0) DESC,
               v.facility_code NULLS LAST
    ) AS rn
  FROM all_visits v
)
SELECT
  (av.source_code || ':' || av.patient_id || ':' || av.visit_date)::text AS visit_uid,
  av.patient_id,
  av.source_code,
  av.visit_date,
  av.facility_code,
  p.sex_code,
  p.birthdate,
  CASE
    WHEN p.birthdate IS NULL THEN NULL
    ELSE EXTRACT(YEAR FROM age(av.visit_date, p.birthdate))::int
  END AS age_years,
  CASE
    WHEN p.birthdate IS NULL THEN 'unknown'
    WHEN EXTRACT(YEAR FROM age(av.visit_date, p.birthdate))::int BETWEEN 18 AND 29 THEN '18-29'
    WHEN EXTRACT(YEAR FROM age(av.visit_date, p.birthdate))::int BETWEEN 30 AND 44 THEN '30-44'
    WHEN EXTRACT(YEAR FROM age(av.visit_date, p.birthdate))::int BETWEEN 45 AND 59 THEN '45-59'
    WHEN EXTRACT(YEAR FROM age(av.visit_date, p.birthdate))::int BETWEEN 60 AND 74 THEN '60-74'
    WHEN EXTRACT(YEAR FROM age(av.visit_date, p.birthdate))::int >= 75 THEN '75+'
    ELSE 'unknown'
  END AS age_group,
  hd.home_district_code,
  av.cancel_status,
  (av.rn = 1) AS is_dedup_kept,
  COALESCE(av.risk_dm,     FALSE) AS risk_dm,
  COALESCE(av.risk_hpt,    FALSE) AS risk_hpt,
  COALESCE(av.risk_cvd,    FALSE) AS risk_cvd,
  COALESCE(av.risk_bmi,    FALSE) AS risk_bmi,
  COALESCE(av.risk_stroke, FALSE) AS risk_stroke,
  -- Promote post-test OR self-history to found_* (matches old EAV pivot)
  COALESCE(av.found_dm,           FALSE) OR COALESCE(sh.hx_dm,           FALSE) AS found_dm,
  COALESCE(av.found_hpt,          FALSE) OR COALESCE(sh.hx_hpt,          FALSE) AS found_hpt,
  COALESCE(av.found_cvd,          FALSE) OR COALESCE(sh.hx_cvd,          FALSE) AS found_cvd,
  COALESCE(av.found_obesity,      FALSE)                                        AS found_obesity,
  COALESCE(av.found_dyslipidemia, FALSE) OR COALESCE(sh.hx_dyslipidemia, FALSE) AS found_dyslipidemia,
  COALESCE(av.found_stroke,       FALSE) OR COALESCE(sh.hx_stroke,       FALSE) AS found_stroke,
  av.sbp, av.dbp, av.height_cm, av.weight_kg, av.waist_cm, av.bmi,
  av.phq9_total, av.st5_total
FROM all_visits_ranked av
LEFT JOIN bma_med.patient p     ON p.patient_id  = av.patient_id
LEFT JOIN home_district   hd    ON hd.patient_id = av.patient_id
LEFT JOIN self_history_all sh   ON sh.patient_id = av.patient_id
WITH NO DATA;

-- Indexes (matching the old EAV-backed MV — see migration 104)
CREATE UNIQUE INDEX uq_mv_visit_resolved
    ON public.mv_visit_resolved (visit_uid);
CREATE INDEX idx_mv_visit_resolved_dist
    ON public.mv_visit_resolved (home_district_code, visit_date);
CREATE INDEX idx_mv_visit_resolved_src
    ON public.mv_visit_resolved (source_code);
CREATE INDEX idx_mv_visit_resolved_patient
    ON public.mv_visit_resolved (patient_id, visit_date);
CREATE INDEX idx_mv_visit_resolved_facility
    ON public.mv_visit_resolved (facility_code);
CREATE INDEX idx_mv_visit_resolved_age_sex
    ON public.mv_visit_resolved (age_group, sex_code);

GRANT SELECT ON public.mv_visit_resolved TO bma_med_reader, bma_med_clinician, bma_med_loader;

COMMENT ON MATERIALIZED VIEW public.mv_visit_resolved IS
    'Foundation MV — UNION of bma_med.app1_vitalsignslf + portal_vitalsignslf, '
    'enriched with bma_med.patient (sex/age) + homevisit (district) + homehealth '
    '(self-reported history). All summary_* MVs/views depend on this. '
    'is_dedup_kept = TRUE marks the canonical row per (patient, source, vstdate).';


-- =============================================================================
-- 2. summary_district_disease — district × source × disease counts
--
--    36 API references in FRONTEND-API-MAP.md. Patient-level rollup: each
--    patient counted once per (district, source) using OR-aggregation across
--    their non-cancelled is_dedup_kept visits.
--
--    Schema matches migration 105's compat-VIEW so no router code changes.
-- =============================================================================

CREATE MATERIALIZED VIEW public.summary_district_disease AS
WITH patient_flags AS (
    SELECT
        vr.home_district_code AS district_code,
        vr.source_code        AS data_source,
        vr.patient_id,
        bool_or(COALESCE(vr.risk_dm,            FALSE)) AS risk_dm,
        bool_or(COALESCE(vr.risk_hpt,           FALSE)) AS risk_hpt,
        bool_or(COALESCE(vr.risk_cvd,           FALSE)) AS risk_cvd,
        bool_or(COALESCE(vr.risk_bmi,           FALSE)) AS risk_bmi,
        bool_or(COALESCE(vr.risk_stroke,        FALSE)) AS risk_stroke,
        bool_or(COALESCE(vr.found_dm,           FALSE)) AS found_dm,
        bool_or(COALESCE(vr.found_hpt,          FALSE)) AS found_hpt,
        bool_or(COALESCE(vr.found_cvd,          FALSE)) AS found_cvd,
        bool_or(COALESCE(vr.found_stroke,       FALSE)) AS found_stroke,
        bool_or(COALESCE(vr.found_obesity,      FALSE)) AS found_obesity,
        bool_or(COALESCE(vr.found_dyslipidemia, FALSE)) AS found_dyslipidemia
    FROM public.mv_visit_resolved vr
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.home_district_code IS NOT NULL
    GROUP BY vr.home_district_code, vr.source_code, vr.patient_id
)
SELECT
    data_source::text                                              AS data_source,
    district_code::text                                            AS district_code,
    COUNT(DISTINCT patient_id)::bigint                              AS total_screened,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)::bigint       AS risk_dm_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)::bigint      AS risk_hpt_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)::bigint      AS risk_cvd_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_bmi)::bigint      AS risk_bmi_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_stroke)::bigint   AS risk_stroke_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_dm)::bigint      AS found_dm_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_hpt)::bigint     AS found_hpt_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_cvd)::bigint     AS found_cvd_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_stroke)::bigint  AS found_stroke_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_obesity)::bigint AS found_obesity_count,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_dyslipidemia)::bigint
                                                                    AS found_dyslipidemia_count,
    ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)::numeric
                / NULLIF(COUNT(DISTINCT patient_id), 0)::numeric, 2) AS pct_risk_dm,
    ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)::numeric
                / NULLIF(COUNT(DISTINCT patient_id), 0)::numeric, 2) AS pct_risk_hpt,
    ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)::numeric
                / NULLIF(COUNT(DISTINCT patient_id), 0)::numeric, 2) AS pct_risk_cvd,
    ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE found_dm)::numeric
                / NULLIF(COUNT(DISTINCT patient_id), 0)::numeric, 2) AS pct_found_dm,
    ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE found_hpt)::numeric
                / NULLIF(COUNT(DISTINCT patient_id), 0)::numeric, 2) AS pct_found_hpt,
    ROUND(100.0 * COUNT(DISTINCT patient_id) FILTER (WHERE found_cvd)::numeric
                / NULLIF(COUNT(DISTINCT patient_id), 0)::numeric, 2) AS pct_found_cvd,
    NOW() AS refreshed_at
FROM patient_flags
GROUP BY data_source, district_code
HAVING COUNT(DISTINCT patient_id) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_summary_district_disease
    ON public.summary_district_disease (data_source, district_code);
CREATE INDEX idx_summary_district_disease_dc
    ON public.summary_district_disease (district_code);

GRANT SELECT ON public.summary_district_disease TO bma_med_reader, bma_med_clinician, bma_med_loader;

COMMENT ON MATERIALIZED VIEW public.summary_district_disease IS
    'Per (data_source × district): patient-level disease counts. Rolls up '
    'mv_visit_resolved is_dedup_kept rows; HAVING COUNT(DISTINCT patient_id) >= 5 '
    'enforces k-anonymity at the SQL layer.';


-- =============================================================================
-- 3. summary_facility — per-facility screening + diagnosis counts
--
--    9 API references. Each facility (hptcode) gets one row per source.
-- =============================================================================

CREATE MATERIALIZED VIEW public.summary_facility AS
WITH patient_facility AS (
    SELECT
        vr.facility_code,
        vr.source_code,
        vr.patient_id,
        vr.home_district_code,
        bool_or(COALESCE(vr.risk_dm,            FALSE)) AS risk_dm,
        bool_or(COALESCE(vr.risk_hpt,           FALSE)) AS risk_hpt,
        bool_or(COALESCE(vr.risk_cvd,           FALSE)) AS risk_cvd,
        bool_or(COALESCE(vr.risk_bmi,           FALSE)) AS risk_bmi,
        bool_or(COALESCE(vr.found_dm,           FALSE)) AS found_dm,
        bool_or(COALESCE(vr.found_hpt,          FALSE)) AS found_hpt,
        bool_or(COALESCE(vr.found_obesity,      FALSE)) AS found_obesity,
        bool_or(COALESCE(vr.found_dyslipidemia, FALSE)) AS found_dyslipidemia,
        MIN(vr.visit_date) AS first_visit,
        MAX(vr.visit_date) AS last_visit
    FROM public.mv_visit_resolved vr
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.facility_code IS NOT NULL
    GROUP BY vr.facility_code, vr.source_code, vr.patient_id, vr.home_district_code
)
SELECT
    facility_code::text                                            AS facility_code,
    source_code::text                                              AS data_source,
    -- Most-common district per facility (mode); legacy column kept for /admin
    MODE() WITHIN GROUP (ORDER BY home_district_code)::text        AS district_code,
    COUNT(DISTINCT patient_id)::bigint                              AS total_screened,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)::bigint       AS risk_dm,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)::bigint      AS risk_hpt,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)::bigint      AS risk_cvd,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_bmi)::bigint      AS risk_bmi,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_dm)::bigint      AS found_dm,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_hpt)::bigint     AS found_hpt,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_obesity)::bigint AS found_obesity,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_dyslipidemia)::bigint
                                                                    AS found_dyslipidemia,
    -- Lab completion: patients with any non-cancelled lab row
    (SELECT COUNT(DISTINCT le.patient_id)
       FROM (
         SELECT patient_id FROM bma_med.app1_labhealth   WHERE patient_id IS NOT NULL
           AND COALESCE(dup_pid_vstdate, 0) = 0
         UNION ALL
         SELECT patient_id FROM bma_med.portal_labhealth WHERE patient_id IS NOT NULL
           AND COALESCE(dup_pid_vstdate, 0) = 0
       ) le
       WHERE le.patient_id IN (SELECT patient_id FROM patient_facility pf2
                               WHERE pf2.facility_code = pf.facility_code)
    )::bigint                                                       AS lab_completed,
    MIN(first_visit)::date                                          AS first_screening,
    MAX(last_visit)::date                                           AS last_screening
FROM patient_facility pf
GROUP BY facility_code, source_code
HAVING COUNT(DISTINCT patient_id) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_summary_facility
    ON public.summary_facility (facility_code, data_source);
CREATE INDEX idx_summary_facility_district
    ON public.summary_facility (district_code);

GRANT SELECT ON public.summary_facility TO bma_med_reader, bma_med_clinician, bma_med_loader;

COMMENT ON MATERIALIZED VIEW public.summary_facility IS
    'Per (facility × source): patient-level screening + diagnosis counts. '
    'district_code = most-common home district of patients seen at this '
    'facility (mode). lab_completed = distinct patients with any lab row. '
    'k-anonymity gate at >= 5 distinct patients.';


-- =============================================================================
-- 4. summary_disease_age_sex — district × sex × age × disease cross-tab
--
--    8 API references. Includes 'all' rollup row per (district, age_group)
--    so the API can serve filter=sex=all without GROUP BY at query time.
--    Sex categories: 'M' (sex_code=10), 'F' (sex_code=20), 'unknown' (NULL).
-- =============================================================================

CREATE MATERIALIZED VIEW public.summary_disease_age_sex AS
WITH patient_dim AS (
    SELECT
        vr.home_district_code AS district_code,
        vr.patient_id,
        CASE vr.sex_code
            WHEN 10 THEN 'M'
            WHEN 20 THEN 'F'
            ELSE         'unknown'
        END                   AS sex_raw,
        vr.age_group,
        bool_or(COALESCE(vr.risk_dm,            FALSE)) AS risk_dm,
        bool_or(COALESCE(vr.risk_hpt,           FALSE)) AS risk_hpt,
        bool_or(COALESCE(vr.risk_cvd,           FALSE)) AS risk_cvd,
        bool_or(COALESCE(vr.risk_bmi,           FALSE)) AS risk_bmi,
        bool_or(COALESCE(vr.risk_stroke,        FALSE)) AS risk_stroke,
        bool_or(COALESCE(vr.found_dm,           FALSE)) AS found_dm,
        bool_or(COALESCE(vr.found_hpt,          FALSE)) AS found_hpt,
        bool_or(COALESCE(vr.found_cvd,          FALSE)) AS found_cvd,
        bool_or(COALESCE(vr.found_stroke,       FALSE)) AS found_stroke,
        bool_or(COALESCE(vr.found_obesity,      FALSE)) AS found_obesity,
        bool_or(COALESCE(vr.found_dyslipidemia, FALSE)) AS found_dyslipidemia
    FROM public.mv_visit_resolved vr
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.home_district_code IS NOT NULL
    GROUP BY vr.home_district_code, vr.patient_id, vr.sex_code, vr.age_group
),
sex_grain AS (
    -- Real per-sex rows
    SELECT district_code, sex_raw AS sex, age_group, patient_id,
           risk_dm, risk_hpt, risk_cvd, risk_bmi, risk_stroke,
           found_dm, found_hpt, found_cvd, found_stroke,
           found_obesity, found_dyslipidemia
    FROM patient_dim
    UNION ALL
    -- 'all' rollup row per (district, age_group)
    SELECT district_code, 'all'::text AS sex, age_group, patient_id,
           risk_dm, risk_hpt, risk_cvd, risk_bmi, risk_stroke,
           found_dm, found_hpt, found_cvd, found_stroke,
           found_obesity, found_dyslipidemia
    FROM patient_dim
)
SELECT
    district_code::text                                                  AS district_code,
    sex::text                                                            AS sex,
    age_group::text                                                      AS age_group,
    COUNT(DISTINCT patient_id)::bigint                                    AS total_screened,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)::bigint             AS risk_dm,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)::bigint            AS risk_hpt,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)::bigint            AS risk_cvd,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_bmi)::bigint            AS risk_bmi,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_stroke)::bigint         AS risk_stroke,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_dm)::bigint            AS found_dm,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_hpt)::bigint           AS found_hpt,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_cvd)::bigint           AS found_cvd,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_stroke)::bigint        AS found_stroke,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_obesity)::bigint       AS found_obesity,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_dyslipidemia)::bigint  AS found_dyslipidemia
FROM sex_grain
GROUP BY district_code, sex, age_group
HAVING COUNT(DISTINCT patient_id) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_summary_disease_age_sex
    ON public.summary_disease_age_sex (district_code, sex, age_group);

GRANT SELECT ON public.summary_disease_age_sex TO bma_med_reader, bma_med_clinician, bma_med_loader;

COMMENT ON MATERIALIZED VIEW public.summary_disease_age_sex IS
    'Per (district × sex × age_group): patient-level screening counts. '
    'sex=''all'' rows are pre-rolled. risk_* = pre-test, found_* = post-test. '
    'k-anonymity gate at >= 5 distinct patients.';


-- =============================================================================
-- COLD STUB VIEWS — return columns but always 0 rows (or 0 counts).
--
-- Purpose: the routers querying these endpoints don't 500 just because we
-- haven't ported the full aggregation yet. As demand for each endpoint
-- shows up in production, promote the stub here to a real MATERIALIZED VIEW
-- backed by the bma_med.* tables (use the hot-MV examples above as a model).
--
-- Each stub mirrors the column list of the legacy version so existing
-- router SQL keeps parsing and returns an empty result set.
-- =============================================================================

-- Stub 1. summary_district_risk_factors  (sex × age × smoke × exercise)
CREATE VIEW public.summary_district_risk_factors AS
SELECT
    NULL::text          AS district_code,
    NULL::int           AS sex,
    NULL::text          AS age_group,
    NULL::int           AS smoking,
    NULL::int           AS exercise,
    0::bigint           AS patient_count,
    NULL::numeric       AS avg_sbp,
    NULL::numeric       AS avg_dbp,
    NULL::numeric       AS avg_weight_kg,
    NULL::numeric       AS avg_waist_cm,
    NULL::numeric       AS avg_bmi
WHERE FALSE;
-- TODO(promote): aggregate from bma_med.app1_vitalsignslf + portal_vitalsignslf:
--   smoke (DOUBLE PRECISION), alcohal, height, weight, wstl, hbpn/lbpn,
--   bmi_calc; group by (home_district_code, sex_code, age_group, smoke,
--   excercise from app1_homehealth/portal_homehealth).
GRANT SELECT ON public.summary_district_risk_factors TO bma_med_reader, bma_med_clinician, bma_med_loader;


-- Stub 2. summary_district_lab  (avg lab values per district + anemia/CKD %)
CREATE VIEW public.summary_district_lab AS
SELECT
    NULL::text     AS district_code,
    0::bigint      AS total_lab_patients,
    NULL::numeric  AS avg_hemoglobin,
    NULL::numeric  AS avg_hematocrit,
    NULL::numeric  AS avg_fbs,
    NULL::numeric  AS avg_cholesterol,
    NULL::numeric  AS avg_triglyceride,
    NULL::numeric  AS avg_hdl,
    NULL::numeric  AS avg_ldl,
    NULL::numeric  AS avg_creatinine,
    NULL::numeric  AS avg_egfr,
    NULL::numeric  AS avg_uric_acid,
    NULL::numeric  AS avg_sgot,
    NULL::numeric  AS avg_sgpt,
    NULL::numeric  AS pct_anemia,
    NULL::numeric  AS pct_ckd
WHERE FALSE;
-- TODO(promote): from bma_med.app1_labhealth + portal_labhealth:
--   hmgb (hemoglobin), hmtc (hematocrit), fbs (numeric in app1, TEXT in portal),
--   cholest, trigly, hdl, ldl, crtinine, egfr_lab, egfrrs, uricacid, sgot, sgpt.
--   Note: portal lab columns are TEXT — cast with NULLIF + ::numeric.
--   Join to mv_visit_resolved on patient_id to get home_district_code.
GRANT SELECT ON public.summary_district_lab TO bma_med_reader, bma_med_clinician, bma_med_loader;


-- Stub 3. summary_district_mental  (PHQ-9, ST5, depression-2Q)
CREATE VIEW public.summary_district_mental AS
SELECT
    NULL::text     AS district_code,
    0::bigint      AS total_screened,
    NULL::numeric  AS pct_depression_risk,
    NULL::numeric  AS pct_phq9_moderate,
    NULL::numeric  AS pct_high_stress
WHERE FALSE;
-- TODO(promote): from mv_visit_resolved.phq9_total / st5_total + scr2q1/scr2q2
-- (depression 2Q) on app1_vitalsignslf + portal_vitalsignslf. Thresholds:
--   PHQ-9 >= 10 → moderate-or-worse depression
--   ST-5  >= 7  → high stress
--   2Q (scr2q1 OR scr2q2 >= 1) → depression-screen positive
GRANT SELECT ON public.summary_district_mental TO bma_med_reader, bma_med_clinician, bma_med_loader;


-- Stub 4. summary_district_demographics  (edu/occ/privilege/housing breakdown)
CREATE VIEW public.summary_district_demographics AS
SELECT
    NULL::text  AS district_code,
    0::bigint   AS total_respondents,
    0::bigint   AS edu_none,
    0::bigint   AS edu_primary,
    0::bigint   AS edu_secondary,
    0::bigint   AS edu_high_school,
    0::bigint   AS edu_vocational,
    0::bigint   AS edu_bachelor,
    0::bigint   AS edu_postgrad,
    0::bigint   AS occ_government,
    0::bigint   AS occ_private,
    0::bigint   AS occ_self_employed,
    0::bigint   AS occ_agriculture,
    0::bigint   AS occ_unemployed,
    0::bigint   AS occ_student,
    0::bigint   AS occ_retired,
    0::bigint   AS priv_ucs,
    0::bigint   AS priv_sso,
    0::bigint   AS priv_csmbs,
    0::bigint   AS priv_other,
    0::bigint   AS house_owned,
    0::bigint   AS house_rented,
    0::bigint   AS house_condo,
    0::bigint   AS house_other
WHERE FALSE;
-- TODO(promote): from bma_med.app1_homevisit / portal_homevisit:
--   edu (codebook 1-7), occptn (1-9), prvlg (1-99), hometype (1-5).
--   See CODES_REFERENCE.md for mapping codebook values → column buckets.
GRANT SELECT ON public.summary_district_demographics TO bma_med_reader, bma_med_clinician, bma_med_loader;


-- Stub 5. summary_lab_disease_cross  (lab × disease cross-tab)
CREATE VIEW public.summary_lab_disease_cross AS
SELECT
    NULL::text     AS district_code,
    NULL::text     AS lab_test,
    NULL::text     AS disease,
    0::bigint      AS total_count,
    NULL::numeric  AS pct,
    0::bigint      AS total_patients
WHERE FALSE;
-- TODO(promote): per (district, lab_test, disease) count distinct patients
-- where abnormal_lab AND found_disease. Lab thresholds:
--   fbs       >= 126 → abnormal (DM)
--   cholest   >= 200 → abnormal (dyslipidemia)
--   ldl       >= 130 → abnormal (dyslipidemia)
--   trigly    >= 150 → abnormal (dyslipidemia)
--   egfr_lab  <  60  → abnormal (CKD)
-- Diseases: 'diabetes' (found_dm), 'dyslipidemia' (found_dyslipidemia),
--           'kidney' (msd_kidney from labhealth).
GRANT SELECT ON public.summary_lab_disease_cross TO bma_med_reader, bma_med_clinician, bma_med_loader;


-- Stub 6. summary_bmi_waist  (BMI cat × waist-risk × sex per district)
CREATE VIEW public.summary_bmi_waist AS
SELECT
    NULL::text     AS district_code,
    NULL::int      AS sex,
    0::bigint      AS total_measured,
    0::bigint      AS bmi_underweight,
    0::bigint      AS bmi_normal,
    0::bigint      AS bmi_overweight,
    0::bigint      AS bmi_obese,
    0::bigint      AS bmi_severely_obese,
    NULL::numeric  AS avg_bmi,
    0::bigint      AS total_waist_measured,
    NULL::numeric  AS avg_waist,
    0::bigint      AS male_waist_risk,
    0::bigint      AS female_waist_risk,
    NULL::numeric  AS avg_height,
    NULL::numeric  AS avg_weight
WHERE FALSE;
-- TODO(promote): from mv_visit_resolved (bmi, height_cm, weight_kg, waist_cm,
-- sex_code, home_district_code). BMI cuts WHO Asia-Pacific:
--   <18.5 underweight, 18.5-22.99 normal, 23-24.99 overweight,
--   25-29.99 obese, >=30 severe.
-- Waist risk: male >= 90cm, female >= 80cm.
GRANT SELECT ON public.summary_bmi_waist TO bma_med_reader, bma_med_clinician, bma_med_loader;


-- Stub 7. summary_screening_tests  (counts of patients with each test type)
CREATE VIEW public.summary_screening_tests AS
SELECT
    NULL::text  AS district_code,
    0::bigint   AS chest_xray_count,
    0::bigint   AS chest_xray_abnormal,
    0::bigint   AS ekg_count,
    0::bigint   AS ekg_abnormal,
    0::bigint   AS cervical_count,
    0::bigint   AS cervical_abnormal,
    0::bigint   AS colon_count,
    0::bigint   AS colon_abnormal,
    0::bigint   AS lab_complete_count
WHERE FALSE;
-- TODO(promote): from app1_vitalsignslf (chest, ekg) + app1_labhealth /
-- portal_labhealth (cvcrs, clcrs). 1 = normal, 2 = abnormal per factsheet.
GRANT SELECT ON public.summary_screening_tests TO bma_med_reader, bma_med_clinician, bma_med_loader;


-- Stub 8. summary_chronic_history  (self-reported chronic + family history)
CREATE VIEW public.summary_chronic_history AS
SELECT
    NULL::text  AS district_code,
    0::bigint   AS total_respondents,
    0::bigint   AS hx_dm_count,
    0::bigint   AS hx_hpt_count,
    0::bigint   AS hx_stroke_count,
    0::bigint   AS hx_chltr_count,
    0::bigint   AS hx_hrt_count,
    0::bigint   AS hx_kidney_count,
    0::bigint   AS family_dm_count,
    0::bigint   AS family_hpt_count,
    0::bigint   AS family_stroke_count,
    0::bigint   AS family_hrt_count
WHERE FALSE;
-- TODO(promote): from bma_med.app1_homehealth + portal_homehealth:
--   self: dm/hpt/stroke/chltr/hrt/kidney columns
--   family (parent prefix): pdm, phpt, pstroke, phrtm
-- Group by mv_visit_resolved.home_district_code via patient_id join.
GRANT SELECT ON public.summary_chronic_history TO bma_med_reader, bma_med_clinician, bma_med_loader;


-- =============================================================================
-- REFRESH commands — run AFTER data load. Operator runs manually; commented
-- out so the migration is idempotent on first apply.
--
-- Refresh order matters: mv_visit_resolved must be populated first because
-- the 3 summary_* MVs SELECT from it. The stub VIEWs need no refresh.
--
-- After ingestion (`python /Users/dev/bma-med/ingest.py …`):
--
--   REFRESH MATERIALIZED VIEW public.mv_visit_resolved;
--   -- on subsequent loads (after the unique index is populated) use:
--   --   REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_visit_resolved;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.summary_district_disease;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.summary_facility;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.summary_disease_age_sex;
-- =============================================================================


-- =============================================================================
-- Summary
-- =============================================================================
-- HOT (real MATERIALIZED VIEWS, populated by REFRESH after data load):
--   public.mv_visit_resolved          — foundation; UNION app1+portal vital +
--                                        patient + homevisit (district) +
--                                        homehealth (self-history). Indexed
--                                        on visit_uid, district+date, source,
--                                        patient+date, facility, age+sex.
--   public.summary_district_disease   — district × source × disease counts;
--                                        36 API references; depends on
--                                        mv_visit_resolved.
--   public.summary_facility           — facility × source counts + lab
--                                        completion proxy; 9 API references;
--                                        depends on mv_visit_resolved
--                                        (and reads bma_med.*labhealth
--                                        directly for lab_completed).
--   public.summary_disease_age_sex    — district × sex × age × disease cross
--                                        with sex='all' rollup pre-rolled;
--                                        8 API references; depends on
--                                        mv_visit_resolved.
--
-- STUB (plain VIEW, returns 0 rows; promote when endpoint becomes hot):
--   public.summary_district_risk_factors  — sex × age × smoke × exercise
--   public.summary_district_lab           — avg lab values + anemia/CKD %
--   public.summary_district_mental        — PHQ-9 / ST-5 / 2Q rates
--   public.summary_district_demographics  — edu / occ / privilege / housing
--   public.summary_lab_disease_cross      — abnormal-lab × found-disease
--   public.summary_bmi_waist              — BMI cat × waist-risk × sex
--   public.summary_screening_tests        — chest XR / EKG / cervical / colon
--   public.summary_chronic_history        — self + family chronic-disease hx
--
-- Dependency graph:
--   mv_visit_resolved (root)
--   ├── summary_district_disease
--   ├── summary_facility           (also reads bma_med.*labhealth direct)
--   └── summary_disease_age_sex
--   (stub views have no MV dependency — they're WHERE FALSE placeholders)
--
-- K-anonymity: all 3 hot summary_* MVs include
--   `HAVING COUNT(DISTINCT patient_id) >= 5`
-- enforcing k-anon at the SQL layer in addition to the application-layer
-- gate in /Users/dev/bma-med/security/k_anon.py.
-- =============================================================================
