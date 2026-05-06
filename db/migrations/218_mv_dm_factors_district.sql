-- =============================================================================
-- Migration 218 — mv_dm_factors_district (DM × risk-factor at DISTRICT scope)
-- =============================================================================
-- Companion to mig 217's mv_dm_factors (zone-level). Same 6 factors, same
-- semantics — only difference is grouping by `home_district_code` instead of
-- `zone_code`. Used by the hover tooltip when the map is drilled to district
-- level (after a zone click).
--
-- Spike (2026-05-05): smallest BKK district = 856 patients; BMI 4-group split
-- has 100% keep rate at k-anon=5. Other factors (3 or 2 groups) survive
-- even better. Confirmed safe to ship at district granularity.
--
-- Output columns mirror mv_dm_factors with district_code as the key. The
-- dm_n / dm_pct semantics are identical (any_dm_signal = pattern!='0000').
-- =============================================================================

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS public.mv_dm_factors_district CASCADE;

CREATE MATERIALIZED VIEW public.mv_dm_factors_district AS
WITH
patient_dist AS (
  SELECT DISTINCT v.patient_id,
         v.home_district_code AS district_code,
         d.zone_code,
         d.name_th AS district_name
  FROM public.mv_visit_resolved v
  JOIN public.ref_districts d ON d.dcode = v.home_district_code
  WHERE v.is_dedup_kept = TRUE
    AND v.home_district_code IS NOT NULL
    AND v.home_district_code BETWEEN '1001' AND '1050'
),

-- ── DM signal per patient (same as mig 217) ──────────────────────────────
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

-- ── Combined patient table: district + DM signal flags ───────────────────
patient_dm AS (
  -- has_dm = c1 OR c2 OR c4 only — c3_family excluded (see 217).
  SELECT
    pd.patient_id,
    pd.district_code,
    pd.zone_code,
    pd.district_name,
    (COALESCE(r.c1_risk, FALSE)
       OR COALESCE(d.c2_diag, FALSE)
       OR COALESCE(l.fpg_high, FALSE)) AS has_dm,
    COALESCE(f.c3_family, FALSE) AS c3_family
  FROM patient_dist pd
  LEFT JOIN risk_per_patient r ON r.patient_id = pd.patient_id
  LEFT JOIN diag_all          d ON d.patient_id = pd.patient_id
  LEFT JOIN fam_all           f ON f.patient_id = pd.patient_id
  LEFT JOIN labs              l ON l.patient_id = pd.patient_id
),

-- ── Per-patient factor values (same idiom as mig 217) ────────────────────
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
  SELECT pd.patient_id, pd.district_code, pd.district_name, pd.zone_code, pd.has_dm,
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
  SELECT pd.patient_id, pd.district_code, pd.district_name, pd.zone_code, pd.has_dm,
         'smoke',
         CASE s.smk WHEN 0 THEN 'non_smoker' WHEN 1 THEN 'former_smoker' WHEN 2 THEN 'current_smoker' END,
         CASE s.smk WHEN 0 THEN 'ไม่สูบ' WHEN 1 THEN 'เคยสูบ' WHEN 2 THEN 'สูบปัจจุบัน' END,
         CASE s.smk WHEN 0 THEN 'Non-smoker' WHEN 1 THEN 'Former smoker' WHEN 2 THEN 'Current smoker' END
  FROM patient_dm pd JOIN smoke_all s USING (patient_id)
  WHERE s.smk IN (0,1,2)

  UNION ALL
  -- Alcohol
  SELECT pd.patient_id, pd.district_code, pd.district_name, pd.zone_code, pd.has_dm,
         'alcohol',
         CASE a.alc WHEN 0 THEN 'non_drinker' WHEN 1 THEN 'former_drinker' WHEN 2 THEN 'current_drinker' END,
         CASE a.alc WHEN 0 THEN 'ไม่ดื่ม' WHEN 1 THEN 'เคยดื่ม' WHEN 2 THEN 'ดื่มปัจจุบัน' END,
         CASE a.alc WHEN 0 THEN 'Non-drinker' WHEN 1 THEN 'Former drinker' WHEN 2 THEN 'Current drinker' END
  FROM patient_dm pd JOIN alcohol_all a USING (patient_id)
  WHERE a.alc IN (0,1,2)

  UNION ALL
  -- Excercise
  SELECT pd.patient_id, pd.district_code, pd.district_name, pd.zone_code, pd.has_dm,
         'excercise',
         CASE e.exc WHEN 1 THEN 'regular' WHEN 2 THEN 'sometimes' WHEN 3 THEN 'never' END,
         CASE e.exc WHEN 1 THEN 'ออกกำลังเป็นประจำ' WHEN 2 THEN 'ออกกำลังเป็นบางครั้ง' WHEN 3 THEN 'ไม่ออกกำลัง' END,
         CASE e.exc WHEN 1 THEN 'Regular exercise' WHEN 2 THEN 'Sometimes' WHEN 3 THEN 'Never' END
  FROM patient_dm pd JOIN excercise_all e USING (patient_id)
  WHERE e.exc IN (1,2,3)

  UNION ALL
  -- Age group
  SELECT pd.patient_id, pd.district_code, pd.district_name, pd.zone_code, pd.has_dm,
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
  SELECT pd.patient_id, pd.district_code, pd.district_name, pd.zone_code, pd.has_dm,
         'family_dm',
         CASE WHEN pd.c3_family THEN 'family_yes' ELSE 'family_no' END,
         CASE WHEN pd.c3_family THEN 'มีประวัติครอบครัว' ELSE 'ไม่มีประวัติครอบครัว' END,
         CASE WHEN pd.c3_family THEN 'Family history' ELSE 'No family history' END
  FROM patient_dm pd
)

SELECT
  district_code,
  district_name,
  zone_code,
  factor_key,
  factor_group,
  factor_group_th,
  factor_group_en,
  COUNT(*)::bigint                          AS n,
  COUNT(*) FILTER (WHERE has_dm)::bigint    AS dm_n,
  ROUND(100.0 * COUNT(*) FILTER (WHERE has_dm) / NULLIF(COUNT(*), 0), 2) AS dm_pct
FROM factor_rows
WHERE factor_group IS NOT NULL
GROUP BY district_code, district_name, zone_code, factor_key, factor_group, factor_group_th, factor_group_en
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_dm_factors_district
  ON public.mv_dm_factors_district (district_code, factor_key, factor_group);
CREATE INDEX idx_mv_dm_factors_district_dc
  ON public.mv_dm_factors_district (district_code);
CREATE INDEX idx_mv_dm_factors_district_zone
  ON public.mv_dm_factors_district (zone_code);

GRANT SELECT ON public.mv_dm_factors_district
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_dm_factors_district IS
  'DM × risk-factor cross-tab per district (BKK 50). Companion to '
  'mv_dm_factors (zone-level). Same factors and dm_n semantics. '
  'k-anon n>=5; spike showed 100% keep at district granularity.';

REFRESH MATERIALIZED VIEW public.mv_dm_factors_district;

COMMIT;

-- =============================================================================
-- END migration 218
-- =============================================================================
