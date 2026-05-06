"""``density_plot`` block — KDE / ridge plot for distributions (S11).

One curve per stratum, with optional reference-range bands (e.g. WHO
Asian BMI cut-offs ≥23 / ≥25 / ≥30 highlighted as colored vertical
bands behind the curves).

Audience-neutral (``audience_target=None``) — KDE plots read fine for
clinicians, executives reviewing the full report, and academic readers.

Data path
---------
Same three-tier pattern as the other S11 blocks:

    1. ``ctx.extra["density_rows"]`` — list[dict] for tests / orchestrator
    2. ``ctx.extra["density_provider"]`` — async callable
       ``(column, stratify_by, filters) -> list[dict]``
    3. Empty list (graceful)

Each dict carries the value at ``params.column`` (and the stratum
indicator at ``params.stratify_by`` if set). Non-numeric values are
skipped at plot time.

KDE
---
Uses :class:`scipy.stats.gaussian_kde` if scipy is available;
otherwise falls back to a simple histogram-based density estimate so
the block stays usable in stripped-down environments. Either way the
filled-area aesthetic is preserved (semi-transparent fill + outlined
curve), so reports look consistent across both code paths.
"""
from __future__ import annotations

import logging
import math
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from services.reports.blocks._chart_matplotlib import (
    _try_register_thai_font,
    is_matplotlib_available,
    _build_dir,
)
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.blocks._render_helpers import (
    safe_label_part,
    wrap_figure_html,
    wrap_figure_latex,
)
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.density_plot")


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _DensityParams(BaseModel):
    """Parameters for the ``density_plot`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    column: str
    stratify_by: Optional[str] = None
    # Tuples are passed as 3-element lists in YAML and validated here.
    reference_ranges: List[Tuple[float, float, str]] = Field(default_factory=list)
    filters: Dict[str, Any] = Field(default_factory=dict)
    caption_th: Optional[str] = None
    caption_en: Optional[str] = None
    # Optional x-axis range override; if both are None, computed from data.
    x_min: Optional[float] = None
    x_max: Optional[float] = None


# ---------------------------------------------------------------------------
# Data path
# ---------------------------------------------------------------------------


async def _resolve_rows(
    ctx: RenderContext,
    column: str,
    stratify_by: Optional[str],
    filters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Three-tier injection — same pattern as the other S11 blocks."""
    if ctx.extra:
        explicit = ctx.extra.get("density_rows")
        if isinstance(explicit, list):
            return list(explicit)
        provider = ctx.extra.get("density_provider")
        if callable(provider):
            try:
                rows = await provider(column, stratify_by, filters)
                return list(rows) if rows else []
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("density_provider failed: %s", exc)
                return []
    return []


def _split_strata(
    rows: List[Dict[str, Any]],
    column: str,
    stratify_by: Optional[str],
) -> Dict[str, List[float]]:
    """Group rows into ``{stratum_label: [values]}``. ``stratify_by=None``
    yields a single ``"all"`` bucket. Non-numeric / NaN values are skipped."""
    out: Dict[str, List[float]] = {}
    for r in rows:
        v = r.get(column)
        if v is None:
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(x) or math.isinf(x):
            continue
        key = "all"
        if stratify_by:
            sk = r.get(stratify_by)
            key = "—" if sk is None else str(sk)
        out.setdefault(key, []).append(x)
    return out


# ---------------------------------------------------------------------------
# KDE
# ---------------------------------------------------------------------------


def _gaussian_kde_curve(values: List[float], grid: List[float]) -> List[float]:
    """KDE curve at ``grid``. Falls back to a histogram density if scipy
    is missing OR the input has zero variance (gaussian_kde would crash)."""
    n = len(values)
    if n == 0:
        return [0.0] * len(grid)
    try:
        import numpy as np
        from scipy.stats import gaussian_kde

        arr = np.asarray(values, dtype=float)
        if arr.std(ddof=0) <= 0:
            # Zero-variance fallback — render a tall narrow spike at the
            # value, scaled so the area integrates to ~1 over the grid.
            return _spike_curve(arr[0], grid)
        kde = gaussian_kde(arr)
        return [float(v) for v in kde(np.asarray(grid))]
    except ImportError:
        return _hist_density(values, grid)


def _hist_density(values: List[float], grid: List[float]) -> List[float]:
    """Histogram-based fallback when scipy is unavailable."""
    if not values or len(grid) < 2:
        return [0.0] * len(grid)
    g_min, g_max = grid[0], grid[-1]
    bin_w = (g_max - g_min) / max(1, len(grid) - 1)
    counts = [0.0] * len(grid)
    for v in values:
        if v < g_min or v > g_max or bin_w <= 0:
            continue
        idx = int((v - g_min) / bin_w)
        if 0 <= idx < len(counts):
            counts[idx] += 1
    n = sum(counts) * bin_w
    if n <= 0:
        return [0.0] * len(grid)
    return [c / n for c in counts]


def _spike_curve(x0: float, grid: List[float]) -> List[float]:
    """Tight Gaussian-like spike at ``x0`` for zero-variance strata."""
    if not grid:
        return []
    span = grid[-1] - grid[0] if len(grid) > 1 else 1.0
    sigma = max(span * 0.005, 1e-6)
    out = []
    for x in grid:
        d = (x - x0) / sigma
        out.append(math.exp(-0.5 * d * d) / (sigma * math.sqrt(2 * math.pi)))
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _empty_figure(caption: str) -> str:
    """Empty-state PNG."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    family = _try_register_thai_font()
    if family:
        plt.rcParams["font.family"] = family

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=200)
    ax.text(
        0.5,
        0.5,
        "ไม่มีข้อมูล",
        ha="center",
        va="center",
        fontsize=18,
        color="#666666",
        transform=ax.transAxes,
    )
    ax.set_xticks([])
    ax.set_yticks([])
    fig.tight_layout()
    out = _build_dir() / f"density_empty_{abs(hash(caption)) % 0xFFFFFFFF:08x}.png"
    fig.savefig(str(out), dpi=200, bbox_inches="tight")
    plt.close(fig)
    return str(out)


def _render_figure(
    strata: Dict[str, List[float]],
    column: str,
    reference_ranges: List[Tuple[float, float, str]],
    x_min: Optional[float],
    x_max: Optional[float],
    cache_token: str,
) -> str:
    """KDE plot. Returns absolute PNG path."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    import numpy as np

    family = _try_register_thai_font()
    if family:
        plt.rcParams["font.family"] = family

    cmap = plt.get_cmap("tab10")

    # Collapse all values for axis range computation.
    all_vals: List[float] = []
    for vs in strata.values():
        all_vals.extend(vs)
    if not all_vals:
        return _empty_figure(column)

    arr = np.asarray(all_vals, dtype=float)
    lo = float(x_min) if x_min is not None else float(np.percentile(arr, 1))
    hi = float(x_max) if x_max is not None else float(np.percentile(arr, 99))
    if hi <= lo:
        # Degenerate range (single value) — pad ±1.
        hi = lo + 1.0
        lo = lo - 1.0
    grid = list(np.linspace(lo, hi, num=300))

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=200)

    # Reference bands FIRST so they sit behind the curves.
    band_alpha = 0.10
    band_palette = ["#F4B400", "#DB4437", "#0F9D58", "#4285F4", "#A142F4"]
    for i, (b_lo, b_hi, label) in enumerate(reference_ranges):
        try:
            band_lo = float(b_lo)
            band_hi = float(b_hi)
        except (TypeError, ValueError):
            continue
        color = band_palette[i % len(band_palette)]
        ax.axvspan(
            band_lo,
            band_hi,
            color=color,
            alpha=band_alpha,
            zorder=0,
            label=str(label),
        )

    # Plot one curve per stratum, sorted by stratum label for stable color.
    keys = sorted(strata.keys())
    for i, key in enumerate(keys):
        values = strata[key]
        if not values:
            continue
        curve = _gaussian_kde_curve(values, grid)
        color = cmap(i % 10)
        ax.fill_between(grid, curve, color=color, alpha=0.30, zorder=2)
        ax.plot(grid, curve, color=color, lw=1.5, label=str(key), zorder=3)

    ax.set_xlim(lo, hi)
    ax.set_xlabel(column)
    ax.set_ylabel("Density")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="best", fontsize=9, frameon=False)
    fig.tight_layout()
    out = _build_dir() / f"density_{cache_token}.png"
    fig.savefig(str(out), dpi=200, bbox_inches="tight")
    plt.close(fig)
    return str(out)


# ---------------------------------------------------------------------------
# LaTeX / HTML wrappers
# ---------------------------------------------------------------------------


def _wrap_figure_latex(png_path: str, caption: str, column: str) -> str:
    """Compose a density-plot figure body and route through the shared
    caption-below wrapper. Width stays at 0.95\\textwidth — the legacy
    setting that fills the page since density plots benefit from the
    extra horizontal space."""
    body = f"\\includegraphics[width=0.95\\textwidth]{{{png_path}}}"
    label = "density:" + safe_label_part(column)
    return wrap_figure_latex(body, caption, label)


def _wrap_figure_html(png_path: str, caption: str, column: str) -> str:
    """Compose the ``<img>`` tag for a density plot and route through
    the shared caption-below wrapper. ``data-column`` lets stylesheets
    and tests target the specific metric without parsing alt text."""
    col_safe = (
        column.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    img = (
        f'<img src="file://{png_path}" '
        f'alt="density plot for {col_safe}" />'
    )
    return wrap_figure_html(
        img,
        caption,
        css_class="density-plot",
        extra_attrs={"data-column": column},
    )


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class DensityPlotBlock(ContentBlock):
    """KDE / ridge density plot for distributions (S11 — any audience)."""

    block_id: ClassVar[str] = "density_plot"
    Parameters: ClassVar[type[BaseModel]] = _DensityParams
    audience_target: ClassVar[Optional[AudienceTarget]] = None  # any audience

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _DensityParams)
        if not is_matplotlib_available():
            return {
                "n": 0,
                "column": params.column,
                "stratify_by": params.stratify_by,
                "strata": {},
                "reference_ranges": list(params.reference_ranges),
                "caption": params.caption_th or "",
                "empty_reason": "matplotlib unavailable",
            }
        rows = await _resolve_rows(
            ctx, params.column, params.stratify_by, params.filters
        )
        strata = _split_strata(rows, params.column, params.stratify_by)
        n_total = sum(len(vs) for vs in strata.values())

        if ctx.lang == "en":
            caption = (
                params.caption_en
                or params.caption_th
                or (
                    f"Distribution of {params.column}"
                    + (f" by {params.stratify_by}" if params.stratify_by else "")
                )
            )
        else:
            caption = (
                params.caption_th
                or params.caption_en
                or (
                    f"การกระจายของ {params.column}"
                    + (f" จำแนกตาม {params.stratify_by}" if params.stratify_by else "")
                )
            )

        return {
            "n": int(n_total),
            "column": params.column,
            "stratify_by": params.stratify_by,
            "strata": strata,
            "stratum_sizes": {k: len(v) for k, v in strata.items()},
            "reference_ranges": [
                (float(b[0]), float(b[1]), str(b[2]))
                for b in params.reference_ranges
            ],
            "x_min": params.x_min,
            "x_max": params.x_max,
            "caption": caption,
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
        if not is_matplotlib_available():
            from services.latex_utils import latex_escape
            return (
                r"\textit{[density_plot: matplotlib unavailable — "
                + latex_escape(str(data.get("caption", "")))
                + "]}\n"
            )
        column = str(data.get("column", "?"))
        caption = str(data.get("caption", ""))
        strata: Dict[str, List[float]] = data.get("strata", {})
        if not strata or sum(len(v) for v in strata.values()) < 2:
            png = _empty_figure(caption or column)
            return _wrap_figure_latex(png, caption, column)
        reference_ranges = [
            (float(t[0]), float(t[1]), str(t[2]))
            for t in data.get("reference_ranges", [])
        ]
        cache_token = (
            f"{abs(hash((column, tuple(sorted(strata.keys())), data.get('n', 0)))) % 0xFFFFFFFF:08x}"
        )
        png = _render_figure(
            strata,
            column,
            reference_ranges,
            data.get("x_min"),
            data.get("x_max"),
            cache_token,
        )
        return _wrap_figure_latex(png, caption, column)

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        if not is_matplotlib_available():
            return (
                '<figure class="density-plot error">'
                "<p><em>matplotlib unavailable</em></p></figure>"
            )
        column = str(data.get("column", "?"))
        caption = str(data.get("caption", ""))
        strata: Dict[str, List[float]] = data.get("strata", {})
        if not strata or sum(len(v) for v in strata.values()) < 2:
            png = _empty_figure(caption or column)
            return _wrap_figure_html(png, caption, column)
        reference_ranges = [
            (float(t[0]), float(t[1]), str(t[2]))
            for t in data.get("reference_ranges", [])
        ]
        cache_token = (
            f"{abs(hash((column, tuple(sorted(strata.keys())), data.get('n', 0)))) % 0xFFFFFFFF:08x}"
        )
        png = _render_figure(
            strata,
            column,
            reference_ranges,
            data.get("x_min"),
            data.get("x_max"),
            cache_token,
        )
        return _wrap_figure_html(png, caption, column)
