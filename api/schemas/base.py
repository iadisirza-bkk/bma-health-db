"""
Base Pydantic v2 schemas shared across all domain modules.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Base for all API response models."""
    model_config = ConfigDict(from_attributes=True)


class KAnonymityMeta(BaseModel):
    k_anonymity_threshold: int = 5
    suppressed_count: int = Field(0, description="Number of rows suppressed for privacy")


class ErrorResponse(BaseModel):
    error_code: str
    detail: str
    request_id: Optional[str] = None


class DiseaseSummaryItem(BaseModel):
    disease_key: str
    total_at_risk: int = 0
    pct: float = 0.0


class ZoneBrief(BaseModel):
    zone_code: str
    name_th: str
    total_screened: int = 0


class DistrictBrief(BaseModel):
    district_code: str
    district_name: Optional[str] = None
    zone_code: Optional[str] = None
    total_screened: int = 0
