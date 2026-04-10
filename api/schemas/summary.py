"""
Pydantic schemas for Summary endpoints.
Matches the exact output shapes of the current API.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import Field

from .base import DiseaseSummaryItem, HealthResponse, ZoneBrief


class OverviewResponse(HealthResponse):
    total_screened: int = 0
    target: int = 0
    zones_count: int = 0
    districts_count: int = 0
    last_updated: Optional[str] = None
    by_zone: list[ZoneBrief] = Field(default_factory=list)
    by_disease: list[DiseaseSummaryItem] = Field(default_factory=list)


class ZoneSummary(HealthResponse):
    zone_code: str
    name_th: str
    name_en: Optional[str] = None
    district_count: int = 0
    total_screened: int = 0
    diabetes: int = 0
    hypertension: int = 0
    cardiovascular: int = 0
    obesity: int = 0
    dyslipidemia: int = 0
    stroke: int = 0


class ZoneDetail(HealthResponse):
    zone_code: str
    name_th: str
    total_screened: int = 0
    districts: list[dict] = Field(default_factory=list)
    diseases: dict = Field(default_factory=dict)


class DistrictSummary(HealthResponse):
    district_code: str
    district_name: Optional[str] = None
    zone_code: Optional[str] = None
    total_screened: int = 0
    pct_risk_dm: Optional[float] = None
    pct_risk_hpt: Optional[float] = None
    pct_risk_cvd: Optional[float] = None


class FilteredSummaryResponse(HealthResponse):
    data: list[dict] = Field(default_factory=list)
    total: int = 0
    k_anonymity_applied: bool = True


class LabSummaryResponse(HealthResponse):
    data: list[dict] = Field(default_factory=list)


class MentalHealthResponse(HealthResponse):
    data: list[dict] = Field(default_factory=list)


class DemographicsResponse(HealthResponse):
    data: list[dict] = Field(default_factory=list)
