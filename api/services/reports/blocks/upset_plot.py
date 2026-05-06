"""``upset_plot`` block — multi-set comorbidity overlap (S11).

Hand-rolled UpSet plot in matplotlib (no ``upsetplot`` pip dep). The
canonical use for the BMA whitepaper is comorbidity overlap of {DM, HT,
CVD, dyslipid, obesity, stroke}: each patient has 6 binary cols, the
plot summarises which combinations of diseases co-occur most often.

Layout (Wikipedia reference):

    ┌────────────────────────────────────────────┐
    │ Top panel: bar of intersection sizes       │
    │  ▌ ▌▎▎▎▎ ▏ ▏ ...                            │
    ├────────────────────────────────────────────┤
    │ Bottom-left:    │  Bottom-right:           │
    │ horizontal bar  │  Dot matrix (sets × x)   │
    │ of set sizes    │  ● ● ● ○ ● ○ ○ ○         │
    │                 │  ○ ● ● ● ○ ● ● ○         │
    └─────────────────┴──────────────────────────┘

Data path
---------
Same injection pattern as :mod:`phenotype_clusters` — three sources in
priority order:

    1. ``ctx.extra["upset_rows"]`` — list[dict] supplied by tests / orchestrator.
    2. ``ctx.extra["upset_provider"]`` — async callable
       ``(sets_spec_id, filters) -> list[dict]``.
    3. Empty list (graceful figure).

Each dict is expected to have a ``patient_id`` key (or any single
identifier — we don't actually read it) and one boolean / 0-1 column
per set listed in the spec. Set names are inferred from the dict keys
(minus ``patient_id``) on first row, sorted alphabetically for stable
column order.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from services.reports.blocks._chart_matplotlib import (
    _try_register_thai_font,
    is_matplotlib_available,
    _build_dir,
)
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.upset_plot")


# Reasonable cap on max distinct intersections shown in the dot matrix.
_DEFAULT_MAX_INTERSECTIONS = 15


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _UpSetParams(BaseModel):
    """Parameters for the ``upset_plot`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    sets_spec_id: str
    max_intersections: int = _DEFAULT_MAX_INTERSECTIONS
    caption_th: Optional[str] = None
    # Optional explicit set ordering for stability across renders.
    set_order: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Data path
# ---------------------------------------------------------------------------


async def _resolve_rows(
    ctx: RenderContext, sets_spec_id: str
) -> List[Dict[str, Any]]:
    """Three-tier injection (see module docstring)."""
    if ctx.extra:
        explicit = ctx.extra.get("upset_rows")
        if isinstance(explicit, list):
            return list(explicit)
        provider = ctx.extra.get("upset_provider")
        if callable(provider):
            try:
                rows = await provider(sets_spec_id, {})
                return list(rows) if rows else []
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("upset_provider failed: %s", exc)
                return []
    return []


def _infer_set_names(rows: List[Dict[str, Any]], explicit: List[str]) -> List[str]:
    """Pull set names from rows (skip ``patient_id`` and any non-bool fields)."""
    if explicit:
        return list(explicit)
    if not rows:
        return []
    seen: List[str] = []
    skip = {"patient_id", "id", "pid"}
    for r in rows:
        for k, v in r.items():
            if k.lower() in skip:
                continue
            if k in seen:
                continue
            if isinstance(v, (bool, int)):
                seen.append(k)
            elif isinstance(v, float) and (v == 0 or v == 1):
                seen.append(k)
    return sorted(seen)


def _build_membership_matrix(
    rows: List[Dict[str, Any]], set_names: List[str]
) -> List[Tuple[Tuple[bool, ...], int]]:
    """Group rows by their (set_a, set_b, ...) boolean tuple. Returns a list
    of (tuple, count) sorted by count descending. Tuples with all-False are
    filtered (no intersection at all = "no diseases" — uninteresting)."""
    counts: Dict[Tuple[bool, ...], int] = {}
    for r in rows:
        key = tuple(bool(r.get(name, False)) for name in set_names)
        if not any(key):
            continue
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def _set_sizes(
    rows: List[Dict[str, Any]], set_names: List[str]
) -> Dict[str, int]:
    """Total membership count per individual set (denominator for the
    horizontal bar on the left)."""
    sizes: Dict[str, int] = {n: 0 for n in set_names}
    for r in rows:
        for n in set_names:
            if r.get(n):
                sizes[n] += 1
    return sizes


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _empty_figure(caption: str) -> str:
    """Render the empty-state figure as a captioned PNG."""
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
    out = _build_dir() / f"upset_empty_{abs(hash(caption)) % 0xFFFFFFFF:08x}.png"
    fig.savefig(str(out), dpi=200, bbox_inches="tight")
    plt.close(fig)
    return str(out)


def _render_figure(
    intersections: List[Tuple[Tuple[bool, ...], int]],
    set_names: List[str],
    set_sizes: Dict[str, int],
    cache_token: str,
) -> str:
    """Hand-rolled UpSet layout. Returns absolute PNG path."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    import numpy as np

    family = _try_register_thai_font()
    if family:
        plt.rcParams["font.family"] = family

    n_inter = len(intersections)
    n_sets = len(set_names)

    # Figure layout — three subplots arranged via gridspec:
    #   row 1, col 1 (intersection bars)
    #   row 2, col 0 (set-size bars, narrow)
    #   row 2, col 1 (dot matrix, wide)
    fig = plt.figure(figsize=(max(8, 0.55 * max(n_inter, 8) + 3), 4.0 + 0.35 * max(n_sets, 4)), dpi=200)
    gs = fig.add_gridspec(
        nrows=2,
        ncols=2,
        width_ratios=[1.0, max(3.0, 0.35 * max(n_inter, 8))],
        height_ratios=[2.5, max(1.5, 0.32 * max(n_sets, 4))],
        hspace=0.04,
        wspace=0.03,
    )

    ax_top = fig.add_subplot(gs[0, 1])
    ax_left = fig.add_subplot(gs[1, 0])
    ax_dot = fig.add_subplot(gs[1, 1], sharex=ax_top)

    bma_green = "#00744B"
    accent = "#3B71B8"

    # --- Top panel: intersection size bars -----------------------------
    if n_inter:
        xs = np.arange(n_inter)
        sizes = [c for _, c in intersections]
        ax_top.bar(xs, sizes, color=bma_green, width=0.8)
        max_h = max(sizes) if sizes else 1
        for x, s in zip(xs, sizes):
            ax_top.text(
                x, s + max_h * 0.02, f"{s}", ha="center", va="bottom", fontsize=8
            )
        ax_top.set_ylim(0, max_h * 1.18 + 1)
    else:
        ax_top.text(0.5, 0.5, "ไม่มีข้อมูล", ha="center", va="center", transform=ax_top.transAxes)
    ax_top.set_ylabel("Intersection size")
    ax_top.spines["top"].set_visible(False)
    ax_top.spines["right"].set_visible(False)
    ax_top.tick_params(axis="x", labelbottom=False)
    ax_top.tick_params(axis="x", bottom=False)

    # --- Left panel: per-set total bars (horizontal) -------------------
    if n_sets:
        ys = np.arange(n_sets)
        sizes_l = [set_sizes.get(name, 0) for name in set_names]
        ax_left.barh(ys, sizes_l, color=accent, height=0.55)
        ax_left.invert_xaxis()
        max_w = max(sizes_l) if sizes_l else 1
        for y, s in zip(ys, sizes_l):
            ax_left.text(
                s + max_w * 0.02, y, f"{s}", ha="right", va="center", fontsize=8
            )
        ax_left.set_xlim(max_w * 1.15 + 1, 0)
        ax_left.set_yticks(ys)
        ax_left.set_yticklabels(set_names, fontsize=9)
        ax_left.set_xlabel("Set size")
    ax_left.spines["top"].set_visible(False)
    ax_left.spines["right"].set_visible(False)

    # --- Dot matrix panel (bottom-right) -------------------------------
    if n_sets and n_inter:
        # Light grid lines first.
        for y in range(n_sets):
            ax_dot.axhline(y, color="#dddddd", lw=0.5, zorder=0)
        for i, (combo, _) in enumerate(intersections):
            # Draw all sets as faint markers, then highlight the in-set ones.
            for j in range(n_sets):
                if combo[j]:
                    ax_dot.scatter(i, j, color="#222222", s=80, zorder=2)
                else:
                    ax_dot.scatter(i, j, color="#cccccc", s=40, zorder=1)
            # Connector line through the active sets.
            on_idx = [j for j, on in enumerate(combo) if on]
            if len(on_idx) >= 2:
                ax_dot.plot(
                    [i, i],
                    [min(on_idx), max(on_idx)],
                    color="#222222",
                    lw=1.5,
                    zorder=3,
                )
        ax_dot.set_xlim(-0.5, n_inter - 0.5)
        ax_dot.set_ylim(-0.5, n_sets - 0.5)
        ax_dot.invert_yaxis()
        # Match the y axis with the left panel exactly so rows line up.
        ax_dot.set_yticks(range(n_sets))
        ax_dot.set_yticklabels([])
        ax_dot.set_xticks(range(n_inter))
        ax_dot.set_xticklabels([])
        ax_dot.spines["top"].set_visible(False)
        ax_dot.spines["right"].set_visible(False)
        ax_dot.spines["bottom"].set_visible(False)
        ax_dot.spines["left"].set_visible(False)
        ax_dot.tick_params(axis="both", which="both", length=0)
        # Sync left axis ordering with dot matrix (both reversed top-down).
        ax_left.invert_yaxis()
    else:
        ax_dot.text(0.5, 0.5, "ไม่มีข้อมูล", ha="center", va="center", transform=ax_dot.transAxes)
        ax_dot.set_xticks([])
        ax_dot.set_yticks([])

    fig.tight_layout()
    out = _build_dir() / f"upset_{cache_token}.png"
    fig.savefig(str(out), dpi=200, bbox_inches="tight")
    plt.close(fig)
    return str(out)


# ---------------------------------------------------------------------------
# LaTeX / HTML wrappers
# ---------------------------------------------------------------------------


def _wrap_figure_latex(png_path: str, caption: str, sets_spec_id: str) -> str:
    """Compose an UpSet plot figure body and route through the shared
    caption-below wrapper. Width stays at 0.95\\textwidth — UpSet plots
    benefit from horizontal space for the dot matrix."""
    from services.reports.blocks._render_helpers import (
        safe_label_part,
        wrap_figure_latex,
    )

    body = f"\\includegraphics[width=0.95\\textwidth]{{{png_path}}}"
    label = "upset:" + safe_label_part(sets_spec_id)
    return wrap_figure_latex(body, caption, label)


def _wrap_figure_html(png_path: str, caption: str, sets_spec_id: str) -> str:
    """Compose the ``<img>`` tag for an UpSet plot and route through
    the shared caption-below wrapper. ``data-spec-id`` lets stylesheets
    and tests target the specific set without parsing alt text."""
    from services.reports.blocks._render_helpers import wrap_figure_html

    spec_safe = (
        sets_spec_id.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    img = (
        f'<img src="file://{png_path}" '
        f'alt="upset plot for {spec_safe}" />'
    )
    return wrap_figure_html(
        img,
        caption,
        css_class="upset-plot",
        extra_attrs={"data-spec-id": sets_spec_id},
    )


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class UpSetPlotBlock(ContentBlock):
    """Multi-set comorbidity overlap UpSet plot (S11 — RESEARCHER audience)."""

    block_id: ClassVar[str] = "upset_plot"
    Parameters: ClassVar[type[BaseModel]] = _UpSetParams
    audience_target: ClassVar[Optional[AudienceTarget]] = AudienceTarget.RESEARCHER

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _UpSetParams)
        if not is_matplotlib_available():
            return {
                "n": 0,
                "set_names": [],
                "intersections": [],
                "set_sizes": {},
                "caption": params.caption_th or "",
                "sets_spec_id": params.sets_spec_id,
                "max_intersections": int(params.max_intersections),
                "empty_reason": "matplotlib unavailable",
            }
        rows = await _resolve_rows(ctx, params.sets_spec_id)
        set_names = _infer_set_names(rows, list(params.set_order))
        intersections = _build_membership_matrix(rows, set_names)
        max_inter = max(1, int(params.max_intersections))
        intersections_top = intersections[:max_inter]
        set_sizes = _set_sizes(rows, set_names)

        if ctx.lang == "en":
            caption = params.caption_th or f"UpSet plot — {params.sets_spec_id}"
        else:
            caption = params.caption_th or f"UpSet plot — {params.sets_spec_id}"

        return {
            "n": len(rows),
            "set_names": set_names,
            "intersections": [
                {"combo": list(combo), "count": int(c)}
                for combo, c in intersections_top
            ],
            "set_sizes": {k: int(v) for k, v in set_sizes.items()},
            "caption": caption,
            "sets_spec_id": params.sets_spec_id,
            "max_intersections": max_inter,
            "_intersections_arr": intersections_top,  # internal — used by render
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
                r"\textit{[upset_plot: matplotlib unavailable — "
                + latex_escape(str(data.get("caption", "")))
                + "]}\n"
            )
        caption = str(data.get("caption", ""))
        sets_spec_id = str(data.get("sets_spec_id", "?"))
        if not data.get("set_names") or not data.get("_intersections_arr"):
            png = _empty_figure(caption or sets_spec_id)
            return _wrap_figure_latex(png, caption, sets_spec_id)
        set_names = list(data["set_names"])
        intersections = data["_intersections_arr"]
        set_sizes = dict(data["set_sizes"])
        cache_token = (
            f"{abs(hash((tuple(set_names), tuple((tuple(c), n) for c, n in intersections)))) % 0xFFFFFFFF:08x}"
        )
        png = _render_figure(intersections, set_names, set_sizes, cache_token)
        return _wrap_figure_latex(png, caption, sets_spec_id)

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
                '<figure class="upset-plot error">'
                "<p><em>matplotlib unavailable</em></p></figure>"
            )
        caption = str(data.get("caption", ""))
        sets_spec_id = str(data.get("sets_spec_id", "?"))
        if not data.get("set_names") or not data.get("_intersections_arr"):
            png = _empty_figure(caption or sets_spec_id)
            return _wrap_figure_html(png, caption, sets_spec_id)
        set_names = list(data["set_names"])
        intersections = data["_intersections_arr"]
        set_sizes = dict(data["set_sizes"])
        cache_token = (
            f"{abs(hash((tuple(set_names), tuple((tuple(c), n) for c, n in intersections)))) % 0xFFFFFFFF:08x}"
        )
        png = _render_figure(intersections, set_names, set_sizes, cache_token)
        return _wrap_figure_html(png, caption, sets_spec_id)
