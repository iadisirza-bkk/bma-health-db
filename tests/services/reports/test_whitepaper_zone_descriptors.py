"""Smoke tests for the ported whitepaper + zone ReportDescriptors (S4.4).

What's covered:
    * Both YAMLs at ``config/reports/{whitepaper,zone}.yaml`` deserialize via
      ``ReportRegistry.discover`` and register.
    * Every ``section.block`` reference in each descriptor resolves to a
      registered block — failure here means a typo or a block S4.4 expected
      doesn't actually exist (would have shown up as an "unknown block"
      ValueError during discovery, but we assert it again with a more
      specific message).
    * Whitepaper renders end-to-end through a fake renderer pipeline using
      stub blocks with predictable markup, and the section count of the
      rendered output equals the section count in the YAML.
    * ``ReportService.render`` substitutes ``{zone_code}``-style placeholders
      across nested string fields so a "zone" report can be rendered for any
      of the 8 BMA zones from the same descriptor.

The blocks under test are deliberately stub classes registered in-process
(via ``BlockRegistry``); the real implementations live in S4.4 and are not
needed to validate the descriptor contract.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pydantic import BaseModel, ConfigDict

# Make ``api/`` importable for ``services.reports.*`` (mirrors siblings).
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks import BlockRegistry, ContentBlock  # noqa: E402
from services.reports.registry import ReportRegistry  # noqa: E402
from services.reports.renderer import RendererRegistry, ReportRenderer  # noqa: E402
from services.reports.service import (  # noqa: E402
    ReportService,
    _resolve_descriptor,
    _substitute,
)
from services.reports.spec import (  # noqa: E402
    RenderContext,
    RenderedSection,
    ReportDescriptor,
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_CONFIG_DIR = REPO_ROOT / "config" / "reports"
WHITEPAPER_YAML = REPORTS_CONFIG_DIR / "whitepaper.yaml"
ZONE_YAML = REPORTS_CONFIG_DIR / "zone.yaml"


# ---------------------------------------------------------------------------
# Stub blocks — one per block_id referenced in the two descriptors.
# Each stub stamps a unique marker into its rendered output so the test
# can verify section ordering & count without a real renderer pipeline.
# ---------------------------------------------------------------------------


class _StubBlock(ContentBlock):
    """Base stub: emits ``<section block_id=...>...</section>`` HTML."""

    block_id: str = "_stub"

    class Parameters(BaseModel):
        model_config = ConfigDict(extra="allow")

    def collect(
        self, ctx: RenderContext, params: BaseModel
    ) -> Dict[str, Any]:
        return {"params": params.model_dump()}

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        return f"<section data-block=\"{self.block_id}\">stub</section>"

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        return f"% block: {self.block_id}\n"


class _CoverPage(_StubBlock):
    block_id = "cover_page"


class _Heading(_StubBlock):
    block_id = "heading"


class _KpiGrid(_StubBlock):
    block_id = "kpi_grid"


class _Chart(_StubBlock):
    block_id = "chart"


class _Table(_StubBlock):
    block_id = "table"


class _Paragraph(_StubBlock):
    block_id = "paragraph"


class _AppendixMethodology(_StubBlock):
    block_id = "appendix_methodology"


# S6 blocks added for whitepaper-template parity. Stubs only — full impls
# are tested in test_new_leaf_blocks / test_table_shaped_blocks /
# test_branching_ai_blocks / test_layout_block.
class _Callout(_StubBlock):
    block_id = "callout"


class _Formula(_StubBlock):
    block_id = "formula"


class _TrendTable(_StubBlock):
    block_id = "trend_table"


class _DiseaseDistrictGrid(_StubBlock):
    block_id = "disease_district_grid"


class _Crosstab(_StubBlock):
    block_id = "crosstab"


class _StatisticalTestResults(_StubBlock):
    block_id = "statistical_test_results"


class _AiInsight(_StubBlock):
    block_id = "ai_insight"


class _TwoColumnLayout(_StubBlock):
    block_id = "two_column_layout"


def _stub_block_registry() -> BlockRegistry:
    """Build a registry with all blocks the two descriptors reference.

    Includes S4 baseline blocks (cover_page, heading, kpi_grid, chart, table,
    paragraph, appendix_methodology) AND the 8 S6 blocks added for
    whitepaper-template parity.
    """
    reg = BlockRegistry()
    for cls in (
        _CoverPage,
        _Heading,
        _KpiGrid,
        _Chart,
        _Table,
        _Paragraph,
        _AppendixMethodology,
        # S6 additions
        _Callout,
        _Formula,
        _TrendTable,
        _DiseaseDistrictGrid,
        _Crosstab,
        _StatisticalTestResults,
        _AiInsight,
        _TwoColumnLayout,
    ):
        reg.register(cls)
    return reg


# ---------------------------------------------------------------------------
# Fake renderer + data collector
# ---------------------------------------------------------------------------


class _FakeHtmlRenderer(ReportRenderer):
    fmt = "html"

    def render(
        self,
        desc: ReportDescriptor,
        sections: List[RenderedSection],
        ctx: RenderContext,
        out_path: Path,
    ) -> Path:
        body = "\n".join(str(s.markup) for s in sections)
        # Capture section count + ids on the renderer so the test can
        # assert without re-parsing.
        self.last_section_count = len(sections)
        self.last_section_ids = [s.section_id for s in sections]
        out_path.write_text(
            f"<html><body>\n{body}\n</body></html>\n", encoding="utf-8"
        )
        return out_path


class _FakeDataCollector:
    """Stand-in for the real ReportDataCollector (S4.2).

    Returns a stable shape so blocks that read ``ctx.data_collector``
    don't blow up; descriptor tests don't actually touch SQL.
    """

    def get_summary(self, **_: Any) -> Dict[str, int]:
        return {
            "total_screened": 0,
            "districts_covered": 50,
            "found_ncd": 0,
        }


# ---------------------------------------------------------------------------
# Discovery — both YAMLs register
# ---------------------------------------------------------------------------


def test_descriptor_yamls_exist() -> None:
    assert WHITEPAPER_YAML.is_file(), (
        f"Expected whitepaper descriptor at {WHITEPAPER_YAML}"
    )
    assert ZONE_YAML.is_file(), (
        f"Expected zone descriptor at {ZONE_YAML}"
    )


def test_descriptor_registry_loads_both_yamls() -> None:
    """ReportRegistry.discover loads whitepaper + zone via the stub blocks."""
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)

    assert "whitepaper" in reg
    assert "zone" in reg
    assert set(reg.list_ids()) >= {"whitepaper", "zone"}


def test_whitepaper_descriptor_shape() -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)

    desc = reg.get("whitepaper")
    assert desc.report_id == "whitepaper"
    assert "latex" in desc.formats and "html" in desc.formats
    # 10 ISO codes per ADR-03 / task spec.
    assert set(desc.languages) >= {
        "th", "en", "zh", "ja", "ko", "ru", "my", "hi", "vi", "fr",
    }
    # S4 baseline shipped 9 sections; S6 grew this to 32 for full
    # template parity (8 new blocks across descriptive stats, factor
    # analysis, inferential, trend, ai insight, two-column rankings,
    # callouts, formulas, closing). Don't pin an exact count — just
    # assert the descriptor has more sections than the bare scaffold.
    assert len(desc.sections) >= 9
    assert desc.metadata.get("legacy_template") == "report_whitepaper.tex.j2"


def test_zone_descriptor_shape() -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)

    desc = reg.get("zone")
    assert desc.report_id == "zone"
    assert desc.formats == ["latex", "html"]
    assert desc.languages == ["th", "en"]
    # cover + 3 (heading + chart) trios + methodology = 8 sections.
    assert len(desc.sections) == 8
    assert desc.metadata.get("parameterized_by") == "zone_code"


# ---------------------------------------------------------------------------
# Cross-registry — every block reference resolves
# ---------------------------------------------------------------------------


def test_every_block_reference_resolves_in_whitepaper() -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)
    desc = reg.get("whitepaper")
    for section in desc.sections:
        assert section.block in blocks, (
            f"whitepaper.{section.id} references unknown block "
            f"{section.block!r}"
        )


def test_every_block_reference_resolves_in_zone() -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)
    desc = reg.get("zone")
    for section in desc.sections:
        assert section.block in blocks, (
            f"zone.{section.id} references unknown block {section.block!r}"
        )


def test_unknown_block_reference_fails_fast(tmp_path: Path) -> None:
    """Defense-in-depth: discovery refuses descriptors that point at
    unregistered blocks."""
    (tmp_path / "tiny.yaml").write_text(
        "report_id: tiny\n"
        "title_th: tiny\n"
        "formats: [html]\n"
        "languages: [th]\n"
        "sections:\n"
        "  - id: x\n"
        "    block: never_registered\n"
        "    params: {}\n",
        encoding="utf-8",
    )
    blocks = _stub_block_registry()
    with pytest.raises(ValueError, match="unknown.*block"):
        ReportRegistry.discover(tmp_path, blocks=blocks)


# ---------------------------------------------------------------------------
# Chart spec_id sanity — only reference confirmed-working chart specs.
# ---------------------------------------------------------------------------


CONFIRMED_CHART_SPEC_IDS = {
    "age_pyramid",
    "behavior_disease",
    "disease_lab_crosstab",
    "repeat_screening",
    "risk_factor_profile",
    "screening_coverage",
}


def test_chart_block_references_confirmed_spec_ids() -> None:
    """Each ``chart`` block in either descriptor references a chart spec
    that lives in ``config/charts/`` and is on the confirmed-working list.
    """
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)
    for rid in ("whitepaper", "zone"):
        desc = reg.get(rid)
        for section in desc.sections:
            if section.block != "chart":
                continue
            spec_id = section.params.get("spec_id")
            assert spec_id in CONFIRMED_CHART_SPEC_IDS, (
                f"{rid}.{section.id} references unknown spec_id={spec_id!r}"
            )


# ---------------------------------------------------------------------------
# Param substitution — `_substitute` and `_resolve_descriptor`
# ---------------------------------------------------------------------------


def test_substitute_walks_strings_in_dicts_and_lists() -> None:
    tree = {
        "title": "report for {zone_code}",
        "nested": {"a": "{zone_code}-x", "b": 42},
        "list_of": [{"name": "{zone_code}"}, "leaf-{zone_code}"],
        "no_placeholder": "static",
    }
    out = _substitute(tree, {"zone_code": "01"})
    assert out["title"] == "report for 01"
    assert out["nested"]["a"] == "01-x"
    assert out["nested"]["b"] == 42
    assert out["list_of"] == [{"name": "01"}, "leaf-01"]
    assert out["no_placeholder"] == "static"


def test_substitute_noop_when_params_empty() -> None:
    tree = {"title": "report for {zone_code}"}
    assert _substitute(tree, {}) is tree


def test_resolve_descriptor_substitutes_zone_code() -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)
    desc = reg.get("zone")

    resolved = _resolve_descriptor(desc, {"zone_code": "01"})
    assert "{zone_code}" not in resolved.title_th
    assert "01" in resolved.title_th
    # Cover subtitle in params should also be substituted.
    cover = next(s for s in resolved.sections if s.id == "cover")
    assert cover.params["subtitle_th"] == "เขตสุขภาพ 01"
    # Filters in chart blocks too.
    pyramid = next(s for s in resolved.sections if s.id == "pyramid")
    assert pyramid.params["filters"]["zone"] == "01"


# ---------------------------------------------------------------------------
# End-to-end — render whitepaper HTML through the fake pipeline
# ---------------------------------------------------------------------------


def test_render_whitepaper_html_section_count_matches_yaml(
    tmp_path: Path,
) -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)

    renderers = RendererRegistry()
    fake_html = _FakeHtmlRenderer()
    renderers.register(fake_html)

    svc = ReportService(
        registry=reg,
        blocks=blocks,
        renderers=renderers,
        data_collector=_FakeDataCollector(),
    )
    out_path = tmp_path / "whitepaper.html"
    result = asyncio.run(svc.render("whitepaper", "html", "th", out_path=out_path))

    assert result == out_path
    assert out_path.is_file()
    body = out_path.read_text(encoding="utf-8")
    assert "<html>" in body
    assert "</body>" in body

    # Section count = number of YAML sections.
    desc = reg.get("whitepaper")
    assert fake_html.last_section_count == len(desc.sections)
    assert fake_html.last_section_ids == [s.id for s in desc.sections]
    # Each section produced markup containing its block_id marker.
    for section in desc.sections:
        assert f'data-block="{section.block}"' in body


def test_render_zone_with_zone_code_substitution(tmp_path: Path) -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)

    renderers = RendererRegistry()
    renderers.register(_FakeHtmlRenderer())

    svc = ReportService(
        registry=reg,
        blocks=blocks,
        renderers=renderers,
        data_collector=_FakeDataCollector(),
    )
    out_path = tmp_path / "zone.html"
    asyncio.run(
        svc.render("zone", "html", "th", out_path=out_path, params={"zone_code": "03"})
    )

    # Smoke: the file exists and is non-empty.
    assert out_path.is_file() and out_path.stat().st_size > 0


def test_render_rejects_unknown_format(tmp_path: Path) -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)
    renderers = RendererRegistry()
    renderers.register(_FakeHtmlRenderer())

    svc = ReportService(
        registry=reg, blocks=blocks, renderers=renderers,
        data_collector=_FakeDataCollector(),
    )
    with pytest.raises(ValueError, match="does not declare format"):
        asyncio.run(
            svc.render(
                "whitepaper", "pptx", "th",
                out_path=tmp_path / "x.pptx",
            )
        )


def test_render_rejects_unknown_language(tmp_path: Path) -> None:
    blocks = _stub_block_registry()
    reg = ReportRegistry.discover(REPORTS_CONFIG_DIR, blocks=blocks)
    renderers = RendererRegistry()
    renderers.register(_FakeHtmlRenderer())

    svc = ReportService(
        registry=reg, blocks=blocks, renderers=renderers,
        data_collector=_FakeDataCollector(),
    )
    with pytest.raises(ValueError, match="does not declare language"):
        asyncio.run(
            svc.render(
                "zone", "html", "ko",   # zone descriptor only declares th/en
                out_path=tmp_path / "x.html",
                params={"zone_code": "01"},
            )
        )
