"""Reports package — descriptors, blocks, renderers, and orchestrator.

Per ADR-03 (docs/adr/ADR-03-report-descriptors.md), this package houses the
config-driven report-generation plumbing:

    - spec.py         Pydantic v2 descriptors + dataclass holders
    - blocks.py       ContentBlock ABC + BlockRegistry (S4.1)
    - registry.py     ReportRegistry — descriptor loader (S4.1)
    - renderer.py     ReportRenderer ABC + RendererRegistry (S4.1)
    - data_collector.py  ReportDataCollector — wraps the legacy collector (S4.2)
    - renderers/      Concrete renderers (latex.py, html.py, pptx.py — S4.2/S4.3)
    - service.py      ReportService — orchestrator (S4.5)

The names re-exported below are the stable public surface used by the
orchestrator, the renderers, and the test suite. Anything else stays
behind submodule imports.

The renderer registry is NOT auto-discovered. ``bootstrap()`` (added once
concrete renderer modules land in S4.2/S4.3) is the single entry point
that imports each renderer module so it self-registers, keeping the
wiring centralised.
"""
from __future__ import annotations

from .blocks import BlockRegistry, ContentBlock, block_registry
from .registry import ReportRegistry, report_registry
from .renderer import RendererRegistry, ReportRenderer, renderer_registry
from .spec import (
    CacheSpec,
    RenderContext,
    RenderedSection,
    ReportDescriptor,
    SectionSpec,
    StyleSpec,
)

__all__ = [
    "BlockRegistry",
    "CacheSpec",
    "ContentBlock",
    "RenderContext",
    "RenderedSection",
    "RendererRegistry",
    "ReportDescriptor",
    "ReportRegistry",
    "ReportRenderer",
    "SectionSpec",
    "StyleSpec",
    "block_registry",
    "renderer_registry",
    "report_registry",
]
