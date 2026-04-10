"""Pydantic schemas for report generation API."""
from __future__ import annotations

from typing import Optional

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


# --- Dashboard models ---


class ReportDashboardItem(BaseModel):
    """A single report in the dashboard catalog."""
    label: str = Field(description="Display name of the report")
    url: str = Field(description="Download URL path")
    cached: bool = Field(description="Whether a cached PDF exists on disk")
    size: int = Field(default=0, description="File size in bytes (0 if not cached)")
    updated_at: Optional[str] = Field(default=None, description="ISO 8601 UTC timestamp of file mtime, null if not cached")


class ReportCategory(BaseModel):
    """A category grouping related reports."""
    id: str = Field(description="Category identifier (e.g. 'executive', 'zones')")
    label: str = Field(description="Thai display label")
    icon: str = Field(description="Icon key for frontend")
    reports: list[ReportDashboardItem] = Field(default_factory=list)


class GenerationError(BaseModel):
    """A single report generation error with reason."""
    report: str = Field(description="Report label (e.g. 'whitepaper/th', 'zone/1')")
    reason: str = Field(description="Error message explaining why generation failed")


class GenerationProgress(BaseModel):
    """Real-time progress of background report generation."""
    running: bool = Field(default=False)
    percent: float = Field(default=0.0, description="Completion percentage 0-100")
    completed: int = Field(default=0)
    total: int = Field(default=0)
    current: str = Field(default="", description="Label of report currently being generated")
    started_at: Optional[str] = Field(default=None, description="ISO 8601 UTC")
    finished_at: Optional[str] = Field(default=None, description="ISO 8601 UTC")
    errors: list[GenerationError] = Field(default_factory=list)


class SchedulerInfo(BaseModel):
    """Nightly scheduler state."""
    enabled: bool = Field(default=True)
    cron: str = Field(default="00:30")
    last_run: Optional[str] = Field(default=None, description="ISO 8601 UTC")
    next_run: Optional[str] = Field(default=None, description="ISO 8601 UTC")
    running: bool = Field(default=False)


class DashboardSummary(BaseModel):
    """Aggregate counts for quick display."""
    total_reports: int = Field(default=0, description="Total number of reports in catalog")
    cached_reports: int = Field(default=0, description="Number of reports with cached PDFs")
    percent_ready: float = Field(default=0.0, description="Percentage of reports ready (0-100)")


class ReportDashboardResponse(BaseModel):
    """Unified dashboard: generation progress + scheduler + catalog + summary."""
    generation: GenerationProgress
    scheduler: SchedulerInfo
    categories: list[ReportCategory]
    summary: DashboardSummary
