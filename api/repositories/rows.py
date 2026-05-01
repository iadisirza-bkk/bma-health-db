"""Pydantic v2 row models — one per chart `query_id` exposed by `MVRepository`.

These models describe the shape of a single row returned by the Repository
(NOT the wire-JSON shape — that's the ChartService's job). Every model uses
`extra="forbid"` so a typo in a column name fails loudly at parse time.

If a column is added to a backing MV, add it here too. Don't relax the
`extra` setting.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict


class _RowBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DistrictDiseaseRow(_RowBase):
    """One row of `summary_district_disease` (per data_source × district)."""

    data_source: str
    district_code: str
    total_screened: int
    risk_dm_count: int
    risk_hpt_count: int
    risk_cvd_count: int
    risk_bmi_count: int
    risk_stroke_count: int
    found_dm_count: int
    found_hpt_count: int
    found_cvd_count: int
    found_stroke_count: int
    found_obesity_count: int
    found_dyslipidemia_count: int
    pct_risk_dm: Optional[float] = None
    pct_risk_hpt: Optional[float] = None
    pct_risk_cvd: Optional[float] = None
    pct_found_dm: Optional[float] = None
    pct_found_hpt: Optional[float] = None
    pct_found_cvd: Optional[float] = None


class FacilityRow(_RowBase):
    """One row of `summary_facility` (per facility × source)."""

    facility_code: str
    data_source: str
    district_code: Optional[str] = None
    total_screened: int
    risk_dm: int
    risk_hpt: int
    risk_cvd: int
    risk_bmi: int
    found_dm: int
    found_hpt: int
    found_obesity: int
    found_dyslipidemia: int
    lab_completed: int
    first_screening: Optional[date] = None
    last_screening: Optional[date] = None


class DiseaseAgeSexRow(_RowBase):
    """One row of `summary_disease_age_sex` (district × sex × age_group)."""

    district_code: str
    sex: str
    age_group: str
    total_screened: int
    risk_dm: int
    risk_hpt: int
    risk_cvd: int
    risk_bmi: int
    risk_stroke: int
    found_dm: int
    found_hpt: int
    found_cvd: int
    found_stroke: int
    found_obesity: int
    found_dyslipidemia: int


class RiskFactorRow(_RowBase):
    """Long-format row for the NCD risk-factor profile chart.

    The backing MV `mv_summary_districts` is wide (one row per district,
    one column per factor). The repo pivots that into one row per factor
    so the chart spec can stay simple ``(factor_name × percentage)``.
    """

    factor_name: str
    factor_label_th: str
    percentage: Optional[float] = None
    persons: int


class BehaviorDiseaseRow(_RowBase):
    """One row of `mv_lifestyle` filtered to a single behavior (variable_key)."""

    district_code: str
    source_code: str
    variable_key: str
    value: Optional[str] = None
    persons: int


class AgePyramidRow(_RowBase):
    """One row of `mv_demographics` (district × source × sex × age_band)."""

    district_code: str
    source_code: str
    sex_code: str
    age_band: str
    persons: int


class ScreeningCoverageRow(_RowBase):
    """Per-district screening coverage — `mv_summary_districts` projected
    onto the screened-vs-population pair plus zone metadata."""

    district_code: str
    district_name: Optional[str] = None
    zone_code: Optional[str] = None
    total_screened: int
    population: Optional[int] = None
    pct_coverage: Optional[float] = None


class RepeatScreeningRow(_RowBase):
    """One row of `mv_kpi_tier1` — distinct persons vs visit count per
    (district × source × bucket). visits/persons ratio = repeat-screening."""

    district_code: str
    source_code: str
    bucket: str
    persons: int
    visits: int


class LabDistributionRow(_RowBase):
    """One row of `mv_lab_distribution` filtered to a single lab marker."""

    district_code: str
    source_code: str
    lab_marker: str
    value_bin: int
    n: int


class MentalRow(_RowBase):
    """One row of `mv_mental_health` (district × source × phq9 × st5 band)."""

    district_code: str
    source_code: str
    phq9_band: str
    st5_band: str
    persons: int
