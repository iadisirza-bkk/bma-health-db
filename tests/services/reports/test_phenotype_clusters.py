"""Sprint S11 — PhenotypeClustersBlock tests.

Coverage:
    * Happy path with synthetic 4-cluster data → silhouette > 0.4
    * Empty data → graceful empty figure
    * Single distinct row → graceful (degenerate KMeans)
    * Auto-k silhouette sweep picks k > 1 when k_clusters is None
    * Fixed k_clusters honored
    * Reference loadings present in payload (one entry per lab column)
    * HTML output parses cleanly via lxml
    * LaTeX output contains ``\\includegraphics{...}`` to a generated PNG
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

try:
    import sklearn  # noqa: F401
    _HAS_SK = True
except ImportError:  # pragma: no cover
    _HAS_SK = False

from services.reports.blocks.base import AudienceTarget
from services.reports.blocks.phenotype_clusters import (  # noqa: E402
    PhenotypeClustersBlock,
)
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)


pytestmark = pytest.mark.skipif(
    not (_HAS_MPL and _HAS_SK),
    reason="matplotlib + sklearn required",
)


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


def _synth_4cluster_rows(n_per: int = 50, seed: int = 42) -> List[Dict[str, Any]]:
    """Four well-separated 6-dim Gaussian blobs — silhouette ~0.5+ expected."""
    import numpy as np

    rng = np.random.default_rng(seed)
    cols = ["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"]
    centers = np.array(
        [
            [+3, +3, +3, +3, +3, +3],
            [-3, -3, -3, -3, -3, -3],
            [+3, -3, +3, -3, +3, -3],
            [-3, +3, -3, +3, -3, +3],
        ],
        dtype=float,
    )
    rows: List[Dict[str, Any]] = []
    for c in centers:
        pts = rng.normal(loc=c, scale=0.5, size=(n_per, len(cols)))
        for p in pts:
            rows.append({col: float(v) for col, v in zip(cols, p)})
    return rows


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_phenotype_happy_path_finds_clusters_and_renders_png() -> None:
    block = PhenotypeClustersBlock()
    rows = _synth_4cluster_rows()
    params = block.Parameters(
        lab_columns=["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"],
        caption_th="Phenotype demo",
    )
    ctx = _ctx(extra={"phenotype_rows": rows})
    data = await block.collect(ctx, params)
    assert data["n"] == len(rows)
    assert 2 <= data["k"] <= 6
    assert data["silhouette"] > 0.4  # well-separated blobs
    assert len(data["pca_explained"]) == 2
    assert len(data["loadings"]) == 6
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex
    assert ".png}" in latex
    assert r"\begin{figure}" in latex


@pytest.mark.anyio
async def test_phenotype_silhouette_for_synthetic_4cluster_data() -> None:
    """Specifically check the deliverable claim — silhouette ≥0.5 for clean 4-blob."""
    block = PhenotypeClustersBlock()
    rows = _synth_4cluster_rows(n_per=80, seed=123)
    params = block.Parameters(
        lab_columns=["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"],
        k_clusters=4,
    )
    ctx = _ctx(extra={"phenotype_rows": rows})
    data = await block.collect(ctx, params)
    assert data["k"] == 4
    assert data["silhouette"] >= 0.5
    assert len(data["cluster_centers"]) == 4


# ---------------------------------------------------------------------------
# Empty / degenerate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_phenotype_empty_data_renders_gracefully() -> None:
    block = PhenotypeClustersBlock()
    params = block.Parameters(lab_columns=["fbs", "ldl"])
    ctx = _ctx(extra={"phenotype_rows": []})
    data = await block.collect(ctx, params)
    assert data["n"] == 0
    assert data["k"] == 0
    latex = block.render_latex(data, params, ctx)
    # Empty path should still produce a figure with includegraphics so
    # the surrounding LaTeX doesn't bleed.
    assert r"\includegraphics" in latex


@pytest.mark.anyio
async def test_phenotype_single_row_is_degenerate_and_safe() -> None:
    block = PhenotypeClustersBlock()
    params = block.Parameters(lab_columns=["fbs", "ldl"])
    rows = [{"fbs": 100.0, "ldl": 130.0}]
    ctx = _ctx(extra={"phenotype_rows": rows})
    data = await block.collect(ctx, params)
    # n<2 → empty path triggers
    assert data["k"] == 0
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex


@pytest.mark.anyio
async def test_phenotype_zero_variance_columns_dont_crash() -> None:
    """Column where every row has the same value → safe stds path."""
    block = PhenotypeClustersBlock()
    rows = [
        {"fbs": 100.0, "ldl": v, "hdl": 50.0}  # ldl varies, others constant
        for v in [110, 120, 130, 140, 150, 90, 100, 80, 70, 60]
    ]
    params = block.Parameters(lab_columns=["fbs", "ldl", "hdl"])
    ctx = _ctx(extra={"phenotype_rows": rows})
    data = await block.collect(ctx, params)
    # Should not raise; clustering may collapse but block returns something.
    assert data["n"] == len(rows)


@pytest.mark.anyio
async def test_phenotype_drops_nan_rows_listwise() -> None:
    block = PhenotypeClustersBlock()
    base = _synth_4cluster_rows(n_per=20)
    base.append({"fbs": float("nan"), "ldl": 1, "hdl": 1, "trigly": 1, "egfr": 1, "sgot": 1})
    base.append({"fbs": 1, "ldl": 1, "hdl": 1, "trigly": 1, "egfr": 1, "sgot": None})
    params = block.Parameters(
        lab_columns=["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"],
        k_clusters=4,
    )
    ctx = _ctx(extra={"phenotype_rows": base})
    data = await block.collect(ctx, params)
    # Two pollutant rows must be dropped.
    assert data["n"] == 80


# ---------------------------------------------------------------------------
# Parameters honored
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_phenotype_fixed_k_clusters_honored() -> None:
    block = PhenotypeClustersBlock()
    rows = _synth_4cluster_rows()
    params = block.Parameters(
        lab_columns=["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"],
        k_clusters=3,
    )
    ctx = _ctx(extra={"phenotype_rows": rows})
    data = await block.collect(ctx, params)
    assert data["k"] == 3


@pytest.mark.anyio
async def test_phenotype_pca_explained_variance_decreasing() -> None:
    """PC1 variance ratio should always be ≥ PC2 by definition of PCA."""
    block = PhenotypeClustersBlock()
    rows = _synth_4cluster_rows()
    params = block.Parameters(
        lab_columns=["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"],
    )
    ctx = _ctx(extra={"phenotype_rows": rows})
    data = await block.collect(ctx, params)
    assert data["pca_explained"][0] >= data["pca_explained"][1]


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_phenotype_html_parses_cleanly_via_lxml() -> None:
    pytest.importorskip("lxml")
    from lxml import etree

    block = PhenotypeClustersBlock()
    rows = _synth_4cluster_rows(n_per=20)
    params = block.Parameters(
        lab_columns=["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"],
        caption_th="ทดสอบ",
    )
    ctx = _ctx(extra={"phenotype_rows": rows})
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    # Wrap in a single root for parser strictness.
    root = etree.fromstring(f"<root>{html}</root>")
    assert root.find(".//figure") is not None
    assert root.find(".//img") is not None


@pytest.mark.anyio
async def test_phenotype_latex_contains_includegraphics_to_generated() -> None:
    block = PhenotypeClustersBlock()
    rows = _synth_4cluster_rows(n_per=20)
    params = block.Parameters(
        lab_columns=["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"],
    )
    ctx = _ctx(extra={"phenotype_rows": rows})
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex
    assert ".png}" in latex
    # Path must exist on disk after render.
    import re
    m = re.search(r"\\includegraphics\[[^\]]*\]\{([^}]+\.png)\}", latex)
    assert m is not None
    assert Path(m.group(1)).exists()


# ---------------------------------------------------------------------------
# Audience target + provider
# ---------------------------------------------------------------------------


def test_phenotype_block_targets_researcher_audience() -> None:
    assert PhenotypeClustersBlock.audience_target == AudienceTarget.RESEARCHER


@pytest.mark.anyio
async def test_phenotype_provider_callable_path() -> None:
    """Verify the async ``phenotype_provider`` injection works."""
    rows = _synth_4cluster_rows(n_per=20)

    async def fake_provider(lab_cols: List[str], filters: Dict[str, Any]):
        return rows

    block = PhenotypeClustersBlock()
    params = block.Parameters(
        lab_columns=["fbs", "ldl", "hdl", "trigly", "egfr", "sgot"],
    )
    ctx = _ctx(extra={"phenotype_provider": fake_provider})
    data = await block.collect(ctx, params)
    assert data["n"] == len(rows)
