"""``spatial_autocorr`` block — Moran's I + LISA hotspot table (S11).

Sprint S11 ("PhD-grade Whitepaper") — academic-grade spatial-statistics
section. Reports global Moran's I (with permutation p-value) and a
table of locally-significant LISA outliers (HH / LL / HL / LH) for one
chart-spec outcome aggregated to either the 8 BMA health zones or the
50 districts.

Render contract: ``audience_target = AudienceTarget.RESEARCHER`` so the
audience-routing layer drops this section unless ``?audience=researcher``
(or no audience filter) is set.

Data path
---------
The block calls the chart-service layer (same as ``ChartBlock``) with
the configured ``outcome_spec_id`` and groups the returned rows by the
``geographic_unit`` (zone code or district code) extracted from each
row. Aggregation: simple mean of the ``y`` values within each unit
(weighted by ``n`` if both are present, otherwise straight mean). For
spatial autocorrelation we only need one number per unit.

Bangkok-specific note
---------------------
Today only ``geographic_unit='zone'`` ships — district-level queen
contiguity needs a real geojson polygon set, which is deferred. With
``zone`` we use the hand-coded :data:`ZONE_ADJACENCY` from
:mod:`_spatial_helpers`. Pulled out behind a ``Literal`` so a future
sprint can add ``"district"`` without breaking the wire surface.

References (for the methodology block + the appendix bibliography):
* Moran, P.A.P. (1950). *Biometrika* 37: 17-23.
* Anselin, L. (1995). *Geographical Analysis* 27(2): 93-115.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks._spatial_helpers import (
    QUADRANT_LABELS_EN,
    QUADRANT_LABELS_TH,
    ZONE_ADJACENCY,
    lisa,
    morans_i,
    queen_contiguity_w,
)
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger("api.services.reports.blocks.spatial_autocorr")


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _SpatialAutocorrParams(BaseModel):
    """Parameters for the ``spatial_autocorr`` block.

    ``outcome_spec_id`` is the chart-spec id whose ``y`` values are
    aggregated per zone / district — same id you'd pass to a ``chart``
    block. The block does NOT re-implement the SQL; it goes through the
    same ``ChartService`` layer that other blocks use, then collapses
    the rows into one value per spatial unit.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    outcome_spec_id: str
    geographic_unit: Literal["zone", "district"] = "zone"
    filters: Dict[str, Any] = Field(default_factory=dict)
    n_perm: int = 999
    alpha: float = 0.05
    random_state: Optional[int] = None
    caption_th: Optional[str] = None


# Minimum number of spatial units below which we skip the test — Moran's
# I is undefined for n=1 and uninformative for very small n. Five is
# conventional in the spatial-statistics literature (and matches the
# k-anonymity floor everywhere else in this codebase).
_MIN_SPATIAL_UNITS = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _resolve_chart_service(ctx: RenderContext) -> Any:
    """Return a ChartService instance, mirroring ``ChartBlock``.

    Honours ``ctx.extra["chart_service"]`` first (test injection),
    otherwise falls back to the global registry.
    """
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
    """Aggregate chart rows by spatial-unit code → mean ``y`` value.

    Each row should have a unit code in one of the ``unit_keys`` keys
    (we accept multiple key names — different chart specs use ``zone``,
    ``zone_code``, ``dcode``, ``district``, etc.). Rows missing every
    key are silently dropped. Within a unit, ``y`` values are averaged
    using ``n`` weights when both are present (else simple mean).
    """
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
            y_val = r.get("y")
            if y_val is None:
                y_val = r.get("n", 0)
            y = float(y_val)
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
# Block
# ---------------------------------------------------------------------------


class SpatialAutocorrBlock(ContentBlock):
    """Global Moran's I + LISA outlier table for one outcome × geo unit."""

    block_id: ClassVar[str] = "spatial_autocorr"
    Parameters: ClassVar[type[BaseModel]] = _SpatialAutocorrParams
    audience_target: ClassVar[Optional[AudienceTarget]] = AudienceTarget.RESEARCHER

    # ------------------------------------------------------------------
    # collect
    # ------------------------------------------------------------------

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _SpatialAutocorrParams)

        # Today only ``zone`` is implemented — district-level adjacency
        # requires loading geojson polygons which is deferred to a
        # future sprint. We keep the wire surface ready so adding
        # ``"district"`` is a one-line ``adjacency = DISTRICT_ADJACENCY``
        # change.
        if params.geographic_unit == "zone":
            adjacency = ZONE_ADJACENCY
            # Same fallback as choropleth_block: chart specs surface their
            # x-axis column as "x" in the wire format. Allow it as a key.
            unit_keys = ["zone", "zone_code", "zc", "x"]
        else:
            return {
                "skipped": True,
                "skip_reason": (
                    "geographic_unit='district' requires geojson "
                    "polygons which are not yet wired"
                ),
                "n": 0,
                "geographic_unit": params.geographic_unit,
                "outcome_spec_id": params.outcome_spec_id,
                "global_I": None,
                "lisa_rows": [],
            }

        # Pull the chart-spec rows once.
        try:
            service = _resolve_chart_service(ctx)
            resp = await service.render(params.outcome_spec_id, params.filters)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "spatial_autocorr: chart service render failed for %s: %s",
                params.outcome_spec_id, exc,
            )
            return {
                "skipped": True,
                "skip_reason": f"chart service error: {exc!s}",
                "n": 0,
                "geographic_unit": params.geographic_unit,
                "outcome_spec_id": params.outcome_spec_id,
                "global_I": None,
                "lisa_rows": [],
            }
        body = _response_to_dict(resp)
        rows: List[Dict[str, Any]] = body.get("data", []) or []
        unit_values = _aggregate_by_unit(rows, unit_keys)

        # Normalize zero-padded zone codes ("01") so they match
        # ZONE_ADJACENCY which keys on integer-stringified codes ("1").
        # We try both forms and keep whichever one matches the W
        # matrix labels — saves callers from having to know the
        # internal convention.
        if params.geographic_unit == "zone":
            normalized: Dict[str, float] = {}
            for k, v in unit_values.items():
                key_int = str(int(k)) if k.isdigit() else k
                normalized[key_int] = v
            unit_values = normalized

        # Build the W matrix — labels is the canonical row order.
        W, labels = queen_contiguity_w(adjacency)
        # values vector aligned with labels — units missing from the
        # chart data fall back to 0.0 (mean would bias things less but
        # we lose the "missing" signal; explicit 0.0 keeps the W-matrix
        # alignment trivial. Document this for audit.)
        values = np.array(
            [unit_values.get(label, 0.0) for label in labels],
            dtype=np.float64,
        )
        n_present = sum(1 for label in labels if label in unit_values)

        if n_present < _MIN_SPATIAL_UNITS:
            return {
                "skipped": True,
                "skip_reason": (
                    f"insufficient spatial units ({n_present} of "
                    f"{_MIN_SPATIAL_UNITS} required)"
                ),
                "n": n_present,
                "geographic_unit": params.geographic_unit,
                "outcome_spec_id": params.outcome_spec_id,
                "global_I": None,
                "lisa_rows": [],
            }

        # Global Moran's I.
        I_obs, I_exp, I_p = morans_i(
            values, W,
            n_perm=params.n_perm,
            random_state=params.random_state,
        )

        # LISA per location.
        lisa_out = lisa(
            values, W,
            n_perm=params.n_perm,
            random_state=params.random_state,
            alpha=params.alpha,
        )

        labels_table = (
            QUADRANT_LABELS_EN if ctx.lang == "en" else QUADRANT_LABELS_TH
        )

        # Compose lisa_rows — one entry per spatial unit. The renderers
        # filter to significant outliers but the data payload keeps
        # everything so callers can show the full Moran scatter if they
        # want.
        lisa_rows: List[Dict[str, Any]] = []
        for i, label in enumerate(labels):
            quadrant_int = int(lisa_out["quadrant"][i])
            lisa_rows.append({
                "unit_code": label,
                "value": float(values[i]),
                "Ii": float(lisa_out["Ii"][i]),
                "p_value": float(lisa_out["p_values"][i]),
                "quadrant": quadrant_int,
                "quadrant_label": labels_table.get(quadrant_int, str(quadrant_int)),
                "is_significant": bool(lisa_out["is_significant"][i]),
            })

        return {
            "skipped": False,
            "skip_reason": None,
            "n": int(values.shape[0]),
            "geographic_unit": params.geographic_unit,
            "outcome_spec_id": params.outcome_spec_id,
            "alpha": float(params.alpha),
            "n_perm": int(params.n_perm),
            "global_I": {
                "I": float(I_obs),
                "expected": float(I_exp),
                "p_value": float(I_p),
            },
            "lisa_rows": lisa_rows,
            "lang": ctx.lang,
            "caption_th": params.caption_th,
        }

    # ------------------------------------------------------------------
    # Render — HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        if data.get("skipped"):
            reason = data.get("skip_reason") or "skipped"
            return (
                '<section class="spatial-autocorr skipped">'
                '<p class="empty"><em>'
                f"Spatial autocorrelation: {_html_escape(str(reason))}"
                '</em></p></section>'
            )
        lang = data.get("lang", "th")
        caption = data.get("caption_th") or self._default_caption(data, lang)
        gI = data.get("global_I", {}) or {}
        I = gI.get("I")
        E = gI.get("expected")
        p = gI.get("p_value")
        sig_marker = "*" if (p is not None and p < float(data.get("alpha", 0.05))) else ""

        # Significant LISA rows only — the data dict has all of them
        # but the rendered table lists outliers.
        lisa_rows: List[Dict[str, Any]] = data.get("lisa_rows", []) or []
        sig_rows = [r for r in lisa_rows if r.get("is_significant")]

        title_th = "การกระจุกตัวเชิงพื้นที่ (Moran's I + LISA)"
        title_en = "Spatial autocorrelation (Moran's I + LISA)"
        title = title_en if lang == "en" else title_th

        global_label = "Global Moran's I"
        if lang != "en":
            global_label = "Moran's I ทั้งภาพรวม"
        n_label = "n" if lang == "en" else "จำนวนหน่วยพื้นที่"
        # Localised tone for the global stats summary.
        intro = (
            f"<p>{_html_escape(global_label)}: "
            f"<strong>{I:.4f}</strong> "
            f"(E[I] = {E:.4f}, p = {p:.4f}{sig_marker}, "
            f"{n_label} = {data.get('n', 0)}, "
            f"perms = {data.get('n_perm', 0)})</p>"
        )

        if not sig_rows:
            no_sig = (
                "ไม่พบจุดที่มีนัยสำคัญทางสถิติ"
                if lang != "en"
                else "No locally-significant LISA clusters at the chosen alpha."
            )
            body = f'<p class="empty"><em>{_html_escape(no_sig)}</em></p>'
        else:
            head = (
                "<thead><tr>"
                f"<th>{_html_escape('Zone' if lang == 'en' else 'เขตสุขภาพ')}</th>"
                f"<th>{_html_escape('Value' if lang == 'en' else 'ค่า')}</th>"
                f"<th>{_html_escape('I_i')}</th>"
                f"<th>{_html_escape('p')}</th>"
                f"<th>{_html_escape('Cluster type' if lang == 'en' else 'ประเภทกลุ่ม')}</th>"
                "</tr></thead>"
            )
            tr_rows: List[str] = []
            for r in sig_rows:
                tr_rows.append(
                    "<tr>"
                    f"<td>{_html_escape(str(r['unit_code']))}</td>"
                    f"<td>{r['value']:.3f}</td>"
                    f"<td>{r['Ii']:.3f}</td>"
                    f"<td>{r['p_value']:.4f}</td>"
                    f"<td>{_html_escape(str(r['quadrant_label']))}</td>"
                    "</tr>"
                )
            body = (
                f'<table class="spatial-autocorr lisa">{head}'
                f'<tbody>{"".join(tr_rows)}</tbody></table>'
            )

        return (
            f'<section class="spatial-autocorr">'
            f'<h3>{_html_escape(title)}</h3>'
            f'{intro}'
            f'<p class="caption"><em>{_html_escape(caption)}</em></p>'
            f'{body}'
            '</section>'
        )

    # ------------------------------------------------------------------
    # Render — LaTeX
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        if data.get("skipped"):
            reason = data.get("skip_reason") or "skipped"
            return (
                r"\subsection*{Moran's I}" + "\n"
                + r"\textit{Spatial autocorrelation: "
                + latex_escape(str(reason)) + "}\n"
            )
        lang = data.get("lang", "th")
        caption = data.get("caption_th") or self._default_caption(data, lang)
        gI = data.get("global_I", {}) or {}
        I = gI.get("I")
        E = gI.get("expected")
        p = gI.get("p_value")
        sig_marker = (
            "$^{*}$"
            if (p is not None and p < float(data.get("alpha", 0.05)))
            else ""
        )

        lisa_rows: List[Dict[str, Any]] = data.get("lisa_rows", []) or []
        sig_rows = [r for r in lisa_rows if r.get("is_significant")]

        title_th = "การกระจุกตัวเชิงพื้นที่ (Moran's I + LISA)"
        title_en = "Spatial autocorrelation (Moran's I + LISA)"
        title = title_en if lang == "en" else title_th

        out: List[str] = [r"\subsection*{" + latex_escape(title) + "}"]
        # Global Moran's I summary as a paragraph.
        global_summary = (
            "Global Moran's I"
            + (" ทั้งภาพรวม" if lang != "en" else "")
            + f" = \\textbf{{{I:.4f}}} (E[I] = {E:.4f}, p = {p:.4f}{sig_marker}, "
            + f"n = {data.get('n', 0)}, perms = {data.get('n_perm', 0)})."
        )
        out.append(global_summary)
        out.append(r"\par\smallskip")
        out.append(r"\textit{" + latex_escape(caption) + r"}")
        out.append(r"\par\smallskip")

        if not sig_rows:
            no_sig = (
                "ไม่พบจุดที่มีนัยสำคัญทางสถิติ"
                if lang != "en"
                else "No locally-significant LISA clusters at the chosen alpha."
            )
            out.append(r"\textit{" + latex_escape(no_sig) + r"}")
            return "\n".join(out) + "\n"

        zone_h = "Zone" if lang == "en" else "เขตสุขภาพ"
        val_h = "Value" if lang == "en" else "ค่า"
        cluster_h = "Cluster type" if lang == "en" else "ประเภทกลุ่ม"
        out.append(r"\begin{tabular}{l|r|r|r|l}")
        out.append(r"\toprule")
        out.append(
            r"\textbf{" + latex_escape(zone_h) + "} & "
            + r"\textbf{" + latex_escape(val_h) + "} & "
            + r"\textbf{$I_i$} & \textbf{p} & "
            + r"\textbf{" + latex_escape(cluster_h) + r"} \\"
        )
        out.append(r"\midrule")
        for r in sig_rows:
            out.append(
                latex_escape(str(r["unit_code"]))
                + " & " + f"{r['value']:.3f}"
                + " & " + f"{r['Ii']:.3f}"
                + " & " + f"{r['p_value']:.4f}"
                + " & " + latex_escape(str(r["quadrant_label"]))
                + r" \\"
            )
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_caption(data: Dict[str, Any], lang: str) -> str:
        spec = str(data.get("outcome_spec_id", "?"))
        unit = str(data.get("geographic_unit", "?"))
        if lang == "en":
            return (
                f"Moran's I + LISA permutation test for outcome "
                f"'{spec}' aggregated to {unit} level."
            )
        return (
            f"การทดสอบ Moran's I + LISA "
            f"(การกระจุกตัวเชิงพื้นที่) ของตัวชี้วัด '{spec}' "
            f"จำแนกระดับ{unit}"
        )


__all__ = ["SpatialAutocorrBlock"]
