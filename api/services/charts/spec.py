"""Pydantic v2 models for the chart-spec wire / config contract.

These models implement the contract spelled out in ADR-01 §1, §3.

Design notes:
    * Pydantic v2 syntax — `model_config = ConfigDict(...)`, NOT
      `class Config:`.
    * `extra="forbid"` everywhere so a YAML typo fails loud at startup.
    * `validate_assignment=True` so post-construction mutation also
      goes through validation.
    * No defaults that mutate (we follow Pydantic v2 best practice and
      use `Field(default_factory=...)` for `dict` / `list`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Permitted chart kinds. Frontend OptionBuilder strategy registry must have
# a builder for every kind in this Literal — see ADR-01 §6.
ChartKind = Literal[
    "bar",
    "line",
    "pyramid",
    "scatter",
    "boxplot",
    "heatmap",
    "donut",
    "stacked_bar",
]

# Permitted filter kinds — covers what the public dashboard surface
# actually exposes today. Extend this when a new filter dimension lands.
#
# `enum` is a special kind: the filter accepts one of an enumerated list
# of string values declared inline via `values:`. Use this for free-form
# categorical knobs that aren't a registered dimension (e.g. selecting
# which behaviour column to plot — smoking / alcohol / exercise).
FilterKind = Literal[
    "district",
    "zone",
    "sex",
    "age_band",
    "year",
    "facility",
    "disease",
    "enum",
]


class FilterParam(BaseModel):
    """Single filter input a chart accepts."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    kind: FilterKind
    required: bool = False
    # Only meaningful when kind == "enum". List of permitted string
    # values; anything else from the caller raises 422 in the route.
    values: Optional[List[str]] = None


class AxesSpec(BaseModel):
    """Axes / series binding for a chart kind.

    `value` is for chart kinds whose data cells carry a third dimension
    not encoded by x / y — heatmaps in particular (cell intensity), and
    scatter (point size). For bar / line / pyramid / donut, leave
    `value` unset and the renderer reads `n` from each ChartDataRow.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    x: str
    y: str
    series: Optional[str] = None
    value: Optional[str] = None


class ChartSpec(BaseModel):
    """Single source of truth for a chart — see ADR-01 §1."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    spec_id: str  # filename stem, must be unique across project
    kind: ChartKind
    title_th: str
    title_en: Optional[str] = None
    description_th: Optional[str] = None
    query_id: str  # name of an MVRepository method
    query_params: Dict[str, Any] = Field(default_factory=dict)
    accepts: List[FilterParam] = Field(default_factory=list)
    axes: AxesSpec
    # Name of the integer count column on each repo row that k-anonymity
    # is enforced against. Defaults to "n"; specs whose backing MV uses
    # a different count column (`persons`, `total_screened`, …) should
    # set it explicitly so the spec → row contract is declared in YAML.
    count_field: str = "n"
    color_palette: Optional[List[str]] = None
    k_anon_threshold: int = 5
    units: Dict[str, str] = Field(default_factory=dict)
    extra: Dict[str, Any] = Field(default_factory=dict)


class ChartDataRow(BaseModel):
    """One data point in the wire response."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    x: Any  # str | int | float — Pydantic v2 will accept all three
    y: Optional[float] = None
    n: int
    series: Optional[str] = None
    masked: bool = False


class ChartMeta(BaseModel):
    """Metadata block returned alongside chart data — see ADR-01 §3."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    n_total: int
    k_anon_threshold: int
    k_anon_dropped: int
    filters_applied: Dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class ChartResponse(BaseModel):
    """Full wire envelope. Same shape for every chart — ADR-01 §3."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    kind: ChartKind
    spec_id: str
    data: List[ChartDataRow] = Field(default_factory=list)
    meta: ChartMeta
