-- =============================================================================
-- Migration 112 — Promote 4 stub views to real materialized views
-- =============================================================================
-- Background:
--   Migration 105 dropped the legacy summary_disease_age_sex,
--   summary_lab_disease_cross, summary_comorbidity, and summary_disease_control
--   MVs and replaced them with empty placeholder VIEWs returning 0 rows
--   (`SELECT ... WHERE false`). This migration promotes them to real
--   materialised views computed from public.mv_visit_resolved (refreshed
--   nightly by refresh_all_mvs()).
--
-- Endpoints unblocked:
--   /api/v2/epidemiology/age-group-prevalence  (summary_disease_age_sex)
--   /api/v2/epidemiology/age-pyramid           (summary_disease_age_sex)
--   /api/v2/epidemiology/disease-lab-crosstab  (summary_lab_disease_cross)
--   /api/v2/epidemiology/multi-disease-matrix  (summary_comorbidity)
--   /api/v2/research/statistical-test          (summary_disease_age_sex)
--   /api/v2/kpi/control-rates                  (summary_disease_control)
--
-- Conventions used in this migration (see per-MV comments for detail):
--   * "Patient-level" rollup: each patient counted once per district,
--     using OR-aggregation across all their non-cancelled visits in
--     mv_visit_resolved.is_dedup_kept = TRUE rows.
--   * "found_*" flags = clinical positive on screening (post-test).
--     "risk_*"  flags = pre-test risk-questionnaire positive.
--     The matrices/comorbidity views use found_* (confirmed disease)
--     for AND combinations and risk_* for the screening pyramid (where
--     "risk" maps to a person flagged for further investigation).
--   * Sex categories: 'M', 'F', 'unknown' for actual rows + 'all' aggregate.
--     Patients with NULL private.patient.sex_code → 'unknown'.
--   * Age groups: derived from (current_year - birth_year) using buckets
--     '18-29','30-44','45-59','60-74','75+','unknown' (NULL birth_year →
--     'unknown'). current_year fixed at extract(year FROM CURRENT_DATE).
-- =============================================================================

-- Safety: drop placeholder VIEWs (CASCADE because they may have grants attached).
DROP VIEW IF EXISTS public.summary_disease_age_sex   CASCADE;
DROP VIEW IF EXISTS public.summary_lab_disease_cross CASCADE;
DROP VIEW IF EXISTS public.summary_comorbidity       CASCADE;
DROP VIEW IF EXISTS public.summary_disease_control   CASCADE;
-- In case any prior migration left an MV with the same name:
DROP MATERIALIZED VIEW IF EXISTS public.summary_disease_age_sex   CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_lab_disease_cross CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_comorbidity       CASCADE;
DROP MATERIALIZED VIEW IF EXISTS public.summary_disease_control   CASCADE;


-- =============================================================================
-- 1. summary_disease_age_sex
--    Per (district, sex, age_group): COUNT(DISTINCT patient_id) for risk_*,
--    found_* flags. Includes a 'sex=all' rollup row per (district, age_group)
--    so the API can serve filter=sex=all without GROUP BY at query time.
-- =============================================================================

CREATE MATERIALIZED VIEW public.summary_disease_age_sex AS
WITH patient_dim AS (
    -- Per-patient: district + demographic dims + OR-aggregated risk/found flags
    SELECT
        vr.home_district_code AS district_code,
        vr.patient_id,
        COALESCE(p.sex_code, 'unknown')          AS sex_raw,
        CASE
            WHEN p.birth_year IS NULL THEN 'unknown'
            WHEN extract(year FROM CURRENT_DATE)::int - p.birth_year BETWEEN 18 AND 29 THEN '18-29'
            WHEN extract(year FROM CURRENT_DATE)::int - p.birth_year BETWEEN 30 AND 44 THEN '30-44'
            WHEN extract(year FROM CURRENT_DATE)::int - p.birth_year BETWEEN 45 AND 59 THEN '45-59'
            WHEN extract(year FROM CURRENT_DATE)::int - p.birth_year BETWEEN 60 AND 74 THEN '60-74'
            WHEN extract(year FROM CURRENT_DATE)::int - p.birth_year >= 75              THEN '75+'
            ELSE 'unknown'
        END AS age_group,
        bool_or(COALESCE(vr.risk_dm,           false)) AS risk_dm,
        bool_or(COALESCE(vr.risk_hpt,          false)) AS risk_hpt,
        bool_or(COALESCE(vr.risk_cvd,          false)) AS risk_cvd,
        bool_or(COALESCE(vr.risk_bmi,          false)) AS risk_bmi,
        bool_or(COALESCE(vr.risk_stroke,       false)) AS risk_stroke,
        bool_or(COALESCE(vr.found_dm,          false)) AS found_dm,
        bool_or(COALESCE(vr.found_hpt,         false)) AS found_hpt,
        bool_or(COALESCE(vr.found_cvd,         false)) AS found_cvd,
        bool_or(COALESCE(vr.found_stroke,      false)) AS found_stroke,
        bool_or(COALESCE(vr.found_obesity,     false)) AS found_obesity,
        bool_or(COALESCE(vr.found_dyslipidemia,false)) AS found_dyslipidemia
    FROM public.mv_visit_resolved vr
    LEFT JOIN private.patient p ON p.id = vr.patient_id AND NOT p.is_erased
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.home_district_code IS NOT NULL
    GROUP BY vr.home_district_code, vr.patient_id, p.sex_code, p.birth_year
),
sex_grain AS (
    -- Real per-sex rows
    SELECT district_code, sex_raw AS sex, age_group,
           patient_id,
           risk_dm, risk_hpt, risk_cvd, risk_bmi, risk_stroke,
           found_dm, found_hpt, found_cvd, found_stroke, found_obesity, found_dyslipidemia
      FROM patient_dim
    UNION ALL
    -- 'all' rollup row per (district, age_group)
    SELECT district_code, 'all'::text AS sex, age_group,
           patient_id,
           risk_dm, risk_hpt, risk_cvd, risk_bmi, risk_stroke,
           found_dm, found_hpt, found_cvd, found_stroke, found_obesity, found_dyslipidemia
      FROM patient_dim
)
SELECT
    district_code::varchar(4)  AS district_code,
    sex::varchar(20)           AS sex,
    age_group::varchar(20)     AS age_group,
    COUNT(DISTINCT patient_id)::bigint                                       AS total_screened,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)::bigint                 AS risk_dm,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)::bigint                AS risk_hpt,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)::bigint                AS risk_cvd,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_bmi)::bigint                AS risk_bmi,
    COUNT(DISTINCT patient_id) FILTER (WHERE risk_stroke)::bigint             AS risk_stroke,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_dm)::bigint                AS found_dm,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_hpt)::bigint               AS found_hpt,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_cvd)::bigint               AS found_cvd,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_stroke)::bigint            AS found_stroke,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_obesity)::bigint           AS found_obesity,
    COUNT(DISTINCT patient_id) FILTER (WHERE found_dyslipidemia)::bigint      AS found_dyslipidemia
FROM sex_grain
GROUP BY district_code, sex, age_group;

CREATE UNIQUE INDEX uq_summary_disease_age_sex
    ON public.summary_disease_age_sex (district_code, sex, age_group);

GRANT SELECT ON public.summary_disease_age_sex TO bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.summary_disease_age_sex IS
    'Per-district (sex × age-group) screening counts. Sex=''all'' rows are pre-rolled. '
    'risk_* = pre-test screening flags, found_* = post-test confirmed flags. '
    'Sources: public.mv_visit_resolved + private.patient. Refresh nightly.';


-- =============================================================================
-- 2. summary_lab_disease_cross
--    5 lab tests × 2 diseases = 10 rows per district, with abnormal-and-disease
--    counts and the % of district's lab-tested population.
--
--    Threshold table (clinical cutoffs commonly cited in Thai NCD screening):
--       fbs                ≥ 126 mg/dL  → abnormal (fasting hyperglycaemia)
--       hba1c              ≥ 6.5  %     → abnormal (HbA1c not yet in dataset; row will be 0/0)
--       total_cholesterol  ≥ 200 mg/dL  → abnormal (borderline-high)
--       ldl                ≥ 130 mg/dL  → abnormal (borderline-high)
--       triglyceride       ≥ 150 mg/dL  → abnormal (borderline-high)
--    Diseases: 'diabetes' (found_dm), 'dyslipidemia' (found_dyslipidemia)
--    pct = total_count / district_lab_patients * 100
-- =============================================================================

CREATE MATERIALIZED VIEW public.summary_lab_disease_cross AS
WITH
-- Patient × district mapping (one row per (patient,district))
patient_district AS (
    SELECT DISTINCT vr.patient_id, vr.home_district_code AS district_code
    FROM public.mv_visit_resolved vr
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.home_district_code IS NOT NULL
),
-- Disease flags collapsed per (patient, district) via OR
patient_flags AS (
    SELECT vr.patient_id, vr.home_district_code AS district_code,
           bool_or(COALESCE(vr.found_dm,           false)) AS found_dm,
           bool_or(COALESCE(vr.found_dyslipidemia, false)) AS found_dyslipidemia
    FROM public.mv_visit_resolved vr
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.home_district_code IS NOT NULL
    GROUP BY vr.patient_id, vr.home_district_code
),
-- Most-recent lab value per (patient, variable_key) — labs live in lab_event/lab_measurement
patient_lab AS (
    SELECT
        le.patient_id,
        vd.variable_key,
        lm.value_number AS lab_value,
        ROW_NUMBER() OVER (
            PARTITION BY le.patient_id, vd.variable_key
            ORDER BY le.lab_date DESC NULLS LAST, le.id DESC
        ) AS rn
    FROM private.lab_event le
    JOIN private.lab_measurement lm ON lm.lab_id = le.id
    JOIN private.variable_definition vd ON vd.id = lm.variable_id
    WHERE le.cancel_status IS DISTINCT FROM 1
      AND lm.value_number IS NOT NULL
      AND vd.variable_key IN ('fbs','hba1c','total_cholesterol','ldl','triglyceride')
),
patient_lab_latest AS (
    SELECT patient_id, variable_key, lab_value
    FROM patient_lab WHERE rn = 1
),
-- Patients × district with a given lab test (tot lab population per district)
district_lab_pop AS (
    SELECT pd.district_code,
           COUNT(DISTINCT pd.patient_id) FILTER (
               WHERE EXISTS (SELECT 1 FROM patient_lab_latest pll WHERE pll.patient_id = pd.patient_id)
           ) AS lab_pop_any
    FROM patient_district pd
    GROUP BY pd.district_code
),
-- Per (district, lab_test) cohort size = patients with that lab measured
district_lab_cohort AS (
    SELECT pd.district_code,
           pll.variable_key AS lab_test,
           COUNT(DISTINCT pd.patient_id) AS lab_cohort
    FROM patient_district pd
    JOIN patient_lab_latest pll ON pll.patient_id = pd.patient_id
    GROUP BY pd.district_code, pll.variable_key
),
-- Cross-tab body: count distinct patients per (district, lab_test, disease) where
-- abnormal_lab AND disease_flag both true
crosstab AS (
    SELECT
        pd.district_code,
        pll.variable_key AS lab_test,
        d.disease,
        COUNT(DISTINCT pd.patient_id) FILTER (
            WHERE
                CASE pll.variable_key
                    WHEN 'fbs'               THEN pll.lab_value >= 126
                    WHEN 'hba1c'             THEN pll.lab_value >= 6.5
                    WHEN 'total_cholesterol' THEN pll.lab_value >= 200
                    WHEN 'ldl'               THEN pll.lab_value >= 130
                    WHEN 'triglyceride'      THEN pll.lab_value >= 150
                    ELSE false
                END
                AND CASE d.disease
                    WHEN 'diabetes'     THEN pf.found_dm
                    WHEN 'dyslipidemia' THEN pf.found_dyslipidemia
                    ELSE false
                END
        ) AS total_count
    FROM patient_district pd
    JOIN patient_flags    pf  ON pf.patient_id  = pd.patient_id AND pf.district_code = pd.district_code
    JOIN patient_lab_latest pll ON pll.patient_id = pd.patient_id
    CROSS JOIN (VALUES ('diabetes'), ('dyslipidemia')) AS d(disease)
    GROUP BY pd.district_code, pll.variable_key, d.disease
)
SELECT
    ct.district_code::varchar(4)        AS district_code,
    ct.lab_test::varchar(50)            AS lab_test,
    ct.disease::varchar(50)             AS disease,
    ct.total_count::bigint              AS total_count,
    ROUND(
        100.0 * ct.total_count
        / NULLIF(dlp.lab_pop_any, 0)::numeric
    , 2)::numeric                       AS pct,
    -- total_patients = denominator cohort (patients in this district who had
    -- THIS lab measured). Exposed so /epidemiology/disease-lab-crosstab can
    -- gate rows with K-anonymity (router reads `total_patients`).
    COALESCE(dlc.lab_cohort, 0)::bigint AS total_patients
FROM crosstab ct
LEFT JOIN district_lab_pop    dlp ON dlp.district_code = ct.district_code
LEFT JOIN district_lab_cohort dlc ON dlc.district_code = ct.district_code AND dlc.lab_test = ct.lab_test;

CREATE UNIQUE INDEX uq_summary_lab_disease_cross
    ON public.summary_lab_disease_cross (district_code, lab_test, disease);

GRANT SELECT ON public.summary_lab_disease_cross TO bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.summary_lab_disease_cross IS
    'Per-district lab × disease cross-tab. Counts patients with abnormal lab '
    'AND positive disease flag; pct = % of district''s lab-tested cohort. '
    'Lab thresholds: fbs≥126, hba1c≥6.5, chol≥200, ldl≥130, tg≥150. '
    'Diseases: diabetes (found_dm), dyslipidemia (found_dyslipidemia).';


-- =============================================================================
-- 3. summary_comorbidity
--    Patient-level: roll up multi-disease combinations per district.
--
--    Convention: uses found_* flags (post-test confirmed disease) so e.g.
--    "dm_and_hpt" means patients confirmed with BOTH diabetes AND hypertension
--    on screening. CVD/stroke don't have widely-populated risk flags so we
--    use found_cvd / found_stroke too.
--
--    metabolic_syndrome simplification: ≥3 of {found_dm, found_hpt,
--    found_obesity, found_dyslipidemia}. (Not the full ATP-III definition,
--    which needs HDL+TG+waist circumference; this is a documented proxy.)
--
--    no_disease: patient where ALL six found_* flags are false.
--    multi_disease_count: patients with ≥2 of {found_dm, found_hpt, found_cvd,
--    found_stroke, found_obesity, found_dyslipidemia}.
-- =============================================================================

CREATE MATERIALIZED VIEW public.summary_comorbidity AS
WITH patient_flags AS (
    SELECT
        vr.home_district_code AS district_code,
        vr.patient_id,
        bool_or(COALESCE(vr.found_dm,           false)) AS dm,
        bool_or(COALESCE(vr.found_hpt,          false)) AS hpt,
        bool_or(COALESCE(vr.found_cvd,          false)) AS cvd,
        bool_or(COALESCE(vr.found_stroke,       false)) AS stroke,
        bool_or(COALESCE(vr.found_obesity,      false)) AS obesity,
        bool_or(COALESCE(vr.found_dyslipidemia, false)) AS dyslipidemia
    FROM public.mv_visit_resolved vr
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.home_district_code IS NOT NULL
    GROUP BY vr.home_district_code, vr.patient_id
),
patient_with_count AS (
    SELECT pf.*,
           (dm::int + hpt::int + cvd::int + stroke::int
              + obesity::int + dyslipidemia::int)            AS n_diseases,
           (dm::int + hpt::int + obesity::int + dyslipidemia::int) AS n_metabolic
    FROM patient_flags pf
)
SELECT
    district_code::varchar(4)                                          AS district_code,
    COUNT(DISTINCT patient_id)::bigint                                  AS total_screened,
    -- Single-disease (exclusive) counts
    COUNT(DISTINCT patient_id) FILTER (WHERE  dm AND NOT hpt AND NOT obesity)
                                                ::bigint                AS dm_only,
    COUNT(DISTINCT patient_id) FILTER (WHERE  hpt AND NOT dm AND NOT obesity)
                                                ::bigint                AS hpt_only,
    COUNT(DISTINCT patient_id) FILTER (WHERE  obesity AND NOT dm AND NOT hpt)
                                                ::bigint                AS obesity_only,
    -- Two-way co-occurrences (NOT mutually exclusive — overlap intentional)
    COUNT(DISTINCT patient_id) FILTER (WHERE  dm  AND hpt)::bigint      AS dm_and_hpt,
    COUNT(DISTINCT patient_id) FILTER (WHERE  dm  AND obesity)::bigint  AS dm_and_obesity,
    COUNT(DISTINCT patient_id) FILTER (WHERE  dm  AND dyslipidemia)::bigint
                                                                        AS dm_and_dyslipidemia,
    COUNT(DISTINCT patient_id) FILTER (WHERE  hpt AND obesity)::bigint  AS hpt_and_obesity,
    COUNT(DISTINCT patient_id) FILTER (WHERE  hpt AND dyslipidemia)::bigint
                                                                        AS hpt_and_dyslipidemia,
    COUNT(DISTINCT patient_id) FILTER (WHERE  cvd AND stroke)::bigint   AS cvd_and_stroke,
    COUNT(DISTINCT patient_id) FILTER (WHERE  dm  AND cvd)::bigint      AS dm_and_cvd,
    -- Metabolic syndrome proxy: ≥3 of {dm,hpt,obesity,dyslipidemia}
    COUNT(DISTINCT patient_id) FILTER (WHERE n_metabolic >= 3)::bigint  AS metabolic_syndrome,
    -- Triple
    COUNT(DISTINCT patient_id) FILTER (WHERE  dm  AND hpt AND obesity)::bigint
                                                                        AS dm_hpt_obesity,
    -- Multi (≥2 of any 6 NCDs)
    COUNT(DISTINCT patient_id) FILTER (WHERE n_diseases >= 2)::bigint   AS multi_disease_count,
    -- Healthy (none of the 6 NCDs flagged)
    COUNT(DISTINCT patient_id) FILTER (WHERE n_diseases = 0)::bigint    AS no_disease
FROM patient_with_count
GROUP BY district_code;

CREATE UNIQUE INDEX uq_summary_comorbidity
    ON public.summary_comorbidity (district_code);

GRANT SELECT ON public.summary_comorbidity TO bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.summary_comorbidity IS
    'Per-district disease co-occurrence counts. Uses found_* flags. '
    '*_only columns are mutually exclusive (no overlap with hpt/dm/obesity); '
    '*_and_* columns count overlapping patients (NOT mutually exclusive). '
    'metabolic_syndrome = ≥3 of {dm,hpt,obesity,dyslipidemia} (proxy, not '
    'full ATP-III). no_disease = none of 6 NCDs flagged.';


-- =============================================================================
-- 4. summary_disease_control
--    Per-district disease-control rates.
--      lab_patients   = distinct patients in district with ANY lab measurement
--                       (FBS, cholesterol, ldl, triglyceride, hdl) — proxy for
--                       lab-tested cohort.
--      dm_with_lab    = found_dm patients with a recorded FBS value.
--      dm_controlled  = of those, most-recent FBS < 126 mg/dL.
--      hpt_with_bp    = found_hpt patients with a recorded SBP measurement.
--      hpt_controlled = of those, most-recent SBP < 140 AND most-recent DBP < 90.
--
--    "Most-recent" uses lab_event.lab_date for labs and visit_event.visit_date
--    for vitals. Ties broken by id DESC.
-- =============================================================================

CREATE MATERIALIZED VIEW public.summary_disease_control AS
WITH
-- Patient × district (de-duped via mv_visit_resolved)
patient_district AS (
    SELECT DISTINCT vr.patient_id, vr.home_district_code AS district_code
    FROM public.mv_visit_resolved vr
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.home_district_code IS NOT NULL
),
-- Disease flags per (patient, district)
patient_flags AS (
    SELECT vr.patient_id, vr.home_district_code AS district_code,
           bool_or(COALESCE(vr.found_dm,  false)) AS found_dm,
           bool_or(COALESCE(vr.found_hpt, false)) AS found_hpt
    FROM public.mv_visit_resolved vr
    WHERE vr.is_dedup_kept = TRUE
      AND vr.cancel_status IS DISTINCT FROM 1
      AND vr.home_district_code IS NOT NULL
    GROUP BY vr.patient_id, vr.home_district_code
),
-- Most-recent FBS per patient (labs are in lab_event/lab_measurement)
patient_fbs AS (
    SELECT le.patient_id, lm.value_number AS fbs,
           ROW_NUMBER() OVER (
               PARTITION BY le.patient_id
               ORDER BY le.lab_date DESC NULLS LAST, le.id DESC
           ) AS rn
    FROM private.lab_event le
    JOIN private.lab_measurement lm ON lm.lab_id = le.id
    JOIN private.variable_definition vd ON vd.id = lm.variable_id
    WHERE le.cancel_status IS DISTINCT FROM 1
      AND vd.variable_key = 'fbs'
      AND lm.value_number IS NOT NULL
),
patient_fbs_latest AS (
    SELECT patient_id, fbs FROM patient_fbs WHERE rn = 1
),
-- Patients with ANY lab measurement at all (lab_pop)
patient_any_lab AS (
    SELECT DISTINCT le.patient_id
    FROM private.lab_event le
    JOIN private.lab_measurement lm ON lm.lab_id = le.id
    WHERE le.cancel_status IS DISTINCT FROM 1
      AND lm.value_number IS NOT NULL
),
-- Most-recent SBP/DBP per patient (vitals are in visit_event/visit_measurement)
patient_bp AS (
    SELECT
        ve.patient_id,
        MAX(vm.value_number) FILTER (WHERE vd.variable_key = 'sbp') AS sbp,
        MAX(vm.value_number) FILTER (WHERE vd.variable_key = 'dbp') AS dbp,
        ve.visit_date,
        ROW_NUMBER() OVER (
            PARTITION BY ve.patient_id
            ORDER BY ve.visit_date DESC NULLS LAST, ve.id DESC
        ) AS rn
    FROM private.visit_event ve
    JOIN private.visit_measurement vm ON vm.visit_id = ve.id
    JOIN private.variable_definition vd ON vd.id = vm.variable_id
    WHERE ve.cancel_status IS DISTINCT FROM 1
      AND vd.variable_key IN ('sbp','dbp')
      AND vm.value_number IS NOT NULL
    GROUP BY ve.patient_id, ve.id, ve.visit_date
),
patient_bp_latest AS (
    -- pick the latest visit that has at least an SBP measurement
    SELECT patient_id, sbp, dbp
    FROM patient_bp
    WHERE rn = 1 AND sbp IS NOT NULL
)
SELECT
    pd.district_code::varchar(4)                                                    AS district_code,
    COUNT(DISTINCT pd.patient_id) FILTER (
        WHERE EXISTS (SELECT 1 FROM patient_any_lab pal WHERE pal.patient_id = pd.patient_id)
    )::bigint                                                                        AS lab_patients,
    COUNT(DISTINCT pd.patient_id) FILTER (
        WHERE pf.found_dm
          AND EXISTS (SELECT 1 FROM patient_fbs_latest pfl WHERE pfl.patient_id = pd.patient_id)
    )::bigint                                                                        AS dm_with_lab,
    COUNT(DISTINCT pd.patient_id) FILTER (
        WHERE pf.found_dm
          AND EXISTS (
              SELECT 1 FROM patient_fbs_latest pfl
              WHERE pfl.patient_id = pd.patient_id AND pfl.fbs < 126
          )
    )::bigint                                                                        AS dm_controlled,
    COUNT(DISTINCT pd.patient_id) FILTER (
        WHERE pf.found_hpt
          AND EXISTS (SELECT 1 FROM patient_bp_latest pbl WHERE pbl.patient_id = pd.patient_id)
    )::bigint                                                                        AS hpt_with_bp,
    COUNT(DISTINCT pd.patient_id) FILTER (
        WHERE pf.found_hpt
          AND EXISTS (
              SELECT 1 FROM patient_bp_latest pbl
              WHERE pbl.patient_id = pd.patient_id
                AND pbl.sbp IS NOT NULL AND pbl.sbp < 140
                AND pbl.dbp IS NOT NULL AND pbl.dbp < 90
          )
    )::bigint                                                                        AS hpt_controlled
FROM patient_district pd
LEFT JOIN patient_flags pf
    ON pf.patient_id = pd.patient_id AND pf.district_code = pd.district_code
GROUP BY pd.district_code;

CREATE UNIQUE INDEX uq_summary_disease_control
    ON public.summary_disease_control (district_code);

GRANT SELECT ON public.summary_disease_control TO bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.summary_disease_control IS
    'Per-district disease-control metrics. dm_controlled = found_dm AND latest '
    'FBS<126 mg/dL. hpt_controlled = found_hpt AND latest SBP<140 AND DBP<90. '
    'lab_patients = distinct patients in district with ANY lab value. '
    'Sources: mv_visit_resolved + private.lab_event/lab_measurement + '
    'private.visit_event/visit_measurement.';


-- =============================================================================
-- ANALYZE for planner stats
-- =============================================================================
ANALYZE public.summary_disease_age_sex;
ANALYZE public.summary_lab_disease_cross;
ANALYZE public.summary_comorbidity;
ANALYZE public.summary_disease_control;
