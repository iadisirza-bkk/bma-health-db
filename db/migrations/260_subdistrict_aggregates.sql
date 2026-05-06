-- =============================================================================
-- Migration 260 — Per-subdistrict aggregate MVs for all 11 diseases
-- =============================================================================
-- Purpose:
--   Headline n_total / n_signal / signal_pct per (subdistrict_code,
--   district_code) for each of the 11 diseases tracked by this pipeline.
--   Mirrors the per-district MVs from migrations 213/220 (NCDs) and 250-258
--   (screening) but at the smaller subdistrict geography.
--
-- Builds 12 materialized views:
--   mv_patient_subdistrict           helper: patient_id → (district, subdistrict)
--   mv_dm_subdistrict                NCD (DM)
--   mv_hpt_subdistrict               NCD (HPT)
--   mv_ckd_subdistrict               screening
--   mv_liver_subdistrict             screening
--   mv_anemia_subdistrict            screening
--   mv_xray_subdistrict              screening
--   mv_cervical_subdistrict          screening
--   mv_colon_subdistrict             screening
--   mv_obesity_subdistrict           screening
--   mv_cvd_subdistrict               screening
--   mv_dyslipidemia_subdistrict      screening
--
-- Aggregate columns (every disease MV):
--   subdistrict_code  text     6-digit
--   district_code     text     4-digit (LEFT(subdistrict_code, 4))
--   n_total           bigint   denominator
--   n_signal          bigint   numerator (any_<key>_signal)
--   signal_pct        numeric  100 * n_signal / n_total, 2 dp
--   HAVING n_total >= 5  (k-anonymity)
--
-- NCD signal definition (mirrors mig 213/220):
--   any_<key>_signal = c1_risk OR c2_diag OR c4_lab  (c3 family excluded).
--
-- Screening signal definition (mirrors mig 250-258):
--   patient_abnormal CTE re-used verbatim from each existing screening MV.
--
-- Subdistrict geo derivation:
--   App1     bma_med.app1_homevisit.subdistrict       NUMERIC (always 6-digit)
--   Portal   bma_med.portal_homevisit.hsubdistrict    TEXT (most coverage)
--   Portal   bma_med.portal_homevisit.subdistrict     TEXT (fallback ~11K rows)
--   LPAD to 6 digits and pick the latest vstdate per patient_id (mirrors the
--   home_district CTE in mig 200's mv_visit_resolved).
-- =============================================================================

BEGIN;

-- =============================================================================
-- 0. Helper MV — mv_patient_subdistrict
-- =============================================================================
-- One row per patient_id, picking the latest non-null subdistrict observed in
-- app1_homevisit / portal_homevisit. district_code is derived from the first
-- 4 digits of the resolved subdistrict_code (TH subdistrict codes are always
-- DDDDXX where DDDD = province+district). No k-anon — patient mapping only.

DROP MATERIALIZED VIEW IF EXISTS public.mv_patient_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_patient_subdistrict AS
WITH unioned AS (
  SELECT
    h.patient_id,
    LPAD(NULLIF(h.subdistrict::text, ''), 6, '0') AS home_subdistrict_code,
    h.vstdate
  FROM bma_med.app1_homevisit h
  WHERE h.patient_id IS NOT NULL
    AND h.subdistrict IS NOT NULL

  UNION ALL

  SELECT
    h.patient_id,
    COALESCE(
      LPAD(NULLIF(h.hsubdistrict, ''), 6, '0'),
      LPAD(NULLIF(h.subdistrict,  ''), 6, '0')
    ) AS home_subdistrict_code,
    h.vstdate
  FROM bma_med.portal_homevisit h
  WHERE h.patient_id IS NOT NULL
    AND (h.hsubdistrict IS NOT NULL OR h.subdistrict IS NOT NULL)
)
SELECT DISTINCT ON (patient_id)
  patient_id,
  LEFT(home_subdistrict_code, 4) AS home_district_code,
  home_subdistrict_code
FROM unioned
WHERE home_subdistrict_code IS NOT NULL
  AND home_subdistrict_code ~ '^[0-9]{6}$'
ORDER BY patient_id, vstdate DESC NULLS LAST
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_patient_subdistrict
  ON public.mv_patient_subdistrict (patient_id);
CREATE INDEX idx_mv_patient_subdistrict_sub
  ON public.mv_patient_subdistrict (home_subdistrict_code);
CREATE INDEX idx_mv_patient_subdistrict_dist
  ON public.mv_patient_subdistrict (home_district_code);

GRANT SELECT ON public.mv_patient_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_patient_subdistrict IS
  'Patient → home subdistrict mapping (latest vstdate, app1+portal homevisit). '
  '6-digit subdistrict_code, with district_code = LEFT(...,4). '
  'Helper for per-subdistrict disease MVs (mig 260). No k-anon — mapping only.';

-- =============================================================================
-- 1. mv_dm_subdistrict — DM headline rate per subdistrict
-- =============================================================================
-- Pattern: any_dm_signal = c1_risk OR c2_diag OR c4_fpg  (mirrors mig 213).

DROP MATERIALIZED VIEW IF EXISTS public.mv_dm_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_dm_subdistrict AS
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
risk_per_patient AS (
  SELECT patient_id, bool_or(risk_dm) AS c1_risk
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE AND home_district_code IS NOT NULL
  GROUP BY patient_id
),
patient_signal AS (
  SELECT
    ps.patient_id,
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    (COALESCE(r.c1_risk, FALSE)
       OR COALESCE(d.c2_diag,  FALSE)
       OR COALESCE(l.fpg_high, FALSE)) AS any_signal
  FROM public.mv_patient_subdistrict ps
  LEFT JOIN risk_per_patient r ON r.patient_id = ps.patient_id
  LEFT JOIN diag_all          d ON d.patient_id = ps.patient_id
  LEFT JOIN labs              l ON l.patient_id = ps.patient_id
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE any_signal)::bigint    AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE any_signal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM patient_signal
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_dm_subdistrict
  ON public.mv_dm_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_dm_subdistrict_dc
  ON public.mv_dm_subdistrict (district_code);

GRANT SELECT ON public.mv_dm_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_dm_subdistrict IS
  'DM headline rate per subdistrict. n_signal = patients with c1/c2/c4 '
  '(family/c3 excluded; mirrors mig 213). k-anon n_total>=5.';

-- =============================================================================
-- 2. mv_hpt_subdistrict — HPT headline rate per subdistrict
-- =============================================================================
-- Pattern: any_hpt_signal = c1_risk OR c2_diag OR c4_bp_high  (mirrors mig 220).

DROP MATERIALIZED VIEW IF EXISTS public.mv_hpt_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_hpt_subdistrict AS
WITH
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
patient_signal AS (
  SELECT
    ps.patient_id,
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    (COALESCE(r.c1_risk, FALSE)
       OR COALESCE(d.c2_diag, FALSE)
       OR COALESCE(l.bp_high, FALSE)) AS any_signal
  FROM public.mv_patient_subdistrict ps
  LEFT JOIN risk_per_patient r ON r.patient_id = ps.patient_id
  LEFT JOIN diag_all          d ON d.patient_id = ps.patient_id
  LEFT JOIN labs              l ON l.patient_id = ps.patient_id
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE any_signal)::bigint    AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE any_signal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM patient_signal
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_hpt_subdistrict
  ON public.mv_hpt_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_hpt_subdistrict_dc
  ON public.mv_hpt_subdistrict (district_code);

GRANT SELECT ON public.mv_hpt_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_hpt_subdistrict IS
  'HPT headline rate per subdistrict. n_signal = patients with c1/c2/c4 '
  '(family/c3 excluded; mirrors mig 220). k-anon n_total>=5.';

-- =============================================================================
-- 3. mv_ckd_subdistrict — CKD screening abnormal per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_ckd_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_ckd_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id, (msd_kidney = 1) AS abnormal
    FROM bma_med.app1_labhealth
    WHERE patient_id IS NOT NULL AND msd_kidney IS NOT NULL
    UNION ALL
    SELECT patient_id, (msd_kidney = 1) AS abnormal
    FROM bma_med.portal_labhealth
    WHERE patient_id IS NOT NULL AND msd_kidney IS NOT NULL
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_ckd_subdistrict
  ON public.mv_ckd_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_ckd_subdistrict_dc
  ON public.mv_ckd_subdistrict (district_code);

GRANT SELECT ON public.mv_ckd_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_ckd_subdistrict IS
  'CKD screening abnormal rate per subdistrict (mirrors mig 250). '
  'k-anon n_total>=5.';

-- =============================================================================
-- 4. mv_liver_subdistrict — Liver screening abnormal per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_liver_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_liver_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id, (msd_liver = 1) AS abnormal
    FROM bma_med.app1_labhealth
    WHERE patient_id IS NOT NULL AND msd_liver IS NOT NULL
    UNION ALL
    SELECT patient_id, (msd_liver = 1) AS abnormal
    FROM bma_med.portal_labhealth
    WHERE patient_id IS NOT NULL AND msd_liver IS NOT NULL
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_liver_subdistrict
  ON public.mv_liver_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_liver_subdistrict_dc
  ON public.mv_liver_subdistrict (district_code);

GRANT SELECT ON public.mv_liver_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_liver_subdistrict IS
  'Liver screening abnormal rate per subdistrict (mirrors mig 251). '
  'k-anon n_total>=5.';

-- =============================================================================
-- 5. mv_anemia_subdistrict — Anemia screening abnormal per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_anemia_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_anemia_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id, (msd_anemia = 1) AS abnormal
    FROM bma_med.app1_labhealth
    WHERE patient_id IS NOT NULL AND msd_anemia IS NOT NULL
    UNION ALL
    SELECT patient_id, (msd_anemia = 1) AS abnormal
    FROM bma_med.portal_labhealth
    WHERE patient_id IS NOT NULL AND msd_anemia IS NOT NULL
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_anemia_subdistrict
  ON public.mv_anemia_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_anemia_subdistrict_dc
  ON public.mv_anemia_subdistrict (district_code);

GRANT SELECT ON public.mv_anemia_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_anemia_subdistrict IS
  'Anemia screening abnormal rate per subdistrict (mirrors mig 252). '
  'k-anon n_total>=5.';

-- =============================================================================
-- 6. mv_xray_subdistrict — Chest X-ray abnormal per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_xray_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_xray_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id, (msd_chest = 1) AS abnormal
    FROM bma_med.app1_vitalsignslf
    WHERE patient_id IS NOT NULL AND msd_chest IS NOT NULL
    UNION ALL
    SELECT patient_id, (msd_chest = 1) AS abnormal
    FROM bma_med.portal_vitalsignslf
    WHERE patient_id IS NOT NULL AND msd_chest IS NOT NULL
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_xray_subdistrict
  ON public.mv_xray_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_xray_subdistrict_dc
  ON public.mv_xray_subdistrict (district_code);

GRANT SELECT ON public.mv_xray_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_xray_subdistrict IS
  'Chest X-ray abnormal rate per subdistrict (mirrors mig 253). '
  'k-anon n_total>=5.';

-- =============================================================================
-- 7. mv_cervical_subdistrict — Cervical screening abnormal per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_cervical_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_cervical_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id, (msd_cervical = 1) AS abnormal
    FROM bma_med.app1_labhealth
    WHERE patient_id IS NOT NULL AND msd_cervical IS NOT NULL
    UNION ALL
    SELECT patient_id, (msd_cervical = 1) AS abnormal
    FROM bma_med.portal_labhealth
    WHERE patient_id IS NOT NULL AND msd_cervical IS NOT NULL
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_cervical_subdistrict
  ON public.mv_cervical_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_cervical_subdistrict_dc
  ON public.mv_cervical_subdistrict (district_code);

GRANT SELECT ON public.mv_cervical_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_cervical_subdistrict IS
  'Cervical screening abnormal rate per subdistrict (mirrors mig 254). '
  'k-anon n_total>=5.';

-- =============================================================================
-- 8. mv_colon_subdistrict — Colon screening abnormal per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_colon_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_colon_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id, (msd_colon = 1) AS abnormal
    FROM bma_med.app1_labhealth
    WHERE patient_id IS NOT NULL AND msd_colon IS NOT NULL
    UNION ALL
    SELECT patient_id, (msd_colon = 1) AS abnormal
    FROM bma_med.portal_labhealth
    WHERE patient_id IS NOT NULL AND msd_colon IS NOT NULL
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_colon_subdistrict
  ON public.mv_colon_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_colon_subdistrict_dc
  ON public.mv_colon_subdistrict (district_code);

GRANT SELECT ON public.mv_colon_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_colon_subdistrict IS
  'Colon screening abnormal rate per subdistrict (mirrors mig 255). '
  'k-anon n_total>=5.';

-- =============================================================================
-- 9. mv_obesity_subdistrict — Obesity (BMI ≥ 23) per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_obesity_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_obesity_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id, (bmi_calc >= 23 AND bmi_calc < 80) AS abnormal
    FROM bma_med.app1_vitalsignslf
    WHERE patient_id IS NOT NULL AND bmi_calc IS NOT NULL AND bmi_calc > 0 AND bmi_calc < 80
    UNION ALL
    SELECT patient_id, (bmi_calc >= 23 AND bmi_calc < 80) AS abnormal
    FROM bma_med.portal_vitalsignslf
    WHERE patient_id IS NOT NULL AND bmi_calc IS NOT NULL AND bmi_calc > 0 AND bmi_calc < 80
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_obesity_subdistrict
  ON public.mv_obesity_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_obesity_subdistrict_dc
  ON public.mv_obesity_subdistrict (district_code);

GRANT SELECT ON public.mv_obesity_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_obesity_subdistrict IS
  'Obesity (BMI>=23) rate per subdistrict (mirrors mig 256). '
  'k-anon n_total>=5.';

-- =============================================================================
-- 10. mv_cvd_subdistrict — CVD (EKG abnormal) per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_cvd_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_cvd_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id, (msd_cvd_ekg = 1) AS abnormal
    FROM bma_med.app1_vitalsignslf
    WHERE patient_id IS NOT NULL AND msd_cvd_ekg IS NOT NULL
    UNION ALL
    SELECT patient_id, (msd_cvd_ekg = 1) AS abnormal
    FROM bma_med.portal_vitalsignslf
    WHERE patient_id IS NOT NULL AND msd_cvd_ekg IS NOT NULL
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_cvd_subdistrict
  ON public.mv_cvd_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_cvd_subdistrict_dc
  ON public.mv_cvd_subdistrict (district_code);

GRANT SELECT ON public.mv_cvd_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_cvd_subdistrict IS
  'CVD (EKG abnormal) rate per subdistrict (mirrors mig 258). '
  'k-anon n_total>=5.';

-- =============================================================================
-- 11. mv_dyslipidemia_subdistrict — Dyslipidemia per subdistrict
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS public.mv_dyslipidemia_subdistrict CASCADE;

CREATE MATERIALIZED VIEW public.mv_dyslipidemia_subdistrict AS
WITH
patient_abnormal AS (
  SELECT patient_id, bool_or(abnormal) AS abnormal
  FROM (
    SELECT patient_id,
           (cholest >= 200 AND cholest < 1000) AS abnormal
    FROM bma_med.app1_labhealth
    WHERE patient_id IS NOT NULL AND cholest IS NOT NULL AND cholest > 0 AND cholest < 1000
    UNION ALL
    SELECT patient_id,
           (CASE WHEN cholest ~ '^[0-9.]+$' THEN cholest::numeric BETWEEN 0 AND 1000 AND cholest::numeric >= 200 END) AS abnormal
    FROM bma_med.portal_labhealth
    WHERE patient_id IS NOT NULL
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
joined AS (
  SELECT
    ps.home_subdistrict_code AS subdistrict_code,
    ps.home_district_code    AS district_code,
    ps.patient_id,
    pa.abnormal
  FROM public.mv_patient_subdistrict ps
  JOIN patient_abnormal pa ON pa.patient_id = ps.patient_id
  WHERE pa.abnormal IS NOT NULL
)
SELECT
  subdistrict_code,
  district_code,
  COUNT(*)::bigint                              AS n_total,
  COUNT(*) FILTER (WHERE abnormal)::bigint      AS n_signal,
  ROUND(100.0 * COUNT(*) FILTER (WHERE abnormal) / NULLIF(COUNT(*), 0), 2) AS signal_pct
FROM joined
GROUP BY subdistrict_code, district_code
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_dyslipidemia_subdistrict
  ON public.mv_dyslipidemia_subdistrict (subdistrict_code);
CREATE INDEX idx_mv_dyslipidemia_subdistrict_dc
  ON public.mv_dyslipidemia_subdistrict (district_code);

GRANT SELECT ON public.mv_dyslipidemia_subdistrict
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_dyslipidemia_subdistrict IS
  'Dyslipidemia (cholest>=200) rate per subdistrict (mirrors mig 257). '
  'k-anon n_total>=5.';

-- =============================================================================
-- REFRESH all 12 MVs (helper first, then 11 disease MVs)
-- =============================================================================
REFRESH MATERIALIZED VIEW public.mv_patient_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_dm_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_hpt_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_ckd_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_liver_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_anemia_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_xray_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_cervical_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_colon_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_obesity_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_cvd_subdistrict;
REFRESH MATERIALIZED VIEW public.mv_dyslipidemia_subdistrict;

COMMIT;

-- =============================================================================
-- END migration 260
-- =============================================================================
