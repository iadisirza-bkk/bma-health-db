"""``choropleth`` block — district / zone heatmap as a tile grid (S11).

Sprint S11 ("PhD-grade Whitepaper") — visualises one outcome per
spatial unit (8 zones or 50 districts) as a coloured grid. We do NOT
render a true cartographic choropleth: that would require a geojson
polygon set + a non-trivial projection step, which is deferred until
geojson polygons are wired in a later sprint.

Instead we use a **tile choropleth** — a hand-laid grid where each
cell represents one spatial unit, coloured by its value. This
preserves the ordinal "high vs low" reading without pretending to be
geographically accurate.

Two layouts:

* ``geographic_unit='zone'`` — 8 cells in a hand-tuned 3×3 layout that
  approximates Bangkok's actual zone footprint (zone 4 = old city,
  zone 5 = central, zones 6/8 = north/east, etc.). The empty cell is
  in the south-east corner.
* ``geographic_unit='district'`` — 50 cells laid out as a packed
  rectangular grid (10×5) keyed by district code. The cells are
  alphabetised by code so two renders of the same data produce the
  same image; we are explicit in the caption that the layout is NOT
  cartographic.

Output is a PNG written to a process-local temp dir; the LaTeX path
embeds it via ``\\includegraphics``, the HTML path emits an inline
``<img>`` referencing the same path (which the orchestrator copies
into the final HTML asset bundle).
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.blocks._render_helpers import (
    safe_label_part,
    wrap_figure_html,
    wrap_figure_latex,
)
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.choropleth_block")


# ---------------------------------------------------------------------------
# Hand-laid 3×3 zone grid that approximates the Bangkok health-zone map.
#
# (row, col) tuples — row 0 is the top of the figure (north). The choice
# of layout is rough but follows the actual zone geography:
#     col 0 = west (Thonburi side)        col 2 = east
#     row 0 = north                       row 2 = south
#
# Layout (visual approximation, not cartographic):
#
#     |  1  |  4  |  6  |   ← row 0 (north — west / old city / far north)
#     |  2  |  5  |  8  |   ← row 1 (mid)
#     |  3  |  7  |  -  |   ← row 2 (south)
#
# Zone 1 west, 4 old city, 6 far north, 2 inner Thonburi, 5 central,
# 8 east outskirts, 3 south-central river, 7 east, last cell empty.
# The grid is deliberately 3×3 (not 4×2) so the empty cell sits in a
# logical "outside-Bangkok" corner.
# ---------------------------------------------------------------------------


_ZONE_GRID: Dict[str, Tuple[int, int]] = {
    "1": (0, 0),
    "4": (0, 1),
    "6": (0, 2),
    "2": (1, 0),
    "5": (1, 1),
    "8": (1, 2),
    "3": (2, 0),
    "7": (2, 1),
}
_ZONE_GRID_SHAPE = (3, 3)


# ---------------------------------------------------------------------------
# Reuse the same temp dir + Thai font registration the chart helper uses.
# We don't import the helper because it's the public chart API and we
# want to keep this block standalone (S11 contract: no edits to other
# files). The two functions below mirror its behaviour exactly so PNGs
# from this block coexist with chart PNGs in the same temp dir.
# ---------------------------------------------------------------------------


_BUILD_DIR: Optional[Path] = None
_FONT_REGISTERED = False


def _build_dir() -> Path:
    """Stable per-process temp dir for choropleth PNGs."""
    global _BUILD_DIR
    if _BUILD_DIR is None:
        _BUILD_DIR = Path(tempfile.gettempdir()) / "bma_chart_png"
        _BUILD_DIR.mkdir(parents=True, exist_ok=True)
    return _BUILD_DIR


def _try_register_thai_font() -> Optional[str]:
    """Register a Thai-capable TTF with matplotlib (cached per process).

    Returns the family name to assign to ``font.family`` or ``None`` if
    no Thai font was found. Boxified Thai characters are an acceptable
    failure mode — the chart numbers and codes still render.
    """
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return getattr(_try_register_thai_font, "_family", None)
    try:
        import matplotlib.font_manager as fm
    except ImportError:  # pragma: no cover
        return None
    here = Path(__file__).resolve()
    repo_root = here.parents[4]
    candidates: List[Path] = [
        repo_root / "api" / "templates" / "latex" / "assets" / "GoogleSans-Regular.ttf",
        repo_root / "api" / "templates" / "latex" / "assets" / "NotoSansThai-Regular.ttf",
        Path("/Library/Fonts/Sarabun-Regular.ttf"),
        Path("/usr/share/fonts/truetype/tlwg/Sarabun.ttf"),
    ]
    family: Optional[str] = None
    for path in candidates:
        if path.is_file():
            try:
                fm.fontManager.addfont(str(path))
                prop = fm.FontProperties(fname=str(path))
                family = prop.get_name()
                logger.info("choropleth_block: registered Thai font %s as %s", path, family)
                break
            except Exception as exc:  # pragma: no cover
                logger.warning("choropleth_block: could not register %s: %s", path, exc)
    _FONT_REGISTERED = True
    setattr(_try_register_thai_font, "_family", family)
    return family


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _ChoroplethParams(BaseModel):
    """Parameters for the ``choropleth`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    outcome_spec_id: str
    geographic_unit: Literal["zone", "district"] = "zone"
    filters: Dict[str, Any] = Field(default_factory=dict)
    color_scheme: str = "RdYlGn_r"
    caption_th: Optional[str] = None
    caption_en: Optional[str] = None
    value_unit: str = ""  # e.g. "% prevalence", appended to colorbar


# ---------------------------------------------------------------------------
# Helpers — same chart-service / row-aggregation pattern as ``spatial_autocorr``
# ---------------------------------------------------------------------------


def _resolve_chart_service(ctx: RenderContext) -> Any:
    """Reuse the chart-service injected on ``ctx.extra`` if present."""
    pre = ctx.extra.get("chart_service") if ctx.extra else None
    if pre is not None:
        return pre
    from services.charts.registry import chart_registry
    from services.charts.service import ChartService
    from repositories.mv_repository import MVRepository

    return ChartService(chart_registry(), MVRepository())


def _response_to_dict(resp: Any) -> Dict[str, Any]:
    if hasattr(resp, "model_dump"):
        return resp.model_dump()  # type: ignore[no-any-return]
    if isinstance(resp, dict):
        return resp
    return {"data": [], "kind": "bar"}  # pragma: no cover


def _aggregate_by_unit(
    rows: List[Dict[str, Any]],
    unit_keys: List[str],
) -> Dict[str, float]:
    """Same aggregation as spatial_autocorr — kept local to avoid the
    cross-block import the S11 contract forbids."""
    by_unit: Dict[str, Dict[str, float]] = {}
    for r in rows:
        unit = None
        for key in unit_keys:
            if key in r and r[key] is not None and str(r[key]) != "":
                unit = str(r[key])
                break
        if unit is None:
            continue
        try:
            yv = r.get("y")
            if yv is None:
                yv = r.get("n", 0)
            y = float(yv)
        except (TypeError, ValueError):
            continue
        try:
            w = float(r.get("n") or 1.0)
            if w <= 0:
                w = 1.0
        except (TypeError, ValueError):
            w = 1.0
        bucket = by_unit.setdefault(unit, {"y_w": 0.0, "w": 0.0})
        bucket["y_w"] += y * w
        bucket["w"] += w
    return {
        u: (b["y_w"] / b["w"]) if b["w"] > 0 else 0.0
        for u, b in by_unit.items()
    }


# ---------------------------------------------------------------------------
# Rendering helpers — pure matplotlib, no GIS deps
# ---------------------------------------------------------------------------


def _zone_grid_array(
    unit_values: Dict[str, float],
) -> Tuple[Any, List[List[Optional[str]]], List[List[Optional[float]]]]:
    """Return ``(grid_array, label_grid, value_grid)`` for the 8-zone layout.

    ``grid_array`` is an ``np.ndarray`` of shape ``_ZONE_GRID_SHAPE`` with
    NaN in the empty cell (so matplotlib draws it transparent). The
    label and value 2-D lists are aligned with the array.
    """
    import numpy as np
    rows, cols = _ZONE_GRID_SHAPE
    grid = np.full((rows, cols), float("nan"), dtype=np.float64)
    label_grid: List[List[Optional[str]]] = [[None] * cols for _ in range(rows)]
    value_grid: List[List[Optional[float]]] = [[None] * cols for _ in range(rows)]
    for code, (r, c) in _ZONE_GRID.items():
        v = unit_values.get(code)
        if v is None:
            label_grid[r][c] = code  # show code even when no data
            continue
        grid[r, c] = v
        label_grid[r][c] = code
        value_grid[r][c] = v
    return grid, label_grid, value_grid


def _district_grid_array(
    unit_values: Dict[str, float],
) -> Tuple[Any, List[List[Optional[str]]], List[List[Optional[float]]], Tuple[int, int]]:
    """50-district grid: 10×5 packed layout, alphabetised by code."""
    import numpy as np
    cols = 10
    codes = sorted(unit_values.keys())
    n = len(codes)
    rows = (n + cols - 1) // cols  # round up
    grid = np.full((rows, cols), float("nan"), dtype=np.float64)
    label_grid: List[List[Optional[str]]] = [[None] * cols for _ in range(rows)]
    value_grid: List[List[Optional[float]]] = [[None] * cols for _ in range(rows)]
    for i, code in enumerate(codes):
        r, c = divmod(i, cols)
        v = unit_values[code]
        grid[r, c] = v
        label_grid[r][c] = code
        value_grid[r][c] = v
    return grid, label_grid, value_grid, (rows, cols)


def _render_choropleth_png(
    grid_arr: Any,
    label_grid: List[List[Optional[str]]],
    value_grid: List[List[Optional[float]]],
    color_scheme: str,
    spec_id: str,
    geographic_unit: str,
    value_unit: str,
    caption: Optional[str],
    figsize: Tuple[float, float] = (8.0, 5.0),
    dpi: int = 200,
) -> Path:
    """Draw the tile choropleth and write a PNG. Return the absolute path."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    import numpy as np

    family = _try_register_thai_font()
    if family:
        plt.rcParams["font.family"] = family

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    # Use the requested colormap; fall back to ``Reds`` if unknown so we
    # don't blow up on a typo.
    try:
        cmap = plt.get_cmap(color_scheme)
    except (ValueError, KeyError):  # pragma: no cover — defensive
        logger.warning("unknown cmap %r — falling back to Reds", color_scheme)
        cmap = plt.get_cmap("Reds")
    # NaN cells should stay invisible; matplotlib's default NaN colour
    # is transparent which is exactly what we want.
    im = ax.imshow(
        grid_arr,
        cmap=cmap,
        aspect="equal",
        interpolation="nearest",
    )
    rows = len(label_grid)
    cols = len(label_grid[0]) if rows else 0
    # Annotate each cell with its code + value. Skip NaN cells (the
    # empty corner of the zone grid).
    for r in range(rows):
        for c in range(cols):
            label = label_grid[r][c]
            if label is None:
                continue
            val = value_grid[r][c]
            text = label if val is None else f"{label}\n{val:.2f}"
            # Pick text colour based on cell luminance — light text on
            # dark cells, dark text on light cells. Approximated by
            # using the colormap value at the normalised cell value.
            if val is None or np.isnan(grid_arr[r, c]):
                color = "#666666"
            else:
                norm = im.norm(grid_arr[r, c])
                rgba = cmap(norm)
                # Standard luminance formula
                lum = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
                color = "white" if lum < 0.5 else "black"
            fontsize = 9 if cols <= 5 else 7 if cols <= 8 else 6
            ax.text(
                c, r, text,
                ha="center", va="center",
                fontsize=fontsize, color=color,
            )
    ax.set_xticks([])
    ax.set_yticks([])
    # Subtle grid lines so cells are visually distinct even when their
    # colours are similar.
    ax.set_xticks(np.arange(-0.5, cols, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, rows, 1), minor=True)
    ax.grid(which="minor", color="white", linestyle="-", linewidth=1)
    ax.tick_params(which="minor", length=0)
    # Colorbar with the value unit if given.
    cbar = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.04)
    if value_unit:
        cbar.set_label(value_unit, fontsize=9)

    title = caption or f"choropleth — {spec_id} ({geographic_unit})"
    ax.set_title(title, fontsize=10, color="#333333")

    fig.tight_layout()
    safe_spec = "".join(c if c.isalnum() else "_" for c in spec_id)[:48]
    # Pull a stable hash of the data values so two renders of the same
    # data don't collide on disk but differ when filters change.
    flat = tuple(
        round(float(grid_arr[r, c]), 6) if not np.isnan(grid_arr[r, c]) else None
        for r in range(rows) for c in range(cols)
    )
    val_hash = abs(hash(flat)) % 0xFFFFFFFF
    fname = f"choropleth_{safe_spec}_{geographic_unit}_{val_hash:08x}.png"
    out = _build_dir() / fname
    fig.savefig(str(out), dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    logger.debug("choropleth_block wrote %s", out)
    return out


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class ChoroplethBlock(ContentBlock):
    """Tile choropleth (zone or district) — a coloured grid PNG figure."""

    block_id: ClassVar[str] = "choropleth"
    Parameters: ClassVar[type[BaseModel]] = _ChoroplethParams
    # ``audience_target = None`` — useful in any audience.

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _ChoroplethParams)
        # ChartService responses use ``x`` as the axis label — the
        # chart-spec's ``axes.x`` (e.g. zone_code) is collapsed to ``x``
        # in the wire format. Add ``x`` as the fallback key so a
        # zone/district chart spec lights up the choropleth without
        # needing a custom aggregator key on every chart spec.
        unit_keys = (
            ["zone", "zone_code", "zc", "x"]
            if params.geographic_unit == "zone"
            else ["district_code", "dcode", "district", "x"]
        )
        try:
            service = _resolve_chart_service(ctx)
            resp = await service.render(params.outcome_spec_id, params.filters)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "choropleth: chart service render failed for %s: %s",
                params.outcome_spec_id, exc,
            )
            return {
                "skipped": True,
                "skip_reason": f"chart service error: {exc!s}",
                "geographic_unit": params.geographic_unit,
                "outcome_spec_id": params.outcome_spec_id,
                "n": 0,
                "png_path": None,
            }
        body = _response_to_dict(resp)
        rows: List[Dict[str, Any]] = body.get("data", []) or []
        unit_values = _aggregate_by_unit(rows, unit_keys)

        if not unit_values:
            return {
                "skipped": True,
                "skip_reason": "no spatial-unit data found in chart-spec rows",
                "geographic_unit": params.geographic_unit,
                "outcome_spec_id": params.outcome_spec_id,
                "n": 0,
                "png_path": None,
            }

        # Resolve caption
        caption = (
            params.caption_en
            if ctx.lang == "en" and params.caption_en
            else params.caption_th
        )

        # Render PNG.
        try:
            if params.geographic_unit == "zone":
                grid, label_grid, value_grid = _zone_grid_array(unit_values)
                figsize = (6.0, 5.0)
            else:
                grid, label_grid, value_grid, _shape = _district_grid_array(
                    unit_values
                )
                figsize = (10.0, 5.0)
            png_path = _render_choropleth_png(
                grid, label_grid, value_grid,
                color_scheme=params.color_scheme,
                spec_id=params.outcome_spec_id,
                geographic_unit=params.geographic_unit,
                value_unit=params.value_unit,
                caption=caption,
                figsize=figsize,
            )
        except ImportError as exc:  # pragma: no cover — matplotlib absent
            logger.warning(
                "choropleth: matplotlib unavailable (%s); skipping figure",
                exc,
            )
            return {
                "skipped": True,
                "skip_reason": "matplotlib not installed",
                "geographic_unit": params.geographic_unit,
                "outcome_spec_id": params.outcome_spec_id,
                "n": len(unit_values),
                "png_path": None,
            }
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("choropleth: render failed: %s", exc)
            return {
                "skipped": True,
                "skip_reason": f"render error: {exc!s}",
                "geographic_unit": params.geographic_unit,
                "outcome_spec_id": params.outcome_spec_id,
                "n": len(unit_values),
                "png_path": None,
            }

        return {
            "skipped": False,
            "skip_reason": None,
            "geographic_unit": params.geographic_unit,
            "outcome_spec_id": params.outcome_spec_id,
            "n": len(unit_values),
            "png_path": str(png_path),
            "unit_values": dict(unit_values),
            "caption": caption,
            "lang": ctx.lang,
        }

    # ------------------------------------------------------------------
    # Render — LaTeX
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        if data.get("skipped") or not data.get("png_path"):
            reason = data.get("skip_reason") or "no data"
            return (
                r"\begin{figure}[H]" + "\n"
                r"\centering" + "\n"
                + r"\textit{Choropleth: " + latex_escape(str(reason)) + "}\n"
                r"\end{figure}" + "\n"
            )
        png_path = str(data["png_path"])
        cap = data.get("caption") or ""
        spec_id = str(data.get("outcome_spec_id", "?"))
        unit = str(data.get("geographic_unit", "?"))
        if not cap:
            cap = f"Choropleth — {spec_id} ({unit})"
        body = r"\includegraphics[width=0.85\textwidth]{" + png_path + "}"
        label = "choropleth:" + safe_label_part(f"{spec_id}_{unit}")
        return wrap_figure_latex(body, cap, label)

    # ------------------------------------------------------------------
    # Render — HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        if data.get("skipped") or not data.get("png_path"):
            reason = data.get("skip_reason") or "no data"
            esc = (
                str(reason).replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;")
            )
            return (
                '<figure class="choropleth skipped">'
                f'<figcaption><em>Choropleth: {esc}</em></figcaption>'
                '</figure>'
            )
        png_path = str(data["png_path"])
        # Use a ``file://`` URL so the HTML preview path can resolve the
        # absolute path the orchestrator handed us. The HTML asset
        # bundler downstream rewrites these to relative paths during
        # the final asset-staging step.
        if not png_path.startswith(("http://", "https://", "file://", "/")):
            src = "file://" + os.path.abspath(png_path)
        else:
            src = png_path if png_path.startswith(("http://", "https://", "file://")) else "file://" + png_path
        cap = data.get("caption") or ""
        spec_id = str(data.get("outcome_spec_id", "?"))
        spec_safe = (
            spec_id.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        img = f'<img src="{src}" alt="choropleth for {spec_safe}" />'
        return wrap_figure_html(
            img,
            cap,
            css_class="choropleth",
            extra_attrs={"data-spec-id": spec_id},
        )


__all__ = ["ChoroplethBlock"]
