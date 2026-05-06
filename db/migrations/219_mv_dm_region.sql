-- =============================================================================
-- Migration 219 — Region-scope DM MVs (non-BKK 4-region split)
-- =============================================================================
-- Builds three region-level MVs companion to the BKK-only versions in
-- migs 213/215/217/218:
--
--   public.mv_dm_classification_region   ← mirrors mv_dm_classification
--   public.mv_dm_factors_region          ← mirrors mv_dm_factors_district
--   public.mv_dm_new_findings_region     ← mirrors mv_dm_new_findings
--
-- Region split (province prefix = LEFT(home_district_code, 2)::int):
--   N    50–58            ภาคเหนือ        Northern
--   NE   30–49            ภาคอีสาน        Northeastern
--   S    80–96            ภาคใต้          Southern
--   C    11–29, 60–77     ภาคกลาง         Central
--   --   10 (BKK), 99     excluded (BKK has its own MVs; 99 = unknown)
--
-- Spike (2026-05-05): C=28,426 / NE=3,239 / S=600 / N=590 patients ⇒
-- all four regions clear k-anon (>=5) at every factor stratum.
--
-- Definitions are otherwise identical to the BKK MVs — the only change is
-- the geography GROUP BY key.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. mv_dm_classification_region
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS public.mv_dm_classification_region CASCADE;

CREATE MATERIALIZED VIEW public.mv_dm_classification_region AS
WITH
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
patient_flags AS (
  SELECT
    v.patient_id,
    -- region_code from province prefix; NULL = BKK (10) or unknown (99) → excluded
    CASE
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 50 AND 58 THEN 'N'
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 30 AND 49 THEN 'NE'
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 80 AND 96 THEN 'S'
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 11 AND 29 THEN 'C'
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 60 AND 77 THEN 'C'
      ELSE NULL
    END AS region_code,
    v.risk_dm                    AS c1_risk,
    COALESCE(d.c2_diag,  FALSE)  AS c2_diag,
    COALESCE(f.p_dm,     FALSE)  AS c3_family,
    COALESCE(l.fpg_high, FALSE)  AS c4_fpg
  FROM v_per_patient v
  LEFT JOIN diag_all d ON d.patient_id = v.patient_id
  LEFT JOIN fam      f ON f.patient_id = v.patient_id
  LEFT JOIN labs     l ON l.patient_id = v.patient_id
  WHERE v.home_district_code ~ '^[0-9]{4}$'
)
SELECT
  region_code,
  CONCAT(c1_risk::int, c2_diag::int, c3_family::int, c4_fpg::int) AS pattern,
  COUNT(*)::bigint AS n_patients
FROM patient_flags
WHERE region_code IS NOT NULL
GROUP BY region_code, pattern
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_dm_classification_region
  ON public.mv_dm_classification_region (region_code, pattern);
CREATE INDEX idx_mv_dm_classification_region_rc
  ON public.mv_dm_classification_region (region_code);

GRANT SELECT ON public.mv_dm_classification_region
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_dm_classification_region IS
  '4-bit DM classification per non-BKK region (N/NE/S/C). Same semantics as '
  'mv_dm_classification. Region from province prefix of home_district_code; '
  'BKK (10) and unknown (99) excluded. k-anon n>=5.';


-- =============================================================================
-- 2. mv_dm_factors_region
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS public.mv_dm_factors_region CASCADE;

CREATE MATERIALIZED VIEW public.mv_dm_factors_region AS
WITH
patient_region AS (
  SELECT DISTINCT v.patient_id,
         CASE
           WHEN LEFT(v.home_district_code, 2)::int BETWEEN 50 AND 58 THEN 'N'
           WHEN LEFT(v.home_district_code, 2)::int BETWEEN 30 AND 49 THEN 'NE'
           WHEN LEFT(v.home_district_code, 2)::int BETWEEN 80 AND 96 THEN 'S'
           WHEN LEFT(v.home_district_code, 2)::int BETWEEN 11 AND 29 THEN 'C'
           WHEN LEFT(v.home_district_code, 2)::int BETWEEN 60 AND 77 THEN 'C'
           ELSE NULL
         END AS region_code
  FROM public.mv_visit_resolved v
  WHERE v.is_dedup_kept = TRUE
    AND v.home_district_code IS NOT NULL
    AND v.home_district_code ~ '^[0-9]{4}$'
),
patient_region_filtered AS (
  SELECT patient_id, region_code FROM patient_region WHERE region_code IS NOT NULL
),

-- ── DM signal (same idiom as mig 217/218) ──────────────────────────────
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
fam_app1 AS (
  SELECT patient_id, bool_or(pdm = 1) AS p_dm
  FROM bma_med.app1_homehealth WHERE patient_id IS NOT NULL GROUP BY patient_id
),
fam_portal AS (
  SELECT patient_id, bool_or(pdm IN ('1','true','TRUE')) AS p_dm
  FROM bma_med.portal_homehealth WHERE patient_id IS NOT NULL GROUP BY patient_id
),
fam_all AS (
  SELECT patient_id, bool_or(p_dm) AS c3_family
  FROM (SELECT * FROM fam_app1 UNION ALL SELECT * FROM fam_portal) u
  GROUP BY patient_id
),
labs AS (
  SELECT patient_id, bool_or(fpg_high) AS fpg_high
  FROM (
    SELECT patient_id, (COALESCE(fbs, bldsugar) >= 126) AS fpg_high
    FROM bma_med.app1_labhealth WHERE patient_id IS NOT NULL
    UNION ALL
    SELECT patient_id,
           (COALESCE(
              CASE WHEN fbs      ~ '^[0-9]+(\.[0-9]+)?$' THEN fbs::numeric      END,
              CASE WHEN bldsugar ~ '^[0-9]+(\.[0-9]+)?$' THEN bldsugar::numeric END
            ) >= 126) AS fpg_high
    FROM bma_med.portal_labhealth WHERE patient_id IS NOT NULL
  ) u
  GROUP BY patient_id
),
risk_per_patient AS (
  SELECT patient_id, bool_or(risk_dm) AS c1_risk
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE AND home_district_code IS NOT NULL
  GROUP BY patient_id
),
patient_dm AS (
  -- has_dm = c1 OR c2 OR c4 only (family/c3 excluded — see 217 for rationale).
  SELECT
    pr.patient_id,
    pr.region_code,
    (COALESCE(r.c1_risk, FALSE)
       OR COALESCE(d.c2_diag, FALSE)
       OR COALESCE(l.fpg_high, FALSE)) AS has_dm,
    COALESCE(f.c3_family, FALSE) AS c3_family
  FROM patient_region_filtered pr
  LEFT JOIN risk_per_patient r ON r.patient_id = pr.patient_id
  LEFT JOIN diag_all          d ON d.patient_id = pr.patient_id
  LEFT JOIN fam_all           f ON f.patient_id = pr.patient_id
  LEFT JOIN labs              l ON l.patient_id = pr.patient_id
),

-- ── Per-patient factor values (same idiom as mig 217/218) ────────────────
smoke_app1 AS (
  SELECT patient_id, MAX(smoke)::int AS smk
  FROM bma_med.app1_vitalsignslf WHERE patient_id IS NOT NULL AND smoke IN (0,1,2)
  GROUP BY patient_id
),
smoke_portal AS (
  SELECT patient_id, MAX(smoke::int) AS smk
  FROM bma_med.portal_vitalsignslf WHERE patient_id IS NOT NULL AND smoke ~ '^[0-2]$'
  GROUP BY patient_id
),
smoke_all AS (
  SELECT patient_id, MAX(smk) AS smk
  FROM (SELECT * FROM smoke_app1 UNION ALL SELECT * FROM smoke_portal) u
  GROUP BY patient_id
),
alcohol_app1 AS (
  SELECT patient_id, MAX(alcohal)::int AS alc
  FROM bma_med.app1_vitalsignslf WHERE patient_id IS NOT NULL AND alcohal IN (0,1,2)
  GROUP BY patient_id
),
alcohol_portal AS (
  SELECT patient_id, MAX(alcohal::int) AS alc
  FROM bma_med.portal_vitalsignslf WHERE patient_id IS NOT NULL AND alcohal ~ '^[0-2]$'
  GROUP BY patient_id
),
alcohol_all AS (
  SELECT patient_id, MAX(alc) AS alc
  FROM (SELECT * FROM alcohol_app1 UNION ALL SELECT * FROM alcohol_portal) u
  GROUP BY patient_id
),
excercise_app1 AS (
  SELECT patient_id, MIN(excercise)::int AS exc
  FROM bma_med.app1_homehealth WHERE patient_id IS NOT NULL AND excercise IN (1,2,3)
  GROUP BY patient_id
),
excercise_portal AS (
  SELECT patient_id, MIN(excercise::int) AS exc
  FROM bma_med.portal_homehealth WHERE patient_id IS NOT NULL AND excercise ~ '^[1-3]$'
  GROUP BY patient_id
),
excercise_all AS (
  SELECT patient_id, MIN(exc) AS exc
  FROM (SELECT * FROM excercise_app1 UNION ALL SELECT * FROM excercise_portal) u
  GROUP BY patient_id
),
bmi_per_patient AS (
  SELECT patient_id, AVG(bmi_calc) AS bmi
  FROM (
    SELECT patient_id, bmi_calc
    FROM bma_med.app1_vitalsignslf
    WHERE patient_id IS NOT NULL AND bmi_calc BETWEEN 10 AND 80
    UNION ALL
    SELECT patient_id, bmi_calc
    FROM bma_med.portal_vitalsignslf
    WHERE patient_id IS NOT NULL AND bmi_calc BETWEEN 10 AND 80
  ) u
  GROUP BY patient_id
),
age_per_patient AS (
  SELECT patient_id, MAX(age_years) AS age
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE AND age_years BETWEEN 18 AND 120
  GROUP BY patient_id
),

-- ── Long-format factor rows ──────────────────────────────────────────────
factor_rows AS (
  -- BMI
  SELECT pd.patient_id, pd.region_code, pd.has_dm,
         'bmi_cat'::text AS factor_key,
         CASE WHEN b.bmi<23 THEN 'normal'
              WHEN b.bmi<25 THEN 'overweight'
              WHEN b.bmi<30 THEN 'obese'
              WHEN b.bmi>=30 THEN 'severely_obese' END AS factor_group,
         CASE WHEN b.bmi<23 THEN 'ปกติ (<23)'
              WHEN b.bmi<25 THEN 'น้ำหนักเกิน (23–24.99)'
              WHEN b.bmi<30 THEN 'อ้วน (25–29.99)'
              WHEN b.bmi>=30 THEN 'อ้วนรุนแรง (≥30)' END AS factor_group_th,
         CASE WHEN b.bmi<23 THEN 'Normal (<23)'
              WHEN b.bmi<25 THEN 'Overweight (23–24.99)'
              WHEN b.bmi<30 THEN 'Obese (25–29.99)'
              WHEN b.bmi>=30 THEN 'Severely obese (≥30)' END AS factor_group_en
  FROM patient_dm pd JOIN bmi_per_patient b USING (patient_id)
  WHERE b.bmi IS NOT NULL

  UNION ALL
  -- Smoke
  SELECT pd.patient_id, pd.region_code, pd.has_dm,
         'smoke',
         CASE s.smk WHEN 0 THEN 'non_smoker' WHEN 1 THEN 'former_smoker' WHEN 2 THEN 'current_smoker' END,
         CASE s.smk WHEN 0 THEN 'ไม่สูบ' WHEN 1 THEN 'เคยสูบ' WHEN 2 THEN 'สูบปัจจุบัน' END,
         CASE s.smk WHEN 0 THEN 'Non-smoker' WHEN 1 THEN 'Former smoker' WHEN 2 THEN 'Current smoker' END
  FROM patient_dm pd JOIN smoke_all s USING (patient_id)
  WHERE s.smk IN (0,1,2)

  UNION ALL
  -- Alcohol
  SELECT pd.patient_id, pd.region_code, pd.has_dm,
         'alcohol',
         CASE a.alc WHEN 0 THEN 'non_drinker' WHEN 1 THEN 'former_drinker' WHEN 2 THEN 'current_drinker' END,
         CASE a.alc WHEN 0 THEN 'ไม่ดื่ม' WHEN 1 THEN 'เคยดื่ม' WHEN 2 THEN 'ดื่มปัจจุบัน' END,
         CASE a.alc WHEN 0 THEN 'Non-drinker' WHEN 1 THEN 'Former drinker' WHEN 2 THEN 'Current drinker' END
  FROM patient_dm pd JOIN alcohol_all a USING (patient_id)
  WHERE a.alc IN (0,1,2)

  UNION ALL
  -- Excercise
  SELECT pd.patient_id, pd.region_code, pd.has_dm,
         'excercise',
         CASE e.exc WHEN 1 THEN 'regular' WHEN 2 THEN 'sometimes' WHEN 3 THEN 'never' END,
         CASE e.exc WHEN 1 THEN 'ออกกำลังเป็นประจำ' WHEN 2 THEN 'ออกกำลังเป็นบางครั้ง' WHEN 3 THEN 'ไม่ออกกำลัง' END,
         CASE e.exc WHEN 1 THEN 'Regular exercise' WHEN 2 THEN 'Sometimes' WHEN 3 THEN 'Never' END
  FROM patient_dm pd JOIN excercise_all e USING (patient_id)
  WHERE e.exc IN (1,2,3)

  UNION ALL
  -- Age group
  SELECT pd.patient_id, pd.region_code, pd.has_dm,
         'age_group',
         CASE WHEN a.age BETWEEN 18 AND 29 THEN '18-29'
              WHEN a.age BETWEEN 30 AND 44 THEN '30-44'
              WHEN a.age BETWEEN 45 AND 59 THEN '45-59'
              WHEN a.age BETWEEN 60 AND 74 THEN '60-74'
              WHEN a.age >= 75              THEN '75+' END,
         CASE WHEN a.age BETWEEN 18 AND 29 THEN '18–29 ปี'
              WHEN a.age BETWEEN 30 AND 44 THEN '30–44 ปี'
              WHEN a.age BETWEEN 45 AND 59 THEN '45–59 ปี'
              WHEN a.age BETWEEN 60 AND 74 THEN '60–74 ปี'
              WHEN a.age >= 75              THEN '75 ปีขึ้นไป' END,
         CASE WHEN a.age BETWEEN 18 AND 29 THEN '18–29 yrs'
              WHEN a.age BETWEEN 30 AND 44 THEN '30–44 yrs'
              WHEN a.age BETWEEN 45 AND 59 THEN '45–59 yrs'
              WHEN a.age BETWEEN 60 AND 74 THEN '60–74 yrs'
              WHEN a.age >= 75              THEN '75+ yrs' END
  FROM patient_dm pd JOIN age_per_patient a USING (patient_id)
  WHERE a.age >= 18

  UNION ALL
  -- Family DM
  SELECT pd.patient_id, pd.region_code, pd.has_dm,
         'family_dm',
         CASE WHEN pd.c3_family THEN 'family_yes' ELSE 'family_no' END,
         CASE WHEN pd.c3_family THEN 'มีประวัติครอบครัว' ELSE 'ไม่มีประวัติครอบครัว' END,
         CASE WHEN pd.c3_family THEN 'Family history' ELSE 'No family history' END
  FROM patient_dm pd
)

SELECT
  region_code,
  CASE region_code
    WHEN 'N'  THEN 'ภาคเหนือ'
    WHEN 'NE' THEN 'ภาคอีสาน'
    WHEN 'S'  THEN 'ภาคใต้'
    WHEN 'C'  THEN 'ภาคกลาง'
  END AS region_name_th,
  CASE region_code
    WHEN 'N'  THEN 'Northern'
    WHEN 'NE' THEN 'Northeastern'
    WHEN 'S'  THEN 'Southern'
    WHEN 'C'  THEN 'Central'
  END AS region_name_en,
  factor_key,
  factor_group,
  factor_group_th,
  factor_group_en,
  COUNT(*)::bigint                          AS n,
  COUNT(*) FILTER (WHERE has_dm)::bigint    AS dm_n,
  ROUND(100.0 * COUNT(*) FILTER (WHERE has_dm) / NULLIF(COUNT(*), 0), 2) AS dm_pct
FROM factor_rows
WHERE factor_group IS NOT NULL
GROUP BY region_code, factor_key, factor_group, factor_group_th, factor_group_en
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_dm_factors_region
  ON public.mv_dm_factors_region (region_code, factor_key, factor_group);
CREATE INDEX idx_mv_dm_factors_region_rc
  ON public.mv_dm_factors_region (region_code);

GRANT SELECT ON public.mv_dm_factors_region
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_dm_factors_region IS
  'DM × risk-factor cross-tab per non-BKK region (N/NE/S/C). Mirrors '
  'mv_dm_factors_district. Same factors and dm_n semantics. k-anon n>=5.';


-- =============================================================================
-- 3. mv_dm_new_findings_region
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS public.mv_dm_new_findings_region CASCADE;

CREATE MATERIALIZED VIEW public.mv_dm_new_findings_region AS
WITH
self_app1 AS (
  SELECT patient_id,
         bool_or(dm = 1) AS self_yes,
         bool_or(dm = 2) AS self_no
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
v_per_patient AS (
  SELECT DISTINCT patient_id, home_district_code
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE
    AND home_district_code IS NOT NULL
    AND home_district_code ~ '^[0-9]{4}$'
),
per_patient AS (
  SELECT
    v.patient_id,
    CASE
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 50 AND 58 THEN 'N'
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 30 AND 49 THEN 'NE'
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 80 AND 96 THEN 'S'
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 11 AND 29 THEN 'C'
      WHEN LEFT(v.home_district_code, 2)::int BETWEEN 60 AND 77 THEN 'C'
      ELSE NULL
    END AS region_code,
    COALESCE(s.self_yes, FALSE) AS self_yes,
    COALESCE(s.self_no,  FALSE) AS self_no,
    COALESCE(l.fpg_high, FALSE) AS fpg_high
  FROM v_per_patient v
  LEFT JOIN self_all s ON s.patient_id = v.patient_id
  LEFT JOIN labs     l ON l.patient_id = v.patient_id
)
SELECT
  region_code,
  COUNT(*)::bigint AS total_patients,
  COUNT(*) FILTER (WHERE fpg_high)::bigint AS fpg_positive_total,
  COUNT(*) FILTER (WHERE self_no AND NOT self_yes AND fpg_high)::bigint
    AS newly_found_strict,
  COUNT(*) FILTER (WHERE NOT self_yes AND fpg_high)::bigint
    AS newly_found_loose
FROM per_patient
WHERE region_code IS NOT NULL
GROUP BY region_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_dm_new_findings_region
  ON public.mv_dm_new_findings_region (region_code);

GRANT SELECT ON public.mv_dm_new_findings_region
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_dm_new_findings_region IS
  'Newly-found DM cohort per non-BKK region. Mirrors mv_dm_new_findings. '
  'Strict = self_no AND NOT self_yes AND fpg_high. k-anon n>=5.';


-- =============================================================================
-- Refresh all three
-- =============================================================================
REFRESH MATERIALIZED VIEW public.mv_dm_classification_region;
REFRESH MATERIALIZED VIEW public.mv_dm_factors_region;
REFRESH MATERIALIZED VIEW public.mv_dm_new_findings_region;

COMMIT;

-- =============================================================================
-- END migration 219
-- =============================================================================
