"""Unit tests for :class:`ReportService` orchestration.

Surface under test (ADR-03 §5 + ULTRAPLAN S4.2):
    * Cache-miss path: descriptor lookup -> renderer lookup -> per-section
      block.collect + block.render_<fmt> -> renderer.render -> hash
      sidecar write.
    * Cache-hit short-circuit: existing artefact + matching .hash sidecar
      bypass the renderer entirely.
    * ``visible_in`` filter: a section restricted to other formats is
      skipped.
    * Catalog (``ReportService.list``) and spec lookup (``describe``).
    * Bad input: unknown report_id / fmt / lang surface as KeyError or
      ValueError.

The test mounts hand-built fakes for every collaborator so the service
can be exercised end-to-end without touching the DB, the filesystem
template tree, or Tectonic.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import pytest
from pydantic import BaseModel

# Make ``api/`` importable for ``services.reports.*`` (mirrors the
# convention used by tests/services/charts/test_service.py).
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks.base import (  # noqa: E402
    BlockRegistry,
    ContentBlock,
)
from services.reports.data_collector import ReportDataCollector  # noqa: E402
from services.reports.registry import ReportRegistry  # noqa: E402
from services.reports.renderer import (  # noqa: E402
    RendererRegistry,
    ReportRenderer,
)
from services.reports.service import ReportService  # noqa: E402
from services.reports.spec import (  # noqa: E402
    CacheSpec,
    RenderContext,
    RenderedSection,
    ReportDescriptor,
    SectionSpec,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _HelloParams(BaseModel):
    salutation: str = "Hello"


class _HelloBlock(ContentBlock):
    """Echo block — render_latex emits a fixed snippet that proves both
    ``ctx`` and ``params`` were threaded through correctly."""

    block_id = "hello"
    Parameters = _HelloParams

    def collect(self, ctx: RenderContext, params: BaseModel) -> Dict[str, Any]:
        # Touch the data_collector so cache-hit assertions can verify
        # block code went through it (or didn't, on cache hit).
        try:
            ctx.data_collector.data()
        except Exception:
            pass
        return {"lang": ctx.lang, "salutation": params.salutation}

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        return rf"\section{{{params.salutation} {ctx.lang}}}"

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        return f"<h1>{params.salutation} {ctx.lang}</h1>"


class _NoParams(BaseModel):
    """Pydantic v2 cannot instantiate ``BaseModel`` directly (it's the ABC),
    so blocks with no params still need a concrete subclass."""


class _LatexOnlyBlock(ContentBlock):
    """Block that only implements LaTeX — used in the visible_in tests
    to verify the orchestrator skips sections with the wrong fmt."""

    block_id = "latex_only"
    Parameters = _NoParams

    def collect(self, ctx: RenderContext, params: BaseModel) -> Dict[str, Any]:
        return {}

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        return r"\paragraph{LaTeX-only}"


class _RecordingRenderer(ReportRenderer):
    """Renderer that captures (desc, sections, ctx, out_path) for assertions
    and returns a fixed Path so tests don't need a real filesystem."""

    fmt = "latex"

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def render(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
        out_path: Path,
    ) -> Path:
        self.calls.append(
            {
                "report_id": desc.report_id,
                "section_ids": [s.section_id for s in sections],
                "section_markups": [s.markup for s in sections],
                "lang": ctx.lang,
                "fmt": ctx.fmt,
                "out_path": out_path,
            }
        )
        # Pretend the renderer wrote the artefact — touch the file so
        # cache-hit logic can find it on a subsequent call.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"%PDF-fake")
        return out_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def block_registry_with_blocks() -> BlockRegistry:
    reg = BlockRegistry()
    reg.register(_HelloBlock)
    reg.register(_LatexOnlyBlock)
    return reg


@pytest.fixture
def fake_data() -> ReportDataCollector:
    """A collector backed by a frozen dict + frozen hash — no DB."""
    return ReportDataCollector(
        cache_ttl_seconds=300,
        collector_fn=lambda: {"total_screened": 12345},
        hash_fn=lambda: "deadbeef",
    )


@pytest.fixture
def hello_descriptor() -> ReportDescriptor:
    return ReportDescriptor(
        report_id="hello_report",
        title_th="สวัสดี",
        title_en="Hello",
        formats=["latex"],
        languages=["th", "en"],
        sections=[
            SectionSpec(
                id="greet",
                block="hello",
                params={"salutation": "Hi"},
            ),
        ],
        cache=CacheSpec(enabled=True),
    )


@pytest.fixture
def descriptor_registry(
    hello_descriptor: ReportDescriptor,
) -> ReportRegistry:
    return ReportRegistry({hello_descriptor.report_id: hello_descriptor})


def _make_service(
    descriptors: ReportRegistry,
    blocks: BlockRegistry,
    data: ReportDataCollector,
    out_dir: Path,
    *,
    renderer: Optional[_RecordingRenderer] = None,
) -> tuple[ReportService, _RecordingRenderer]:
    rec = renderer or _RecordingRenderer()
    rreg = RendererRegistry()
    rreg.register(rec)
    svc = ReportService(
        descriptors=descriptors,
        blocks=blocks,
        renderers=rreg,
        data=data,
        out_dir=out_dir,
    )
    return svc, rec


# ---------------------------------------------------------------------------
# Cache miss — full orchestration path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_render_runs_block_collect_and_render(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    """End-to-end: descriptor -> block -> renderer."""
    svc, rec = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )

    out = await svc.render("hello_report", "latex", "th")

    # Renderer was invoked exactly once with the expected payload.
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["report_id"] == "hello_report"
    assert call["section_ids"] == ["greet"]
    assert call["section_markups"] == [r"\section{Hi th}"]
    assert call["lang"] == "th"
    assert call["fmt"] == "latex"
    assert out == call["out_path"]

    # Cache layout: <out_dir>/<lang>/<report_id>.pdf
    assert out == tmp_path / "th" / "hello_report.pdf"
    assert out.exists()

    # Hash sidecar written next to the artefact.
    sidecar = out.with_suffix(".hash")
    assert sidecar.exists()
    assert sidecar.read_text(encoding="utf-8") == "deadbeef"


@pytest.mark.anyio
async def test_render_threads_lang_through_to_block(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    """``ctx.lang`` reaches block.render_<fmt>."""
    svc, rec = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )

    await svc.render("hello_report", "latex", "en")

    assert rec.calls[0]["section_markups"] == [r"\section{Hi en}"]
    assert rec.calls[0]["out_path"] == tmp_path / "en" / "hello_report.pdf"


# ---------------------------------------------------------------------------
# Cache hit — short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_render_cache_hit_short_circuits_renderer(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    """A pre-existing PDF + matching .hash bypasses the renderer."""
    # Pre-populate the cache as if a previous render succeeded.
    cache_pdf = tmp_path / "th" / "hello_report.pdf"
    cache_pdf.parent.mkdir(parents=True, exist_ok=True)
    cache_pdf.write_bytes(b"%PDF-cached")
    cache_pdf.with_suffix(".hash").write_text(
        fake_data.data_hash(), encoding="utf-8"
    )

    svc, rec = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )

    out = await svc.render("hello_report", "latex", "th")

    assert out == cache_pdf
    # Crucial: the renderer was NOT called — that's the whole point of
    # the cache-hit path.
    assert rec.calls == []
    # And the cached bytes were preserved (we didn't accidentally
    # re-render and overwrite).
    assert cache_pdf.read_bytes() == b"%PDF-cached"


@pytest.mark.anyio
async def test_render_stale_hash_triggers_rerender(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    """A cached artefact whose hash sidecar mismatches re-renders."""
    cache_pdf = tmp_path / "th" / "hello_report.pdf"
    cache_pdf.parent.mkdir(parents=True, exist_ok=True)
    cache_pdf.write_bytes(b"%PDF-stale")
    cache_pdf.with_suffix(".hash").write_text("stale-hash", encoding="utf-8")

    svc, rec = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )

    await svc.render("hello_report", "latex", "th")

    assert len(rec.calls) == 1, "stale hash should force a re-render"
    # And the sidecar is updated to the live hash.
    assert cache_pdf.with_suffix(".hash").read_text(
        encoding="utf-8"
    ) == "deadbeef"


# ---------------------------------------------------------------------------
# visible_in skip
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_render_skips_section_not_visible_in_fmt(
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    """Sections with ``visible_in`` excluding the requested fmt are skipped."""
    desc = ReportDescriptor(
        report_id="multi_section_report",
        title_th="หลายส่วน",
        formats=["latex"],
        languages=["th"],
        sections=[
            # Always rendered (visible_in=None).
            SectionSpec(
                id="always",
                block="hello",
                params={"salutation": "Hi"},
            ),
            # Only visible in HTML — should be skipped for latex.
            SectionSpec(
                id="html_only",
                block="hello",
                params={"salutation": "Hidden"},
                visible_in=["html"],
            ),
            # Visible only in latex — included.
            SectionSpec(
                id="latex_only",
                block="latex_only",
                params={},
                visible_in=["latex"],
            ),
        ],
    )
    reg = ReportRegistry({desc.report_id: desc})

    svc, rec = _make_service(
        reg, block_registry_with_blocks, fake_data, tmp_path
    )

    await svc.render("multi_section_report", "latex", "th")

    assert rec.calls[0]["section_ids"] == ["always", "latex_only"]


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_render_unknown_report_id_raises(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    svc, _ = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )
    with pytest.raises(KeyError, match="unknown report_id"):
        await svc.render("does_not_exist", "latex", "th")


@pytest.mark.anyio
async def test_render_undeclared_fmt_raises(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    svc, _ = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )
    # Descriptor declares only "latex".
    with pytest.raises(ValueError, match="does not declare format"):
        await svc.render("hello_report", "html", "th")


@pytest.mark.anyio
async def test_render_undeclared_lang_raises(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    svc, _ = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )
    # Descriptor declares ["th", "en"] only.
    with pytest.raises(ValueError, match="does not declare language"):
        await svc.render("hello_report", "latex", "fr")


# ---------------------------------------------------------------------------
# Catalog / describe
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_returns_catalog_shape(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    svc, _ = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )
    items = await svc.list()
    assert items == [
        {
            "report_id": "hello_report",
            "title_th": "สวัสดี",
            "title_en": "Hello",
            "formats": ["latex"],
            "languages": ["th", "en"],
            "audience": ["public"],
        }
    ]


@pytest.mark.anyio
async def test_describe_returns_descriptor(
    descriptor_registry: ReportRegistry,
    block_registry_with_blocks: BlockRegistry,
    fake_data: ReportDataCollector,
    tmp_path: Path,
) -> None:
    svc, _ = _make_service(
        descriptor_registry, block_registry_with_blocks, fake_data, tmp_path
    )
    desc = await svc.describe("hello_report")
    assert desc.report_id == "hello_report"
    assert desc.title_th == "สวัสดี"


# ---------------------------------------------------------------------------
# Data collector pluggability
# ---------------------------------------------------------------------------


def test_data_collector_caches_within_ttl() -> None:
    """``ReportDataCollector.data()`` returns the cached payload until invalidated."""
    calls = {"n": 0}

    def _collect() -> Dict[str, Any]:
        calls["n"] += 1
        return {"v": calls["n"]}

    c = ReportDataCollector(cache_ttl_seconds=60, collector_fn=_collect)
    first = c.data()
    second = c.data()
    assert first is second
    assert calls["n"] == 1

    c.invalidate()
    third = c.data()
    assert third is not first
    assert calls["n"] == 2


def test_data_collector_hash_fn_pluggable() -> None:
    c = ReportDataCollector(hash_fn=lambda: "abc123")
    assert c.data_hash() == "abc123"
