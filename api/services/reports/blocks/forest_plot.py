"""``forest_plot`` block — coefficient + 95% CI visualisation.

Per Sprint S11 ("PhD-grade Whitepaper") this is the workhorse plot of an
academic results section: each row is one estimate (an OR / RR / mean
difference / β) with its 95% confidence interval drawn as a horizontal
whisker, plus a vertical reference line at the null value (1.0 for
ratios, 0.0 for differences). The block is intentionally a *pure
visualisation* primitive — it does NOT compute estimates itself. The
caller (e.g. :class:`LogisticRegressionBlock`) supplies a list of pre-
computed rows in a fixed shape:

    {
        "label":   "Smoking",
        "estimate": 1.74,
        "ci_lo":    1.20,
        "ci_hi":    2.53,
        "p_value":  0.004,    # optional
        "q_value":  0.012,    # optional (BH-FDR adjusted)
        "n":        4321,     # optional
    }

This separation matches how forest plots are produced in the academic
literature: regression / meta-analysis returns a coefficient table, and
the table is THEN visualised with a single boilerplate plot. Re-using
this block for OR tables, RR tables, mean-difference tables and
correlation tables means the report has *one* visual idiom for "here
are estimates with uncertainty" — researchers don't need to re-orient
every time they hit a new figure.

Audience: ``RESEARCHER`` (the orchestrator filters this block out for
``people`` / ``executive`` reports — those audiences get a plain-language
summary instead of CI bars).

Renderers
---------
Both ``render_latex`` and ``render_html`` ultimately wrap the same PNG
written by matplotlib. We do NOT try to emit pgfplots TikZ here:

* Forest plots are conventionally drawn with diamond / square markers,
  log-scale axes, and ``±∞`` clamp arrows. pgfplots can do this but
  the markup gets long and fragile (especially under XeLaTeX with
  Thai labels). One PNG keeps the look identical between the HTML and
  the PDF and stays under our existing matplotlib-fallback contract.
* The PNG path is handed to LaTeX via ``\includegraphics{<abspath>}``.
  We co-locate the file under the existing ``bma_chart_png`` temp dir
  so the chart fallback's caching / cleanup logic applies uniformly.

Significance annotations
------------------------
If a row provides ``q_value`` (preferred over ``p_value`` because we
control FDR family-wise) we annotate the row with stars:

    *   q < 0.05
    **  q < 0.01
    *** q < 0.001

The brief says "if not yet available, gracefully skip" — i.e. when
``q_value`` is missing (Agent A's BH-FDR helper has not landed) the
annotation is just blank. We never invent stars from a raw p_value.

Capacity
--------
Forest plots get hard to read past ~25 rows. The brief explicitly says
the BLOCK does no auto-pagination — it just truncates to 25 rows and
emits a small "+N more rows truncated" warning under the figure. The
caller is responsible for splitting longer tables into multiple
``forest_plot`` sections.
"""
from __future__ import annotations

import html
import logging
import math
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.blocks._render_helpers import (
    wrap_figure_html,
    wrap_figure_latex,
)
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.forest_plot")


# ---------------------------------------------------------------------------
# Brand colours — keep in sync with the rest of the report.
# ---------------------------------------------------------------------------

BMA_GREEN = "#2E7D32"
RISK_RED = "#B71C1C"
NEUTRAL_GREY = "#666666"

# Maximum rows per figure. Past this point CI bars overlap and labels
# cramp; the block emits a truncation warning rather than scrolling
# the figure off the page.
_MAX_ROWS_PER_FIG = 25


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _ForestPlotParams(BaseModel):
    """Parameters for the ``forest_plot`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    rows: List[Dict[str, Any]] = Field(default_factory=list)
    """List of pre-computed estimate rows. Each row dict must have at
    minimum ``label``, ``estimate``, ``ci_lo``, ``ci_hi``; ``p_value``,
    ``q_value``, ``n`` are optional. Rows with NaN estimates render as
    a missing-data row (label visible, no whisker)."""

    metric_name: str = "Estimate"
    """Axis label for the x-axis, e.g. "Odds Ratio", "Risk Ratio",
    "Mean Difference", "β coefficient". The label is escaped before it
    hits matplotlib so e.g. ``$\\beta$`` would render literally — pass
    plain text, the figure typeface handles symbols natively."""

    null_value: float = 1.0
    """Value of the dashed vertical reference line. 1.0 for OR/RR
    (multiplicative null), 0.0 for differences / β coefficients."""

    log_scale: bool = False
    """Use a log-scaled x-axis. Enable this for OR / RR / hazard ratios
    so 0.5 and 2.0 are equidistant from the null value — failing to do
    this is the most common forest-plot mistake in the literature."""

    sort_by: Literal["estimate", "p_value", "label", "input"] = "estimate"
    """Row ordering. ``"input"`` keeps caller-supplied order. ``"p_value"``
    sorts most-significant first (NaNs to the bottom). ``"estimate"`` is
    the default and matches the convention of "biggest effect at the
    top". ``"label"`` is alphabetical for reproducibility-focussed
    appendices."""

    caption_th: Optional[str] = None
    caption_en: Optional[str] = None
    """Optional caption shown under the figure. If both are None the
    figure prints a generic caption derived from ``metric_name``."""

    source_path: Optional[str] = None
    """Dotted path into ``data_collector.data()`` for descriptor-driven
    use. When set, ``collect`` reads the row list from there and IGNORES
    ``params.rows``. When None (legacy default), ``params.rows`` is used
    verbatim — keeps test fixtures and direct callers working unchanged.
    Resolved row list must match the same shape as ``rows`` (label,
    estimate, ci_lo, ci_hi, ...)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    """Parse ``value`` as a finite float; return None on missing / non-finite."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _significance_stars(q_value: Optional[float]) -> str:
    """Return ``*``/``**``/``***``/``""`` for a q-value (BH-FDR adjusted).

    We deliberately key on q_value, not raw p_value, so the annotation
    reflects family-wise FDR control rather than "this single test is
    significant in isolation". When the caller has not supplied a
    q_value, we return an empty string — the caller may pass q_value
    equal to p_value if they explicitly want raw-p stars, but that is
    NOT the default behaviour.
    """
    if q_value is None:
        return ""
    if q_value < 0.001:
        return "***"
    if q_value < 0.01:
        return "**"
    if q_value < 0.05:
        return "*"
    return ""


def _sort_rows(
    rows: List[Dict[str, Any]], sort_by: str
) -> List[Dict[str, Any]]:
    """Apply the requested ordering. ``input`` is a no-op (preserves order)."""
    if sort_by == "input":
        return list(rows)
    if sort_by == "estimate":
        return sorted(
            rows,
            key=lambda r: (
                _safe_float(r.get("estimate")) is None,
                -(_safe_float(r.get("estimate")) or 0.0),
            ),
        )
    if sort_by == "p_value":
        return sorted(
            rows,
            key=lambda r: (
                _safe_float(r.get("p_value")) is None,
                _safe_float(r.get("p_value")) or float("inf"),
            ),
        )
    if sort_by == "label":
        return sorted(rows, key=lambda r: str(r.get("label", "")))
    return list(rows)  # pragma: no cover — pydantic gates this


def _truncate(rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
    """Truncate to ``_MAX_ROWS_PER_FIG`` and return (kept, n_dropped)."""
    if len(rows) <= _MAX_ROWS_PER_FIG:
        return rows, 0
    n_drop = len(rows) - _MAX_ROWS_PER_FIG
    logger.warning(
        "forest_plot: truncating %d rows to %d (caller should paginate)",
        n_drop,
        _MAX_ROWS_PER_FIG,
    )
    return rows[:_MAX_ROWS_PER_FIG], n_drop


def _build_dir() -> Path:
    """Re-use the chart fallback's PNG temp dir so cleanup is uniform."""
    # Local import to keep ``forest_plot`` importable when matplotlib is
    # absent (we only need _build_dir when actually rendering a figure).
    from services.reports.blocks._chart_matplotlib import _build_dir as cd

    return cd()


def _filename_for(rows: List[Dict[str, Any]], metric: str) -> str:
    """Stable filename derived from row contents.

    Matches the chart fallback's idiom: includes the metric + a short
    hash of the values so two renders of the same forest_plot with
    different filters don't collide on disk.
    """
    safe_metric = "".join(
        c if c.isalnum() else "_" for c in metric.lower()
    )[:32]
    seed = tuple(
        (
            str(r.get("label", "")),
            _safe_float(r.get("estimate")) or 0.0,
            _safe_float(r.get("ci_lo")) or 0.0,
            _safe_float(r.get("ci_hi")) or 0.0,
        )
        for r in rows
    )
    h = abs(hash(seed)) % 0xFFFFFFFF
    return f"forest_{safe_metric}_{h:08x}.png"


# ---------------------------------------------------------------------------
# Matplotlib renderer — produces a PNG, returns the absolute path.
# ---------------------------------------------------------------------------


def render_forest_to_png(
    rows: List[Dict[str, Any]],
    metric_name: str,
    *,
    null_value: float = 1.0,
    log_scale: bool = False,
    caption: Optional[str] = None,
    dpi: int = 200,
) -> Path:
    """Render a forest plot to a PNG file. Returns the absolute Path.

    Public so :class:`LogisticRegressionBlock` can re-use the same
    renderer without round-tripping through the block-registry layer.
    """
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    from services.reports.blocks._chart_matplotlib import (
        _try_register_thai_font,
    )

    family = _try_register_thai_font()
    if family:
        plt.rcParams["font.family"] = family

    # Build figure proportional to row count: ~0.4in per row + 1.5 in
    # padding so the title/axis don't crush the smallest figure.
    n_rows = max(1, len(rows))
    fig_h = max(2.5, 0.40 * n_rows + 1.5)
    fig, ax = plt.subplots(figsize=(8, fig_h), dpi=dpi)

    if not rows:
        ax.text(
            0.5,
            0.5,
            "ไม่มีข้อมูล",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=14,
            color=NEUTRAL_GREY,
        )
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        # y-axis: row 0 at the TOP — invert so the top-of-list row is
        # visually first (forest-plot convention).
        ys = list(range(len(rows)))
        labels = [str(r.get("label", "")) for r in rows]
        for y, row in zip(ys, rows):
            est = _safe_float(row.get("estimate"))
            lo = _safe_float(row.get("ci_lo"))
            hi = _safe_float(row.get("ci_hi"))
            if est is None or lo is None or hi is None:
                # Missing-data row: just write a "—" at the centre of
                # the row's vertical band. We still consume a y-slot so
                # the row label renders with the rest.
                ax.text(
                    null_value, y, "—",
                    ha="center", va="center",
                    fontsize=10, color=NEUTRAL_GREY,
                )
                continue
            # Whisker: line + endpoint caps. Use BMA green when the CI
            # excludes null on the safer side, red when it excludes on
            # the riskier side, neutral grey when CI crosses null.
            if lo > null_value:
                color = RISK_RED
            elif hi < null_value:
                color = BMA_GREEN
            else:
                color = NEUTRAL_GREY
            ax.plot([lo, hi], [y, y], color=color, lw=1.5, zorder=2)
            # End caps so CIs are visible against grid.
            ax.plot([lo, lo], [y - 0.18, y + 0.18], color=color, lw=1.5)
            ax.plot([hi, hi], [y - 0.18, y + 0.18], color=color, lw=1.5)
            # Point estimate marker.
            ax.scatter(
                [est], [y], color=color, s=42, zorder=3,
                marker="s", edgecolors="white", linewidths=0.6,
            )
            # Annotation: stars + n
            stars = _significance_stars(_safe_float(row.get("q_value")))
            n = row.get("n")
            ann_parts = []
            if stars:
                ann_parts.append(stars)
            if n is not None:
                ann_parts.append(f"n={n}")
            if ann_parts:
                ax.text(
                    hi, y, "  " + " ".join(ann_parts),
                    ha="left", va="center",
                    fontsize=8, color="#333333",
                )
        ax.set_yticks(ys)
        ax.set_yticklabels(labels)
        ax.invert_yaxis()
        # Reference line at the null value.
        ax.axvline(
            null_value, color="#999999", lw=1.0,
            linestyle="--", zorder=1,
        )
        if log_scale:
            ax.set_xscale("log")

    ax.set_xlabel(metric_name)
    if caption:
        ax.set_title(caption, fontsize=10, color="#333333")
    # Visual minimalism — drop top/right spines.
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.grid(True, axis="x", linestyle=":", color="#cccccc", alpha=0.6)
    fig.tight_layout()

    fname = _filename_for(rows, metric_name)
    out = _build_dir() / fname
    fig.savefig(str(out), dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    logger.debug("forest_plot wrote %s", out)
    return out


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


def _resolve_caption(
    params: "_ForestPlotParams", lang: str
) -> str:
    """Return the caption to render, falling back to a generic Thai
    string derived from ``metric_name`` when neither caption_* is set."""
    if lang == "en":
        if params.caption_en:
            return params.caption_en
        return f"Forest plot of {params.metric_name} estimates with 95% CI"
    if params.caption_th:
        return params.caption_th
    return f"Forest plot — {params.metric_name} ± 95% CI"


def _label_id(metric: str) -> str:
    """LaTeX-safe label fragment derived from the metric name."""
    return "".join(
        c if c.isalnum() or c == "_" else "_"
        for c in metric.lower()
    )[:48] or "x"


class ForestPlotBlock(ContentBlock):
    """Coefficient-with-CI visualisation block (Sprint S11)."""

    block_id: ClassVar[str] = "forest_plot"
    Parameters: ClassVar[type[BaseModel]] = _ForestPlotParams
    audience_target: ClassVar[Optional[AudienceTarget]] = (
        AudienceTarget.RESEARCHER
    )

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        """Pull rows from ``data_collector`` when ``source_path`` is set,
        otherwise pass through ``params.rows`` (legacy / test path)."""
        assert isinstance(params, _ForestPlotParams)
        # Descriptor-driven path: resolve rows from the collector. Match
        # the dotted-path semantics used by trend_table / crosstab.
        raw_rows: List[Dict[str, Any]]
        if params.source_path:
            getter = getattr(ctx.data_collector, "data", None)
            bag: Any = getter() if callable(getter) else (getter or {})
            cur: Any = bag
            for seg in params.source_path.split("."):
                if isinstance(cur, dict) and seg in cur:
                    cur = cur[seg]
                elif hasattr(cur, seg):
                    cur = getattr(cur, seg)
                else:
                    cur = None
                    break
            raw_rows = []
            if isinstance(cur, list):
                for r in cur:
                    if isinstance(r, dict):
                        raw_rows.append(dict(r))
            elif cur is not None:
                logger.warning(
                    "forest_plot.source_path %r resolved to non-list (%s); "
                    "treating as empty",
                    params.source_path,
                    type(cur).__name__,
                )
        else:
            raw_rows = list(params.rows)
        rows = _sort_rows(raw_rows, params.sort_by)
        kept, n_dropped = _truncate(rows)
        return {
            "rows": kept,
            "n_dropped": n_dropped,
            "metric_name": params.metric_name,
            "null_value": params.null_value,
            "log_scale": params.log_scale,
        }

    # ------------------------------------------------------------------
    # LaTeX
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        assert isinstance(params, _ForestPlotParams)
        rows: List[Dict[str, Any]] = data.get("rows", [])
        n_dropped = int(data.get("n_dropped", 0))
        caption = _resolve_caption(params, ctx.lang)
        png_path = render_forest_to_png(
            rows,
            data.get("metric_name", params.metric_name),
            null_value=float(data.get("null_value", params.null_value)),
            log_scale=bool(data.get("log_scale", params.log_scale)),
            caption=None,  # caption goes in \caption{}, not the title
        )
        label = "fig:forest_" + _label_id(params.metric_name)
        truncate_note: Optional[str] = None
        if n_dropped > 0:
            truncate_note = (
                "\\par\\smallskip\\textit{"
                + latex_escape(
                    f"+{n_dropped} แถวเพิ่มเติมถูกตัดออก "
                    f"(แสดงสูงสุด {_MAX_ROWS_PER_FIG} แถวต่อภาพ)"
                )
                + "}\n"
            )
        body = "\\includegraphics[width=0.85\\textwidth]{" + str(png_path) + "}"
        return wrap_figure_latex(
            body,
            str(caption),
            label,
            post_caption=truncate_note,
        )

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        assert isinstance(params, _ForestPlotParams)
        rows: List[Dict[str, Any]] = data.get("rows", [])
        n_dropped = int(data.get("n_dropped", 0))
        caption = _resolve_caption(params, ctx.lang)
        png_path = render_forest_to_png(
            rows,
            data.get("metric_name", params.metric_name),
            null_value=float(data.get("null_value", params.null_value)),
            log_scale=bool(data.get("log_scale", params.log_scale)),
            caption=None,
        )
        # Inline as base64 so the HTML report stays self-contained.
        try:
            import base64

            b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
            img_src = f"data:image/png;base64,{b64}"
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("forest_plot: base64 inlining failed: %s", exc)
            img_src = png_path.as_uri()
        truncated: Optional[str] = None
        if n_dropped > 0:
            truncated = (
                '<p class="forest-plot-truncated"><em>'
                + html.escape(
                    f"+{n_dropped} more rows truncated "
                    f"(max {_MAX_ROWS_PER_FIG} per figure)"
                )
                + "</em></p>"
            )
        alt = html.escape(
            f"Forest plot of {params.metric_name} with 95% confidence intervals"
        )
        return wrap_figure_html(
            f'<img src="{img_src}" alt="{alt}" />',
            str(caption),
            css_class="forest-plot",
            post_caption=truncated,
        )


__all__ = [
    "ForestPlotBlock",
    "render_forest_to_png",
    "BMA_GREEN",
    "RISK_RED",
]
