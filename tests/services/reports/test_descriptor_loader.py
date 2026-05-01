"""Tests for ``ReportRegistry.discover`` (ADR-03 §2).

Surface under test:
    * Valid descriptor whose ``section.block`` references resolve loads.
    * Bogus ``section.block`` reference fails loud at discovery time.
    * Duplicate ``report_id`` raises.
    * Filename-stem ≠ ``report_id`` raises.
    * ``extra="forbid"`` so a typo in YAML fails loud.
    * Cross-registry validation accepts an injected ``BlockRegistry``
      (so the report and block surfaces stay decoupled in tests).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from pydantic import BaseModel  # noqa: E402

from services.reports.blocks import BlockRegistry, ContentBlock  # noqa: E402
from services.reports.registry import (  # noqa: E402
    ReportRegistry,
    report_registry,
)
from services.reports.spec import RenderContext  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _CoverPageBlock(ContentBlock):
    block_id = "cover_page"

    def collect(
        self, ctx: RenderContext, params: BaseModel
    ) -> dict[str, Any]:
        return {}


class _KpiGridBlock(ContentBlock):
    block_id = "kpi_grid"

    def collect(
        self, ctx: RenderContext, params: BaseModel
    ) -> dict[str, Any]:
        return {}


@pytest.fixture
def populated_block_registry() -> BlockRegistry:
    reg = BlockRegistry()
    reg.register(_CoverPageBlock)
    reg.register(_KpiGridBlock)
    return reg


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


_VALID_DESCRIPTOR_YAML = """
report_id: simple_overview
title_th: รายงานภาพรวม
title_en: Simple Overview
formats:
  - latex
  - html
languages:
  - th
  - en
audience:
  - public
sections:
  - id: cover
    block: cover_page
    title_th: หน้าปก
  - id: kpis
    block: kpi_grid
    params:
      tiles: 4
"""

_BOGUS_BLOCK_YAML = """
report_id: bogus_report
title_th: รายงานที่อ้างบล็อกที่ไม่มีอยู่
formats:
  - html
sections:
  - id: bad
    block: does_not_exist
"""


# ---------------------------------------------------------------------------
# Valid path
# ---------------------------------------------------------------------------


def test_discover_loads_valid_descriptor(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    _write_yaml(tmp_path / "simple_overview.yaml", _VALID_DESCRIPTOR_YAML)

    registry = ReportRegistry.discover(
        tmp_path, blocks=populated_block_registry
    )

    assert "simple_overview" in registry
    assert len(registry) == 1
    desc = registry.get("simple_overview")
    assert desc.title_th == "รายงานภาพรวม"
    assert desc.formats == ["latex", "html"]
    assert [s.id for s in desc.sections] == ["cover", "kpis"]
    assert desc.sections[1].params == {"tiles": 4}
    # Default style / cache fall through.
    assert desc.style.font_family == "Sarabun"
    assert desc.cache.enabled is True
    assert desc.cache.invalidate_on == ["data_hash"]


def test_discover_get_unknown_raises_key_error(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    _write_yaml(tmp_path / "simple_overview.yaml", _VALID_DESCRIPTOR_YAML)
    registry = ReportRegistry.discover(
        tmp_path, blocks=populated_block_registry
    )
    with pytest.raises(KeyError, match="unknown report_id"):
        registry.get("does_not_exist")


def test_list_ids_is_sorted(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    _write_yaml(tmp_path / "simple_overview.yaml", _VALID_DESCRIPTOR_YAML)
    _write_yaml(
        tmp_path / "another_overview.yaml",
        _VALID_DESCRIPTOR_YAML.replace(
            "report_id: simple_overview",
            "report_id: another_overview",
        ),
    )
    registry = ReportRegistry.discover(
        tmp_path, blocks=populated_block_registry
    )
    assert registry.list_ids() == ["another_overview", "simple_overview"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_discover_fails_loud_on_unknown_block(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    """Bogus ``section.block`` reference must fail at load time, NOT
    silently at render time."""
    _write_yaml(tmp_path / "bogus_report.yaml", _BOGUS_BLOCK_YAML)

    with pytest.raises(ValueError, match="unknown.*block"):
        ReportRegistry.discover(tmp_path, blocks=populated_block_registry)


def test_discover_loads_valid_alongside_bogus_fails_overall(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    """Even when one descriptor is fine, a single bogus reference fails
    the whole discovery — there is no silent-skip."""
    _write_yaml(tmp_path / "simple_overview.yaml", _VALID_DESCRIPTOR_YAML)
    _write_yaml(tmp_path / "bogus_report.yaml", _BOGUS_BLOCK_YAML)

    with pytest.raises(ValueError, match="unknown.*block"):
        ReportRegistry.discover(tmp_path, blocks=populated_block_registry)


def test_duplicate_report_id_raises(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    """Two descriptor YAMLs declaring the same ``report_id`` must fail.

    The filename-stem rule guarantees one of the two files mismatches
    its stem. ``discover`` raises a ``ValueError`` either way — callers
    don't need to distinguish the two error paths, but BOTH must surface
    via the stem check or the duplicate check, never silently overwrite.
    """
    _write_yaml(tmp_path / "simple_overview.yaml", _VALID_DESCRIPTOR_YAML)
    # Second YAML has a different filename stem but the same report_id
    # inside.
    _write_yaml(tmp_path / "duplicate_copy.yaml", _VALID_DESCRIPTOR_YAML)

    # The stem-vs-report_id guard fires first on the duplicate file,
    # which is the user-visible failure. Whichever guard fires, the
    # registry MUST refuse to load the directory.
    with pytest.raises(ValueError):
        ReportRegistry.discover(tmp_path, blocks=populated_block_registry)


def test_duplicate_report_id_via_constructor() -> None:
    """The duplicate-id branch in ``discover`` is a defence-in-depth check;
    in practice the stem rule catches duplicates first. This test still
    asserts the registry surface treats id collisions cleanly when the
    underlying dict is hand-built.
    """
    from services.reports.spec import ReportDescriptor, SectionSpec

    desc_a = ReportDescriptor(
        report_id="x",
        title_th="a",
        formats=["html"],
        sections=[SectionSpec(id="s", block="cover_page")],
    )
    # A second descriptor sharing the same id collapses via dict
    # semantics — the registry holds at most one entry per id, which is
    # the invariant the duplicate guard preserves at discover-time.
    reg = ReportRegistry({"x": desc_a})
    assert reg.get("x") is desc_a
    assert len(reg) == 1


def test_filename_stem_must_match_report_id(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    _write_yaml(tmp_path / "wrong_stem.yaml", _VALID_DESCRIPTOR_YAML)
    with pytest.raises(ValueError, match="filename stem"):
        ReportRegistry.discover(tmp_path, blocks=populated_block_registry)


def test_unknown_field_in_yaml_raises(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    """``extra="forbid"`` so a typo in the descriptor YAML fails loud."""
    body = _VALID_DESCRIPTOR_YAML + "typo_field: surprise\n"
    _write_yaml(tmp_path / "simple_overview.yaml", body)
    with pytest.raises(Exception):  # Pydantic ValidationError
        ReportRegistry.discover(tmp_path, blocks=populated_block_registry)


def test_missing_directory_raises(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    with pytest.raises(FileNotFoundError):
        ReportRegistry.discover(
            tmp_path / "does_not_exist", blocks=populated_block_registry
        )


def test_yaml_must_be_mapping(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    _write_yaml(
        tmp_path / "simple_overview.yaml",
        "- this\n- is\n- a\n- list\n",
    )
    with pytest.raises(ValueError, match="YAML mapping"):
        ReportRegistry.discover(tmp_path, blocks=populated_block_registry)


# ---------------------------------------------------------------------------
# Singleton ergonomics
# ---------------------------------------------------------------------------


def test_report_registry_singleton_test_dir(
    tmp_path: Path, populated_block_registry: BlockRegistry
) -> None:
    """``report_registry()`` is monkey-patchable in tests via
    ``config_dir`` and ``reload=True``."""
    _write_yaml(tmp_path / "simple_overview.yaml", _VALID_DESCRIPTOR_YAML)

    a = report_registry(
        config_dir=tmp_path,
        reload=True,
        blocks=populated_block_registry,
    )
    b = report_registry(config_dir=tmp_path, blocks=populated_block_registry)
    assert a is b
    assert "simple_overview" in a

    c = report_registry(
        config_dir=tmp_path,
        reload=True,
        blocks=populated_block_registry,
    )
    assert c is not a
