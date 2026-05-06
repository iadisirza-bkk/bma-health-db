"""Matplotlib-based PNG chart fallback for ``ChartBlock``.

Used when pgfplots can't reasonably express the chart kind (heatmap,
scatter, boxplot, choropleth) or when ``REPORT_CHART_BACKEND=matplotlib``
is set in the environment.

Output is written to a process-local temp dir and the absolute path is
returned. The LaTeX block emits ``\\includegraphics{<absolute_path>}``
which Tectonic resolves verbatim — no asset staging needed.

Thai font handling
------------------
matplotlib doesn't auto-discover bundled .ttfs. We probe a small list of
known locations and register the first one we find. If none load, we
fall back to a generic family — Thai labels will then render as boxes
but the chart numbers / axes still work, which is the best we can do
without bundling a font as a hard dependency.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("api.services.reports.blocks._chart_matplotlib")

# Singleton for the build dir — reused across blocks in one process so
# tectonic sees a stable path.
_BUILD_DIR: Optional[Path] = None
# Whether we've already registered a Thai font. We try once per process.
_FONT_REGISTERED = False


def _build_dir() -> Path:
    """Return (and create on first call) a stable temp dir for chart PNGs."""
    global _BUILD_DIR
    if _BUILD_DIR is None:
        _BUILD_DIR = Path(tempfile.gettempdir()) / "bma_chart_png"
        _BUILD_DIR.mkdir(parents=True, exist_ok=True)
    return _BUILD_DIR


def _try_register_thai_font() -> Optional[str]:
    """Try to register GoogleSans (or a Thai-capable fallback) with mpl.

    Returns the family name to use for ``font.family`` / ``rcParams``,
    or ``None`` if no Thai font could be registered (caller falls back
    to system default — labels will boxify but chart still renders).
    """
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return getattr(_try_register_thai_font, "_family", None)

    try:
        import matplotlib.font_manager as fm
    except ImportError:  # pragma: no cover — matplotlib absent
        return None

    # Candidate paths for a Thai-capable .ttf. First match wins.
    here = Path(__file__).resolve()
    repo_root = here.parents[4]  # blocks/_chart_matplotlib.py → repo root
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
                # Pull the family name out of the file's metadata so we
                # can pin rcParams to it precisely.
                prop = fm.FontProperties(fname=str(path))
                family = prop.get_name()
                logger.info("matplotlib chart fallback: registered %s as %s", path, family)
                break
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("could not register %s: %s", path, exc)
    _FONT_REGISTERED = True
    setattr(_try_register_thai_font, "_family", family)
    return family


def _as_floats(rows: List[Dict[str, Any]]) -> Tuple[List[str], List[float]]:
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


def render_to_png(
    chart_kind: str,
    rows: List[Dict[str, Any]],
    spec_id: str,
    caption: Optional[str] = None,
    dpi: int = 200,
) -> Path:
    """Render rows to a PNG file. Returns the absolute Path written.

    ``chart_kind`` selects the plot type. Unknown kinds fall back to a
    horizontal bar chart so we always produce *something* embeddable.
    """
    import matplotlib

    matplotlib.use("Agg", force=False)  # headless — no display
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    family = _try_register_thai_font()
    if family:
        plt.rcParams["font.family"] = family

    bma_green = "#00744B"
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=dpi)

    labels, values = _as_floats(rows)

    if not rows:
        ax.text(
            0.5,
            0.5,
            "ไม่มีข้อมูล",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=14,
            color="#666666",
        )
        ax.set_xticks([])
        ax.set_yticks([])
    elif chart_kind in ("choropleth",):
        # Choropleth has no real coordinates — show a ranked horizontal
        # bar chart of (x, value) pairs. Top 20 only.
        n = min(20, len(values))
        order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)[:n]
        ys = list(reversed([labels[i] for i in order]))
        vs = list(reversed([values[i] for i in order]))
        ax.barh(ys, vs, color=bma_green)
        ax.set_xlabel("value")
    elif chart_kind in ("scatter",):
        xs = list(range(len(values)))
        ax.scatter(xs, values, color=bma_green, s=40)
        ax.set_xticks(xs)
        ax.set_xticklabels(labels, rotation=45 if any(len(l) > 8 for l in labels) else 0, ha="right")
    elif chart_kind in ("boxplot",):
        # Group rows by series if present, else one box per x value.
        ax.boxplot([values], labels=[spec_id])
    elif chart_kind in ("heatmap",):
        # Build a 2D grid keyed by (x, series); falls back to a 1xN row
        # if no series.
        try:
            import numpy as np
            series_keys: List[str] = sorted(
                {str(r.get("series") or "_") for r in rows}
            )
            x_keys: List[str] = []
            for r in rows:
                xs = str(r.get("x", ""))
                if xs not in x_keys:
                    x_keys.append(xs)
            grid = np.zeros((len(series_keys), len(x_keys)))
            for r in rows:
                sx = series_keys.index(str(r.get("series") or "_"))
                xi = x_keys.index(str(r.get("x", "")))
                v = r.get("y", r.get("n", 0)) or 0
                try:
                    grid[sx][xi] = float(v)
                except (TypeError, ValueError):
                    grid[sx][xi] = 0.0
            im = ax.imshow(grid, cmap="Greens", aspect="auto")
            ax.set_xticks(range(len(x_keys)))
            ax.set_xticklabels(x_keys, rotation=45, ha="right")
            ax.set_yticks(range(len(series_keys)))
            ax.set_yticklabels(series_keys)
            fig.colorbar(im, ax=ax)
        except ImportError:  # pragma: no cover — numpy missing
            ax.bar(range(len(values)), values, color=bma_green)
    else:
        # Default: vertical bar
        rotation = 45 if any(len(l) > 8 for l in labels) else 0
        ax.bar(range(len(values)), values, color=bma_green)
        ax.set_xticks(range(len(values)))
        ax.set_xticklabels(labels, rotation=rotation, ha="right" if rotation else "center")
        if values and max(values) >= 1000:
            ax.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _p: f"{int(v):,}")
            )

    if caption:
        ax.set_title(caption, fontsize=10, color="#333333")
    fig.tight_layout()

    # Filename includes spec_id + a short hash of values so two renders
    # of the same chart with different filters don't collide.
    safe_spec = "".join(c if c.isalnum() else "_" for c in spec_id)[:64]
    val_hash = abs(hash(tuple(values))) % 0xFFFFFFFF
    fname = f"chart_{safe_spec}_{val_hash:08x}.png"
    out = _build_dir() / fname
    fig.savefig(str(out), dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    logger.debug("matplotlib chart fallback wrote %s", out)
    return out


def is_matplotlib_available() -> bool:
    """Return True iff matplotlib is importable in this process."""
    try:
        import matplotlib  # noqa: F401
        return True
    except ImportError:
        return False


def is_matplotlib_forced() -> bool:
    """Return True iff REPORT_CHART_BACKEND=matplotlib is set."""
    return os.environ.get("REPORT_CHART_BACKEND", "").lower() == "matplotlib"
