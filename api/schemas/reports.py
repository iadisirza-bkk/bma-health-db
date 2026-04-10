"""Pydantic schemas for report generation API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ReportInfo(BaseModel):
    """Status information for a single report variant."""
    lang: str = Field(description="ISO language code")
    report_type: str = Field(description="'whitepaper' or 'slides'")
    cached: bool = Field(description="Whether a cached PDF exists")
    size_bytes: int = Field(description="File size in bytes (0 if not cached)")
    valid: bool = Field(description="Whether cache matches current data")
    url: str = Field(default="", description="Download URL for this report")


class ReportStatusResponse(BaseModel):
    """Status of all report variants."""
    reports: list[ReportInfo]
    data_hash: str = Field(description="Current data hash for cache validation")
    total_cached: int = Field(description="Number of cached reports")


class ReportGenerateResponse(BaseModel):
    """Response after triggering report generation."""
    status: str = Field(description="'started', 'completed', or 'error'")
    message: str
    lang: str
    report_type: str
    url: str = Field(default="", description="Download URL (populated if completed)")
