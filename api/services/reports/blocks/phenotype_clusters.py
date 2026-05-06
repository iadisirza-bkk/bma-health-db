"""``phenotype_clusters`` block — PCA + KMeans phenotype discovery (S11).

Multivariate analysis block for the Sprint S11 PhD-grade whitepaper.
Takes a list of lab columns, z-scores them, projects to a 2-D PCA biplot,
and clusters the projection with KMeans (auto-k via silhouette over
2..6 if ``k_clusters`` is None). Renders a 2-panel matplotlib figure:

    * left:  PCA scatter, points colored by cluster, loading arrows
             on the principal-component axes.
    * right: parallel-coordinates plot of cluster centroids across the
             original lab dimensions (z-scored).

Caption appended below: "k=4 phenotypes (silhouette=0.42, n=23,541)".

Data path
---------
The block needs RAW per-patient lab rows — aggregated MV rows can't be
unbinned for PCA. Three injection paths in priority order, all
consulted in ``collect``:

    1. ``ctx.extra["phenotype_rows"]`` — list[dict] supplied by the
       orchestrator or test harness. Each dict has the lab columns
       named in ``params.lab_columns``.
    2. ``ctx.extra["phenotype_provider"]`` — async callable
       ``(lab_columns, filters) -> list[dict]`` for production use
       (the orchestrator wires this against MVRepository / a private
       SQL helper as data becomes available).
    3. Empty list — graceful "ไม่มีข้อมูล" figure.

This avoids touching MVRepository or other agents' files; the
orchestrator owns the wiring decision when raw-row access lands.

Numerics
--------
* Inputs are capped at 100,000 rows (random subsample) — academic
  PCA biplots don't gain readability from a denser cloud.
* Each lab column is z-scored within the surviving rows.
* Rows with NaN in ANY lab column are dropped (listwise).
* Degenerate cases (zero variance, single cluster, <2 distinct rows)
  surface as a friendly figure rather than a crash.

NOTE: this block targets ``AudienceTarget.RESEARCHER`` — the raw PCA
biplot is jargon for clinicians/people audiences, so the orchestrator
filters it out for non-researcher renders.
"""
from __future__ import annotations

import logging
import random
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from services.reports.blocks._chart_matplotlib import (
    _try_register_thai_font,
    is_matplotlib_available,
    _build_dir,
)
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.phenotype_clusters")

# Hard cap on rows fed to PCA — PCA scatter beyond ~100k is point-cloud
# noise; subsample with a fixed seed for reproducibility.
_MAX_ROWS = 100_000
_RNG_SEED = 20260501

# Auto-k sweep range when ``k_clusters`` is None.
_K_RANGE = range(2, 7)  # 2..6 inclusive


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _PhenotypeParams(BaseModel):
    """Parameters for the ``phenotype_clusters`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    lab_columns: List[str]
    filters: Dict[str, Any] = Field(default_factory=dict)
    # ``None`` triggers silhouette sweep over k in 2..6.
    k_clusters: Optional[int] = None
    pca_components: int = 2
    caption_th: Optional[str] = None


# ---------------------------------------------------------------------------
# Data path
# ---------------------------------------------------------------------------


async def _resolve_rows(
    ctx: RenderContext, lab_columns: List[str], filters: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Return raw rows for clustering. Tries injected sources in order:

        1. ``ctx.extra["phenotype_rows"]`` — explicit list (tests / forced)
        2. ``ctx.extra["phenotype_provider"]`` — async callable
        3. Empty list (graceful)
    """
    if ctx.extra:
        explicit = ctx.extra.get("phenotype_rows")
        if isinstance(explicit, list):
            return list(explicit)
        provider = ctx.extra.get("phenotype_provider")
        if callable(provider):
            try:
                rows = await provider(lab_columns, filters)
                return list(rows) if rows else []
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("phenotype_provider failed: %s", exc)
                return []
    return []


def _to_matrix(
    rows: List[Dict[str, Any]], lab_columns: List[str]
) -> Tuple[Any, int]:
    """Drop rows with any NaN, return (np.ndarray of shape (n, d), n)."""
    import numpy as np

    if not rows:
        return np.zeros((0, len(lab_columns))), 0

    raw = []
    for r in rows:
        try:
            row_vals = [float(r.get(col)) for col in lab_columns]
        except (TypeError, ValueError):
            continue
        if any(np.isnan(v) for v in row_vals):
            continue
        raw.append(row_vals)
    if not raw:
        return np.zeros((0, len(lab_columns))), 0
    arr = np.asarray(raw, dtype=float)
    # Cap at _MAX_ROWS via a deterministic random subsample.
    if arr.shape[0] > _MAX_ROWS:
        rng = random.Random(_RNG_SEED)
        idx = rng.sample(range(arr.shape[0]), _MAX_ROWS)
        arr = arr[sorted(idx)]
    return arr, arr.shape[0]


def _z_score(arr: Any) -> Tuple[Any, Any, Any]:
    """Z-score each column. Returns (z_arr, means, stds). Zero-variance
    columns are kept (their z-column becomes all zeros)."""
    import numpy as np

    means = arr.mean(axis=0)
    stds = arr.std(axis=0, ddof=0)
    safe_stds = np.where(stds > 0, stds, 1.0)
    z = (arr - means) / safe_stds
    return z, means, stds


def _compute_clusters(
    z_arr: Any, k_clusters: Optional[int]
) -> Tuple[int, Any, Any, float]:
    """Run KMeans (or sweep over k) on the z-scored matrix.

    Returns ``(k_used, labels_arr, centers_z_arr, silhouette)``.

    Silhouette is computed on a sample (cap 5_000) — the metric is
    O(n²) and exact silhouette on 100k points is wasteful for the
    cluster-quality summary line.
    """
    import numpy as np
    from sklearn.cluster import KMeans

    n = z_arr.shape[0]
    if n < 2:
        return 1, np.zeros(n, dtype=int), z_arr.mean(axis=0, keepdims=True), 0.0

    # If user fixed k, run once. Otherwise sweep 2..6 and pick best
    # silhouette. KMeans n_init kept small — phenotype clusters are
    # robust under inertia minima here.
    candidate_ks: List[int]
    if k_clusters is None:
        max_k = min(max(_K_RANGE), n - 1) if n > 2 else 1
        candidate_ks = [k for k in _K_RANGE if k <= max_k]
        if not candidate_ks:
            candidate_ks = [min(2, max(1, n - 1))]
    else:
        candidate_ks = [max(1, int(k_clusters))]

    best: Tuple[int, Any, Any, float] = (1, np.zeros(n, dtype=int), z_arr.mean(axis=0, keepdims=True), 0.0)
    best_score = -1.0
    for k in candidate_ks:
        if k < 2 or k >= n:
            continue
        try:
            km = KMeans(n_clusters=k, n_init=5, random_state=_RNG_SEED)
            labels = km.fit_predict(z_arr)
            centers = km.cluster_centers_
            sil = _silhouette(z_arr, labels)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("KMeans k=%d failed: %s", k, exc)
            continue
        if sil > best_score:
            best_score = sil
            best = (k, labels, centers, sil)
    return best


def _silhouette(z_arr: Any, labels: Any) -> float:
    """Sampled silhouette (cap 5_000 points for runtime)."""
    import numpy as np
    from sklearn.metrics import silhouette_score

    if len(set(labels)) < 2:
        return 0.0
    n = z_arr.shape[0]
    if n > 5000:
        rng = np.random.default_rng(_RNG_SEED)
        idx = rng.choice(n, size=5000, replace=False)
        sample = z_arr[idx]
        sample_labels = labels[idx]
        if len(set(sample_labels)) < 2:
            return 0.0
        return float(silhouette_score(sample, sample_labels))
    return float(silhouette_score(z_arr, labels))


def _run_pca(z_arr: Any, n_components: int) -> Tuple[Any, Any, Any]:
    """Project to ``n_components`` via PCA. Returns (proj, explained_var, loadings).

    ``loadings`` is the (d × n_components) matrix used for biplot arrow
    drawing — i.e. each lab column's contribution to each principal axis.
    """
    import numpy as np
    from sklearn.decomposition import PCA

    n_samples, d = z_arr.shape
    eff_components = max(1, min(n_components, d, n_samples))
    pca = PCA(n_components=eff_components, random_state=_RNG_SEED)
    proj = pca.fit_transform(z_arr)
    # Pad to requested n_components if PCA gave fewer.
    if proj.shape[1] < n_components:
        pad = np.zeros((proj.shape[0], n_components - proj.shape[1]))
        proj = np.hstack([proj, pad])
    var = list(pca.explained_variance_ratio_)
    while len(var) < n_components:
        var.append(0.0)
    # Loadings: components_ has shape (n_components, d). Transpose for the
    # biplot drawing convention (rows = features, cols = PCs).
    loadings = pca.components_.T
    if loadings.shape[1] < n_components:
        pad = np.zeros((loadings.shape[0], n_components - loadings.shape[1]))
        loadings = np.hstack([loadings, pad])
    return proj, np.asarray(var[:n_components]), loadings[:, :n_components]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _empty_figure(caption: str) -> str:
    """Render the empty/degenerate case as a tiny matplotlib PNG and return
    the LaTeX ``\\includegraphics`` snippet for it. We match the pattern
    used elsewhere (one image, captioned) so the LaTeX flow is identical."""
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
    out = _build_dir() / f"phenotype_empty_{abs(hash(caption)) % 0xFFFFFFFF:08x}.png"
    fig.savefig(str(out), dpi=200, bbox_inches="tight")
    plt.close(fig)
    return str(out)


def _render_figure(
    proj: Any,
    labels: Any,
    centers: Any,
    loadings: Any,
    explained_var: Any,
    lab_columns: List[str],
    cache_token: str,
) -> str:
    """Render the 2-panel PCA biplot + parallel-coords figure. Returns the
    absolute path to the PNG file."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    import numpy as np

    family = _try_register_thai_font()
    if family:
        plt.rcParams["font.family"] = family

    cmap = plt.get_cmap("tab10")
    fig, (ax_pca, ax_pc) = plt.subplots(1, 2, figsize=(12, 5.0), dpi=200)

    # --- Panel 1: PCA biplot -------------------------------------------
    if proj.shape[0] > 0:
        unique = sorted(set(labels.tolist()))
        for ci in unique:
            mask = labels == ci
            ax_pca.scatter(
                proj[mask, 0],
                proj[mask, 1],
                color=cmap(ci % 10),
                s=12,
                alpha=0.55,
                edgecolors="none",
                label=f"C{ci}",
            )
        # Loading arrows — scale to ~80% of plot extent so they stay
        # visually meaningful next to the cloud.
        if loadings.size:
            scale = 0.8 * max(
                float(np.max(np.abs(proj[:, 0])) if proj.size else 1.0),
                float(np.max(np.abs(proj[:, 1])) if proj.size else 1.0),
                1.0,
            )
            for i, name in enumerate(lab_columns):
                lx = float(loadings[i, 0]) * scale
                ly = float(loadings[i, 1]) * scale
                ax_pca.annotate(
                    "",
                    xy=(lx, ly),
                    xytext=(0, 0),
                    arrowprops={"arrowstyle": "->", "color": "#333333", "lw": 1.0},
                )
                ax_pca.text(
                    lx * 1.05,
                    ly * 1.05,
                    name,
                    fontsize=9,
                    color="#222222",
                )
        pc1 = float(explained_var[0]) * 100 if len(explained_var) >= 1 else 0.0
        pc2 = float(explained_var[1]) * 100 if len(explained_var) >= 2 else 0.0
        ax_pca.set_xlabel(f"PC1 ({pc1:.1f}%)")
        ax_pca.set_ylabel(f"PC2 ({pc2:.1f}%)")
        ax_pca.set_title("PCA biplot — phenotype clusters")
        ax_pca.legend(loc="best", fontsize=8, frameon=False)
        ax_pca.axhline(0, color="#888888", lw=0.5)
        ax_pca.axvline(0, color="#888888", lw=0.5)
    else:
        ax_pca.text(0.5, 0.5, "ไม่มีข้อมูล", ha="center", va="center", transform=ax_pca.transAxes)
        ax_pca.set_xticks([])
        ax_pca.set_yticks([])

    # --- Panel 2: parallel-coords of centroids -------------------------
    if centers.size:
        xs = list(range(len(lab_columns)))
        for ci in range(centers.shape[0]):
            ax_pc.plot(
                xs,
                centers[ci, :],
                color=cmap(ci % 10),
                marker="o",
                lw=1.6,
                label=f"C{ci}",
            )
        ax_pc.set_xticks(xs)
        ax_pc.set_xticklabels(lab_columns, rotation=30, ha="right")
        ax_pc.set_ylabel("z-score")
        ax_pc.axhline(0, color="#888888", lw=0.5)
        ax_pc.set_title("Cluster centroids — parallel coordinates")
        ax_pc.legend(loc="best", fontsize=8, frameon=False, ncol=2)
    else:
        ax_pc.text(0.5, 0.5, "ไม่มีข้อมูล", ha="center", va="center", transform=ax_pc.transAxes)
        ax_pc.set_xticks([])
        ax_pc.set_yticks([])

    fig.tight_layout()
    out = _build_dir() / f"phenotype_clusters_{cache_token}.png"
    fig.savefig(str(out), dpi=200, bbox_inches="tight")
    plt.close(fig)
    return str(out)


# ---------------------------------------------------------------------------
# LaTeX / HTML helpers
# ---------------------------------------------------------------------------


def _summary_line(k: int, sil: float, n: int, lang: str) -> str:
    if lang == "en":
        return f"k={k} phenotypes (silhouette={sil:.2f}, n={n:,})"
    return f"k={k} phenotype (silhouette={sil:.2f}, n={n:,})"


def _wrap_figure_latex(png_path: str, caption: str, summary: str) -> str:
    """Compose phenotype-clusters body (image + summary line) and route
    through the shared caption-below wrapper. ``summary`` (k value,
    silhouette, n) sits BETWEEN the image and the caption — embedded in
    the body argument so the helper still emits ``\\caption`` last."""
    from services.latex_utils import latex_escape
    from services.reports.blocks._render_helpers import wrap_figure_latex

    body = (
        f"\\includegraphics[width=0.95\\textwidth]{{{png_path}}}\n"
        f"\\par\\smallskip\\textit{{{latex_escape(summary)}}}"
    )
    return wrap_figure_latex(body, caption, "phenotype:clusters")


def _wrap_figure_html(png_path: str, caption: str, summary: str) -> str:
    """Compose phenotype-clusters HTML body (image + summary paragraph)
    and route through the shared caption-below wrapper. The summary
    ``<p>`` sits BETWEEN the image and ``<figcaption>`` — embedded in
    the body argument."""
    from services.reports.blocks._render_helpers import wrap_figure_html

    summ = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = (
        f'<img src="file://{png_path}" alt="phenotype clusters PCA biplot" />'
        f"<p><em>{summ}</em></p>"
    )
    return wrap_figure_html(
        body,
        caption,
        css_class="phenotype-clusters",
    )


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class PhenotypeClustersBlock(ContentBlock):
    """PCA biplot + KMeans phenotype discovery (S11 — RESEARCHER audience)."""

    block_id: ClassVar[str] = "phenotype_clusters"
    Parameters: ClassVar[type[BaseModel]] = _PhenotypeParams
    audience_target: ClassVar[Optional[AudienceTarget]] = AudienceTarget.RESEARCHER

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _PhenotypeParams)
        if not is_matplotlib_available():
            return {
                "n": 0,
                "k": 0,
                "silhouette": 0.0,
                "pca_explained": [0.0, 0.0],
                "biplot_points": [],
                "loadings": [],
                "cluster_centers": [],
                "cluster_profiles": {},
                "lab_columns": list(params.lab_columns),
                "caption": params.caption_th or "Phenotype clusters",
                "empty_reason": "matplotlib unavailable",
            }

        import numpy as np

        lab_columns = list(params.lab_columns)
        rows = await _resolve_rows(ctx, lab_columns, params.filters)
        arr, n = _to_matrix(rows, lab_columns)

        # Caption defaults
        if ctx.lang == "en":
            caption = params.caption_th or "Phenotype clusters (PCA + KMeans)"
        else:
            caption = params.caption_th or "ฟีโนไทป์การคัดกรอง (PCA + KMeans)"

        if n < 2 or arr.shape[1] == 0:
            return {
                "n": int(n),
                "k": 0,
                "silhouette": 0.0,
                "pca_explained": [0.0] * params.pca_components,
                "biplot_points": [],
                "loadings": [],
                "cluster_centers": [],
                "cluster_profiles": {},
                "lab_columns": lab_columns,
                "caption": caption,
                "empty_reason": "no data" if n == 0 else "insufficient data",
            }

        z_arr, means, stds = _z_score(arr)
        proj, explained, loadings = _run_pca(z_arr, params.pca_components)
        k_used, labels, centers_z, sil = _compute_clusters(z_arr, params.k_clusters)

        # Profile: mean of (raw) values per cluster, per lab column.
        profiles: Dict[str, List[float]] = {}
        for ci in range(int(centers_z.shape[0])):
            mask = labels == ci
            if not mask.any():
                profiles[f"C{ci}"] = [0.0] * len(lab_columns)
                continue
            profiles[f"C{ci}"] = [
                float(arr[mask, j].mean()) for j in range(arr.shape[1])
            ]

        biplot_points = [
            {
                "pc1": float(proj[i, 0]),
                "pc2": float(proj[i, 1]) if proj.shape[1] > 1 else 0.0,
                "cluster": int(labels[i]),
            }
            for i in range(min(proj.shape[0], 5000))  # cap serialisation
        ]
        loadings_list = [
            {
                "name": lab_columns[i],
                "pc1": float(loadings[i, 0]),
                "pc2": float(loadings[i, 1]) if loadings.shape[1] > 1 else 0.0,
            }
            for i in range(loadings.shape[0])
        ]

        return {
            "n": int(n),
            "k": int(k_used),
            "silhouette": float(sil),
            "pca_explained": [float(v) for v in list(explained)],
            "biplot_points": biplot_points,
            "loadings": loadings_list,
            "cluster_centers": [
                [float(centers_z[i, j]) for j in range(centers_z.shape[1])]
                for i in range(centers_z.shape[0])
            ],
            "cluster_profiles": profiles,
            "lab_columns": lab_columns,
            "caption": caption,
            # Internal — not part of the public payload contract but
            # used by the renderer to avoid recomputing PCA there. Kept
            # under a leading underscore key so dict consumers can drop it.
            "_proj": proj,
            "_labels": labels,
            "_centers_z": centers_z,
            "_loadings_arr": loadings,
            "_explained_arr": explained,
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
                r"\textit{[phenotype_clusters: matplotlib unavailable — "
                + latex_escape(str(data.get("caption", "")))
                + "]}\n"
            )
        caption = str(data.get("caption", ""))
        n = int(data.get("n", 0))
        k = int(data.get("k", 0))
        sil = float(data.get("silhouette", 0.0))
        if n < 2 or k < 1:
            png = _empty_figure(caption or "phenotype_clusters")
            summary = _summary_line(k, sil, n, ctx.lang)
            return _wrap_figure_latex(png, caption, summary)
        proj = data["_proj"]
        labels = data["_labels"]
        centers = data["_centers_z"]
        loadings = data["_loadings_arr"]
        explained = data["_explained_arr"]
        lab_columns = list(data.get("lab_columns", []))
        cache_token = f"{n}_{k}_{abs(hash(tuple(lab_columns))) % 0xFFFFFFFF:08x}"
        png = _render_figure(
            proj, labels, centers, loadings, explained, lab_columns, cache_token
        )
        summary = _summary_line(k, sil, n, ctx.lang)
        return _wrap_figure_latex(png, caption, summary)

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
                '<figure class="phenotype-clusters error">'
                "<p><em>matplotlib unavailable</em></p></figure>"
            )
        caption = str(data.get("caption", ""))
        n = int(data.get("n", 0))
        k = int(data.get("k", 0))
        sil = float(data.get("silhouette", 0.0))
        if n < 2 or k < 1:
            png = _empty_figure(caption or "phenotype_clusters")
            summary = _summary_line(k, sil, n, ctx.lang)
            return _wrap_figure_html(png, caption, summary)
        proj = data["_proj"]
        labels = data["_labels"]
        centers = data["_centers_z"]
        loadings = data["_loadings_arr"]
        explained = data["_explained_arr"]
        lab_columns = list(data.get("lab_columns", []))
        cache_token = f"{n}_{k}_{abs(hash(tuple(lab_columns))) % 0xFFFFFFFF:08x}"
        png = _render_figure(
            proj, labels, centers, loadings, explained, lab_columns, cache_token
        )
        summary = _summary_line(k, sil, n, ctx.lang)
        return _wrap_figure_html(png, caption, summary)
