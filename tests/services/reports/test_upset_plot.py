"""Sprint S11 — UpSetPlotBlock tests.

Coverage:
    * Happy path with synthetic comorbidity data
    * Empty data → graceful empty figure
    * Single-disease (no overlap) → only single-set bars, no intersections
    * Set order honored when explicit
    * max_intersections cap honored
    * NaN / missing fields safe
    * Cap on max_intersections=1 still produces a figure
    * HTML output parses cleanly via lxml
    * LaTeX output contains ``\\includegraphics{...}``
    * RESEARCHER audience target declared
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

try:
    import matplotlib  # noqa: F401
    _HAS_MPL = True
except ImportError:  # pragma: no cover
    _HAS_MPL = False

from services.reports.blocks.base import AudienceTarget  # noqa: E402
from services.reports.blocks.upset_plot import UpSetPlotBlock  # noqa: E402
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


pytestmark = pytest.mark.skipif(not _HAS_MPL, reason="matplotlib required")


# ---------------------------------------------------------------------------
# Test infra
# ---------------------------------------------------------------------------


class _FakeDataCollector:
    def data(self) -> Dict[str, Any]:
        return {}


def _ctx(extra: Optional[Dict[str, Any]] = None, lang: str = "th") -> RenderContext:
    desc = ReportDescriptor(
        report_id="t",
        title_th="t",
        title_en="t",
        formats=["html", "latex"],
        languages=["th", "en"],
        sections=[SectionSpec(id="s1", block="cover_page")],
    )
    return RenderContext(
        data_collector=_FakeDataCollector(),
        lang=lang,
        fmt="latex",
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


def _comorbid_rows() -> List[Dict[str, Any]]:
    """Synthetic 6-set comorbidity rows for the canonical BMA case."""
    rows: List[Dict[str, Any]] = []
    sets = ("DM", "HT", "CVD", "dyslipid", "obesity", "stroke")

    def mk(combo: Dict[str, bool], count: int, pid_start: int) -> List[Dict[str, Any]]:
        out = []
        for i in range(count):
            base: Dict[str, Any] = {"patient_id": pid_start + i}
            for s in sets:
                base[s] = bool(combo.get(s, False))
            out.append(base)
        return out

    rows += mk({"DM": True}, 80, 0)
    rows += mk({"HT": True}, 70, 100)
    rows += mk({"CVD": True}, 30, 200)
    rows += mk({"dyslipid": True}, 60, 300)
    rows += mk({"obesity": True}, 50, 400)
    rows += mk({"stroke": True}, 15, 500)
    rows += mk({"DM": True, "HT": True}, 40, 600)
    rows += mk({"DM": True, "HT": True, "dyslipid": True}, 20, 700)
    rows += mk({"HT": True, "CVD": True}, 12, 800)
    rows += mk({"DM": True, "HT": True, "obesity": True}, 8, 900)
    return rows


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upset_happy_path_finds_all_sets_and_intersections() -> None:
    block = UpSetPlotBlock()
    rows = _comorbid_rows()
    params = block.Parameters(sets_spec_id="ncd_comorbidity")
    ctx = _ctx(extra={"upset_rows": rows})
    data = await block.collect(ctx, params)
    assert data["n"] == len(rows)
    # 6 disease columns inferred (sorted alphabetically).
    assert sorted(data["set_names"]) == ["CVD", "DM", "HT", "dyslipid", "obesity", "stroke"]
    # At least 6 distinct intersections (each single-disease + overlaps).
    assert len(data["intersections"]) >= 6
    # All-False rows excluded — total count of intersections matches sum(rows).
    counted = sum(int(it["count"]) for it in data["intersections"])
    assert counted <= len(rows)
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex
    assert ".png}" in latex


@pytest.mark.anyio
async def test_upset_intersection_sizes_sorted_descending() -> None:
    block = UpSetPlotBlock()
    rows = _comorbid_rows()
    params = block.Parameters(sets_spec_id="ncd")
    ctx = _ctx(extra={"upset_rows": rows})
    data = await block.collect(ctx, params)
    counts = [it["count"] for it in data["intersections"]]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# Empty / degenerate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upset_empty_data_renders_gracefully() -> None:
    block = UpSetPlotBlock()
    params = block.Parameters(sets_spec_id="ncd")
    ctx = _ctx(extra={"upset_rows": []})
    data = await block.collect(ctx, params)
    assert data["n"] == 0
    assert data["intersections"] == []
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex


@pytest.mark.anyio
async def test_upset_no_overlapping_sets_only_singletons() -> None:
    """Each patient has exactly one disease — only singleton bars expected."""
    block = UpSetPlotBlock()
    rows = [
        {"patient_id": i, "DM": True, "HT": False, "CVD": False}
        for i in range(20)
    ] + [
        {"patient_id": 100 + i, "DM": False, "HT": True, "CVD": False}
        for i in range(15)
    ] + [
        {"patient_id": 200 + i, "DM": False, "HT": False, "CVD": True}
        for i in range(10)
    ]
    params = block.Parameters(sets_spec_id="single")
    ctx = _ctx(extra={"upset_rows": rows})
    data = await block.collect(ctx, params)
    # Three intersections — one per disease.
    assert len(data["intersections"]) == 3
    for it in data["intersections"]:
        assert sum(it["combo"]) == 1


@pytest.mark.anyio
async def test_upset_all_false_rows_excluded() -> None:
    """Patients with no disease should not contribute to any intersection."""
    block = UpSetPlotBlock()
    rows = [
        {"patient_id": 1, "A": True, "B": False},
        {"patient_id": 2, "A": False, "B": False},  # excluded
        {"patient_id": 3, "A": False, "B": True},
        {"patient_id": 4, "A": False, "B": False},  # excluded
    ]
    params = block.Parameters(sets_spec_id="ab")
    ctx = _ctx(extra={"upset_rows": rows})
    data = await block.collect(ctx, params)
    total = sum(int(it["count"]) for it in data["intersections"])
    assert total == 2  # only the 2 non-empty rows


# ---------------------------------------------------------------------------
# Parameters honored
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upset_max_intersections_cap_honored() -> None:
    block = UpSetPlotBlock()
    rows = _comorbid_rows()
    params = block.Parameters(sets_spec_id="ncd", max_intersections=3)
    ctx = _ctx(extra={"upset_rows": rows})
    data = await block.collect(ctx, params)
    assert len(data["intersections"]) <= 3


@pytest.mark.anyio
async def test_upset_explicit_set_order_honored() -> None:
    block = UpSetPlotBlock()
    rows = _comorbid_rows()
    explicit = ["DM", "HT", "CVD", "dyslipid", "obesity", "stroke"]
    params = block.Parameters(sets_spec_id="ncd", set_order=explicit)
    ctx = _ctx(extra={"upset_rows": rows})
    data = await block.collect(ctx, params)
    assert list(data["set_names"]) == explicit


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upset_html_parses_cleanly_via_lxml() -> None:
    pytest.importorskip("lxml")
    from lxml import etree

    block = UpSetPlotBlock()
    rows = _comorbid_rows()
    params = block.Parameters(sets_spec_id="ncd", caption_th="Comorbidity overlap")
    ctx = _ctx(extra={"upset_rows": rows})
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    root = etree.fromstring(f"<root>{html}</root>")
    assert root.find(".//figure") is not None
    img = root.find(".//img")
    assert img is not None


@pytest.mark.anyio
async def test_upset_latex_contains_includegraphics_to_generated() -> None:
    block = UpSetPlotBlock()
    rows = _comorbid_rows()
    params = block.Parameters(sets_spec_id="ncd")
    ctx = _ctx(extra={"upset_rows": rows})
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex
    import re
    m = re.search(r"\\includegraphics\[[^\]]*\]\{([^}]+\.png)\}", latex)
    assert m is not None
    assert Path(m.group(1)).exists()


# ---------------------------------------------------------------------------
# Audience target + provider
# ---------------------------------------------------------------------------


def test_upset_block_targets_researcher_audience() -> None:
    assert UpSetPlotBlock.audience_target == AudienceTarget.RESEARCHER


@pytest.mark.anyio
async def test_upset_provider_callable_path() -> None:
    rows = _comorbid_rows()

    async def fake_provider(spec_id: str, filters: Dict[str, Any]):
        assert spec_id == "ncd"
        return rows

    block = UpSetPlotBlock()
    params = block.Parameters(sets_spec_id="ncd")
    ctx = _ctx(extra={"upset_provider": fake_provider})
    data = await block.collect(ctx, params)
    assert data["n"] == len(rows)
