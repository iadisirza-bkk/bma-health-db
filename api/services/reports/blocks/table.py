"""``table`` block — long-format data table from an MV query.

Per ADR-03 §3, the block calls ``MVRepository.run_query`` to get rows
keyed by ``query_id``, projects the requested columns, applies a row
ceiling (``max_rows``), and emits semantic LaTeX / HTML markup. The
block does NOT re-implement chart query SQL — it goes through the
repository, same as any other ADR-01 surface.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.table")


class ColSpec(BaseModel):
    """One column descriptor for the ``table`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    key: str  # row dict key
    header_th: str
    header_en: Optional[str] = None
    format: str = "str"  # one of: str | int | pct | pct2 | ratio


class _TableParams(BaseModel):
    """Parameters for the ``table`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    query_id: str
    columns: List[ColSpec]
    filters: Dict[str, Any] = Field(default_factory=dict)
    max_rows: int = 200


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_cell(raw: Any, kind: str) -> str:
    """Format a raw cell value per its column ``kind``."""
    if raw is None:
        return "—"
    if kind == "str":
        return str(raw)
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
    if kind == "ratio":
        return f"{num:.2f}"
    return str(raw)


def _resolve_repo(ctx: RenderContext) -> Any:
    """Return an object exposing ``async run_query(query_id, params)``.

    Honors a context-injected mock first (tests), then the global
    ``MVRepository``.
    """
    pre = ctx.extra.get("mv_repository") if ctx.extra else None
    if pre is not None:
        return pre
    from repositories.mv_repository import MVRepository
    return MVRepository()


class TableBlock(ContentBlock):
    """A long-format data table backed by an MV query."""

    block_id: ClassVar[str] = "table"
    Parameters: ClassVar[type[BaseModel]] = _TableParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _TableParams)
        repo = _resolve_repo(ctx)
        raw_rows = await repo.run_query(params.query_id, params.filters)
        # Normalize rows to plain dicts — MVRepository returns Pydantic
        # row models; tests / mocks may return dicts directly.
        rows: List[Dict[str, Any]] = []
        for r in raw_rows:
            if hasattr(r, "model_dump"):
                rows.append(r.model_dump())
            elif isinstance(r, dict):
                rows.append(dict(r))
            else:
                # Last-resort coercion via vars(); should not normally
                # happen if the repo contract is honoured.
                rows.append(dict(vars(r)))  # pragma: no cover
        truncated = len(rows) > params.max_rows
        rows = rows[: params.max_rows]
        # Pre-compute header labels per ctx.lang so render_* can stay dumb.
        headers: List[str] = []
        for col in params.columns:
            if ctx.lang == "en" and col.header_en:
                headers.append(col.header_en)
            else:
                headers.append(col.header_th)
        # Project each row into the requested columns + format the value.
        projected: List[List[str]] = []
        for r in rows:
            projected.append(
                [
                    _format_cell(r.get(col.key), col.format)
                    for col in params.columns
                ]
            )
        return {
            "headers": headers,
            "rows": projected,
            "n_rows": len(rows),
            "truncated": truncated,
            "query_id": params.query_id,
        }

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        headers: List[str] = data["headers"]
        rows: List[List[str]] = data["rows"]
        if not headers:
            return ""
        col_spec = "|" + "l|" * len(headers)
        head_line = " & ".join(latex_escape(h) for h in headers)
        body_lines = "\n".join(
            " & ".join(latex_escape(c) for c in r) + r" \\ \hline"
            for r in rows
        )
        out = (
            r"\begin{tabular}{" + col_spec + "}\n"
            r"\hline" + "\n"
            + head_line + r" \\ \hline" + "\n"
            + body_lines + "\n"
            + r"\end{tabular}" + "\n"
        )
        if data.get("truncated"):
            out += (
                r"\textit{(แสดง "
                + str(data["n_rows"])
                + r" แถวแรกจากผลลัพธ์ทั้งหมด)}\n"
            )
        return out

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        headers: List[str] = data["headers"]
        rows: List[List[str]] = data["rows"]
        thead = (
            "<thead><tr>"
            + "".join(f"<th>{_html_escape(h)}</th>" for h in headers)
            + "</tr></thead>"
        )
        body_rows: List[str] = []
        for r in rows:
            cells = "".join(f"<td>{_html_escape(c)}</td>" for c in r)
            body_rows.append(f"<tr>{cells}</tr>")
        tbody = "<tbody>" + "".join(body_rows) + "</tbody>"
        note = ""
        if data.get("truncated"):
            note = (
                f'<p class="table-note">Showing first '
                f'{data["n_rows"]} rows.</p>'
            )
        return f'<table class="data-table">{thead}{tbody}</table>{note}'
