"""
Pydantic schemas for GIS endpoints — facility locations, PM2.5, heatmaps.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field

from .base import HealthResponse


class FacilityLocation(HealthResponse):
    code: str
    name_th: Optional[str] = None
    name_en: Optional[str] = None
    facility_type: Optional[str] = None
    district_code: Optional[str] = None
    zone_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    address: Optional[str] = None
    telephone: Optional[str] = None
    total_screened: Optional[int] = None


class DiseaseHeatmapPoint(HealthResponse):
    district_code: str
    district_name: Optional[str] = None
    centroid_lat: Optional[float] = None
    centroid_lng: Optional[float] = None
    total_screened: int = 0
    disease_key: str = ""
    disease_pct: float = 0.0


class PM25Reading(HealthResponse):
    station_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    pm25_value: Optional[float] = None
    aqi: Optional[int] = None
    measured_at: Optional[str] = None


class DiseaseEnvironmentOverlay(HealthResponse):
    district_code: str
    district_name: Optional[str] = None
    disease_pct: float = 0.0
    avg_pm25: Optional[float] = None
    pollution_source_count: int = 0


class FacilityListResponse(HealthResponse):
    total: int = 0
    data: list[FacilityLocation] = Field(default_factory=list)
