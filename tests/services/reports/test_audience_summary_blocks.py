"""Tests for the S8 ``audience_summary_people`` + ``audience_summary_executive``
content blocks.

Both blocks are pure-data (no chart service / MV repository), so the
fixtures are tiny — a fake ``data_collector`` returning a hand-rolled
``district_data`` payload is enough to exercise:

* ``collect`` produces a non-empty dict
* ``render_html`` returns markup starting with ``<``
* ``render_latex`` returns a non-empty string
* The matrix runs over ``th`` and ``en`` languages — both blocks pin
  the LaTeX/HTML wording to Thai authoring (the audience targets are
  Thai-speaking ประชาชน + ผู้บริหาร), so we still expect non-empty
  output in either language. The English code path exists primarily
  for the disclaimer copy that A1 added.
* The ``filters`` parameter narrows the scope (zone_code) so the
  computed top-3 / priority list reflects only the filtered districts.
* Both blocks declare an ``audience_target`` ClassVar that maps to
  the new ``AudienceTarget`` enum.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks import (  # noqa: E402
    AudienceSummaryExecutiveBlock,
    AudienceSummaryPeopleBlock,
    AudienceTarget,
    block_registry,
)
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeDataCollector:
    """Mirrors the fake from ``test_blocks.py`` — exposes ``data()``."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def data(self) -> Dict[str, Any]:
        return self._payload


def _district_payload() -> Dict[str, Any]:
    """Three districts spanning the green / yellow / red traffic-light bands.

    The shape mirrors the legacy ``ReportData.district_data`` (per-dcode
    dict with ``total_screened``, ``zone_code``, ``name_th``, and a
    ``diseases`` map of ``{key: {pct_at_risk: float}}``).
    """
    return {
        "total_screened": 3000,
        "district_data": {
            "1001": {
                "name_th": "บางรัก",
                "zone_code": "01",
                "total_screened": 1000,
                "diseases": {
                    # ~6% — green
                    "diabetes": {"pct_at_risk": 6.0},
                    # ~14% — yellow
                    "hypertension": {"pct_at_risk": 14.0},
                    # 25% — red
                    "obesity": {"pct_at_risk": 25.0},
                },
            },
            "1002": {
                "name_th": "ดุสิต",
                "zone_code": "01",
                "total_screened": 1000,
                "diseases": {
                    "diabetes": {"pct_at_risk": 5.0},
                    "hypertension": {"pct_at_risk": 12.0},
                    "obesity": {"pct_at_risk": 22.0},
                },
            },
            "1003": {
                # In a different zone — used to verify the filter path.
                "name_th": "บางขุนเทียน",
                "zone_code": "07",
                "total_screened": 1000,
                "diseases": {
                    "diabetes": {"pct_at_risk": 30.0},  # very high
                    "hypertension": {"pct_at_risk": 28.0},
                    "obesity": {"pct_at_risk": 35.0},
                },
            },
        },
    }


def _descriptor() -> ReportDescriptor:
    return ReportDescriptor(
        report_id="t",
        title_th="รายงานทดสอบ",
        formats=["html", "latex"],
        languages=["th", "en"],
        sections=[SectionSpec(id="s1", block="audience_summary_people")],
    )


def _ctx(lang: str = "th") -> RenderContext:
    return RenderContext(
        data_collector=_FakeDataCollector(_district_payload()),
        lang=lang,
        fmt="html",
        descriptor=_descriptor(),
        requested_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        extra={},
    )


# ---------------------------------------------------------------------------
# Audience target classvars — pin the contract Agent B's orchestrator
# will consult.
# ---------------------------------------------------------------------------


def test_people_block_has_people_audience_target() -> None:
    assert (
        AudienceSummaryPeopleBlock.audience_target == AudienceTarget.PEOPLE
    )


def test_executive_block_has_executive_audience_target() -> None:
    assert (
        AudienceSummaryExecutiveBlock.audience_target
        == AudienceTarget.EXECUTIVE
    )


def test_audience_target_back_compat_default_is_none() -> None:
    """A legacy block without an explicit ``audience_target`` keeps
    the ``None`` default (which means "render in any audience")."""
    from services.reports.blocks import (
        ParagraphBlock as _Legacy,
    )
    assert _Legacy.audience_target is None


# ---------------------------------------------------------------------------
# Parametric matrix: block × {html, latex} × {th, en}
# ---------------------------------------------------------------------------


_BLOCKS = [
    pytest.param(AudienceSummaryPeopleBlock, id="people"),
    pytest.param(AudienceSummaryExecutiveBlock, id="executive"),
]
_LANGS = ["th", "en"]


@pytest.mark.anyio
@pytest.mark.parametrize("block_cls", _BLOCKS)
@pytest.mark.parametrize("lang", _LANGS)
async def test_block_collect_returns_non_empty_dict(
    block_cls: Any, lang: str
) -> None:
    block = block_cls()
    params = block.Parameters()
    ctx = _ctx(lang=lang)
    data = await block.collect(ctx, params)
    assert isinstance(data, dict)
    assert len(data) > 0


@pytest.mark.anyio
@pytest.mark.parametrize("block_cls", _BLOCKS)
@pytest.mark.parametrize("lang", _LANGS)
async def test_block_render_html_starts_with_angle_bracket(
    block_cls: Any, lang: str
) -> None:
    block = block_cls()
    params = block.Parameters()
    ctx = _ctx(lang=lang)
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)
    assert isinstance(out, str)
    assert out.lstrip().startswith("<")


@pytest.mark.anyio
@pytest.mark.parametrize("block_cls", _BLOCKS)
@pytest.mark.parametrize("lang", _LANGS)
async def test_block_render_latex_non_empty(
    block_cls: Any, lang: str
) -> None:
    block = block_cls()
    params = block.Parameters()
    ctx = _ctx(lang=lang)
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert isinstance(out, str)
    assert out.strip()


# ---------------------------------------------------------------------------
# Block-specific invariants
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_people_top3_is_sorted_descending() -> None:
    """The 3 traffic-light tiles must be the 3 highest at-risk diseases."""
    block = AudienceSummaryPeopleBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    pcts = [t["pct"] for t in data["top3"]]
    assert pcts == sorted(pcts, reverse=True)
    assert len(data["top3"]) == 3


@pytest.mark.anyio
async def test_people_card_classes_reflect_traffic_light_tier() -> None:
    """A 25% at-risk disease should land on a ``card-red`` HTML class."""
    block = AudienceSummaryPeopleBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    # Highest tile is ~27.5% (weighted obesity) → red.
    assert "card-red" in html


@pytest.mark.anyio
async def test_people_filter_by_zone_narrows_scope() -> None:
    """``filters={zone_code: '07'}`` selects only district 1003."""
    block = AudienceSummaryPeopleBlock()
    params = block.Parameters(filters={"zone_code": "07"})
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert data["scope_n_districts"] == 1
    assert data["scope_total_screened"] == 1000
    # The filtered slice has obesity at 35% — the top tile must be
    # at LEAST 30% (zone-07 outlier).
    assert data["top3"][0]["pct"] >= 30.0


@pytest.mark.anyio
async def test_people_action_checklist_has_3_to_5_items() -> None:
    """The S8 brief pins the action checklist at 3-5 bullets."""
    block = AudienceSummaryPeopleBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert 3 <= len(data["actions"]) <= 5


@pytest.mark.anyio
async def test_executive_kpis_count_three() -> None:
    """The executive block always renders exactly 3 KPI tiles."""
    block = AudienceSummaryExecutiveBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    assert len(data["kpis"]) == 3


@pytest.mark.anyio
async def test_executive_priorities_top3_by_pct() -> None:
    """Priorities are ranked by per-district at-risk pct, descending."""
    block = AudienceSummaryExecutiveBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    prio = data["priorities"]
    assert len(prio) == 3
    pcts = [p["at_risk_pct"] for p in prio]
    assert pcts == sorted(pcts, reverse=True)
    # District 1003 (highest) must be on top.
    assert prio[0]["district_code"] == "1003"


@pytest.mark.anyio
async def test_executive_recommendations_reference_top_districts() -> None:
    """Recommendations must mention at least one district by name —
    the brief insists on data-driven copy, not boilerplate."""
    block = AudienceSummaryExecutiveBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    recs_joined = " ".join(data["recommendations"])
    # บางขุนเทียน is the highest-risk district in the fixture.
    assert "บางขุนเทียน" in recs_joined


@pytest.mark.anyio
async def test_executive_filter_by_zone_changes_priorities() -> None:
    """``filters={zone_code: '01'}`` excludes the outlier district 1003."""
    block = AudienceSummaryExecutiveBlock()
    params = block.Parameters(filters={"zone_code": "01"})
    ctx = _ctx()
    data = await block.collect(ctx, params)
    codes = [p["district_code"] for p in data["priorities"]]
    assert "1003" not in codes
    assert set(codes) <= {"1001", "1002"}


@pytest.mark.anyio
async def test_executive_html_contains_priority_section_header() -> None:
    block = AudienceSummaryExecutiveBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert "เขตที่ต้องเร่งดำเนินการ" in html


@pytest.mark.anyio
async def test_executive_latex_includes_tabular_for_kpis() -> None:
    """The KPI row uses ``\\begin{tabular}`` (no fancy TikZ — S7 stable)."""
    block = AudienceSummaryExecutiveBlock()
    params = block.Parameters()
    ctx = _ctx()
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{tabular}" in latex


# ---------------------------------------------------------------------------
# Registry — both new blocks must be discoverable from
# ``config/reports/blocks/*.yaml`` so the orchestrator can resolve them.
# ---------------------------------------------------------------------------


def test_registry_contains_both_audience_blocks() -> None:
    reg = block_registry(reload=True)
    ids = set(reg.list_ids())
    assert "audience_summary_people" in ids
    assert "audience_summary_executive" in ids
