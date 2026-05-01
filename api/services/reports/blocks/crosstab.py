"""``crosstab`` block — factor × disease cross-tabulation table.

Per ADR-03 §3, this block ports Section 3 of the legacy whitepaper
template (``api/templates/latex/report_whitepaper.tex.j2`` lines 158-190):
a 2-D pivot of long-format rows ``[{factor: …, disease: …, count: …},
…]`` into a wide ``factor × disease`` table whose column count is
discovered from the data, NOT hard-coded.

Unlike ``disease_district_grid`` (which hits the MV repository), this
block reads from ``ctx.data_collector.data()`` via a dotted
``source_path`` — the underlying long rows are precomputed by
``ReportDataCollector``. That keeps SQL out of this block and reuses the
same data pipeline used by every other ADR-03 §7 block.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

from services.reports.blocks.base import ContentBlock
from services.reports.renderers._filters import number_format
from services.reports.renderers._latex_filters import latex_safe
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.crosstab")


CellFormat = Literal["int", "pct", "pct2"]


class CrosstabParams(BaseModel):
    """Parameters for the ``crosstab`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    source_path: str  # dotted path into ``data_collector.data()``
    row_field: str  # key on each row that supplies the row label
    col_field: str  # key that supplies the column label
    value_field: str  # key that supplies the cell value
    row_label_th: Optional[str] = None
    col_label_th: Optional[str] = None
    cell_format: CellFormat = "int"
    include_total: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _resolve_dotted(data: Any, path: str) -> Any:
    """Walk a dotted ``path`` into ``data``; returns ``None`` if missing."""
    if not path:
        return data
    cur: Any = data
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif hasattr(cur, seg):
            cur = getattr(cur, seg)
        else:
            return None
    return cur


def _is_numeric(v: Any) -> bool:
    """Loose numeric check that accepts int / float / numeric str."""
    if v is None:
        return False
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _coerce_num(v: Any) -> float:
    """Coerce to float, returning 0.0 for non-numeric values."""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _format_cell(raw: Any, kind: CellFormat) -> str:
    """Format a cell value per ``cell_format``.

    ``None`` is rendered as ``"—"`` for both pct and pct2 (we do not
    pretend a missing percentage is zero). For ``int``, ``None`` becomes
    ``"0"`` because the empty pivot cell IS literally a count of zero.
    """
    if raw is None:
        return "0" if kind == "int" else "—"
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if num != num:  # NaN
        return "—"
    if kind == "int":
        return f"{int(round(num)):,}"
    if kind == "pct":
        return f"{num:.1f}%"
    if kind == "pct2":
        return f"{num:.2f}%"
    return str(raw)  # type: ignore[unreachable]  # Literal validates kind


# Suppress unused import note — exposed for forward compatibility.
_ = number_format


# ---------------------------------------------------------------------------
# Pivot
# ---------------------------------------------------------------------------


def _pivot(
    rows: List[Dict[str, Any]],
    row_field: str,
    col_field: str,
    value_field: str,
    *,
    include_total: bool,
    cell_format: CellFormat,
) -> Dict[str, Any]:
    """Pivot long-format ``rows`` into a wide dict-of-dicts.

    Returns a dict with keys ``rows`` (ordered list of row labels),
    ``columns`` (ordered list of column labels), ``cells``
    (``{row: {col: value}}``), ``totals_row`` (per-row totals; only
    populated for ``int``-format pivots), ``totals_col`` (per-column
    totals), and ``grand_total``.
    """
    cells: Dict[str, Dict[str, Any]] = {}
    row_order: List[str] = []
    col_order: List[str] = []
    seen_rows: set[str] = set()
    seen_cols: set[str] = set()

    for r in rows:
        if not isinstance(r, dict):
            continue  # type: ignore[unreachable]  # defensive against malformed data
        rv = r.get(row_field)
        cv = r.get(col_field)
        if rv is None or cv is None:
            continue
        rk = str(rv)
        ck = str(cv)
        if rk not in seen_rows:
            seen_rows.add(rk)
            row_order.append(rk)
        if ck not in seen_cols:
            seen_cols.add(ck)
            col_order.append(ck)
        # Last-write-wins on duplicate (row, col) pairs — collisions in
        # this dataset are a precomputation bug upstream, not something
        # the block should silently sum.
        cells.setdefault(rk, {})[ck] = r.get(value_field)

    # Sort columns alphabetically for stable rendering. Rows preserve
    # input order (callers often hand-sort their factor levels).
    col_order = sorted(col_order)

    # Totals only make sense for numeric (int / pct / pct2) data. For
    # ``pct`` totals we sum the percentages — interpretation is
    # deliberately left to the caller. ``int`` is the default cell type
    # for crosstabs of counts so this is the common path.
    totals_row: Dict[str, Optional[float]] = {}
    totals_col: Dict[str, float] = {col: 0.0 for col in col_order}
    grand_total: float = 0.0

    if include_total:
        for rk in row_order:
            running = 0.0
            any_numeric = False
            for col in col_order:
                v = cells.get(rk, {}).get(col)
                if _is_numeric(v):
                    n = _coerce_num(v)
                    running += n
                    totals_col[col] += n
                    grand_total += n
                    any_numeric = True
            totals_row[rk] = running if any_numeric else None

    # Decide the "fill value" for missing cells. For ``int``: 0 (a missing
    # count IS zero). For ``pct``/``pct2``: ``None`` (a missing percentage
    # is not the same as 0%).
    fill_missing: Any = 0 if cell_format == "int" else None
    for rk in row_order:
        rdict = cells.setdefault(rk, {})
        for col in col_order:
            if col not in rdict:
                rdict[col] = fill_missing

    return {
        "rows": row_order,
        "columns": col_order,
        "cells": cells,
        "totals_row": totals_row,
        "totals_col": totals_col if include_total else {},
        "grand_total": grand_total if include_total else 0.0,
        "include_total": include_total,
        "cell_format": cell_format,
    }


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class CrosstabBlock(ContentBlock):
    """Factor × disease cross-tab — replaces Section 3 of the whitepaper."""

    block_id: ClassVar[str] = "crosstab"
    Parameters: ClassVar[type[BaseModel]] = CrosstabParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, CrosstabParams)
        getter = getattr(ctx.data_collector, "data", None)
        bag: Any = getter() if callable(getter) else (getter or {})
        long_rows = _resolve_dotted(bag, params.source_path)
        if long_rows is None:
            long_rows = []
        if not isinstance(long_rows, list):
            logger.warning(
                "crosstab.source_path %r resolved to non-list (%s); "
                "treating as empty",
                params.source_path,
                type(long_rows).__name__,
            )
            long_rows = []
        pivot = _pivot(
            long_rows,
            params.row_field,
            params.col_field,
            params.value_field,
            include_total=params.include_total,
            cell_format=params.cell_format,
        )
        # Stash the human labels so renderers don't reach back into params.
        pivot["row_label_th"] = params.row_label_th or params.row_field
        pivot["col_label_th"] = params.col_label_th or params.col_field
        return pivot

    # ------------------------------------------------------------------
    # LaTeX — ``\\begin{tabular}{l|*{n}{c}|c}`` with N+2 columns where
    # N is the discovered column count (+1 row label, +1 total).
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        rows: List[str] = data.get("rows", [])
        columns: List[str] = data.get("columns", [])
        cells: Dict[str, Dict[str, Any]] = data.get("cells", {})
        cell_format: CellFormat = data.get("cell_format", "int")
        include_total: bool = data.get("include_total", False)
        row_label = data.get("row_label_th") or "ปัจจัย"
        if not columns:
            return ""
        n = len(columns)
        if include_total:
            col_spec = "l|" + "*{" + str(n) + "}{c}|c"
        else:
            col_spec = "l|" + "*{" + str(n) + "}{c}"
        header_cells = [r"\textbf{" + latex_safe(row_label) + "}"]
        header_cells.extend(
            r"\textbf{" + latex_safe(col) + "}" for col in columns
        )
        if include_total:
            header_cells.append(r"\textbf{รวม}")
        out: List[str] = []
        out.append(r"\begin{tabular}{" + col_spec + "}")
        out.append(r"\toprule")
        out.append(" & ".join(header_cells) + r" \\")
        out.append(r"\midrule")
        for rk in rows:
            cells_for_row = cells.get(rk, {})
            row_cells = [latex_safe(rk)]
            for col in columns:
                row_cells.append(
                    _format_cell(cells_for_row.get(col), cell_format)
                )
            if include_total:
                tr = data.get("totals_row", {}).get(rk)
                row_cells.append(_format_cell(tr, cell_format))
            out.append(" & ".join(row_cells) + r" \\")
        if include_total:
            out.append(r"\midrule")
            totals_col: Dict[str, float] = data.get("totals_col", {})
            footer_cells = [r"\textbf{รวม}"]
            for col in columns:
                footer_cells.append(
                    r"\textbf{"
                    + _format_cell(totals_col.get(col, 0.0), cell_format)
                    + "}"
                )
            footer_cells.append(
                r"\textbf{"
                + _format_cell(data.get("grand_total", 0.0), cell_format)
                + "}"
            )
            out.append(" & ".join(footer_cells) + r" \\")
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------
    # HTML — ``<table class="crosstab">`` with the same total-row treatment.
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        rows: List[str] = data.get("rows", [])
        columns: List[str] = data.get("columns", [])
        cells: Dict[str, Dict[str, Any]] = data.get("cells", {})
        cell_format: CellFormat = data.get("cell_format", "int")
        include_total: bool = data.get("include_total", False)
        row_label = data.get("row_label_th") or "ปัจจัย"
        if not columns:
            return '<table class="crosstab"></table>'
        head_cells = [f"<th>{_html_escape(str(row_label))}</th>"]
        head_cells.extend(
            f"<th>{_html_escape(str(col))}</th>" for col in columns
        )
        if include_total:
            head_cells.append("<th>รวม</th>")
        thead = "<thead><tr>" + "".join(head_cells) + "</tr></thead>"
        body_rows: List[str] = []
        for rk in rows:
            cells_for_row = cells.get(rk, {})
            row_html = [f"<th>{_html_escape(rk)}</th>"]
            for col in columns:
                row_html.append(
                    "<td>"
                    + _html_escape(_format_cell(cells_for_row.get(col), cell_format))
                    + "</td>"
                )
            if include_total:
                tr = data.get("totals_row", {}).get(rk)
                row_html.append(
                    "<td>"
                    + _html_escape(_format_cell(tr, cell_format))
                    + "</td>"
                )
            body_rows.append("<tr>" + "".join(row_html) + "</tr>")
        tbody = "<tbody>" + "".join(body_rows) + "</tbody>"
        tfoot = ""
        if include_total:
            totals_col: Dict[str, float] = data.get("totals_col", {})
            foot_cells = ["<th>รวม</th>"]
            for col in columns:
                foot_cells.append(
                    "<td><strong>"
                    + _html_escape(
                        _format_cell(totals_col.get(col, 0.0), cell_format)
                    )
                    + "</strong></td>"
                )
            foot_cells.append(
                "<td><strong>"
                + _html_escape(
                    _format_cell(data.get("grand_total", 0.0), cell_format)
                )
                + "</strong></td>"
            )
            tfoot = "<tfoot><tr>" + "".join(foot_cells) + "</tr></tfoot>"
        return (
            '<table class="crosstab">' + thead + tbody + tfoot + "</table>"
        )
