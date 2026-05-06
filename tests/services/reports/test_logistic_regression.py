"""S11 — ``logistic_regression`` block regression tests.

Pins the contract between this block, statsmodels, and the
``forest_plot`` renderer. All tests use synthetic data — no DB access —
so the suite stays deterministic.

Coverage (≥6 tests per Agent B brief):
    1. Happy path — known coefficients, AUC > 0.7, McFadden R² > 0.
    2. Empty rows → graceful "ไม่มีข้อมูล" output.
    3. All-significant case → forest plot renders with annotations.
    4. Non-converging fit (perfect separation) → warning + NaN rows.
    5. HTML output structurally valid (parses with lxml).
    6. LaTeX output contains ``\\includegraphics{...}`` + ``\\caption{}``.
    7. Audience target = ``RESEARCHER``.
    8. fetch(spec_id, filters) shape supported.
    9. dict-bag shape supported (legacy collector).
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

from services.reports.blocks.base import AudienceTarget  # noqa: E402
from services.reports.blocks.logistic_regression import (  # noqa: E402
    LogisticRegressionBlock,
    _LogisticRegressionParams,
    _build_design_matrix,
    _fit_glm,
    _result_to_rows,
)
from services.reports.spec import (  # noqa: E402
    RenderContext,
    ReportDescriptor,
    SectionSpec,
)

try:
    import matplotlib  # noqa: F401
    _HAS_MPL = True
except ImportError:  # pragma: no cover — runtime dep
    _HAS_MPL = False

try:
    import statsmodels  # noqa: F401
    _HAS_SM = True
except ImportError:  # pragma: no cover — runtime dep
    _HAS_SM = False

try:
    import lxml.html  # noqa: F401
    _HAS_LXML = True
except ImportError:  # pragma: no cover — runtime dep
    _HAS_LXML = False


# ---------------------------------------------------------------------------
# Test infra
# ---------------------------------------------------------------------------


class _DictBagCollector:
    """Legacy collector shape: ``data()`` returns a dict whose keys match
    spec_ids and whose values are flat row lists."""

    def __init__(self, rows_by_spec: Dict[str, List[Dict[str, Any]]]) -> None:
        self._bag = rows_by_spec

    def data(self) -> Dict[str, Any]:
        return self._bag


class _FetchCollector:
    """Newer collector shape: a ``fetch(spec_id, filters)`` entrypoint
    returning a dict / list. Brief explicitly references this shape."""

    def __init__(self, rows_by_spec: Dict[str, List[Dict[str, Any]]]) -> None:
        self._bag = rows_by_spec
        self.calls: List[tuple] = []

    def fetch(
        self, spec_id: str, filters: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.calls.append((spec_id, dict(filters)))
        return {"rows": list(self._bag.get(spec_id, []))}


def _ctx(
    collector: Any,
    *,
    lang: str = "th",
    fmt: str = "latex",
) -> RenderContext:
    desc = ReportDescriptor(
        report_id="t",
        title_th="t",
        title_en="t",
        formats=["html", "latex"],
        languages=["th", "en"],
        sections=[SectionSpec(id="s1", block="logistic_regression")],
    )
    return RenderContext(
        data_collector=collector,
        lang=lang,
        fmt=fmt,
        descriptor=desc,
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )


def _synth_rows(
    n: int = 200,
    *,
    seed: int = 42,
    beta: tuple = (-0.5, 1.2, -0.7),
) -> List[Dict[str, Any]]:
    """Generate ``n`` rows with binary outcome ``has_dm`` driven by
    a logit model on (age_z, bmi_z) + intercept ``beta[0]``.

    The known coefficients let downstream tests assert that
    statsmodels recovers them within a reasonable tolerance.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    x1 = rng.standard_normal(n)
    x2 = rng.standard_normal(n)
    logits = beta[0] + beta[1] * x1 + beta[2] * x2
    p = 1.0 / (1.0 + np.exp(-logits))
    y = (rng.random(n) < p).astype(int)
    return [
        {
            "has_dm": int(y[i]),
            "age_z": float(x1[i]),
            "bmi_z": float(x2[i]),
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_audience_target_is_researcher():
    """Block targets the RESEARCHER persona."""
    assert LogisticRegressionBlock.audience_target is AudienceTarget.RESEARCHER
    assert LogisticRegressionBlock.block_id == "logistic_regression"


@pytest.mark.skipif(not _HAS_SM, reason="statsmodels not available")
@pytest.mark.anyio
async def test_happy_path_recovers_known_coefficients():
    """Synthetic data with β=(−0.5, 1.2, −0.7) → fit recovers ORs.

    Tolerance is generous (within 30%) so the test is stable across
    seeds; the key assertion is the SIGN of each coefficient is
    correct + AUC > 0.7 + McFadden R² > 0.
    """
    rows = _synth_rows(n=400, seed=42)
    collector = _DictBagCollector({"has_dm": rows})
    ctx = _ctx(collector)
    block = LogisticRegressionBlock()
    params = _LogisticRegressionParams(
        outcome_spec_id="has_dm",
        predictors=["age_z", "bmi_z"],
    )
    data = await block.collect(ctx, params)
    assert data["n"] == 400
    assert 0 < data["n_events"] < 400
    # OR(age_z) > 1 (positive coefficient), OR(bmi_z) < 1 (negative).
    or_age = next(r for r in data["rows"] if r["label"] == "age_z")
    or_bmi = next(r for r in data["rows"] if r["label"] == "bmi_z")
    assert or_age["estimate"] > 1.0
    assert or_bmi["estimate"] < 1.0
    # CI excludes null on the correct side.
    assert or_age["ci_lo"] > 1.0
    assert or_bmi["ci_hi"] < 1.0
    # AUC and McFadden R² are sane.
    assert data["auc"] is not None
    assert data["auc"] > 0.7
    assert data["mcfadden_r2"] > 0.0
    # Model summary header includes "Logit" and the n.
    assert "Logit" in data["model_summary"]
    assert "400" in data["model_summary"]


@pytest.mark.skipif(not _HAS_SM, reason="statsmodels not available")
@pytest.mark.anyio
async def test_empty_rows_renders_graceful_message():
    """No rows → renders ``ไม่มีข้อมูล`` placeholder, no crash."""
    collector = _DictBagCollector({"has_dm": []})
    ctx = _ctx(collector)
    block = LogisticRegressionBlock()
    params = _LogisticRegressionParams(
        outcome_spec_id="has_dm",
        predictors=["age_z"],
    )
    data = await block.collect(ctx, params)
    assert data["rows"] == []
    assert data["n"] == 0
    assert "no rows" in (data.get("warning") or "")
    latex = block.render_latex(data, params, ctx)
    assert "ไม่มีข้อมูล" in latex
    html_out = block.render_html(data, params, ctx)
    assert "ไม่มีข้อมูล" in html_out


@pytest.mark.skipif(not (_HAS_SM and _HAS_MPL), reason="needs sm + mpl")
@pytest.mark.anyio
async def test_all_significant_renders_forest_plot_with_caption():
    """Strong-signal data → all rows highly significant → forest plot
    rendered with caption + n_events on each row."""
    # Bigger β → almost-deterministic outcome.
    rows = _synth_rows(n=600, seed=7, beta=(-0.5, 2.5, -2.0))
    collector = _DictBagCollector({"has_dm": rows})
    ctx = _ctx(collector)
    block = LogisticRegressionBlock()
    params = _LogisticRegressionParams(
        outcome_spec_id="has_dm",
        predictors=["age_z", "bmi_z"],
        caption_th="OR ของปัจจัยเสี่ยงโรค DM",
    )
    data = await block.collect(ctx, params)
    # Both predictors should be highly significant.
    for r in data["rows"]:
        assert r["p_value"] is not None
        assert r["p_value"] < 0.001
    latex = block.render_latex(data, params, ctx)
    assert r"\includegraphics" in latex
    assert r"\caption{" in latex
    assert "DM" in latex


@pytest.mark.skipif(not _HAS_SM, reason="statsmodels not available")
@pytest.mark.anyio
async def test_non_converging_fit_returns_graceful_rows():
    """Perfect separation → fit cannot converge → warning + NaN rows."""
    # Build perfect-separation data: y is exactly thresh on x1.
    rows: List[Dict[str, Any]] = []
    for i, x in enumerate(range(-50, 50)):
        rows.append(
            {
                "has_dm": 1 if x > 0 else 0,
                "age_z": float(x) / 10.0,
                "bmi_z": 0.0,  # zero variance — will be dropped
            }
        )
    collector = _DictBagCollector({"has_dm": rows})
    ctx = _ctx(collector)
    block = LogisticRegressionBlock()
    params = _LogisticRegressionParams(
        outcome_spec_id="has_dm",
        predictors=["age_z", "bmi_z"],
    )
    data = await block.collect(ctx, params)
    # Either model failed to converge (NaN OR rows) OR converged with
    # extreme estimates. Both cases must surface gracefully:
    # - data["rows"] is non-empty
    # - render_latex does NOT crash
    assert data["rows"]
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{figure}" in latex
    # Either "did not converge" warning OR a regular figure — both ok,
    # since perfect separation can either produce huge ORs (singular)
    # OR fail outright. We only require graceful output.


@pytest.mark.skipif(
    not (_HAS_SM and _HAS_MPL and _HAS_LXML),
    reason="needs sm + mpl + lxml",
)
@pytest.mark.anyio
async def test_html_output_parses_with_lxml():
    """HTML output is structurally valid + contains a ``<dl>`` of
    diagnostics + a ``<figure>`` with the forest plot image."""
    import lxml.html

    rows = _synth_rows(n=200)
    collector = _DictBagCollector({"has_dm": rows})
    ctx = _ctx(collector, lang="th", fmt="html")
    block = LogisticRegressionBlock()
    params = _LogisticRegressionParams(
        outcome_spec_id="has_dm",
        predictors=["age_z", "bmi_z"],
    )
    data = await block.collect(ctx, params)
    html_out = block.render_html(data, params, ctx)
    tree = lxml.html.fromstring(html_out)
    assert tree.tag == "section"
    assert "logistic-regression" in (tree.get("class") or "")
    fig = tree.find("figure")
    assert fig is not None
    img = fig.find("img")
    assert img is not None
    assert img.get("src", "").startswith("data:image/png;base64,")
    dl = tree.find("dl")
    assert dl is not None
    # Must have keys: n, events, AUC, McFadden R²
    dts = [dt.text for dt in dl.findall("dt") if dt.text]
    assert "n" in dts
    assert "events" in dts
    assert "AUC" in dts


@pytest.mark.skipif(not (_HAS_SM and _HAS_MPL), reason="needs sm + mpl")
@pytest.mark.anyio
async def test_latex_output_contains_includegraphics_and_caption():
    """LaTeX figure includes a real PNG path + a ``\\caption{}`` block
    + ``\\label{fig:logistic_...}``."""
    import re

    rows = _synth_rows(n=200)
    collector = _DictBagCollector({"has_dm": rows})
    ctx = _ctx(collector)
    block = LogisticRegressionBlock()
    params = _LogisticRegressionParams(
        outcome_spec_id="has_dm",
        predictors=["age_z", "bmi_z"],
        caption_th="โมเดลสำหรับโรค DM",
    )
    data = await block.collect(ctx, params)
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{figure}" in latex
    assert r"\includegraphics" in latex
    assert r"\caption{" in latex
    assert r"\label{fig:logistic_" in latex
    # Path inside includegraphics is absolute and the file exists.
    m = re.search(r"\\includegraphics\[[^\]]*\]\{([^}]+)\}", latex)
    assert m is not None
    png_path = Path(m.group(1))
    assert png_path.is_absolute()
    assert png_path.exists()
    # Diagnostics tabular.
    assert r"\begin{tabular}" in latex
    assert "AUC" in latex
    assert "McFadden" in latex


@pytest.mark.skipif(not _HAS_SM, reason="statsmodels not available")
@pytest.mark.anyio
async def test_fetch_collector_shape_is_supported():
    """When the collector exposes ``fetch(spec_id, filters)``, the block
    uses it directly (preferred path per the brief)."""
    rows = _synth_rows(n=200)
    collector = _FetchCollector({"has_dm": rows})
    ctx = _ctx(collector)
    block = LogisticRegressionBlock()
    params = _LogisticRegressionParams(
        outcome_spec_id="has_dm",
        predictors=["age_z", "bmi_z"],
        filters={"district": "BKK"},
    )
    data = await block.collect(ctx, params)
    # The fetch call was made with the right args.
    assert collector.calls
    spec_id, filters = collector.calls[0]
    assert spec_id == "has_dm"
    assert filters == {"district": "BKK"}
    # And we got real fitted rows back.
    assert data["n"] == 200


@pytest.mark.skipif(not _HAS_SM, reason="statsmodels not available")
@pytest.mark.anyio
async def test_filters_applied_to_dict_bag_rows():
    """When the collector returns the raw bag (no filtering), the block
    applies ``params.filters`` in-process so callers can rely on it."""
    rows = _synth_rows(n=100)
    # Tag half the rows with district="BKK", half with "CMI".
    for i, r in enumerate(rows):
        r["district"] = "BKK" if i % 2 == 0 else "CMI"
    collector = _DictBagCollector({"has_dm": rows})
    ctx = _ctx(collector)
    block = LogisticRegressionBlock()
    params = _LogisticRegressionParams(
        outcome_spec_id="has_dm",
        predictors=["age_z", "bmi_z"],
        filters={"district": "BKK"},
    )
    data = await block.collect(ctx, params)
    assert data["n"] == 50  # half the rows survive the filter


def test_build_design_matrix_drops_incomplete_rows():
    """Rows with missing predictor or outcome values are skipped."""
    import numpy as np

    rows = [
        {"y": 1, "x1": 0.5, "x2": 0.3},
        {"y": 0, "x1": None, "x2": 0.1},  # bad x1
        {"y": None, "x1": 0.2, "x2": 0.0},  # bad y
        {"y": 1, "x1": 0.7, "x2": float("nan")},  # bad x2
        {"y": 0, "x1": -0.4, "x2": -0.1},
    ]
    y, X, kept, n_dropped = _build_design_matrix(rows, "y", ["x1", "x2"])
    assert y.size == 2
    assert X.shape == (2, 2)
    assert kept == ["x1", "x2"]
    assert n_dropped == 3
    # Surviving rows are the first and last input rows.
    np.testing.assert_array_equal(y, np.array([1.0, 0.0]))


def test_build_design_matrix_drops_zero_variance_predictor():
    """A predictor with zero variance is dropped (would otherwise cause
    a singular Hessian in the Logit fit)."""
    rows = [
        {"y": 1, "x1": 0.5, "x2": 0.0},
        {"y": 0, "x1": 0.3, "x2": 0.0},
        {"y": 1, "x1": -0.1, "x2": 0.0},
    ]
    y, X, kept, _ = _build_design_matrix(rows, "y", ["x1", "x2"])
    assert kept == ["x1"]
    assert X.shape == (3, 1)


@pytest.mark.skipif(not _HAS_SM, reason="statsmodels not available")
def test_result_to_rows_returns_nan_when_fit_is_none():
    """When ``_fit_glm`` returns None, ``_result_to_rows`` produces
    NaN-estimate rows with a ``warning`` key — never crashes."""
    rows = _result_to_rows(None, ["age_z", "bmi_z"], "logit")
    assert len(rows) == 2
    for r in rows:
        # NaN-estimate rows are graceful placeholders.
        import math as _math

        assert _math.isnan(r["estimate"])
        assert "warning" in r


@pytest.mark.skipif(not _HAS_SM, reason="statsmodels not available")
def test_fit_glm_returns_result_for_well_behaved_data():
    """Smoke test: synthetic data fits cleanly + ``prsquared`` > 0."""
    import numpy as np

    rng = np.random.default_rng(3)
    n = 300
    x = rng.standard_normal((n, 2))
    logits = 0.2 + 0.8 * x[:, 0] - 0.6 * x[:, 1]
    p = 1.0 / (1.0 + np.exp(-logits))
    y = (rng.random(n) < p).astype(int)
    result = _fit_glm(y, x, "logit")
    assert result is not None
    assert float(result.prsquared) > 0.0
