"""ContentBlock package: ABC + initial 7 concrete subclasses.

Per ADR-03 §3, every section type lives in its own module under this
package. The ABC, registry, and YAML wire schema stay in ``base.py``
and are re-exported here so existing imports (``from
services.reports.blocks import BlockRegistry``) keep working unchanged
after the file -> package conversion.

Concrete blocks declare a ``block_id`` and ``Parameters`` class. They
do NOT self-register on import; instead they are registered through
``BlockRegistry.discover()``, which reads
``config/reports/blocks/*.yaml`` at startup and lazy-imports each
``class_path``. That preserves the file → package conversion as a
no-op for the loader.

Adding a new block: drop a new ``<id>.py`` in this package, drop a
matching ``<id>.yaml`` in ``config/reports/blocks/``, optionally
re-export the class from this ``__init__`` for direct ``from
services.reports.blocks import MyBlock`` access. Order of imports
below is deterministic on purpose so any side-effect-free helper that
walks ``__all__`` (test introspection, doc generators) sees the
registration list in a stable order.
"""
from __future__ import annotations

from services.reports.blocks.base import (
    BlockRegistry,
    BlockYaml,
    ContentBlock,
    block_registry,
)
from services.reports.blocks.appendix_methodology import (
    AppendixMethodologyBlock,
)
from services.reports.blocks.chart import ChartBlock
from services.reports.blocks.cover_page import CoverPageBlock
from services.reports.blocks.heading import HeadingBlock
from services.reports.blocks.kpi_grid import KPIGridBlock, KPISpec
from services.reports.blocks.paragraph import ParagraphBlock
from services.reports.blocks.table import ColSpec, TableBlock

__all__ = [
    # ABC + registry surface
    "BlockRegistry",
    "BlockYaml",
    "ContentBlock",
    "block_registry",
    # Concrete blocks (registration order)
    "CoverPageBlock",
    "HeadingBlock",
    "ParagraphBlock",
    "KPIGridBlock",
    "ChartBlock",
    "TableBlock",
    "AppendixMethodologyBlock",
    # Helper Pydantic models exposed for tests / type hints
    "KPISpec",
    "ColSpec",
]
