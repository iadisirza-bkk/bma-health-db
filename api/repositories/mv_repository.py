"""MVRepository — the only place chart SQL lives (ADR-01).

Every method:
  * reads from `public.mv_*` (or `public.summary_*`) — never `bma_med.*`,
    because the api_reader role doesn't have grants on bma_med (by design)
  * uses psycopg2 `%s` placeholders for parameters — NEVER f-strings on
    user input
  * caps the result with `LIMIT 10000` as a belt-and-braces ceiling
  * returns Pydantic v2 row models declared in `rows.py`

Whitelisted enum-style parameters (`behavior`, `lab_marker`) use `typing.Literal`
so even calling the method with an unknown value is rejected at the type
checker / Pydantic boundary — the value never reaches the SQL.

Adding a new chart query:
  1. Add a row model in `rows.py`.
  2. Add a method below, decorated with `@MVRepository.register("query_id")`.
  3. Wire it from a YAML chart spec via the same `query_id`.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel

from .base import QueryNotFound, Repository
from .rows import (
    AgePyramidRow,
    BehaviorDiseaseRow,
    DiseaseAgeSexRow,
    DistrictDiseaseRow,
    FacilityRow,
    LabDistributionRow,
    MentalRow,
    RepeatScreeningRow,
    RiskFactorRow,
    ScreeningCoverageRow,
)

# Whitelists for free-text-looking params. Both also appear as `Literal[...]`
# on the corresponding method signatures — this constant exists so the
# Service layer can introspect the registry without importing typing internals.
ALLOWED_BEHAVIORS = ("smoke", "alcohol", "exercise")
ALLOWED_LAB_MARKERS = (
    "hmgb", "hmtc", "fbs", "cholest", "hdl", "ldl", "trigly",
    "crtinine", "egfrrs", "sgot", "sgpt", "uricacid",
)

# `mv_lifestyle.variable_key` uses 'smoking'/'alcohol'/'exercise' — map our
# friendlier `behavior` keyword onto it without exposing the rename to SQL.
_BEHAVIOR_TO_VARIABLE_KEY = {
    "smoke": "smoking",
    "alcohol": "alcohol",
    "exercise": "exercise",
}

# Hard ceiling on every chart query — no chart should ever need >10k rows.
_LIMIT = 10000


# Module-level shared registry — the `MVRepository.register` classmethod
# below proxies to this dict. Defined here (not inside the class body) so the
# decorator can be invoked from the same class body that defines it
# (Python 3.9 forbids using a classmethod descriptor that way).
_QUERY_REGISTRY: dict[str, Callable[..., Awaitable[list[BaseModel]]]] = {}


def _register(query_id: str) -> Callable:
    """Decorator: bind a method to a `query_id` in the module-level registry.

    Raises ValueError if the same `query_id` is registered twice — that's
    always a bug.
    """
    def _decorator(fn: Callable) -> Callable:
        if query_id in _QUERY_REGISTRY:
            raise ValueError(
                f"query_id {query_id!r} already registered to "
                f"{_QUERY_REGISTRY[query_id].__qualname__}"
            )
        _QUERY_REGISTRY[query_id] = fn
        return fn
    return _decorator


class MVRepository(Repository):
    """Materialized-view-backed chart Repository.

    Methods are registered against `query_id` strings via the
    `@MVRepository.register(...)` decorator, then dispatched at request time
    by `run_query(query_id, params)`.
    """

    # Class-level alias of the module-level registry — exposed so callers
    # (and tests) can introspect "what queries are registered?".
    _queries: ClassVar[dict[str, Callable[..., Awaitable[list[BaseModel]]]]] = _QUERY_REGISTRY

    # ------------------------------------------------------------------ #
    # Registry mechanics
    # ------------------------------------------------------------------ #
    register = staticmethod(_register)

    async def run_query(
        self,
        query_id: str,
        params: Optional[dict[str, Any]] = None,
    ) -> list[BaseModel]:
        """Central dispatch — look up `query_id` and call its method."""
        fn = _QUERY_REGISTRY.get(query_id)
        if fn is None:
            raise QueryNotFound(
                f"No query registered for {query_id!r}. "
                f"Known query_ids: {sorted(_QUERY_REGISTRY)}"
            )
        return await fn(self, **(params or {}))

    # ------------------------------------------------------------------ #
    # Chart query implementations
    # ------------------------------------------------------------------ #

    @_register("district_disease_counts")
    async def district_disease_counts(
        self,
        district: Optional[str] = None,
        zone: Optional[str] = None,
    ) -> list[DistrictDiseaseRow]:
        """Per-district disease counts. Optional district + zone filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if district is not None:
            clauses.append("s.district_code = %s")
            params.append(district)
        if zone is not None:
            clauses.append("rd.zone_code = %s")
            params.append(zone)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT s.data_source, s.district_code,
                   s.total_screened,
                   s.risk_dm_count, s.risk_hpt_count, s.risk_cvd_count,
                   s.risk_bmi_count, s.risk_stroke_count,
                   s.found_dm_count, s.found_hpt_count, s.found_cvd_count,
                   s.found_stroke_count, s.found_obesity_count,
                   s.found_dyslipidemia_count,
                   s.pct_risk_dm, s.pct_risk_hpt, s.pct_risk_cvd,
                   s.pct_found_dm, s.pct_found_hpt, s.pct_found_cvd
              FROM public.summary_district_disease s
              LEFT JOIN public.ref_districts rd ON rd.dcode = s.district_code
              {where}
             ORDER BY s.district_code, s.data_source
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [DistrictDiseaseRow(**r) for r in rows]

    @_register("facility_screening")
    async def facility_screening(
        self,
        district: Optional[str] = None,
    ) -> list[FacilityRow]:
        """Per-facility screening + diagnosis counts."""
        clauses: list[str] = []
        params: list[Any] = []
        if district is not None:
            clauses.append("district_code = %s")
            params.append(district)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT facility_code, data_source, district_code,
                   total_screened,
                   risk_dm, risk_hpt, risk_cvd, risk_bmi,
                   found_dm, found_hpt, found_obesity, found_dyslipidemia,
                   lab_completed, first_screening, last_screening
              FROM public.summary_facility
              {where}
             ORDER BY total_screened DESC, facility_code
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [FacilityRow(**r) for r in rows]

    @_register("disease_age_sex")
    async def disease_age_sex(
        self,
        district: Optional[str] = None,
        zone: Optional[str] = None,
    ) -> list[DiseaseAgeSexRow]:
        """District × sex × age cross-tab of disease counts."""
        clauses: list[str] = []
        params: list[Any] = []
        if district is not None:
            clauses.append("s.district_code = %s")
            params.append(district)
        if zone is not None:
            clauses.append("rd.zone_code = %s")
            params.append(zone)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT s.district_code, s.sex, s.age_group,
                   s.total_screened,
                   s.risk_dm, s.risk_hpt, s.risk_cvd, s.risk_bmi, s.risk_stroke,
                   s.found_dm, s.found_hpt, s.found_cvd, s.found_stroke,
                   s.found_obesity, s.found_dyslipidemia
              FROM public.summary_disease_age_sex s
              LEFT JOIN public.ref_districts rd ON rd.dcode = s.district_code
              {where}
             ORDER BY s.district_code, s.sex, s.age_group
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [DiseaseAgeSexRow(**r) for r in rows]

    # Display labels for the four factors. The bar chart x-axis comes
    # from `factor_label_th` so admins can re-label without code change.
    _RISK_FACTOR_LABELS: ClassVar[dict[str, str]] = {
        "dm": "เบาหวาน",
        "hpt": "ความดันโลหิตสูง",
        "cvd": "หัวใจ-หลอดเลือด",
        "bmi": "BMI สูง",
    }

    @_register("risk_factor_profile")
    async def risk_factor_profile(
        self,
        district: Optional[str] = None,
        zone: Optional[str] = None,
    ) -> list[RiskFactorRow]:
        """Long-format NCD risk factor profile.

        Returns 4 rows (`dm`, `hpt`, `cvd`, `bmi`) with `(factor_name,
        percentage, persons)`. The MV is wide-format; we aggregate
        across the optional district / zone scope and pivot in SQL using
        UNION ALL so the answer comes back ready-to-plot.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if district is not None:
            clauses.append("d.district_code = %s")
            params.append(district)
        if zone is not None:
            clauses.append("d.zone_code = %s")
            params.append(zone)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            WITH agg AS (
                SELECT
                    SUM(d.total_screened)     AS total_screened,
                    SUM(d.risk_dm_count)      AS dm,
                    SUM(d.risk_hpt_count)     AS hpt,
                    SUM(d.risk_cvd_count)     AS cvd,
                    SUM(d.risk_bmi_count)     AS bmi
                  FROM public.mv_summary_districts d
                  {where}
            )
            SELECT 'dm'  AS factor_name, %s AS factor_label_th,
                   CASE WHEN total_screened > 0
                        THEN ROUND(100.0 * dm / total_screened::numeric, 2)
                        ELSE NULL END  AS percentage,
                   COALESCE(dm, 0)::int AS persons
              FROM agg
            UNION ALL
            SELECT 'hpt', %s,
                   CASE WHEN total_screened > 0
                        THEN ROUND(100.0 * hpt / total_screened::numeric, 2)
                        ELSE NULL END,
                   COALESCE(hpt, 0)::int
              FROM agg
            UNION ALL
            SELECT 'cvd', %s,
                   CASE WHEN total_screened > 0
                        THEN ROUND(100.0 * cvd / total_screened::numeric, 2)
                        ELSE NULL END,
                   COALESCE(cvd, 0)::int
              FROM agg
            UNION ALL
            SELECT 'bmi', %s,
                   CASE WHEN total_screened > 0
                        THEN ROUND(100.0 * bmi / total_screened::numeric, 2)
                        ELSE NULL END,
                   COALESCE(bmi, 0)::int
              FROM agg
        """
        # Pass labels as bind params (NOT f-string'd into SQL) so they
        # cannot be used to inject SQL even though they're trusted const.
        labels = self._RISK_FACTOR_LABELS
        params_full = list(params) + [
            labels["dm"], labels["hpt"], labels["cvd"], labels["bmi"],
        ]
        rows = await self.fetch_all(sql, params_full)
        return [RiskFactorRow(**r) for r in rows]

    @_register("behavior_disease_correlation")
    async def behavior_disease_correlation(
        self,
        district: Optional[str] = None,
        behavior: Literal["smoke", "alcohol", "exercise"] = "smoke",
    ) -> list[BehaviorDiseaseRow]:
        """Distribution of a single behavior (smoking / alcohol / exercise)
        across the population, optionally filtered to one district.

        The behavior parameter is whitelisted via `Literal[...]` — never
        substituted as a substring."""
        if behavior not in _BEHAVIOR_TO_VARIABLE_KEY:
            raise ValueError(f"unknown behavior {behavior!r}")
        variable_key = _BEHAVIOR_TO_VARIABLE_KEY[behavior]

        clauses: list[str] = ["variable_key = %s"]
        params: list[Any] = [variable_key]
        if district is not None:
            clauses.append("district_code = %s")
            params.append(district)
        where = "WHERE " + " AND ".join(clauses)
        sql = f"""
            SELECT district_code, source_code, variable_key, value, persons
              FROM public.mv_lifestyle
              {where}
             ORDER BY district_code, source_code, value
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [BehaviorDiseaseRow(**r) for r in rows]

    @_register("age_pyramid")
    async def age_pyramid(
        self,
        district: Optional[str] = None,
    ) -> list[AgePyramidRow]:
        """Sex × age-band breakdown for a district (or citywide if NULL)."""
        clauses: list[str] = []
        params: list[Any] = []
        if district is not None:
            clauses.append("district_code = %s")
            params.append(district)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT district_code, source_code, sex_code, age_band, persons
              FROM public.mv_demographics
              {where}
             ORDER BY district_code, source_code, sex_code, age_band
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [AgePyramidRow(**r) for r in rows]

    @_register("screening_coverage")
    async def screening_coverage(
        self,
        district: Optional[str] = None,
    ) -> list[ScreeningCoverageRow]:
        """Screening coverage per district = total_screened / population.

        Joins `mv_summary_districts` to `ref_districts.population`."""
        clauses: list[str] = []
        params: list[Any] = []
        if district is not None:
            clauses.append("s.district_code = %s")
            params.append(district)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT s.district_code,
                   s.district_name,
                   s.zone_code,
                   s.total_screened,
                   rd.population,
                   CASE WHEN rd.population IS NULL OR rd.population = 0 THEN NULL
                        ELSE ROUND(100.0 * s.total_screened
                                       / rd.population::numeric, 2)
                   END AS pct_coverage
              FROM public.mv_summary_districts s
              LEFT JOIN public.ref_districts rd ON rd.dcode = s.district_code
              {where}
             ORDER BY s.district_code
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [ScreeningCoverageRow(**r) for r in rows]

    @_register("repeat_screening")
    async def repeat_screening(
        self,
        district: Optional[str] = None,
    ) -> list[RepeatScreeningRow]:
        """Repeat-screening signal — distinct persons vs visit count per
        (district × source × bucket). visits/persons > 1 = repeats."""
        clauses: list[str] = []
        params: list[Any] = []
        if district is not None:
            clauses.append("district_code = %s")
            params.append(district)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT district_code, source_code, bucket, persons, visits
              FROM public.mv_kpi_tier1
              {where}
             ORDER BY district_code, source_code, bucket
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [RepeatScreeningRow(**r) for r in rows]

    @_register("lab_distribution")
    async def lab_distribution(
        self,
        district: Optional[str] = None,
        lab_marker: Literal[
            "hmgb", "hmtc", "fbs", "cholest", "hdl", "ldl", "trigly",
            "crtinine", "egfrrs", "sgot", "sgpt", "uricacid",
        ] = "fbs",
    ) -> list[LabDistributionRow]:
        """Histogram (20-bin) for one lab marker across districts.

        `lab_marker` is whitelisted by `Literal[...]` so callers can never
        smuggle a SQL fragment through it."""
        if lab_marker not in ALLOWED_LAB_MARKERS:
            raise ValueError(f"unknown lab_marker {lab_marker!r}")
        clauses: list[str] = ["lab_marker = %s"]
        params: list[Any] = [lab_marker]
        if district is not None:
            clauses.append("district_code = %s")
            params.append(district)
        where = "WHERE " + " AND ".join(clauses)
        sql = f"""
            SELECT district_code, source_code, lab_marker, value_bin, n
              FROM public.mv_lab_distribution
              {where}
             ORDER BY district_code, source_code, value_bin
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [LabDistributionRow(**r) for r in rows]

    @_register("mental_health_distribution")
    async def mental_health_distribution(
        self,
        district: Optional[str] = None,
    ) -> list[MentalRow]:
        """PHQ-9 × ST-5 banded distribution of mental health screening."""
        clauses: list[str] = []
        params: list[Any] = []
        if district is not None:
            clauses.append("district_code = %s")
            params.append(district)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT district_code, source_code, phq9_band, st5_band, persons
              FROM public.mv_mental_health
              {where}
             ORDER BY district_code, source_code, phq9_band, st5_band
             LIMIT {_LIMIT}
        """
        rows = await self.fetch_all(sql, params)
        return [MentalRow(**r) for r in rows]
