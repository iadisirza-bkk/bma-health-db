"""SQL migration generator — emits 4 MVs from a DiseaseSpec:

  mv_<key>_classification           (BKK, district × 4-bit pattern)
  mv_<key>_factors                  (BKK, zone × 6 factors)
  mv_<key>_factors_district         (BKK, district × 6 factors)
  mv_<key>_factors_region           (non-BKK, region × 6 factors)

The lab axis (c4) CTE is injected verbatim from spec.lab — see
diseases.py for the helper builders.

Convention: has_<disease> = c1 OR c2 OR c4   (family/c3 excluded — it is
hereditary risk, not active disease state, so cross-tabbing it against
"any signal" is tautological. Family is still emitted as a separate axis
in the 4-bit pattern and as its own factor row.)
"""
from __future__ import annotations

from ..diseases import DiseaseSpec


def _diag_ctes(spec: DiseaseSpec) -> str:
    """The diag_app1 / diag_portal / diag_all CTEs.

    App1 sources are numeric (1=yes); portal sources are text ('1'/'true'/'TRUE').
    `spec.diag_sources` lists which tables carry the c2 column — defaults
    to all 4 (vitalsignslf + homehealth × app1 + portal). Override per
    disease for cases like CVD where the column only exists in some.
    """
    col = spec.c2_diag_col
    app1_tables = [t for t in spec.diag_sources if t.startswith("app1_")]
    portal_tables = [t for t in spec.diag_sources if t.startswith("portal_")]

    def _union(tables: list[str]) -> str:
        if not tables:
            return f"SELECT NULL::bigint AS patient_id, NULL::text AS {col}_v WHERE FALSE"
        # Cast every source to text — mixed types (smallint/numeric/text)
        # across raw tables otherwise break the UNION ALL.
        parts = [f"SELECT patient_id, {col}::text AS {col}_v FROM bma_med.{t} WHERE patient_id IS NOT NULL" for t in tables]
        return "\n    UNION ALL\n    ".join(parts)

    app1_union = _union(app1_tables)
    portal_union = _union(portal_tables)
    return f"""diag_app1 AS (
  SELECT patient_id, bool_or({col}_v IN ('1','1.0','true','TRUE')) AS diag
  FROM (
    {app1_union}
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
diag_portal AS (
  SELECT patient_id, bool_or({col}_v IN ('1','1.0','true','TRUE')) AS diag
  FROM (
    {portal_union}
  ) u
  WHERE patient_id IS NOT NULL
  GROUP BY patient_id
),
diag_all AS (
  SELECT patient_id, bool_or(diag) AS c2_diag
  FROM (SELECT * FROM diag_app1 UNION ALL SELECT * FROM diag_portal) u
  GROUP BY patient_id
)"""


def _fam_ctes(spec: DiseaseSpec) -> str:
    """Family-history CTEs (parent-only — column is pXxx in homehealth)."""
    col = spec.c3_family_col
    return f"""fam_app1 AS (
  SELECT patient_id, bool_or({col} = 1) AS p_disease
  FROM bma_med.app1_homehealth WHERE patient_id IS NOT NULL GROUP BY patient_id
),
fam_portal AS (
  SELECT patient_id, bool_or({col} IN ('1','true','TRUE')) AS p_disease
  FROM bma_med.portal_homehealth WHERE patient_id IS NOT NULL GROUP BY patient_id
),
fam_all AS (
  SELECT patient_id, bool_or(p_disease) AS c3_family
  FROM (SELECT * FROM fam_app1 UNION ALL SELECT * FROM fam_portal) u
  GROUP BY patient_id
)"""


def _lab_cte(spec: DiseaseSpec) -> str:
    """The labs CTE — wraps the bespoke per-disease SQL.

    If `sql_portal` is empty, the source is mv_visit_resolved (already
    union'd across sources) so we skip the portal branch.
    """
    name = spec.lab.name
    if spec.lab.sql_portal.strip():
        return f"""labs AS (
  SELECT patient_id, bool_or({name}) AS {name}
  FROM (
{_indent(spec.lab.sql_app1, 4)}
    UNION ALL
{_indent(spec.lab.sql_portal, 4)}
  ) u
  GROUP BY patient_id
)"""
    # Single-source CTE (e.g. HPT BP from mv_visit_resolved)
    return f"""labs AS (
  SELECT patient_id, bool_or({name}) AS {name}
  FROM (
{_indent(spec.lab.sql_app1, 4)}
  ) u
  GROUP BY patient_id
)"""


def _risk_cte(spec: DiseaseSpec) -> str:
    return f"""risk_per_patient AS (
  SELECT patient_id, bool_or({spec.c1_risk_col}) AS c1_risk
  FROM public.mv_visit_resolved
  WHERE is_dedup_kept = TRUE AND home_district_code IS NOT NULL
  GROUP BY patient_id
)"""


def _indent(s: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in s.splitlines())


# ── Factor CTEs (shared across all factor MVs — same idiom for every disease) ──

_FACTOR_CTES = """smoke_app1 AS (
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
)"""


def _factor_rows(spec: DiseaseSpec, *, geo_col: str, has_col: str, extra_cols: str = "") -> str:
    """Long-format factor rows (BMI / smoke / alcohol / exercise / age / family).

    `geo_col` is 'zone_code' / 'district_code' / 'region_code' depending on
    the MV. `has_col` is 'has_<key>'. `extra_cols` is additional pd.<col>
    references to pass through to the final aggregate (e.g. zone_code,
    district_name when geo_col='district_code').
    """
    family_factor_key = f"family_{spec.key}"
    extra_proj = (", " + extra_cols) if extra_cols else ""
    return f"""factor_rows AS (
  -- BMI
  SELECT pd.patient_id, pd.{geo_col}{extra_proj}, pd.{has_col},
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
  FROM patient_disease pd JOIN bmi_per_patient b USING (patient_id)
  WHERE b.bmi IS NOT NULL

  UNION ALL
  -- Smoke
  SELECT pd.patient_id, pd.{geo_col}{extra_proj}, pd.{has_col},
         'smoke',
         CASE s.smk WHEN 0 THEN 'non_smoker' WHEN 1 THEN 'former_smoker' WHEN 2 THEN 'current_smoker' END,
         CASE s.smk WHEN 0 THEN 'ไม่สูบ' WHEN 1 THEN 'เคยสูบ' WHEN 2 THEN 'สูบปัจจุบัน' END,
         CASE s.smk WHEN 0 THEN 'Non-smoker' WHEN 1 THEN 'Former smoker' WHEN 2 THEN 'Current smoker' END
  FROM patient_disease pd JOIN smoke_all s USING (patient_id)
  WHERE s.smk IN (0,1,2)

  UNION ALL
  -- Alcohol
  SELECT pd.patient_id, pd.{geo_col}{extra_proj}, pd.{has_col},
         'alcohol',
         CASE a.alc WHEN 0 THEN 'non_drinker' WHEN 1 THEN 'former_drinker' WHEN 2 THEN 'current_drinker' END,
         CASE a.alc WHEN 0 THEN 'ไม่ดื่ม' WHEN 1 THEN 'เคยดื่ม' WHEN 2 THEN 'ดื่มปัจจุบัน' END,
         CASE a.alc WHEN 0 THEN 'Non-drinker' WHEN 1 THEN 'Former drinker' WHEN 2 THEN 'Current drinker' END
  FROM patient_disease pd JOIN alcohol_all a USING (patient_id)
  WHERE a.alc IN (0,1,2)

  UNION ALL
  -- Excercise
  SELECT pd.patient_id, pd.{geo_col}{extra_proj}, pd.{has_col},
         'excercise',
         CASE e.exc WHEN 1 THEN 'regular' WHEN 2 THEN 'sometimes' WHEN 3 THEN 'never' END,
         CASE e.exc WHEN 1 THEN 'ออกกำลังเป็นประจำ' WHEN 2 THEN 'ออกกำลังเป็นบางครั้ง' WHEN 3 THEN 'ไม่ออกกำลัง' END,
         CASE e.exc WHEN 1 THEN 'Regular exercise' WHEN 2 THEN 'Sometimes' WHEN 3 THEN 'Never' END
  FROM patient_disease pd JOIN excercise_all e USING (patient_id)
  WHERE e.exc IN (1,2,3)

  UNION ALL
  -- Age group
  SELECT pd.patient_id, pd.{geo_col}{extra_proj}, pd.{has_col},
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
  FROM patient_disease pd JOIN age_per_patient a USING (patient_id)
  WHERE a.age >= 18

  UNION ALL
  -- Family <disease>
  SELECT pd.patient_id, pd.{geo_col}{extra_proj}, pd.{has_col},
         '{family_factor_key}',
         CASE WHEN pd.c3_family THEN 'family_yes' ELSE 'family_no' END,
         CASE WHEN pd.c3_family THEN 'มีประวัติครอบครัว' ELSE 'ไม่มีประวัติครอบครัว' END,
         CASE WHEN pd.c3_family THEN 'Family history' ELSE 'No family history' END
  FROM patient_disease pd
)"""


# ── Per-MV generators ──────────────────────────────────────────────────

def gen_classification_mv(spec: DiseaseSpec) -> str:
    """mv_<key>_classification: district × 4-bit pattern, k-anon n>=5."""
    k = spec.key
    return f"""-- =============================================================================
-- 1. mv_{k}_classification — district × 4-bit pattern (c1c2c3c4)
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS public.mv_{k}_classification CASCADE;

CREATE MATERIALIZED VIEW public.mv_{k}_classification AS
WITH
{_diag_ctes(spec)},
{_fam_ctes(spec)},
{_lab_cte(spec)},
{_risk_cte(spec)},

patient_flags AS (
  SELECT
    v.patient_id,
    v.home_district_code AS district_code,
    v.{spec.c1_risk_col}                          AS c1_risk,
    COALESCE(d.c2_diag,            FALSE)         AS c2_diag,
    COALESCE(f.c3_family,          FALSE)         AS c3_family,
    COALESCE(l.{spec.lab.name},    FALSE)         AS c4_lab
  FROM public.mv_visit_resolved v
  LEFT JOIN diag_all d ON d.patient_id = v.patient_id
  LEFT JOIN fam_all  f ON f.patient_id = v.patient_id
  LEFT JOIN labs     l ON l.patient_id = v.patient_id
  WHERE v.is_dedup_kept = TRUE
    AND v.home_district_code IS NOT NULL
    AND v.home_district_code ~ '^10[0-9]{{2}}$'
)
SELECT
  district_code,
  CONCAT(c1_risk::int, c2_diag::int, c3_family::int, c4_lab::int) AS pattern,
  COUNT(*)::bigint AS n_patients
FROM patient_flags
GROUP BY district_code, pattern
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_mv_{k}_classification
  ON public.mv_{k}_classification (district_code, pattern);
CREATE INDEX idx_mv_{k}_classification_dc
  ON public.mv_{k}_classification (district_code);

GRANT SELECT ON public.mv_{k}_classification
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.mv_{k}_classification IS
  '4-bit {spec.short_upper} classification per BKK district. '
  'pattern = c1c2c3c4 where c1=risk, c2=diag, c3=family, c4=lab. '
  'k-anon: cells with n<5 dropped at MV build.';

REFRESH MATERIALIZED VIEW public.mv_{k}_classification;
"""


def gen_factors_mv(spec: DiseaseSpec, *, scope: str) -> str:
    """mv_<key>_factors (zone) or mv_<key>_factors_district (district)."""
    k = spec.key
    has_col = f"has_{k}"
    if scope == "zone":
        mv_name = f"mv_{k}_factors"
        geo_col = "zone_code"
        comment_geo = "zone"
        patient_geo_cte = """patient_zone AS (
  SELECT DISTINCT v.patient_id, d.zone_code
  FROM public.mv_visit_resolved v
  JOIN public.ref_districts d ON d.dcode = v.home_district_code
  WHERE v.is_dedup_kept = TRUE
    AND v.home_district_code IS NOT NULL
    AND d.zone_code IS NOT NULL
)"""
        join_alias = "pz"
        from_clause = f"FROM patient_zone {join_alias}"
        patient_extra_cols = ""        # patient_disease CTE extra projection
        factor_extra_cols = ""         # factor_rows CTE pass-through
        select_extra_cols = ""
        select_extra_groupby = ""
    elif scope == "district":
        mv_name = f"mv_{k}_factors_district"
        geo_col = "district_code"
        comment_geo = "district"
        patient_geo_cte = """patient_dist AS (
  SELECT DISTINCT v.patient_id,
         v.home_district_code AS district_code,
         d.zone_code,
         d.name_th AS district_name
  FROM public.mv_visit_resolved v
  JOIN public.ref_districts d ON d.dcode = v.home_district_code
  WHERE v.is_dedup_kept = TRUE
    AND v.home_district_code IS NOT NULL
    AND d.zone_code IS NOT NULL
)"""
        join_alias = "pz"
        from_clause = f"FROM patient_dist {join_alias}"
        patient_extra_cols = f"{join_alias}.zone_code, {join_alias}.district_name,"
        factor_extra_cols = "pd.zone_code, pd.district_name"
        select_extra_cols = ",\n  zone_code,\n  district_name"
        select_extra_groupby = ", zone_code, district_name"
    else:
        raise ValueError(f"Unknown factors scope: {scope}")

    indices = []
    if scope == "zone":
        indices = [
            f"CREATE UNIQUE INDEX uq_{mv_name} ON public.{mv_name} (zone_code, factor_key, factor_group);",
            f"CREATE INDEX idx_{mv_name}_zone ON public.{mv_name} ({geo_col});",
        ]
    else:
        indices = [
            f"CREATE UNIQUE INDEX uq_{mv_name} ON public.{mv_name} (district_code, factor_key, factor_group);",
            f"CREATE INDEX idx_{mv_name}_dc ON public.{mv_name} (district_code);",
            f"CREATE INDEX idx_{mv_name}_zc ON public.{mv_name} (zone_code);",
        ]
    indices_sql = "\n".join(indices)

    return f"""-- =============================================================================
-- {2 if scope == 'zone' else 3}. {mv_name} — {comment_geo} × 6 factors
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS public.{mv_name} CASCADE;

CREATE MATERIALIZED VIEW public.{mv_name} AS
WITH
{patient_geo_cte},
{_diag_ctes(spec)},
{_fam_ctes(spec)},
{_lab_cte(spec)},
{_risk_cte(spec)},

patient_disease AS (
  -- has_{k} = c1 OR c2 OR c4 only (family/c3 excluded — hereditary risk
  -- not active disease state; see scaffold/diseases.py header).
  SELECT
    {join_alias}.patient_id,
    {join_alias}.{geo_col}, {patient_extra_cols}
    (COALESCE(r.c1_risk, FALSE)
       OR COALESCE(d.c2_diag, FALSE)
       OR COALESCE(l.{spec.lab.name}, FALSE)) AS {has_col},
    COALESCE(f.c3_family, FALSE) AS c3_family
  {from_clause}
  LEFT JOIN risk_per_patient r ON r.patient_id = {join_alias}.patient_id
  LEFT JOIN diag_all          d ON d.patient_id = {join_alias}.patient_id
  LEFT JOIN fam_all           f ON f.patient_id = {join_alias}.patient_id
  LEFT JOIN labs              l ON l.patient_id = {join_alias}.patient_id
),

{_FACTOR_CTES},

{_factor_rows(spec, geo_col=geo_col, has_col=has_col, extra_cols=factor_extra_cols)}

SELECT
  {geo_col}{select_extra_cols},
  factor_key,
  factor_group,
  factor_group_th,
  factor_group_en,
  COUNT(*)::bigint                            AS n,
  COUNT(*) FILTER (WHERE {has_col})::bigint   AS {k}_n,
  ROUND(100.0 * COUNT(*) FILTER (WHERE {has_col}) / NULLIF(COUNT(*), 0), 2) AS {k}_pct
FROM factor_rows
WHERE factor_group IS NOT NULL
GROUP BY {geo_col}{select_extra_groupby}, factor_key, factor_group, factor_group_th, factor_group_en
HAVING COUNT(*) >= 5
WITH NO DATA;

{indices_sql}

GRANT SELECT ON public.{mv_name}
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.{mv_name} IS
  '{spec.short_upper} × risk-factor cross-tab per {comment_geo} (BKK only). '
  '{k}_n = patients with c1/c2/c4 (family/c3 excluded). k-anon n>=5.';

REFRESH MATERIALIZED VIEW public.{mv_name};
"""


def gen_factors_region_mv(spec: DiseaseSpec) -> str:
    """mv_<key>_factors_region: non-BKK 4-region (N/NE/S/C) factor cross-tab."""
    k = spec.key
    has_col = f"has_{k}"
    mv_name = f"mv_{k}_factors_region"
    return f"""-- =============================================================================
-- 4. {mv_name} — non-BKK region × 6 factors (4 regions: N/NE/S/C)
-- =============================================================================
DROP MATERIALIZED VIEW IF EXISTS public.{mv_name} CASCADE;

CREATE MATERIALIZED VIEW public.{mv_name} AS
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
    AND v.home_district_code ~ '^[0-9]{{4}}$'
),
patient_region_filtered AS (
  SELECT patient_id, region_code FROM patient_region WHERE region_code IS NOT NULL
),
{_diag_ctes(spec)},
{_fam_ctes(spec)},
{_lab_cte(spec)},
{_risk_cte(spec)},

patient_disease AS (
  -- has_{k} = c1 OR c2 OR c4 only (family/c3 excluded).
  SELECT
    pr.patient_id,
    pr.region_code,
    (COALESCE(r.c1_risk, FALSE)
       OR COALESCE(d.c2_diag, FALSE)
       OR COALESCE(l.{spec.lab.name}, FALSE)) AS {has_col},
    COALESCE(f.c3_family, FALSE) AS c3_family
  FROM patient_region_filtered pr
  LEFT JOIN risk_per_patient r ON r.patient_id = pr.patient_id
  LEFT JOIN diag_all          d ON d.patient_id = pr.patient_id
  LEFT JOIN fam_all           f ON f.patient_id = pr.patient_id
  LEFT JOIN labs              l ON l.patient_id = pr.patient_id
),

{_FACTOR_CTES},

{_factor_rows(spec, geo_col='region_code', has_col=has_col)}

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
  COUNT(*) FILTER (WHERE {has_col})::bigint   AS {k}_n,
  ROUND(100.0 * COUNT(*) FILTER (WHERE {has_col}) / NULLIF(COUNT(*), 0), 2) AS {k}_pct
FROM factor_rows
WHERE factor_group IS NOT NULL
GROUP BY region_code, factor_key, factor_group, factor_group_th, factor_group_en
HAVING COUNT(*) >= 5
WITH NO DATA;

CREATE UNIQUE INDEX uq_{mv_name}
  ON public.{mv_name} (region_code, factor_key, factor_group);
CREATE INDEX idx_{mv_name}_rc
  ON public.{mv_name} (region_code);

GRANT SELECT ON public.{mv_name}
  TO bma_med_reader, bma_med_clinician, bma_med_loader,
     bma_etl_writer, bma_dba_admin, bma_api_reader;

COMMENT ON MATERIALIZED VIEW public.{mv_name} IS
  '{spec.short_upper} × risk-factor cross-tab per non-BKK region. '
  '{k}_n = patients with c1/c2/c4 (family/c3 excluded). k-anon n>=5.';

REFRESH MATERIALIZED VIEW public.{mv_name};
"""


def gen_migration(spec: DiseaseSpec) -> str:
    """Full migration SQL: 4 MVs in one transaction."""
    n = spec.migration_number
    header = f"""-- =============================================================================
-- Migration {n} — {spec.short_upper} pipeline (mv_{spec.key}_*)
-- =============================================================================
-- Auto-generated by scaffold/scaffold.py from scaffold/diseases.py.
-- Edit the DiseaseSpec, then re-run the scaffolder. Do NOT hand-edit this file.
--
-- Builds 4 materialized views:
--   mv_{spec.key}_classification          BKK district × 4-bit pattern
--   mv_{spec.key}_factors                 BKK zone × 6 factors
--   mv_{spec.key}_factors_district        BKK district × 6 factors
--   mv_{spec.key}_factors_region          non-BKK 4-region × 6 factors
--
-- Pattern semantics:
--   c1 = risk    ({spec.c1_risk_col} on mv_visit_resolved)
--   c2 = diag    ({spec.c2_diag_col} self-reported in vitalsignslf/homehealth)
--   c3 = family  ({spec.c3_family_col} parent in homehealth)
--   c4 = {spec.lab.name}  ({spec.lab.chip_label_en})
--
-- has_{spec.key} = c1 OR c2 OR c4 (family/c3 excluded — hereditary risk).
-- =============================================================================

BEGIN;

"""
    return (
        header
        + gen_classification_mv(spec)
        + "\n\n"
        + gen_factors_mv(spec, scope="zone")
        + "\n\n"
        + gen_factors_mv(spec, scope="district")
        + "\n\n"
        + gen_factors_region_mv(spec)
        + f"""

COMMIT;

-- =============================================================================
-- END migration {n}
-- =============================================================================
"""
    )
