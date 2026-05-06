-- =============================================================================
-- Migration 222 — mv_hpt_factors_region (HPT × risk-factor cross-tab, non-BKK)
-- =============================================================================
-- Companion to mv_hpt_factors (zone-level, BKK-only) from mig 220 and to the
-- DM region equivalent in mig 219. Powers the non-BKK factor mini-panel for
-- the HPT layer in the map tooltip.
--
-- Region split (province prefix = LEFT(home_district_code, 2)::int):
--   N    50–58            ภาคเหนือ        Northern
--   NE   30–49            ภาคอีสาน        Northeastern
--   S    80–96            ภาคใต้          Southern
--   C    11–29, 60–77     ภาคกลาง         Central
--   --   10 (BKK), 99     excluded
--
-- has_hpt = c1 OR c2 OR c4 only — c3_family excluded (see mig 220 for
-- rationale: hereditary risk, not active disease state; otherwise the
-- family-history cross-tab is tautological).
-- =============================================================================

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS public.mv_hpt_factors_region CASCADE;

CREATE MATERIALIZED VIEW public.mv_hpt_factors_region AS
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

-- ── HPT signal (mirrors mig 220) ───────────────────────────────────────
diag_app1 AS (
  SELECT patient_id, bool_or(hpt = 1) AS diag
  FROM (
    SELECT patient_id, hpt FROM bma_med.app1_vitalsignslf WHERE patient_id IS NOT NULL
    UNION ALL
    SELECT patient_id, hpt FROM bma_med.app1_homehealth   WHERE patient_id IS NOT NULL
  ) u
  GROUP BY patient_id
),
diag_portal AS (
  SELECT patient_id, bool_or(hpt IN ('1','true','TRUE')) AS diag
  FROM (
    SELECT patient_id, hpt FROM bma_med.portal_vitalsignslf WHERE patient_id IS NOT NULL
    UNION ALL
    SELECT patient_id, hpt FROM bma_med.portal_homehealth   WHERE patient_id IS NOT NULL
  ) u
  GROUP BY patient_id
),
diag_all AS (
  SELECT patient_id, bool_or(diag) AS c2_diag
  FROM (SELECT * FROM diag_app1 UNION ALL SELECT * FROM diag_portal) u
  GROUP BY patient_id
),
fam_app1 AS (
  SELECT patient_id, bool_or(phpt = 1) AS p_hpt
  FROM bma_med.app1_homehealth WHERE patient_id IS NOT NULL GROUP BY patient_id
),
fam_portal AS (
  SELECT patient_id, bool_or(phpt IN ('1','true','TRUE')) AS p_hpt
  FROM bma_med.portal_homehealth WHERE patient_id IS NOT NULL GROUP BY patient_id
),
fam_all AS (
  SELECT patient_id, bool_or(p_hpt) AS c3_family
  FROM (SELECT * FROM fam_app1 UNION ALL SELECT * FROM fam_portal) u
  GROUP BY patient_id
),
labs AS (
  SELECT patient_id, bool_or(bp_high) AS bp_high
  FROM (
    SELECT patient_id,
           ((sbp BETWEEN 50 AND 250 AND sbp >= 140)
            OR (dbp BETWEEN 30 AND 200 AND dbp >= 90)) AS bp_high
    FROM public.mv_visit_resolved
    WHERE is_dedup_kept = TRUE AND patient_id IS NOT NULL
  ) u
  GROUP BY patient_id
),
risk_per_patient AS (
  SELECT patient_id, bool_or(risk_hpt) AS c1_risk
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE AND home_district_code IS NOT NULL
  GROUP BY patient_id
),
patient_hpt AS (
  -- has_hpt = c1 OR c2 OR c4 only (family/c3 excluded — see header).
  SELECT
    pr.patient_id,
    pr.region_code,
    (COALESCE(r.c1_risk, FALSE)
       OR COALESCE(d.c2_diag, FALSE)
       OR COALESCE(l.bp_high, FALSE)) AS has_hpt,
    COALESCE(f.c3_family, FALSE) AS c3_family
  FROM patient_region_filtered pr
  LEFT JOIN risk_per_patient r ON r.patient_id = pr.patient_id
  LEFT JOIN diag_all          d ON d.patient_id = pr.patient_id
  LEFT JOIN fam_all           f ON f.patient_id = pr.patient_id
  LEFT JOIN labs              l ON l.patient_id = pr.patient_id
),

-- ── Per-patient factor values (same as mig 220) ─────────────────────────
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

-- ── Long-format factor rows ─────────────────────────────────────────────
factor_rows AS (
  -- BMI
  SELECT pd.patient_id, pd.region_code, pd.has_hpt,
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
  FROM patient_hpt pd JOIN bmi_per_patient b USING (patient_id)
  WHERE b.bmi IS NOT NULL

  UNION ALL
  -- Smoke
  SELECT pd.patient_id, pd.region_code, pd.has_hpt,
         'smoke',
         CASE s.smk WHEN 0 THEN 'non_smoker' WHEN 1 THEN 'former_smoker' WHEN 2 THEN 'current_smoker' END,
         CASE s.smk WHEN 0 THEN 'ไม่สูบ' WHEN 1 THEN 'เคยสูบ' WHEN 2 THEN 'สูบปัจจุบัน' END,
         CASE s.smk WHEN 0 THEN 'Non-smoker' WHEN 1 THEN 'Former smoker' WHEN 2 THEN 'Current smoker' END
  FROM patient_hpt pd JOIN smoke_all s USING (patient_id)
  WHERE s.smk IN (0,1,2)

  UNION ALL
  -- Alcohol
  SELECT pd.patient_id, pd.region_code, pd.has_hpt,
         'alcohol',
         CASE a.alc WHEN 0 THEN 'non_drinker' WHEN 1 THEN 'former_drinker' WHEN 2 THEN 'current_drinker' END,
         CASE a.alc WHEN 0 THEN 'ไม่ดื่ม' WHEN 1 THEN 'เคยดื่ม' WHEN 2 THEN 'ดื่มปัจจุบัน' END,
         CASE a.alc WHEN 0 THEN 'Non-drinker' WHEN 1 THEN 'Former drinker' WHEN 2 THEN 'Current drinker' END
  FROM patient_hpt pd JOIN alcohol_all a USING (patient_id)
  WHERE a.alc IN (0,1,2)

  UNION ALL
  -- Excercise
  SELECT pd.patient_id, pd.region_code, pd.has_hpt,
         'excercise',
         CASE e.exc WHEN 1 THEN 'regular' WHEN 2 THEN 'sometimes' WHEN 3 THEN 'never' END,
         CASE e.exc WHEN 1 THEN 'ออกกำลังเป็นประจำ' WHEN 2 THEN 'ออกกำลังเป็นบางครั้ง' WHEN 3 THEN 'ไม่ออกกำลัง' END,
         CASE e.exc WHEN 1 THEN 'Regular exercise' WHEN 2 THEN 'Sometimes' WHEN 3 THEN 'Never' END
  FROM patient_hpt pd JOIN excercise_all e USING (patient_id)
  WHERE e.exc IN (1,2,3)

  UNION ALL
  -- Age group
  SELECT pd.patient_id, pd.region_code, pd.has_hpt,
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
  FROM patient_hpt pd JOIN age_per_patient a USING (patient_id)
  WHERE a.age >= 18

  UNION ALL
  -- Family HPT
  SELECT pd.patient_id, pd.region_code, pd.has_hpt,
         'family_hpt',
         CASE WHEN pd.c3_family THEN 'family_yes' ELSE 'family_no' END,
         CASE WHEN pd.c3_family THEN 'มีประวัติครอบครัว' ELSE 'ไม่มีประวัติครอบครัว' END,
         CASE WHEN pd.c3_family THEN 'Family history' ELSE 'No family history' END
  FROM patient_hpt pd
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
  COUNT(*)::bigint                            AS n,
  COUNT(*) FILTER (WHERE has_hpt)::bigint     AS hpt_n,
  ROUND(100.0 * COUNT(*) FILTER (WHERE has_hpt) / NULLIF(COUNT(*), 0), 2) AS hpt_pct
FROM factor_rows
WHERE factor_group IS NOT NULL
GROUP BY region_code, factor_key, factor_group, factor_group_th, factor_group_en
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_hpt_factors_region
  ON public.mv_hpt_factors_region (region_code, factor_key, factor_group);
CREATE INDEX idx_mv_hpt_factors_region_rc
  ON public.mv_hpt_factors_region (region_code);

GRANT SELECT ON public.mv_hpt_factors_region
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_hpt_factors_region IS
  'HPT × risk-factor cross-tab per non-BKK region (N/NE/S/C). Mirrors '
  'mv_dm_factors_region (mig 219). hpt_n = patients with c1/c2/c4 (family '
  'excluded). k-anon n>=5.';

REFRESH MATERIALIZED VIEW public.mv_hpt_factors_region;

COMMIT;

-- =============================================================================
-- END migration 222
-- =============================================================================
