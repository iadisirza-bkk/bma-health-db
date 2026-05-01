"""ChartService + ChartRegistry + ChartSpec package.

Per ADR-01 (docs/adr/ADR-01-chart-registry.md), this package houses the
config-driven dashboard plumbing:

    - spec.py       Pydantic v2 models (ChartSpec is the source of truth)
    - registry.py   Filesystem-backed loader for config/charts/*.yaml
    - service.py    ChartService — orchestrator that calls the repo and
                    applies k-anonymity defense in depth

The FastAPI route + dependency factory live elsewhere (see ADR-01 §4 and
ULTRAPLAN S2.5). This package contains zero SQL.
"""
from __future__ import annotations

from .registry import ChartRegistry, chart_registry
from .service import ChartService
from .spec import (
    AxesSpec,
    ChartDataRow,
    ChartMeta,
    ChartResponse,
    ChartSpec,
    FilterParam,
)

__all__ = [
    "AxesSpec",
    "ChartDataRow",
    "ChartMeta",
    "ChartRegistry",
    "ChartResponse",
    "ChartService",
    "ChartSpec",
    "FilterParam",
    "chart_registry",
]
