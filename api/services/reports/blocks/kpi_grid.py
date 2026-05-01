"""``kpi_grid`` block — a row / grid of headline-number KPI tiles.

Per ADR-03 §3 each KPI references a dotted-path lookup into
``ctx.data_collector.data()`` and a ``format`` choice. The block does
the resolution + formatting once in ``collect`` so both renderers reuse
the same string output.

Format vocabulary (kept tiny on purpose — same set as the dashboard
KPI cards):
    ``int``   — thousands-separated integer (e.g. ``12,345``)
    ``pct``   — percent with 1 decimal (e.g. ``12.5%``)
    ``pct2``  — percent with 2 decimals (e.g. ``12.51%``)
    ``ratio`` — bare float with 2 decimals (e.g. ``1.42``)
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext


KPIFormat = Literal["int", "pct", "pct2", "ratio"]


class KPISpec(BaseModel):
    """Single KPI tile descriptor."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    label_th: str
    label_en: Optional[str] = None
    source_path: str  # dotted path into data_collector.data()
    format: KPIFormat = "int"


class _KPIGridParams(BaseModel):
    """Parameters for the ``kpi_grid`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    metrics: List[KPISpec] = Field(default_factory=list)


def _resolve_dotted(data: Any, path: str) -> Any:
    """Walk a dotted ``path`` into ``data``; returns ``None`` if missing."""
    cur: Any = data
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif hasattr(cur, seg):
            cur = getattr(cur, seg)
        else:
            return None
    return cur


def _format_value(raw: Any, kind: KPIFormat) -> str:
    """Format ``raw`` per ``kind``. ``None``/``NaN`` becomes ``"—"``."""
    if raw is None:
        return "—"
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    # NaN check — float('nan') != float('nan') is the canonical idiom.
    if num != num:
        return "—"
    if kind == "int":
        return f"{int(round(num)):,}"
    if kind == "pct":
        return f"{num:.1f}%"
    if kind == "pct2":
        return f"{num:.2f}%"
    if kind == "ratio":
        return f"{num:.2f}"
    # Defensive default — should never hit because of Literal typing.
    return str(raw)  # type: ignore[unreachable]  # pragma: no cover


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class KPIGridBlock(ContentBlock):
    """A row of KPI tiles, one per metric in ``params.metrics``."""

    block_id: ClassVar[str] = "kpi_grid"
    Parameters: ClassVar[type[BaseModel]] = _KPIGridParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _KPIGridParams)
        getter = getattr(ctx.data_collector, "data", None)
        bag: Any = getter() if callable(getter) else (getter or {})
        tiles: List[Dict[str, Any]] = []
        for m in params.metrics:
            label = (
                m.label_en
                if (ctx.lang == "en" and m.label_en)
                else m.label_th
            )
            raw = _resolve_dotted(bag, m.source_path)
            tiles.append(
                {
                    "label": label,
                    "value": _format_value(raw, m.format),
                    "raw": raw,
                    "format": m.format,
                    "source_path": m.source_path,
                }
            )
        return {"tiles": tiles}

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        tiles: List[Dict[str, Any]] = data["tiles"]
        if not tiles:
            return ""
        # ``tabular`` with N centered columns — one tile per column. We
        # use ``\Large`` for the value row to mimic the dashboard tile
        # visual hierarchy without bringing in a custom LaTeX package.
        col_spec = "|" + "c|" * len(tiles)
        header = " & ".join(
            latex_escape(str(t["label"])) for t in tiles
        )
        values = " & ".join(
            r"\textbf{\Large " + latex_escape(str(t["value"])) + "}"
            for t in tiles
        )
        return (
            r"\begin{center}"
            r"\begin{tabular}{" + col_spec + "}\n"
            r"\hline" + "\n"
            + header + r" \\ \hline" + "\n"
            + values + r" \\ \hline" + "\n"
            + r"\end{tabular}"
            + r"\end{center}" + "\n"
        )

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        tiles: List[Dict[str, Any]] = data["tiles"]
        cards: List[str] = []
        for t in tiles:
            label = _html_escape(str(t["label"]))
            value = _html_escape(str(t["value"]))
            cards.append(
                f'<div class="kpi-tile">'
                f'<div class="kpi-value">{value}</div>'
                f'<div class="kpi-label">{label}</div>'
                f"</div>"
            )
        return '<div class="kpi-grid">' + "".join(cards) + "</div>"
