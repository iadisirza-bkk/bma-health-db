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
    * ``render_latex`` — pgfplots TikZ for ``bar`` / ``line`` /
      ``stacked_bar`` / ``pyramid``; matplotlib PNG fallback (saved to
      a process-local temp dir) for kinds pgfplots can't express
      cleanly (``heatmap``, ``scatter``, ``boxplot``, ``choropleth``)
      or when ``REPORT_CHART_BACKEND=matplotlib`` is set.
      Output is wrapped in ``\\begin{figure}[H]…\\caption{…}\\end{figure}``
      so floats stay near their referencing prose.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.blocks._chart_matplotlib import (
    is_matplotlib_available,
    is_matplotlib_forced,
    render_to_png,
)
from services.reports.blocks._render_helpers import (
    safe_label_part,
    wrap_figure_html,
    wrap_figure_latex,
)
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.chart")


# Chart kinds that pgfplots handles natively in this block.
_PGFPLOTS_KINDS = frozenset({"bar", "line", "stacked_bar", "pyramid", "donut"})

# Chart kinds where matplotlib is the primary backend.
_MATPLOTLIB_KINDS = frozenset({"heatmap", "scatter", "boxplot", "choropleth"})

# Friendly Thai labels for known chart spec_ids — used as a caption fallback
# when the descriptor doesn't supply ``caption_th``. Keeps PDFs readable
# even before every descriptor YAML has been hand-curated. Add an entry
# here when introducing a new spec; missing entries fall back to the raw
# spec_id (matches pre-S10 behavior, just less ugly).
SPEC_ID_LABELS_TH: Dict[str, str] = {
    # Chart specs in config/charts/ (real registered specs):
    "age_pyramid": "ปิรามิดประชากรตามช่วงอายุและเพศ",
    "behavior_disease": "การกระจายพฤติกรรมเสี่ยง",
    "disease_lab_crosstab": "ค่า Lab × โรค (Cross-tab)",
    "repeat_screening": "การคัดกรองซ้ำ (จำนวนครั้ง)",
    "risk_factor_profile": "โปรไฟล์ปัจจัยเสี่ยง NCD",
    "screening_coverage": "อัตราการคัดกรองตามเขต",
    "zone_dm_prevalence": "ความชุกเบาหวาน รายเขตสุขภาพ",
    "zone_hpt_prevalence": "ความชุกความดันโลหิตสูง รายเขตสุขภาพ",
    # MV-backed views (for spec_ids not yet defined as chart specs but
    # referenced via data_collector — kept here for forward-compat):
    "disease_overview": "ภาพรวมโรคเรื้อรัง",
    "disease_age_sex": "อัตราเสี่ยงโรค จำแนกตามอายุ × เพศ",
    "lab_distribution": "การกระจายค่าผลตรวจทางห้องปฏิบัติการ",
    "mental_health": "ดัชนีสุขภาพจิต (PHQ-9 / ST-5)",
    "lifestyle": "พฤติกรรมและวิถีชีวิต (สูบบุหรี่/ออกกำลังกาย)",
    "summary_districts": "สรุปรายเขต — กรุงเทพมหานคร 50 เขต",
    "summary_zones": "สรุปรายเขตสุขภาพ (8 เขต)",
    "summary_global": "สรุปภาพรวมเมือง",
    "kpi_tier1": "ตัวชี้วัดหลัก (Tier 1)",
    "ncd_diagnostic_report": "รายงานการวินิจฉัย NCD",
    "ncd_diagnostic_zone": "รายงานการวินิจฉัย NCD รายเขต",
}

SPEC_ID_LABELS_EN: Dict[str, str] = {
    "age_pyramid": "Population age pyramid by sex",
    "behavior_disease": "Risk behavior distribution",
    "disease_lab_crosstab": "Lab markers × disease cross-tab",
    "repeat_screening": "Repeat screening visit frequency",
    "risk_factor_profile": "NCD risk-factor profile",
    "screening_coverage": "Screening coverage by district",
    "zone_dm_prevalence": "Diabetes prevalence by health zone",
    "zone_hpt_prevalence": "Hypertension prevalence by health zone",
    "disease_overview": "Chronic disease overview",
    "disease_age_sex": "Disease risk by age × sex",
    "lab_distribution": "Lab result distribution",
    "mental_health": "Mental health indicators (PHQ-9 / ST-5)",
    "lifestyle": "Lifestyle behaviors (smoking / exercise)",
    "summary_districts": "District-level summary — 50 BMA districts",
    "summary_zones": "Health-zone summary (8 zones)",
    "summary_global": "City-wide summary",
    "kpi_tier1": "Tier-1 key performance indicators",
    "ncd_diagnostic_report": "NCD diagnostic report",
    "ncd_diagnostic_zone": "NCD diagnostic report by zone",
}


def _friendly_caption(spec_id: str, lang: str) -> str:
    """Return a human-readable label for a spec_id, or the raw id as
    last-resort fallback. Used when the descriptor doesn't supply a
    ``caption_th`` / ``caption_en`` for a chart section."""
    table = SPEC_ID_LABELS_EN if lang == "en" else SPEC_ID_LABELS_TH
    return table.get(spec_id, spec_id)


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


def _extract_xy(rows: List[Dict[str, Any]]) -> Tuple[List[str], List[float]]:
    """Pull (label, numeric value) lists out of a ChartDataRow list."""
    labels: List[str] = []
    values: List[float] = []
    for r in rows:
        v = r.get("y")
        if v is None:
            v = r.get("n", 0)
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            values.append(0.0)
        labels.append(str(r.get("x", "")))
    return labels, values


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
        # Caption: prefer descriptor-supplied wording, but fall back to
        # the friendly label table (SPEC_ID_LABELS_*) so reports never
        # show "Figure 1: age_pyramid" — the raw id is internal jargon.
        caption: str
        if ctx.lang == "en":
            caption = params.caption_en or _friendly_caption(params.spec_id, "en")
        else:
            caption = params.caption_th or _friendly_caption(params.spec_id, "th")
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
        return wrap_figure_html(
            svg,
            str(data.get("caption") or ""),
            css_class="chart",
            extra_attrs={"data-spec-id": str(data.get("spec_id", ""))},
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
    # LaTeX — pgfplots TikZ (primary) / matplotlib PNG (fallback)
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        kind = str(data.get("kind") or "bar")
        rows: List[Dict[str, Any]] = data.get("rows", [])
        spec_id = str(data.get("spec_id", "?"))
        caption = data.get("caption") or spec_id

        label = "chart:" + safe_label_part(spec_id)

        # 1) Empty data → friendly message inside a figure (so the
        # surrounding prose still flows). Apply BEFORE kind dispatch so
        # blank materialised views never produce garbled axes.
        if not rows:
            return wrap_figure_latex(
                r"\textit{ไม่มีข้อมูล}",
                caption,
                label,
            )

        # 2) Forced matplotlib backend (env var) or kinds pgfplots
        # can't express cleanly → matplotlib PNG fallback.
        force_mpl = is_matplotlib_forced()
        if (
            (kind in _MATPLOTLIB_KINDS or force_mpl)
            and is_matplotlib_available()
        ):
            try:
                png_path = render_to_png(kind, rows, spec_id, caption=None)
                body = (
                    r"\includegraphics[width=0.85\textwidth]{"
                    + str(png_path)
                    + "}"
                )
                return wrap_figure_latex(body, caption, label)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "matplotlib chart render failed for %s: %s — falling back",
                    spec_id, exc,
                )

        # 3) Kinds we DON'T support natively in pgfplots and matplotlib
        # was unavailable → keep the legacy placeholder text. This path
        # only hits in degraded environments (no matplotlib installed)
        # and keeps the historical contract for callers that test it.
        if kind not in _PGFPLOTS_KINDS:
            return (
                r"\textit{[Chart "
                + latex_escape(spec_id)
                + ": rendering not available in LaTeX]}\n"
            )

        # 4) pgfplots primary path.
        if kind == "bar":
            inner = self._tikz_bar(rows, spec_id)
        elif kind == "line":
            inner = self._tikz_line(rows, spec_id)
        elif kind == "stacked_bar":
            inner = self._tikz_stacked_bar(rows, spec_id)
        elif kind == "pyramid":
            inner = self._tikz_pyramid(rows, spec_id)
        elif kind == "donut":
            inner = self._tikz_donut(rows, spec_id)
        else:  # pragma: no cover — guarded above
            inner = self._tikz_bar(rows, spec_id)

        return wrap_figure_latex(inner, caption, label)

    # ------------------------------------------------------------------
    # pgfplots renderers — one method per chart kind
    # ------------------------------------------------------------------

    @staticmethod
    def _axis_yfmt(values: List[float]) -> str:
        """Return a pgfplots ``yticklabel style`` line for thousands sep.

        Returns a fully-formed axis-option line (with leading "  " indent
        and trailing "\\n") OR an empty string. The trailing "\\n" matters:
        pgfplots cannot tolerate a whitespace-only line inside the
        ``[ ... ]`` axis options, so when there's no thousands separator
        to emit we MUST emit nothing at all (caller does ``output += yfmt``
        rather than ``f"  {yfmt}\\n"``).
        """
        if values and max(values) >= 1000:
            return (
                "  yticklabel style={/pgf/number format/.cd, "
                "1000 sep={,}, fixed, precision=0},\n"
            )
        return ""

    @staticmethod
    def _label_rotation(labels: List[str]) -> int:
        """45° if any label is over 8 chars, else 0."""
        return 45 if any(len(l) > 8 for l in labels) else 0

    def _tikz_bar(
        self, rows: List[Dict[str, Any]], spec_id: str
    ) -> str:
        labels, values = _extract_xy(rows)
        rot = self._label_rotation(labels)
        yfmt = self._axis_yfmt(values)
        # Symbolic x coords let pgfplots render arbitrary string labels;
        # standard numeric coords force us to invent indices.
        sym_list = ", ".join(
            "{" + latex_escape(l) + "}" for l in labels
        )
        coords = " ".join(
            f"({{{latex_escape(l)}}},{v:g})" for l, v in zip(labels, values)
        )
        return (
            "\\begin{tikzpicture}\n"
            "\\begin{axis}[\n"
            "  ybar,\n"
            "  width=0.85\\textwidth, height=6cm,\n"
            "  bar width=10pt,\n"
            "  enlarge x limits=0.08,\n"
            "  ymin=0,\n"
            f"  symbolic x coords={{{sym_list}}},\n"
            "  xtick=data,\n"
            f"  xticklabel style={{rotate={rot},anchor={'east' if rot else 'center'},font=\\small}},\n"
            + yfmt
            + "  ymajorgrids, grid style={dashed,gray!30},\n"
            "  axis lines=left,\n"
            "]\n"
            f"\\addplot[fill=bmagreen, draw=bmagreen] coordinates {{{coords}}};\n"
            "\\end{axis}\n"
            "\\end{tikzpicture}"
        )

    def _tikz_line(
        self, rows: List[Dict[str, Any]], spec_id: str
    ) -> str:
        labels, values = _extract_xy(rows)
        rot = self._label_rotation(labels)
        yfmt = self._axis_yfmt(values)
        sym_list = ", ".join(
            "{" + latex_escape(l) + "}" for l in labels
        )
        coords = " ".join(
            f"({{{latex_escape(l)}}},{v:g})" for l, v in zip(labels, values)
        )
        return (
            "\\begin{tikzpicture}\n"
            "\\begin{axis}[\n"
            "  width=0.85\\textwidth, height=6cm,\n"
            "  enlarge x limits=0.08,\n"
            f"  symbolic x coords={{{sym_list}}},\n"
            "  xtick=data,\n"
            f"  xticklabel style={{rotate={rot},anchor={'east' if rot else 'center'},font=\\small}},\n"
            + yfmt
            + "  ymajorgrids, grid style={dashed,gray!30},\n"
            "  axis lines=left,\n"
            "]\n"
            f"\\addplot[mark=*, color=bmagreen, thick] coordinates {{{coords}}};\n"
            "\\end{axis}\n"
            "\\end{tikzpicture}"
        )

    def _tikz_stacked_bar(
        self, rows: List[Dict[str, Any]], spec_id: str
    ) -> str:
        # Group rows by series. Falls back to a plain bar chart if no
        # series is present — same keys, just one stack.
        series_keys: List[str] = []
        x_keys: List[str] = []
        for r in rows:
            sk = r.get("series")
            sk_str = "_default" if sk is None else str(sk)
            if sk_str not in series_keys:
                series_keys.append(sk_str)
            xs = str(r.get("x", ""))
            if xs not in x_keys:
                x_keys.append(xs)
        # Pivot into {(series, x) → value}
        cell: Dict[Tuple[str, str], float] = {}
        for r in rows:
            sk = r.get("series")
            sk_str = "_default" if sk is None else str(sk)
            xs = str(r.get("x", ""))
            v = r.get("y")
            if v is None:
                v = r.get("n", 0)
            try:
                cell[(sk_str, xs)] = float(v)
            except (TypeError, ValueError):
                cell[(sk_str, xs)] = 0.0
        max_total = max(
            (sum(cell.get((sk, xs), 0.0) for sk in series_keys) for xs in x_keys),
            default=0.0,
        )
        rot = self._label_rotation(x_keys)
        yfmt = self._axis_yfmt([max_total])
        sym_list = ", ".join("{" + latex_escape(x) + "}" for x in x_keys)
        # Cycle through a small palette so series are visually distinct.
        palette = ["bmagreen", "bmagreenlight", "warnamber", "warnblue", "errred"]
        plots: List[str] = []
        for i, sk in enumerate(series_keys):
            color = palette[i % len(palette)]
            coords = " ".join(
                f"({{{latex_escape(xs)}}},{cell.get((sk, xs), 0.0):g})"
                for xs in x_keys
            )
            label = "" if sk == "_default" else latex_escape(sk)
            plots.append(
                f"\\addplot[fill={color}, draw={color}] coordinates {{{coords}}};"
                + (f"\n\\addlegendentry{{{label}}}" if label else "")
            )
        legend = (
            "  legend style={font=\\small, at={(0.5,-0.30)}, anchor=north, "
            "legend columns=-1},\n"
            if any(sk != "_default" for sk in series_keys)
            else ""
        )
        return (
            "\\begin{tikzpicture}\n"
            "\\begin{axis}[\n"
            "  ybar stacked,\n"
            "  width=0.85\\textwidth, height=6cm,\n"
            "  bar width=12pt,\n"
            "  enlarge x limits=0.08,\n"
            "  ymin=0,\n"
            f"  symbolic x coords={{{sym_list}}},\n"
            "  xtick=data,\n"
            f"  xticklabel style={{rotate={rot},anchor={'east' if rot else 'center'},font=\\small}},\n"
            + yfmt
            + f"{legend}"
            + "  ymajorgrids, grid style={dashed,gray!30},\n"
            "  axis lines=left,\n"
            "]\n"
            + "\n".join(plots)
            + "\n\\end{axis}\n"
            "\\end{tikzpicture}"
        )

    def _tikz_pyramid(
        self, rows: List[Dict[str, Any]], spec_id: str
    ) -> str:
        """Age × sex population pyramid.

        Rows have ``series`` carrying the sex code (``"10"``=male,
        ``"20"``=female per the spec) and ``x`` = age band. Males are
        rendered as negative values so they extend left from the axis;
        females extend right.
        """
        # Discover the age bands (preserving insertion order) and split
        # rows by series. The spec's ``invert_x_for`` declares which
        # series maps to the negative side; default to "10" for legacy.
        # Skip the ``unknown`` band — it has no place on an age axis and
        # makes the pyramid look broken when it lands at the top.
        age_bands: List[str] = []
        male_by_age: Dict[str, float] = {}
        female_by_age: Dict[str, float] = {}
        male_code = "10"
        female_code = "20"
        for r in rows:
            ab = str(r.get("x", ""))
            if ab.lower() in ("unknown", "n/a", "nan", "", "null"):
                continue
            if ab not in age_bands:
                age_bands.append(ab)
            v_raw = r.get("y")
            if v_raw is None:
                v_raw = r.get("n", 0)
            try:
                v = float(v_raw)
            except (TypeError, ValueError):
                v = 0.0
            sk = str(r.get("series") or "")
            if sk == male_code:
                male_by_age[ab] = v
            elif sk == female_code:
                female_by_age[ab] = v
            else:
                # Unknown series: treat as female to keep something on screen.
                female_by_age[ab] = female_by_age.get(ab, 0.0) + v
        max_v = max(
            list(male_by_age.values()) + list(female_by_age.values()),
            default=0.0,
        )
        sym_list = ", ".join(
            "{" + latex_escape(ab) + "}" for ab in age_bands
        )
        coords_m = " ".join(
            f"({-abs(male_by_age.get(ab, 0.0)):g},{{{latex_escape(ab)}}})"
            for ab in age_bands
        )
        coords_f = " ".join(
            f"({female_by_age.get(ab, 0.0):g},{{{latex_escape(ab)}}})"
            for ab in age_bands
        )
        # xmin/xmax symmetric so the axis is a true mirror.
        ext = max(max_v * 1.1, 1.0)
        return (
            "\\begin{tikzpicture}\n"
            "\\begin{axis}[\n"
            "  xbar,\n"
            "  width=0.85\\textwidth, height=8cm,\n"
            "  bar width=10pt,\n"
            f"  symbolic y coords={{{sym_list}}},\n"
            "  ytick=data,\n"
            f"  xmin={-ext:g}, xmax={ext:g},\n"
            "  xtick={" + ",".join(
                f"{x:g}" for x in [-ext, -ext / 2, 0, ext / 2, ext]
            ) + "},\n"
            # ``1000 sep`` adds a thousands separator (30000 → "30,000")
            # which pgfplots otherwise renders as scientific ``3·10^4``.
            # We DON'T abs() here because pgfplots' xticklabel callback
            # doesn't reliably support pgfmath; the negative left-side
            # ticks are conventional pyramid notation and most readers
            # understand "left = male, right = female". A future polish
            # could try ``scaled x ticks=false`` + custom ``xticklabels``.
            "  scaled x ticks=false,\n"
            "  xticklabel={\\pgfmathprintnumber[fixed,precision=0,1000 sep={,}]{\\tick}},\n"
            "  yticklabel style={font=\\small},\n"
            "  legend style={font=\\small, at={(0.5,-0.15)}, anchor=north, "
            "legend columns=-1},\n"
            "  axis lines=left,\n"
            "  enlarge y limits=0.06,\n"
            "]\n"
            f"\\addplot[fill=bmagreen, draw=bmagreen] coordinates {{{coords_m}}};\n"
            "\\addlegendentry{ชาย}\n"
            f"\\addplot[fill=warnamber, draw=warnamber] coordinates {{{coords_f}}};\n"
            "\\addlegendentry{หญิง}\n"
            "\\end{axis}\n"
            "\\end{tikzpicture}"
        )

    def _tikz_donut(
        self, rows: List[Dict[str, Any]], spec_id: str
    ) -> str:
        """Donut/pie fallback: a horizontal bar chart with percentages.

        We don't pull in ``pgf-pie`` because Tectonic's bundle doesn't
        always ship it. A horizontal stacked-100% bar conveys the same
        share-of-total information and stays in the pgfplots-only lane.
        """
        labels, values = _extract_xy(rows)
        total = sum(values) or 1.0
        # Convert to percentages so the visualisation is share-of-total.
        pct = [v / total * 100.0 for v in values]
        sym_list = ", ".join("{" + latex_escape(l) + "}" for l in labels)
        coords = " ".join(
            f"({p:g},{{{latex_escape(l)}}})" for l, p in zip(labels, pct)
        )
        return (
            "\\begin{tikzpicture}\n"
            "\\begin{axis}[\n"
            "  xbar,\n"
            "  width=0.85\\textwidth, height=" + f"{max(3, len(labels) * 0.6):g}cm,\n"
            "  bar width=10pt,\n"
            "  xmin=0, xmax=100,\n"
            f"  symbolic y coords={{{sym_list}}},\n"
            "  ytick=data,\n"
            "  yticklabel style={font=\\small},\n"
            "  xlabel={\\small \\%},\n"
            "  axis lines=left,\n"
            "  enlarge y limits=0.1,\n"
            "  nodes near coords,\n"
            "  nodes near coords align={horizontal},\n"
            "  point meta={x},\n"
            "  every node near coord/.append style="
            "{font=\\tiny, /pgf/number format/.cd, fixed, precision=1},\n"
            "]\n"
            f"\\addplot[fill=bmagreen, draw=bmagreen] coordinates {{{coords}}};\n"
            "\\end{axis}\n"
            "\\end{tikzpicture}"
        )

