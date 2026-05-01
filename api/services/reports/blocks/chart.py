"""``chart`` block — embeds an ADR-01 chart into a report section.

Per ADR-03 §3, this block is the bridge between the chart-spec layer
(ADR-01) and the report-descriptor layer (ADR-03). The block does NOT
re-implement chart query SQL: it delegates to ``ChartService.render``
which knows how to look up a spec, run its query through MVRepository,
and apply k-anonymity. ``collect`` returns the resulting ``ChartResponse``
unchanged so renderers can plot it however they like.

Renderer surface:
    * ``render_html``  — inline SVG. Self-contained — no JS, no CDN.
      Uses ``pyecharts`` if available, otherwise a hand-rolled SVG of
      the simplest bar / line geometry that the data supports.
    * ``render_latex`` — TikZ ``\\begin{tikzpicture}…`` for ``bar`` /
      ``line`` charts. Other kinds fall back to a self-describing
      placeholder line — PDF charts beyond bar/line are tracked as a
      known follow-up.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.chart")


class _ChartParams(BaseModel):
    """Parameters for the ``chart`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    spec_id: str
    filters: Dict[str, Any] = Field(default_factory=dict)
    caption_th: Optional[str] = None
    caption_en: Optional[str] = None
    height: int = 320  # px for HTML, mapped to TikZ ``ymax`` for LaTeX


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _resolve_chart_service(ctx: RenderContext) -> Any:
    """Return a ChartService instance, preferring a context-injected one.

    The orchestrator may stash a pre-built service on
    ``ctx.extra["chart_service"]`` (cheap path: re-use the request-scoped
    service for k-anon caching). Otherwise we build one from the global
    registry + a fresh ``MVRepository``.
    """
    pre = ctx.extra.get("chart_service") if ctx.extra else None
    if pre is not None:
        return pre
    # Lazy imports keep ``services.reports.blocks.chart`` importable in
    # contexts where the chart layer isn't wired (unit tests for other
    # blocks, doc-build runs).
    from services.charts.registry import chart_registry
    from services.charts.service import ChartService
    from repositories.mv_repository import MVRepository

    return ChartService(chart_registry(), MVRepository())


def _response_to_dict(resp: Any) -> Dict[str, Any]:
    """Best-effort conversion of a ``ChartResponse`` into a plain dict."""
    if hasattr(resp, "model_dump"):
        return resp.model_dump()  # type: ignore[no-any-return]
    if isinstance(resp, dict):
        return resp
    # Defensive — should never hit if ChartService keeps its contract.
    return {"data": [], "kind": getattr(resp, "kind", "bar")}  # pragma: no cover


class ChartBlock(ContentBlock):
    """Embed an ADR-01 chart into a report section."""

    block_id: ClassVar[str] = "chart"
    Parameters: ClassVar[type[BaseModel]] = _ChartParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _ChartParams)
        service = _resolve_chart_service(ctx)
        # ChartService.render is async — that's why this whole ``collect``
        # contract is async (see blocks/base.py docstring).
        resp = await service.render(params.spec_id, params.filters)
        body = _response_to_dict(resp)
        # Caption: prefer english when lang=en + en-text supplied.
        caption: Optional[str]
        if ctx.lang == "en" and params.caption_en:
            caption = params.caption_en
        else:
            caption = params.caption_th
        return {
            "spec_id": params.spec_id,
            "kind": body.get("kind", "bar"),
            "rows": body.get("data", []),
            "meta": body.get("meta", {}),
            "caption": caption,
            "height": params.height,
        }

    # ------------------------------------------------------------------
    # HTML — inline SVG (no JS, no CDN)
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # First try pyecharts (richer output) and fall back to the
        # local SVG renderer if the package isn't installed.
        svg = self._try_pyecharts_svg(data)
        if svg is None:
            svg = self._fallback_svg(data)
        caption_html = ""
        if data.get("caption"):
            caption_html = (
                '<figcaption>' + _html_escape(str(data["caption"])) + "</figcaption>"
            )
        spec_id = _html_escape(str(data["spec_id"]))
        return (
            f'<figure class="chart" data-spec-id="{spec_id}">'
            + svg
            + caption_html
            + "</figure>"
        )

    def _try_pyecharts_svg(
        self, data: Dict[str, Any]
    ) -> Optional[str]:
        """Attempt to render via pyecharts; return ``None`` if unavailable.

        Pyecharts produces JS-driven HTML by default — we coax it into
        an SVG-only output to keep the report self-contained.
        """
        try:
            from pyecharts.charts import Bar, Line
            from pyecharts.commons.utils import JsCode
            from pyecharts.options import InitOpts
        except ImportError:
            return None
        kind = data.get("kind")
        rows: List[Dict[str, Any]] = data.get("rows", [])
        if kind not in ("bar", "line") or not rows:
            return None
        try:
            init = InitOpts(renderer="svg", height=f"{data['height']}px")
            chart = (Bar(init_opts=init) if kind == "bar" else Line(init_opts=init))
            xs = [str(r.get("x", "")) for r in rows]
            ys = [
                (r.get("n") if r.get("y") is None else r.get("y"))
                for r in rows
            ]
            chart.add_xaxis(xs)
            chart.add_yaxis(data.get("spec_id", "value"), ys)
            # Render to a string. ``render_embed`` returns HTML + the SVG
            # tag; for embedded reports we just want the SVG.
            html = chart.render_embed()
            # Crude extraction of the <svg> tag — pyecharts wraps the
            # SVG in a <div>; for our purposes either is fine, but we
            # prefer the raw tag.
            start = html.find("<svg")
            end = html.find("</svg>", start)
            if start != -1 and end != -1:
                return str(html[start : end + len("</svg>")])
            return str(html)  # caller still gets *something* embeddable
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("pyecharts render failed, falling back: %s", exc)
            return None

    def _fallback_svg(self, data: Dict[str, Any]) -> str:
        """Hand-rolled minimal SVG bar chart for the ``rows`` series.

        No grid lines, no legend — this is the "we have no pyecharts and
        still need a pixel" path. Looks crude but stays readable.
        """
        rows: List[Dict[str, Any]] = data.get("rows", [])
        height = int(data.get("height", 320))
        width = max(320, 60 * max(len(rows), 1))
        if not rows:
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{width}" height="{height}" role="img" '
                f'aria-label="empty chart"></svg>'
            )
        # Pull a numeric value off each row — prefer ``y``, fall back
        # to ``n`` (bar charts often plot the count itself).
        values: List[float] = []
        for r in rows:
            v = r.get("y")
            if v is None:
                v = r.get("n", 0)
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                values.append(0.0)
        max_v = max(values) if values else 1.0
        if max_v <= 0:
            max_v = 1.0
        bar_w = (width - 40) / len(values)
        bars: List[str] = []
        labels: List[str] = []
        for i, (row, v) in enumerate(zip(rows, values)):
            bh = (v / max_v) * (height - 40)
            x = 20 + i * bar_w
            y = height - 20 - bh
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" '
                f'width="{bar_w - 2:.1f}" height="{bh:.1f}" '
                f'fill="#00744B" />'
            )
            label = _html_escape(str(row.get("x", "")))
            labels.append(
                f'<text x="{x + bar_w/2:.1f}" y="{height - 5:.1f}" '
                f'font-size="10" text-anchor="middle">{label}</text>'
            )
        body = "".join(bars) + "".join(labels)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" role="img" '
            f'aria-label="chart for {_html_escape(str(data.get("spec_id","")))}">'
            + body
            + "</svg>"
        )

    # ------------------------------------------------------------------
    # LaTeX — TikZ for bar/line, placeholder otherwise
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        kind = data.get("kind")
        rows: List[Dict[str, Any]] = data.get("rows", [])
        spec_id = str(data.get("spec_id", "?"))
        caption = data.get("caption")
        if kind not in ("bar", "line") or not rows:
            # Self-describing placeholder — known follow-up tracks
            # turning these into real PDF charts.
            return (
                r"\textit{[Chart "
                + latex_escape(spec_id)
                + ": rendering not available in LaTeX]}\n"
            )
        # Build a TikZ picture. We use raw TikZ rectangles / line
        # segments rather than pgfplots so we don't take on a new package
        # dependency for this milestone.
        return self._render_tikz(rows, kind, spec_id, caption)

    def _render_tikz(
        self,
        rows: List[Dict[str, Any]],
        kind: str,
        spec_id: str,
        caption: Optional[str],
    ) -> str:
        # Extract numeric series.
        values: List[float] = []
        labels: List[str] = []
        for r in rows:
            v = r.get("y")
            if v is None:
                v = r.get("n", 0)
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                values.append(0.0)
            labels.append(str(r.get("x", "")))
        max_v = max(values) if values else 1.0
        if max_v <= 0:
            max_v = 1.0
        # TikZ uses cm; pin the picture to ~ 12cm wide × 4cm tall.
        bar_w = 12.0 / max(len(values), 1)
        scale_y = 4.0 / max_v
        body: List[str] = []
        if kind == "bar":
            for i, (lab, v) in enumerate(zip(labels, values)):
                x0 = i * bar_w
                x1 = x0 + bar_w * 0.8
                y1 = v * scale_y
                body.append(
                    f"\\fill[BMAGreen] ({x0:.2f},0) rectangle ({x1:.2f},{y1:.2f});"
                )
                body.append(
                    f"\\node[below, font=\\tiny] at ({(x0+x1)/2:.2f},0) "
                    f"{{{latex_escape(lab)}}};"
                )
        else:  # line
            pts: List[str] = []
            for i, v in enumerate(values):
                x = i * bar_w + bar_w / 2
                y = v * scale_y
                pts.append(f"({x:.2f},{y:.2f})")
            body.append("\\draw[thick, BMAGreen] " + " -- ".join(pts) + ";")
            for i, (lab, v) in enumerate(zip(labels, values)):
                x = i * bar_w + bar_w / 2
                body.append(
                    f"\\node[below, font=\\tiny] at ({x:.2f},0) "
                    f"{{{latex_escape(lab)}}};"
                )
        # Optional caption — wrap the whole picture in a centered figure
        # only when we have one to attach.
        prefix = (
            r"\definecolor{BMAGreen}{HTML}{00744B}" + "\n"
            r"\begin{center}" + "\n"
            r"\begin{tikzpicture}" + "\n"
        )
        suffix = "\n" + r"\end{tikzpicture}" + "\n"
        if caption:
            suffix += (
                r"\\\textit{" + latex_escape(str(caption)) + "}\n"
            )
        suffix += r"\end{center}" + "\n"
        return prefix + "\n".join(body) + suffix
