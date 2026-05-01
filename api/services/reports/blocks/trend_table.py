r"""``trend_table`` block — a Mann-Kendall trend results table.

Per ADR-03 §3 this ports the legacy whitepaper section 6
(``\trendUp{}`` / ``\trendDown{}`` / ``\trendStable{}`` arrows over
trend rows) into a descriptor-driven block. Rows come from
``ctx.data_collector.data()[source_path]`` — typically a list of dicts
shaped like ``{metric, value, direction, change_pct, p_value}``.

Clinical default: an *increasing* chronic-disease metric is bad
(red ↑), a *decreasing* one is good (green ↓). The arrow column makes
the trend direction scannable at a glance.

LaTeX colour notes — the preamble (``bma_article_preamble.tex`` line
17) loads ``\usepackage{xcolor}`` WITHOUT the ``dvipsnames`` option, so
``ForestGreen`` is NOT available. We use the brand colour
``okgreen`` (line 43 of preamble) which the existing ``\trendDown``
macro also uses — same red / green / gray semantics, no extra
dependency required.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.trend_table")


class _TrendTableParams(BaseModel):
    """Parameters for the ``trend_table`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    source_path: str = "trends"
    metric_filter: Optional[str] = None
    max_rows: int = 30


# Direction → (LaTeX cell expression, HTML span). Matches the legacy
# preamble macros' colour choices: red for "up" (chronic-disease metric
# increasing is bad), okgreen for "down" (improving), gray for "stable".
_LATEX_DIR: Dict[str, str] = {
    "up": r"\textcolor{errred}{$\uparrow$}",
    "down": r"\textcolor{okgreen}{$\downarrow$}",
    "stable": r"\textcolor{gray}{$\rightarrow$}",
}

_HTML_DIR: Dict[str, str] = {
    "up": '<span class="trend-up" style="color: #D32F2F;">&#8593;</span>',
    "down": '<span class="trend-down" style="color: #388E3C;">&#8595;</span>',
    "stable": '<span class="trend-stable" style="color: #9E9E9E;">&#8594;</span>',
}


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_change_pct(raw: Any) -> str:
    """Format a change_pct value as ``+12.3%`` / ``-4.1%`` / ``—``."""
    if raw is None:
        return "—"
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if num != num:  # NaN
        return "—"
    sign = "+" if num >= 0 else ""
    return f"{sign}{num:.1f}%"


def _resolve_dotted(data: Any, path: str) -> Any:
    """Walk a dotted ``path`` into ``data``; ``None`` if any segment misses."""
    cur: Any = data
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif hasattr(cur, seg):
            cur = getattr(cur, seg)
        else:
            return None
    return cur


class TrendTableBlock(ContentBlock):
    """Mann-Kendall trend table rendered with directional arrows."""

    block_id: ClassVar[str] = "trend_table"
    Parameters: ClassVar[type[BaseModel]] = _TrendTableParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _TrendTableParams)
        getter = getattr(ctx.data_collector, "data", None)
        bag: Any = getter() if callable(getter) else (getter or {})
        raw = _resolve_dotted(bag, params.source_path)
        # Graceful empty-list fallback: the path may not be populated for
        # reports that don't carry trend data (e.g. zone-only reports).
        rows: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            for r in raw:
                if isinstance(r, dict):
                    rows.append(dict(r))
        if params.metric_filter:
            rows = [
                r for r in rows
                if r.get("metric") == params.metric_filter
            ]
        truncated = len(rows) > params.max_rows
        rows = rows[: params.max_rows]
        return {
            "rows": rows,
            "n_rows": len(rows),
            "truncated": truncated,
            "source_path": params.source_path,
        }

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        rows: List[Dict[str, Any]] = data["rows"]
        # Column spec: metric | value | change | direction (4 columns).
        col_spec = "|l|r|r|c|"
        header = (
            r"\textbf{Metric} & \textbf{Value} & "
            r"\textbf{Change} & \textbf{Trend}"
        )
        body_lines: List[str] = []
        for r in rows:
            metric = latex_escape(str(r.get("metric", "")))
            value = latex_escape(str(r.get("value", "")))
            change = latex_escape(
                _format_change_pct(r.get("change_pct"))
            )
            direction = _LATEX_DIR.get(
                str(r.get("direction", "")),
                r"\textcolor{gray}{$\rightarrow$}",
            )
            body_lines.append(
                f"{metric} & {value} & {change} & {direction} "
                + r"\\ \hline"
            )
        body = "\n".join(body_lines)
        out = (
            r"\begin{tabular}{" + col_spec + "}\n"
            r"\hline" + "\n"
            + header + r" \\ \hline" + "\n"
            + (body + "\n" if body else "")
            + r"\end{tabular}" + "\n"
        )
        if data.get("truncated"):
            out += (
                r"\textit{(แสดง "
                + str(data["n_rows"])
                + r" แถวแรกจากผลลัพธ์ทั้งหมด)}" + "\n"
            )
        return out

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        rows: List[Dict[str, Any]] = data["rows"]
        thead = (
            "<thead><tr>"
            "<th>Metric</th><th>Value</th>"
            "<th>Change</th><th>Trend</th>"
            "</tr></thead>"
        )
        body_rows: List[str] = []
        for r in rows:
            metric = _html_escape(str(r.get("metric", "")))
            value = _html_escape(str(r.get("value", "")))
            change = _html_escape(_format_change_pct(r.get("change_pct")))
            arrow = _HTML_DIR.get(
                str(r.get("direction", "")),
                _HTML_DIR["stable"],
            )
            body_rows.append(
                f"<tr><td>{metric}</td><td>{value}</td>"
                f"<td>{change}</td><td>{arrow}</td></tr>"
            )
        tbody = "<tbody>" + "".join(body_rows) + "</tbody>"
        note = ""
        if data.get("truncated"):
            note = (
                f'<p class="table-note">Showing first '
                f'{data["n_rows"]} rows.</p>'
            )
        return f'<table class="trend-table">{thead}{tbody}</table>{note}'
