"""Tests for the ``disease_district_grid`` and ``crosstab`` ContentBlock subclasses.

Per ULTRAPLAN S4.5, these two blocks close the biggest whitepaper-template
parity gap (Section 2: per-disease district loop, and Section 3: factor ×
disease cross-tab). The tests cover:

* ``disease_district_grid``
    - Fake ``MVRepository.run_query`` returns hand-built rows for 3
      districts × 4 diseases (well — 4 disease columns; the MV is wide).
    - ``collect()`` produces N tables matching ``params.diseases``.
    - ``render_latex()`` contains ``\\begin{longtable}`` and at least one
      ``เบาหวาน`` heading.
    - ``render_html()`` contains ``<table class="disease-grid">`` and an
      ``<h3>`` per disease.

* ``crosstab``
    - Fake data with 3 rows × 4 columns, 1 row missing some columns to
      verify fill-with-0 behaviour for ``int`` cells (and ``None`` for
      ``pct``).
    - Pivot is correct (cells map to expected values).
    - Total row + column when ``include_total: true``.
    - Cell formatting honours ``cell_format: pct2``.

Both block classes are also asserted to appear in
``block_registry().list_ids()`` after import — that's the wiring
sanity-check the registry test already does for the initial 7 blocks.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

# Make ``api/`` importable for ``services.reports.*``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks import (  # noqa: E402
    CrosstabBlock,
    CrosstabParams,
    DiseaseDistrictGridBlock,
    DiseaseDistrictGridParams,
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
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def data(self) -> Dict[str, Any]:
        return self._payload


def _descriptor() -> ReportDescriptor:
    return ReportDescriptor(
        report_id="t",
        title_th="รายงานทดสอบ",
        title_en="Test Report",
        formats=["html", "latex"],
        languages=["th"],
        sections=[SectionSpec(id="s1", block="disease_district_grid")],
    )


def _ctx(
    payload: Dict[str, Any] | None = None,
    extra: Dict[str, Any] | None = None,
    lang: str = "th",
) -> RenderContext:
    return RenderContext(
        data_collector=_FakeDataCollector(payload or {}),
        lang=lang,
        fmt="html",
        descriptor=_descriptor(),
        requested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# disease_district_grid — fake MV rows for 3 districts × 4 diseases
# ---------------------------------------------------------------------------

# The MV ``summary_district_disease`` is wide. We populate the columns
# ``DistrictDiseaseRow`` requires plus the optional pct columns. The rows
# returned by the fake ``run_query`` are plain dicts — the block's
# ``_row_to_dict`` accepts dicts directly.

def _fake_district_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    template: Dict[str, Any] = {
        "data_source": "msd",
        "total_screened": 0,
        "risk_dm_count": 0,
        "risk_hpt_count": 0,
        "risk_cvd_count": 0,
        "risk_bmi_count": 0,
        "risk_stroke_count": 0,
        "found_dm_count": 0,
        "found_hpt_count": 0,
        "found_cvd_count": 0,
        "found_stroke_count": 0,
        "found_obesity_count": 0,
        "found_dyslipidemia_count": 0,
        "pct_risk_dm": None,
        "pct_risk_hpt": None,
        "pct_risk_cvd": None,
        "pct_found_dm": None,
        "pct_found_hpt": None,
        "pct_found_cvd": None,
    }
    # 3 districts × 4 disease "snapshots" — values chosen so the
    # weighted-average path is exercised but easy to verify by hand.
    by_district = {
        "1001": {
            "total_screened": 1000,
            "risk_dm_count": 120,
            "risk_hpt_count": 200,
            "risk_cvd_count": 50,
            "risk_stroke_count": 10,
            "found_dm_count": 80,
            "found_hpt_count": 150,
            "found_cvd_count": 30,
            "found_stroke_count": 5,
            "pct_risk_dm": 12.0,
            "pct_risk_hpt": 20.0,
            "pct_risk_cvd": 5.0,
            "pct_found_dm": 8.0,
            "pct_found_hpt": 15.0,
            "pct_found_cvd": 3.0,
        },
        "1002": {
            "total_screened": 500,
            "risk_dm_count": 60,
            "risk_hpt_count": 90,
            "risk_cvd_count": 20,
            "risk_stroke_count": 4,
            "found_dm_count": 40,
            "found_hpt_count": 75,
            "found_cvd_count": 15,
            "found_stroke_count": 2,
            "pct_risk_dm": 12.0,
            "pct_risk_hpt": 18.0,
            "pct_risk_cvd": 4.0,
            "pct_found_dm": 8.0,
            "pct_found_hpt": 15.0,
            "pct_found_cvd": 3.0,
        },
        "1003": {
            "total_screened": 800,
            "risk_dm_count": 100,
            "risk_hpt_count": 160,
            "risk_cvd_count": 40,
            "risk_stroke_count": 8,
            "found_dm_count": 70,
            "found_hpt_count": 130,
            "found_cvd_count": 25,
            "found_stroke_count": 4,
            "pct_risk_dm": 12.5,
            "pct_risk_hpt": 20.0,
            "pct_risk_cvd": 5.0,
            "pct_found_dm": 8.75,
            "pct_found_hpt": 16.25,
            "pct_found_cvd": 3.125,
        },
    }
    for code, payload in by_district.items():
        row = dict(template)
        row["district_code"] = code
        row.update(payload)
        rows.append(row)
    return rows


def _fake_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.run_query.return_value = _fake_district_rows()
    return repo


# ---------------------------------------------------------------------------
# disease_district_grid tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_disease_district_grid_collect_produces_one_table_per_disease() -> None:
    block = DiseaseDistrictGridBlock()
    params = DiseaseDistrictGridParams(
        diseases=["dm", "hpt", "cvd", "stroke"],
        metrics=["risk_count", "risk_pct", "found_count", "found_pct"],
    )
    ctx = _ctx(extra={"mv_repository": _fake_repo()})
    data = await block.collect(ctx, params)

    assert "tables" in data
    assert isinstance(data["tables"], list)
    assert len(data["tables"]) == len(params.diseases)
    keys = [t["disease_key"] for t in data["tables"]]
    assert keys == ["dm", "hpt", "cvd", "stroke"]
    # Thai labels are wired up.
    labels = [t["disease_label_th"] for t in data["tables"]]
    assert "เบาหวาน" in labels
    assert "ความดัน" in labels
    # Each table has 3 districts (matching the fake input).
    for tbl in data["tables"]:
        assert len(tbl["rows"]) == 3
        codes = sorted(r["district_code"] for r in tbl["rows"])
        assert codes == ["1001", "1002", "1003"]


@pytest.mark.anyio
async def test_disease_district_grid_dm_values_match_fake_rows() -> None:
    """Verify column-name translation: pct_risk_dm -> risk_pct, etc."""
    block = DiseaseDistrictGridBlock()
    params = DiseaseDistrictGridParams(
        diseases=["dm"],
        metrics=["risk_count", "risk_pct", "found_count", "found_pct"],
    )
    ctx = _ctx(extra={"mv_repository": _fake_repo()})
    data = await block.collect(ctx, params)
    table = data["tables"][0]
    by_code = {r["district_code"]: r for r in table["rows"]}
    # 1001: risk_dm_count=120, pct_risk_dm=12.0; found_dm_count=80, pct=8.0.
    assert by_code["1001"]["risk_count"] == 120
    assert by_code["1001"]["risk_pct"] == pytest.approx(12.0)
    assert by_code["1001"]["found_count"] == 80
    assert by_code["1001"]["found_pct"] == pytest.approx(8.0)


@pytest.mark.anyio
async def test_disease_district_grid_uses_cached_query() -> None:
    """Multiple ``collect`` calls share the cached MV result (one DB round-trip)."""
    block = DiseaseDistrictGridBlock()
    params = DiseaseDistrictGridParams(diseases=["dm", "hpt"])
    repo = _fake_repo()
    ctx = _ctx(extra={"mv_repository": repo})
    await block.collect(ctx, params)
    await block.collect(ctx, params)
    # Only one query — the second collect hits the cache.
    assert repo.run_query.await_count == 1


@pytest.mark.anyio
async def test_disease_district_grid_render_latex_has_longtable_and_thai_heading() -> None:
    block = DiseaseDistrictGridBlock()
    params = DiseaseDistrictGridParams(diseases=["dm", "hpt"])
    ctx = _ctx(extra={"mv_repository": _fake_repo()})
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert r"\begin{longtable}" in out
    assert r"\end{longtable}" in out
    assert "เบาหวาน" in out
    # Heading uses subsection*.
    assert r"\subsection*{" in out


@pytest.mark.anyio
async def test_disease_district_grid_latex_escapes_percent_in_headers() -> None:
    """S7 carryover regression (S8 fix): the column-header strings
    ``%เสี่ยง`` and ``%พบโรค`` contain a literal ``%`` which is the
    LaTeX comment marker. Without escaping it, ``\\textbf{%เสี่ยง}``
    causes LaTeX to swallow the closing brace and eventually halt with
    "File ended while scanning use of \\textbf". The fix escapes the
    header through ``latex_safe`` before emission.

    This test pins the wire format: every ``\\textbf{`` in the output
    must be balanced (no raw ``%`` immediately after the opening brace).
    """
    block = DiseaseDistrictGridBlock()
    params = DiseaseDistrictGridParams(
        diseases=["dm"],
        metrics=["risk_count", "risk_pct", "found_count", "found_pct"],
    )
    ctx = _ctx(extra={"mv_repository": _fake_repo()})
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    # The hot characters are present, but each ``%`` is escaped.
    assert r"\textbf{\%เสี่ยง}" in out
    assert r"\textbf{\%พบโรค}" in out
    # No raw ``\textbf{%`` (which would have been the broken output).
    assert r"\textbf{%" not in out
    # Brace tally — every ``\textbf{`` is closed within the same line.
    for line in out.splitlines():
        opens = line.count(r"\textbf{")
        # ``}`` count is just a sanity check; we don't require strict
        # equality because line may legitimately have other ``{`` /
        # ``}`` from cell separators. The minimal invariant: opens
        # require at least the same number of close braces somewhere on
        # the same line — not on a later line, because LaTeX argument
        # consumption isn't line-aware but unmatched braces ARE the
        # bug we're guarding against.
        assert line.count("}") >= opens, (
            f"unbalanced \\textbf braces on line: {line!r}"
        )


@pytest.mark.anyio
async def test_disease_district_grid_render_html_has_table_class_and_h3_per_disease() -> None:
    block = DiseaseDistrictGridBlock()
    params = DiseaseDistrictGridParams(diseases=["dm", "hpt", "cvd"])
    ctx = _ctx(extra={"mv_repository": _fake_repo()})
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert '<table class="disease-grid">' in html
    # One <h3> per disease.
    assert html.count("<h3>") == 3
    assert "เบาหวาน" in html
    assert "ความดัน" in html
    # Stack separator.
    assert "<hr>" in html


@pytest.mark.anyio
async def test_disease_district_grid_handles_unknown_disease_with_dashes() -> None:
    """``ckd`` / ``mental`` aren't in the MV — render as '—' not crash."""
    block = DiseaseDistrictGridBlock()
    params = DiseaseDistrictGridParams(diseases=["ckd"])
    ctx = _ctx(extra={"mv_repository": _fake_repo()})
    data = await block.collect(ctx, params)
    table = data["tables"][0]
    for row in table["rows"]:
        assert row["risk_count"] is None
        assert row["risk_pct"] is None
        assert row["found_count"] is None
        assert row["found_pct"] is None
    out = block.render_latex(data, params, ctx)
    assert "—" in out
    assert "ไต" in out  # CKD label.


@pytest.mark.anyio
async def test_disease_district_grid_resolves_district_name_from_collector() -> None:
    """When ``district_data`` exposes name_th, it's used in render_*."""
    block = DiseaseDistrictGridBlock()
    params = DiseaseDistrictGridParams(diseases=["dm"])
    payload = {
        "district_data": {
            "1001": {"name_th": "บางรัก"},
            "1002": {"name_th": "ดุสิต"},
            "1003": {"name_th": "พระนคร"},
        }
    }
    ctx = _ctx(payload=payload, extra={"mv_repository": _fake_repo()})
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert "บางรัก" in html
    assert "ดุสิต" in html


# ---------------------------------------------------------------------------
# crosstab — fake long rows: 3 row-values × 4 col-values, with one
# missing combination to test fill-with-0
# ---------------------------------------------------------------------------


def _crosstab_payload() -> Dict[str, Any]:
    """Three factor levels × four diseases. ``smoke_yes/dyslipidemia`` is missing."""
    rows: List[Dict[str, Any]] = [
        {"factor": "smoke_no", "disease": "dm", "count": 10},
        {"factor": "smoke_no", "disease": "hpt", "count": 20},
        {"factor": "smoke_no", "disease": "cvd", "count": 5},
        {"factor": "smoke_no", "disease": "dyslipidemia", "count": 8},
        {"factor": "smoke_yes", "disease": "dm", "count": 30},
        {"factor": "smoke_yes", "disease": "hpt", "count": 50},
        {"factor": "smoke_yes", "disease": "cvd", "count": 15},
        # smoke_yes / dyslipidemia missing → expect 0 fill.
        {"factor": "smoke_unknown", "disease": "dm", "count": 2},
        {"factor": "smoke_unknown", "disease": "hpt", "count": 3},
        {"factor": "smoke_unknown", "disease": "cvd", "count": 1},
        {"factor": "smoke_unknown", "disease": "dyslipidemia", "count": 4},
    ]
    return {"factors": {"smoking": rows}}


@pytest.mark.anyio
async def test_crosstab_pivots_rows_into_wide_dict() -> None:
    block = CrosstabBlock()
    params = CrosstabParams(
        source_path="factors.smoking",
        row_field="factor",
        col_field="disease",
        value_field="count",
        cell_format="int",
        include_total=False,
    )
    ctx = _ctx(payload=_crosstab_payload())
    data = await block.collect(ctx, params)

    # 3 row labels × 4 column labels.
    assert set(data["rows"]) == {"smoke_no", "smoke_yes", "smoke_unknown"}
    assert set(data["columns"]) == {"dm", "hpt", "cvd", "dyslipidemia"}

    # Pivot values where present.
    assert data["cells"]["smoke_no"]["dm"] == 10
    assert data["cells"]["smoke_yes"]["hpt"] == 50
    assert data["cells"]["smoke_unknown"]["dyslipidemia"] == 4
    # Fill-with-0: smoke_yes / dyslipidemia is the missing combo.
    assert data["cells"]["smoke_yes"]["dyslipidemia"] == 0


@pytest.mark.anyio
async def test_crosstab_includes_row_and_column_totals() -> None:
    block = CrosstabBlock()
    params = CrosstabParams(
        source_path="factors.smoking",
        row_field="factor",
        col_field="disease",
        value_field="count",
        cell_format="int",
        include_total=True,
    )
    ctx = _ctx(payload=_crosstab_payload())
    data = await block.collect(ctx, params)

    # Per-row totals.
    # smoke_no: 10 + 20 + 5 + 8 = 43.
    # smoke_yes: 30 + 50 + 15 + 0 = 95.
    # smoke_unknown: 2 + 3 + 1 + 4 = 10.
    assert data["totals_row"]["smoke_no"] == pytest.approx(43.0)
    assert data["totals_row"]["smoke_yes"] == pytest.approx(95.0)
    assert data["totals_row"]["smoke_unknown"] == pytest.approx(10.0)

    # Per-column totals.
    # dm: 10 + 30 + 2 = 42; hpt: 20 + 50 + 3 = 73; cvd: 5 + 15 + 1 = 21;
    # dyslipidemia: 8 + 0 + 4 = 12.
    assert data["totals_col"]["dm"] == pytest.approx(42.0)
    assert data["totals_col"]["hpt"] == pytest.approx(73.0)
    assert data["totals_col"]["cvd"] == pytest.approx(21.0)
    assert data["totals_col"]["dyslipidemia"] == pytest.approx(12.0)

    # Grand total = 43 + 95 + 10 = 148.
    assert data["grand_total"] == pytest.approx(148.0)


@pytest.mark.anyio
async def test_crosstab_render_latex_emits_tabular_with_n_plus_2_columns() -> None:
    block = CrosstabBlock()
    params = CrosstabParams(
        source_path="factors.smoking",
        row_field="factor",
        col_field="disease",
        value_field="count",
        cell_format="int",
        include_total=True,
    )
    ctx = _ctx(payload=_crosstab_payload())
    data = await block.collect(ctx, params)
    out = block.render_latex(data, params, ctx)
    assert r"\begin{tabular}" in out
    assert r"\end{tabular}" in out
    # 4 columns of data + 2 (label + total) = 6 columns. The col spec
    # uses ``*{4}{c}`` for the data block.
    assert "*{4}{c}" in out
    # Total row footer.
    assert r"\textbf{รวม}" in out


@pytest.mark.anyio
async def test_crosstab_render_html_total_row_and_column() -> None:
    block = CrosstabBlock()
    params = CrosstabParams(
        source_path="factors.smoking",
        row_field="factor",
        col_field="disease",
        value_field="count",
        cell_format="int",
        include_total=True,
    )
    ctx = _ctx(payload=_crosstab_payload())
    data = await block.collect(ctx, params)
    html = block.render_html(data, params, ctx)
    assert '<table class="crosstab">' in html
    assert "<tfoot>" in html
    # Grand total cell.
    assert "148" in html


@pytest.mark.anyio
async def test_crosstab_caption_renders_above_tabular() -> None:
    """Phase 0 caption convention: when ``caption_th`` is supplied, the
    LaTeX output wraps the tabular in a ``table`` float with ``\\caption``
    appearing BEFORE ``\\begin{tabular}`` (= caption above), and the HTML
    output puts ``<caption>`` as the first child of ``<table class=
    "crosstab">``. Without a caption, the legacy bare-tabular /
    un-captioned-table output is preserved.
    """
    block = CrosstabBlock()
    params = CrosstabParams(
        source_path="factors.smoking",
        row_field="factor",
        col_field="disease",
        value_field="count",
        cell_format="int",
        include_total=False,
        caption_th="ตารางไขว้พฤติกรรมสูบบุหรี่ × โรค NCD",
        caption_en="Smoking behavior × NCD cross-tab",
    )
    ctx = _ctx(payload=_crosstab_payload())
    data = await block.collect(ctx, params)

    # Caption resolved per ctx.lang (default "th") + label auto-derived
    # from source_path + row × col so distinct pivots get distinct labels.
    assert data["caption"] == "ตารางไขว้พฤติกรรมสูบบุหรี่ × โรค NCD"
    assert data["label"] == "tab:factors_smoking__factor_x_disease"

    # LaTeX: \caption MUST appear BEFORE \begin{tabular} (= above).
    latex = block.render_latex(data, params, ctx)
    assert r"\begin{table}" in latex
    cap_idx = latex.find(r"\caption{")
    tab_idx = latex.find(r"\begin{tabular}")
    assert cap_idx != -1 and tab_idx != -1
    assert cap_idx < tab_idx, "caption must render ABOVE the tabular"

    # HTML: <caption> MUST be the first child of <table class="crosstab">.
    html = block.render_html(data, params, ctx)
    assert '<table class="crosstab"' in html
    assert "<caption>" in html
    table_open = html.find("<table")
    table_open_close = html.find(">", table_open)
    cap_open = html.find("<caption>")
    assert table_open < cap_open <= table_open_close + 1

    # Back-compat: omitting caption_* keeps the legacy output shape.
    params_no_cap = CrosstabParams(
        source_path="factors.smoking",
        row_field="factor",
        col_field="disease",
        value_field="count",
        cell_format="int",
        include_total=False,
    )
    data_no_cap = await block.collect(ctx, params_no_cap)
    latex_no_cap = block.render_latex(data_no_cap, params_no_cap, ctx)
    assert r"\begin{table}" not in latex_no_cap
    assert r"\caption" not in latex_no_cap
    html_no_cap = block.render_html(data_no_cap, params_no_cap, ctx)
    assert "<caption>" not in html_no_cap
    assert html_no_cap.startswith('<table class="crosstab">')


@pytest.mark.anyio
async def test_crosstab_pct2_formatting() -> None:
    block = CrosstabBlock()
    payload = {
        "factors": {
            "smoking_pct": [
                {"factor": "smoke_no", "disease": "dm", "pct": 12.345},
                {"factor": "smoke_yes", "disease": "dm", "pct": 23.4},
            ]
        }
    }
    params = CrosstabParams(
        source_path="factors.smoking_pct",
        row_field="factor",
        col_field="disease",
        value_field="pct",
        cell_format="pct2",
        include_total=False,
    )
    ctx = _ctx(payload=payload)
    data = await block.collect(ctx, params)
    out = block.render_html(data, params, ctx)
    # Two-decimal percentage formatting.
    assert "12.35%" in out  # 12.345 → 12.35 after rounding.
    assert "23.40%" in out


@pytest.mark.anyio
async def test_crosstab_pct_fills_missing_cells_with_dash_not_zero() -> None:
    """Missing pct cells → '—' (not '0%') because 0% is meaningfully different."""
    block = CrosstabBlock()
    payload = {
        "factors": {
            "smoking_pct": [
                {"factor": "smoke_no", "disease": "dm", "pct": 12.5},
                {"factor": "smoke_no", "disease": "hpt", "pct": 8.0},
                # smoke_yes / hpt missing → expect '—' fill.
                {"factor": "smoke_yes", "disease": "dm", "pct": 25.0},
            ]
        }
    }
    params = CrosstabParams(
        source_path="factors.smoking_pct",
        row_field="factor",
        col_field="disease",
        value_field="pct",
        cell_format="pct",
        include_total=False,
    )
    ctx = _ctx(payload=payload)
    data = await block.collect(ctx, params)
    # The pivot itself stores ``None`` for the missing cell (vs. 0 for int).
    assert data["cells"]["smoke_yes"]["hpt"] is None
    out = block.render_html(data, params, ctx)
    assert "—" in out


@pytest.mark.anyio
async def test_crosstab_handles_missing_source_path_gracefully() -> None:
    block = CrosstabBlock()
    params = CrosstabParams(
        source_path="nonexistent.path",
        row_field="factor",
        col_field="disease",
        value_field="count",
    )
    ctx = _ctx(payload={})
    data = await block.collect(ctx, params)
    assert data["rows"] == []
    assert data["columns"] == []
    # Empty render — both formats stay valid.
    assert block.render_latex(data, params, ctx) == ""
    html = block.render_html(data, params, ctx)
    assert "<table" in html


# ---------------------------------------------------------------------------
# Registry — both blocks visible after import
# ---------------------------------------------------------------------------


def test_block_registry_includes_new_blocks() -> None:
    config_dir = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "config"
        / "reports"
        / "blocks"
    )
    registry = block_registry(config_dir=config_dir, reload=True)
    ids = registry.list_ids()
    assert "disease_district_grid" in ids
    assert "crosstab" in ids
