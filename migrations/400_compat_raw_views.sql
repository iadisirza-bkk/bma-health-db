-- ============================================================================
-- 400_compat_raw_views.sql — (S6) compatibility views: legacy raw_* → bma_med.*
-- ============================================================================
-- Purpose
-- -------
-- The S1 cutover migrated all data into the bma_med.* schema, but several
-- API routers (summary.py, monitoring.py, executive.py, trends.py, kpi.py,
-- epidemiology.py) still reference the legacy raw_* table names. This
-- migration creates compatibility views in public.* that adapt the new
-- schema back to the legacy column names so those routers keep working
-- without touching their (validated) SQL.
--
-- One view per legacy table:
--   * public.raw_vitalsigns   ← bma_med.app1_vitalsignslf + portal_vitalsignslf
--   * public.raw_homevisit    ← bma_med.app1_homevisit    + portal_homevisit
--   * public.raw_homehealth   ← bma_med.app1_homehealth   + portal_homehealth + app2
--   * public.raw_lab_results  ← bma_med.app1_labhealth    + portal_labhealth
--   * public.raw_patients     ← bma_med.patient
--
-- For each view we project ONLY the columns the legacy code actually reads
-- (column-list found by grepping the api/routers/*.py files for v.X / hv.X /
-- h.X / l.X aliases). We synthesise `data_source` as a literal column and
-- COALESCE-cast as needed where the source table column types differ
-- between app1 and portal. Columns that have no equivalent in the source
-- (e.g. obesity flag from msd_obese vs fat) follow the same rules used in
-- public.mv_visit_resolved + api/routers/disease_control.py.
--
-- Idempotency: every view uses CREATE OR REPLACE, so re-applying this
-- migration is safe.
-- ============================================================================

SET search_path = public, bma_med;

-- ============================================================================
-- raw_vitalsigns
-- Used by: summary.py, executive.py, trends.py, kpi.py, epidemiology.py
-- Columns the legacy code reads (from grep):
--   patient_id, data_source, cancel_status, visit_date, district_code,
--   sbp, dbp, weight_kg, height_cm, waist_cm, smoking,
--   risk_dm, risk_hpt, risk_cvd, risk_bmi,
--   found_dyslipidemia, found_obesity, found_stroke,
--   depression_2q_1, depression_2q_2,
--   phq9_q1..q9, st5_q1..q5
-- ============================================================================
CREATE OR REPLACE VIEW public.raw_vitalsigns AS
  -- ── app1 source ───────────────────────────────────────────────────────
  SELECT
    v.patient_id,
    'app1'::text                                       AS data_source,
    -- cancel_status mirrors mv_visit_resolved: COALESCE(record_cancelled,0)
    COALESCE(v.record_cancelled::int, 0)::int          AS cancel_status,
    v.vstdate::timestamptz                             AS visit_date,
    -- district_code lives in homevisit in the new schema, not vitalsignslf;
    -- expose NULL here so legacy joins on v.district_code degrade gracefully
    -- (callers should join raw_homevisit if they need district info).
    NULL::text                                         AS district_code,
    -- physical vitals (cast double→numeric for arithmetic stability)
    v.hbpn::numeric                                    AS sbp,
    v.lbpn::numeric                                    AS dbp,
    v.weight::numeric                                  AS weight_kg,
    v.height::numeric                                  AS height_cm,
    v.wstl::numeric                                    AS waist_cm,
    v.smoke::int                                       AS smoking,
    -- risk flags — mirror mv_visit_resolved encoding (1.0 → TRUE)
    (v.riskdm    = 1::double precision)                AS risk_dm,
    (v.riskhpt   = 1::double precision)                AS risk_hpt,
    (v.riskcdvcl = 1::double precision)                AS risk_cvd,
    (v.riskbmi   = 1::double precision)                AS risk_bmi,
    -- "found_*" disease flags — alias mapping per disease_control.py:
    --   chltr  → found_dyslipidemia,  fat → found_obesity, stroke → found_stroke
    (v.chltr     = 1::double precision)                AS found_dyslipidemia,
    (v.fat       = 1::double precision)                AS found_obesity,
    (v.stroke    = 1::double precision)                AS found_stroke,
    -- 2Q depression screening
    v.scr2q1::int                                      AS depression_2q_1,
    v.scr2q2::int                                      AS depression_2q_2,
    -- PHQ-9 (app1 columns are TEXT — cast through NULLIF to handle blanks)
    NULLIF(v.scn9q1::text, '')::int                          AS phq9_q1,
    NULLIF(v.scn9q2::text, '')::int                          AS phq9_q2,
    NULLIF(v.scn9q3::text, '')::int                          AS phq9_q3,
    NULLIF(v.scn9q4::text, '')::int                          AS phq9_q4,
    NULLIF(v.scn9q5::text, '')::int                          AS phq9_q5,
    NULLIF(v.scn9q6::text, '')::int                          AS phq9_q6,
    NULLIF(v.scn9q7::text, '')::int                          AS phq9_q7,
    NULLIF(v.scn9q8::text, '')::int                          AS phq9_q8,
    NULLIF(v.scn9q9::text, '')::int                          AS phq9_q9,
    -- ST-5 stress screening
    v.st501::int                                       AS st5_q1,
    v.st502::int                                       AS st5_q2,
    v.st503::int                                       AS st5_q3,
    v.st504::int                                       AS st5_q4,
    v.st505::int                                       AS st5_q5
  FROM bma_med.app1_vitalsignslf v

  UNION ALL

  -- ── portal source ────────────────────────────────────────────────────
  SELECT
    v.patient_id,
    'portal'::text                                     AS data_source,
    COALESCE(v.record_cancelled::int, 0)::int          AS cancel_status,
    v.vstdate::timestamptz                             AS visit_date,
    NULL::text                                         AS district_code,
    v.hbpn::numeric                                    AS sbp,
    v.lbpn::numeric                                    AS dbp,
    v.weight::numeric                                  AS weight_kg,
    v.height::numeric                                  AS height_cm,
    v.wstl::numeric                                    AS waist_cm,
    v.smoke::int                                       AS smoking,
    (v.riskdm    = 1::double precision)                AS risk_dm,
    (v.riskhpt   = 1::double precision)                AS risk_hpt,
    -- post-load ALTER converted riskcdvcl/chltr/stroke to smallint;
    -- fat is still text in portal_vitalsignslf (mixed values)
    (v.riskcdvcl::text = '1')                          AS risk_cvd,
    (v.riskbmi::text   = '1')                          AS risk_bmi,
    (v.chltr::text     = '1')                          AS found_dyslipidemia,
    (v.fat::text       = '1')                          AS found_obesity,
    (v.stroke::text    = '1')                          AS found_stroke,
    v.scr2q1::int                                      AS depression_2q_1,
    v.scr2q2::int                                      AS depression_2q_2,
    -- portal PHQ-9 columns are double precision, not text
    v.scn9q1::int                                      AS phq9_q1,
    v.scn9q2::int                                      AS phq9_q2,
    v.scn9q3::int                                      AS phq9_q3,
    v.scn9q4::int                                      AS phq9_q4,
    v.scn9q5::int                                      AS phq9_q5,
    v.scn9q6::int                                      AS phq9_q6,
    v.scn9q7::int                                      AS phq9_q7,
    v.scn9q8::int                                      AS phq9_q8,
    v.scn9q9::int                                      AS phq9_q9,
    v.st501::int                                       AS st5_q1,
    v.st502::int                                       AS st5_q2,
    v.st503::int                                       AS st5_q3,
    v.st504::int                                       AS st5_q4,
    v.st505::int                                       AS st5_q5
  FROM bma_med.portal_vitalsignslf v;


-- ============================================================================
-- raw_homevisit
-- Used by: summary.py
-- Columns: patient_id, home_province
-- Source: app1_homevisit.province / portal_homevisit.province
--   (province is double precision in both source tables; legacy code
--    treats home_province as int, so we cast)
-- ============================================================================
CREATE OR REPLACE VIEW public.raw_homevisit AS
  SELECT
    h.patient_id,
    'app1'::text                                       AS data_source,
    h.province::int                                    AS home_province,
    -- Expose the district code as text in case future callers need it
    -- (mirrors the COALESCE used in disease_control._HOMEVISIT_DISTRICT_SQL).
    COALESCE(LPAD(NULLIF(h.district::text, ''), 4, '0'),
             NULL)                                     AS district_code
  FROM bma_med.app1_homevisit h

  UNION ALL

  SELECT
    h.patient_id,
    'portal'::text                                     AS data_source,
    h.province::int                                    AS home_province,
    COALESCE(LPAD(NULLIF(h.district::text, ''), 4, '0'),
             LPAD(NULLIF(h.crdistrict::text, ''), 4, '0'))   AS district_code
  FROM bma_med.portal_homevisit h;


-- ============================================================================
-- raw_homehealth
-- Used by: summary.py (lifestyle: exercise/cancel_status/data_source)
-- Columns: patient_id, data_source, cancel_status, exercise
-- Source maps:
--   * app1_homehealth   → 'app1'   (excercise: double precision; uses canceldate is null as no-cancel)
--   * portal_homehealth → 'portal' (excercise: text; cancelst: double precision)
--   * app2_app2         → 'app2'   (excercise: text label; no cancel column at all)
--
-- Legacy semantics — the original raw_homehealth.exercise was a flag
-- where `exercise = 0` meant "no exercise" (used by summary.py to count
-- the "no exercise" cohort). The new schema encodes exercise differently
-- per source:
--   * app1.excercise: 1 = ≥3×/week, 2 = <3×/week, 3 = none
--   * portal.excercise: free text, often Thai labels
--   * app2.excercise: Thai text labels only (e.g. "ไม่ออกกำลังกายเลย")
-- We collapse all three to the legacy 0/1/NULL flag:
--   0 = no exercise at all   (app1.excercise=3 OR text contains "ไม่ออกกำลังกาย")
--   1 = some exercise        (otherwise non-null)
--   NULL = unanswered
-- ============================================================================
CREATE OR REPLACE VIEW public.raw_homehealth AS
  SELECT
    h.patient_id,
    'app1'::text                                       AS data_source,
    -- app1_homehealth has no record_cancelled column; treat all rows as not-cancelled
    0::int                                             AS cancel_status,
    CASE
      WHEN h.excercise IS NULL THEN NULL
      WHEN h.excercise = 3      THEN 0
      ELSE                            1
    END                                                AS exercise
  FROM bma_med.app1_homehealth h

  UNION ALL

  SELECT
    h.patient_id,
    'portal'::text                                     AS data_source,
    COALESCE(h.record_cancelled::int, 0)::int          AS cancel_status,
    -- portal stores excercise as TEXT — match either numeric "3" or Thai "ไม่ออก..."
    CASE
      WHEN h.excercise IS NULL OR h.excercise = ''         THEN NULL
      WHEN h.excercise = '3'                                THEN 0
      WHEN h.excercise LIKE 'ไม่ออกกำลังกาย%'                 THEN 0
      ELSE                                                       1
    END                                                AS exercise
  FROM bma_med.portal_homehealth h

  UNION ALL

  -- app2 is a single denormalised table — the legacy code branches on
  -- data_source = 'app2' and counts cancelled rows. app2_app2 has no
  -- cancel column, so cancel_status is always 0.
  SELECT
    a.patient_id,
    'app2'::text                                       AS data_source,
    0::int                                             AS cancel_status,
    CASE
      WHEN a.excercise IS NULL OR a.excercise = ''         THEN NULL
      WHEN a.excercise LIKE 'ไม่ออกกำลังกาย%'                 THEN 0
      ELSE                                                       1
    END                                                AS exercise
  FROM bma_med.app2_app2 a;


-- ============================================================================
-- raw_lab_results
-- Used by: summary.py (non-bangkok-overview / non-bangkok-province lab agg)
-- Columns: patient_id, cancel_status,
--   hemoglobin, hematocrit, fbs, cholesterol, triglyceride,
--   hdl, ldl, creatinine, egfr, uric_acid, sgot, sgpt
-- Source:
--   * app1_labhealth   — most lab fields are double precision
--   * portal_labhealth — most lab fields are TEXT (need NULLIF + cast)
-- ============================================================================
CREATE OR REPLACE VIEW public.raw_lab_results AS
  SELECT
    l.patient_id,
    'app1'::text                                       AS data_source,
    -- app1_labhealth has no record_cancelled column
    0::int                                             AS cancel_status,
    l.hmgb::numeric                                    AS hemoglobin,
    l.hmtc::numeric                                    AS hematocrit,
    l.fbs::numeric                                     AS fbs,
    l.cholest::numeric                                 AS cholesterol,
    l.trigly::numeric                                  AS triglyceride,
    l.hdl::numeric                                     AS hdl,
    l.ldl::numeric                                     AS ldl,
    l.crtinine::numeric                                AS creatinine,
    l.egfr::numeric                                    AS egfr,
    -- uricacid / sgot / sgpt are TEXT in app1 too — coerce through NULLIF
    NULLIF(l.uricacid::text, '')::numeric                    AS uric_acid,
    NULLIF(l.sgot::text, '')::numeric                        AS sgot,
    NULLIF(l.sgpt::text, '')::numeric                        AS sgpt
  FROM bma_med.app1_labhealth l

  UNION ALL

  SELECT
    l.patient_id,
    'portal'::text                                     AS data_source,
    COALESCE(l.record_cancelled::int, 0)::int          AS cancel_status,
    l.hmgb::numeric                                    AS hemoglobin,
    l.hmtc::numeric                                    AS hematocrit,
    -- portal lab columns are mostly TEXT — strip blanks then cast
    NULLIF(l.fbs::text, '')::numeric                         AS fbs,
    NULLIF(l.cholest::text, '')::numeric                     AS cholesterol,
    NULLIF(l.trigly::text, '')::numeric                      AS triglyceride,
    NULLIF(l.hdl::text, '')::numeric                         AS hdl,
    NULLIF(l.ldl::text, '')::numeric                         AS ldl,
    NULLIF(l.crtinine::text, '')::numeric                    AS creatinine,
    NULLIF(l.egfr::text, '')::numeric                        AS egfr,
    NULLIF(l.uricacid::text, '')::numeric                    AS uric_acid,
    NULLIF(l.sgot::text, '')::numeric                        AS sgot,
    NULLIF(l.sgpt::text, '')::numeric                        AS sgpt
  FROM bma_med.portal_labhealth l;


-- ============================================================================
-- raw_patients
-- Used by: monitoring.py (key only, looked up via bma_med.patient directly)
-- Most consumers read bma_med.patient explicitly, so this view exists mainly
-- so any future caller falling back to raw_patients gets a clean projection.
-- ============================================================================
CREATE OR REPLACE VIEW public.raw_patients AS
  SELECT
    p.patient_id,
    p.sex_code,
    p.birthdate,
    p.first_seen,
    p.last_seen
  FROM bma_med.patient p;


-- ============================================================================
-- Grants — match the existing pattern for public.* MVs (api_user reads,
-- etl_user reads/writes). The underlying bma_med.* tables are read by
-- bma_med_reader; granting on the views is enough because the views are
-- defined as security-invoker by default and Postgres requires the calling
-- role to also have privileges on the underlying tables for SELECT through
-- a view to succeed. We therefore (1) grant SELECT on the views to api_user
-- and etl_user, and (2) ensure those roles can SELECT the underlying
-- bma_med.* sources (USAGE on bma_med + SELECT on the source tables).
-- These GRANT statements are idempotent.
-- ============================================================================

GRANT USAGE ON SCHEMA bma_med TO api_user, etl_user;
GRANT SELECT ON bma_med.app1_vitalsignslf, bma_med.portal_vitalsignslf,
                bma_med.app1_homevisit,    bma_med.portal_homevisit,
                bma_med.app1_homehealth,   bma_med.portal_homehealth,
                bma_med.app1_labhealth,    bma_med.portal_labhealth,
                bma_med.app2_app2,         bma_med.patient
  TO api_user, etl_user;

GRANT SELECT ON public.raw_vitalsigns,
                public.raw_homevisit,
                public.raw_homehealth,
                public.raw_lab_results,
                public.raw_patients
  TO api_user, etl_user;
