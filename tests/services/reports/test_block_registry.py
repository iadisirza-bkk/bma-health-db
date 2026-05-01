"""Tests for ``ContentBlock`` ABC + ``BlockRegistry`` (ADR-03 §3).

Surface under test:
    * ``ContentBlock`` default ``render_<fmt>`` methods raise
      ``NotImplementedError`` with a clear message naming the block_id
      and the requested format.
    * ``ContentBlock.supports(fmt)`` returns ``True`` iff the subclass
      overrode the corresponding ``render_<fmt>`` method.
    * ``BlockRegistry.register`` is decorator-friendly and
      ``BlockRegistry.get`` resolves by block_id.
    * ``BlockRegistry`` ergonomics: ``__contains__``, ``__len__``,
      ``list_ids``.
    * Singleton helper ``block_registry()`` — lazy init + ``reload=True``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Make ``api/`` importable for ``services.reports.*`` (mirrors
# ``tests/services/charts/test_service.py``).
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from pydantic import BaseModel  # noqa: E402

from services.reports.blocks import (  # noqa: E402
    BlockRegistry,
    BlockYaml,
    ContentBlock,
    block_registry,
)
from services.reports.spec import RenderContext, ReportDescriptor  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures: a minimal block + a richer block with HTML support
# ---------------------------------------------------------------------------


def _make_descriptor() -> ReportDescriptor:
    return ReportDescriptor(
        report_id="t",
        title_th="t",
        formats=["html"],
        sections=[],
    )


def _make_ctx() -> RenderContext:
    from datetime import datetime, timezone

    return RenderContext(
        data_collector=None,
        lang="th",
        fmt="html",
        descriptor=_make_descriptor(),
        requested_at=datetime.now(timezone.utc),
    )


class _BarebonesBlock(ContentBlock):
    """Implements only ``collect`` — should fail every render call."""

    block_id = "barebones"

    def collect(
        self, ctx: RenderContext, params: BaseModel
    ) -> dict[str, Any]:
        return {"hello": "world"}


class _HtmlBlock(ContentBlock):
    """Overrides ``render_html`` only."""

    block_id = "with_html"

    class Parameters(BaseModel):
        bold: bool = False

    def collect(
        self, ctx: RenderContext, params: BaseModel
    ) -> dict[str, Any]:
        return {"text": "hi"}

    def render_html(
        self,
        data: dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        text = data["text"]
        bold = getattr(params, "bold", False)
        return f"<b>{text}</b>" if bold else f"<p>{text}</p>"


# ---------------------------------------------------------------------------
# Default render impls fail loud
# ---------------------------------------------------------------------------


class _EmptyParams(BaseModel):
    """Empty Pydantic model — Pydantic v2 doesn't allow ``BaseModel()``
    direct instantiation, so tests use this trivial subclass."""


def test_default_render_latex_raises_with_clear_message() -> None:
    block = _BarebonesBlock()
    with pytest.raises(NotImplementedError) as excinfo:
        block.render_latex({}, _EmptyParams(), _make_ctx())
    msg = str(excinfo.value)
    assert "barebones" in msg
    assert "latex" in msg


def test_default_render_html_raises_with_clear_message() -> None:
    block = _BarebonesBlock()
    with pytest.raises(NotImplementedError) as excinfo:
        block.render_html({}, _EmptyParams(), _make_ctx())
    msg = str(excinfo.value)
    assert "barebones" in msg
    assert "html" in msg


def test_default_render_pptx_raises_with_clear_message() -> None:
    block = _BarebonesBlock()
    with pytest.raises(NotImplementedError) as excinfo:
        block.render_pptx({}, _EmptyParams(), _make_ctx())
    msg = str(excinfo.value)
    assert "barebones" in msg
    assert "pptx" in msg


# ---------------------------------------------------------------------------
# supports() detects overrides
# ---------------------------------------------------------------------------


def test_supports_false_for_barebones_subclass() -> None:
    assert _BarebonesBlock.supports("latex") is False
    assert _BarebonesBlock.supports("html") is False
    assert _BarebonesBlock.supports("pptx") is False


def test_supports_true_for_html_only_block() -> None:
    assert _HtmlBlock.supports("html") is True
    # Doesn't override the others, so they remain unsupported.
    assert _HtmlBlock.supports("latex") is False
    assert _HtmlBlock.supports("pptx") is False


def test_supports_unknown_format_returns_false() -> None:
    assert _BarebonesBlock.supports("docx") is False


# ---------------------------------------------------------------------------
# Registry ergonomics
# ---------------------------------------------------------------------------


def test_registry_register_and_get_round_trip() -> None:
    reg = BlockRegistry()
    reg.register(_BarebonesBlock)
    reg.register(_HtmlBlock)

    assert reg.get("barebones") is _BarebonesBlock
    assert reg.get("with_html") is _HtmlBlock
    assert "barebones" in reg
    assert len(reg) == 2
    assert reg.list_ids() == ["barebones", "with_html"]


def test_registry_register_used_as_decorator() -> None:
    reg = BlockRegistry()

    @reg.register
    class _Decorated(ContentBlock):
        block_id = "decorated"

        def collect(
            self, ctx: RenderContext, params: BaseModel
        ) -> dict[str, Any]:
            return {}

    assert reg.get("decorated") is _Decorated
    # Decorator returns the class so the binding still points to it.
    assert _Decorated.block_id == "decorated"


def test_registry_rejects_duplicate_block_id() -> None:
    reg = BlockRegistry()
    reg.register(_BarebonesBlock)
    with pytest.raises(ValueError, match="duplicate block_id"):
        reg.register(_BarebonesBlock)


def test_registry_rejects_non_block_class() -> None:
    reg = BlockRegistry()

    class _NotABlock:
        block_id = "fake"

    with pytest.raises(TypeError):
        reg.register(_NotABlock)  # type: ignore[arg-type]


def test_registry_rejects_class_without_block_id() -> None:
    reg = BlockRegistry()

    class _NoId(ContentBlock):
        # block_id deliberately omitted
        def collect(
            self, ctx: RenderContext, params: BaseModel
        ) -> dict[str, Any]:
            return {}

    with pytest.raises(ValueError, match="block_id"):
        reg.register(_NoId)


def test_registry_get_unknown_raises_key_error() -> None:
    reg = BlockRegistry()
    with pytest.raises(KeyError, match="unknown block_id"):
        reg.get("nope")


# ---------------------------------------------------------------------------
# Discovery (filesystem)
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_discover_loads_block_yaml(tmp_path: Path) -> None:
    """Round-trip: a YAML referencing a real ContentBlock subclass loads.

    We point ``class_path`` at the test-module class so we don't need a
    config-driven block to exist on disk.
    """
    _write_yaml(
        tmp_path / "with_html.yaml",
        """
block_id: with_html
class_path: tests.services.reports.test_block_registry:_HtmlBlock
description_th: บล็อก HTML สำหรับทดสอบ
description_en: html block for tests
""",
    )

    reg = BlockRegistry.discover(tmp_path)
    assert "with_html" in reg
    assert reg.get("with_html") is _HtmlBlock


def test_discover_filename_stem_must_match_block_id(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "wrong_filename.yaml",
        """
block_id: with_html
class_path: tests.services.reports.test_block_registry:_HtmlBlock
description_th: x
""",
    )
    with pytest.raises(ValueError, match="filename stem"):
        BlockRegistry.discover(tmp_path)


def test_discover_skips_disabled_entries(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "with_html.yaml",
        """
block_id: with_html
class_path: tests.services.reports.test_block_registry:_HtmlBlock
description_th: x
enabled: false
""",
    )
    reg = BlockRegistry.discover(tmp_path)
    assert "with_html" not in reg
    assert len(reg) == 0


def test_discover_unknown_field_raises(tmp_path: Path) -> None:
    """``extra="forbid"`` so a typo fails loud at boot."""
    _write_yaml(
        tmp_path / "with_html.yaml",
        """
block_id: with_html
class_path: tests.services.reports.test_block_registry:_HtmlBlock
description_th: x
typo_field: surprise
""",
    )
    with pytest.raises(Exception):  # Pydantic ValidationError
        BlockRegistry.discover(tmp_path)


def test_discover_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        BlockRegistry.discover(tmp_path / "does_not_exist")


def test_blockyaml_defaults() -> None:
    spec = BlockYaml(
        block_id="x",
        class_path="tests.services.reports.test_block_registry:_HtmlBlock",
        description_th="…",
    )
    assert spec.enabled is True
    assert spec.description_en is None


# ---------------------------------------------------------------------------
# Singleton helper
# ---------------------------------------------------------------------------


def test_block_registry_singleton_test_dir(tmp_path: Path) -> None:
    """``block_registry()`` is monkey-patchable in tests via ``config_dir``
    and ``reload=True``."""
    _write_yaml(
        tmp_path / "with_html.yaml",
        """
block_id: with_html
class_path: tests.services.reports.test_block_registry:_HtmlBlock
description_th: x
""",
    )
    a = block_registry(config_dir=tmp_path, reload=True)
    b = block_registry(config_dir=tmp_path)
    assert a is b
    assert "with_html" in a

    # ``reload=True`` swaps the cached instance for a fresh one.
    c = block_registry(config_dir=tmp_path, reload=True)
    assert c is not a
