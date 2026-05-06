"""``audience_summary_executive`` block — exec-facing dashboard summary.

Sprint S8 ("Audience-Segmented Report Sections") block #2 of 4. Target
reader is a BMA executive (ผู้บริหาร) — the block is a tight
KPI-tile + priority-list combo with data-driven 1-line action
recommendations.

Layout
------
1. **Top:** three KPI tiles (numeric headline + label + delta-arrow when
   we can compute one against an explicit target).
2. **Middle:** "เขตที่ต้องเร่งดำเนินการ" — top 3 districts ranked by the
   weighted-mean at-risk percentage across the city's diseases.
3. **Bottom:** 1-line action recommendations — templated against the
   KPI tiles + the priority districts so the recommendations stay
   data-driven (not boilerplate).

Data sources are the same as :mod:`audience_summary_people` —
``ctx.data_collector.data()`` (legacy ``ReportData`` dataclass / dict
fallback). The executive block does NOT call
``format_count_per_10`` — exec audiences want raw percentages, not
"3 ใน 10 คน" phrasing.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger(
    "api.services.reports.blocks.audience_summary_executive"
)


# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------

# BMA's published 2026 target = 80% screening coverage. Hard-coded here
# (rather than in the descriptor) because it's the only value the spec
# names; if it ever changes, it changes in one place.
_DEFAULT_SCREENING_TARGET_PCT = 80.0

# Top-N districts to call out as "must-act-now" priorities.
_PRIORITY_TOP_N = 3

# A block of city is considered "needing follow-up" if its
# weighted-mean at-risk pct exceeds this threshold. Same scale as the
# people block's _YELLOW_MAX_PCT — kept distinct because the executive
# context interprets the same number differently.
_FOLLOWUP_PCT_THRESHOLD = 15.0


class _AudienceSummaryExecutiveParams(BaseModel):
    """Parameters for the ``audience_summary_executive`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    filters: Optional[Dict[str, Any]] = Field(default=None)


# ---------------------------------------------------------------------------
# Filtering + aggregation (mirrors the people block's helpers — keeping
# them inline rather than refactoring to a shared module so each block
# stays readable on its own; if a third audience block needs the SAME
# filter contract we'll factor it out at that point).
# ---------------------------------------------------------------------------


def _apply_filters(
    district_data: Dict[str, Any],
    filters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not filters:
        return district_data
    out: Dict[str, Any] = {}
    want_district = filters.get("district_code")
    want_zone = filters.get("zone_code")
    for dcode, dinfo in district_data.items():
        if want_district is not None and str(dcode) != str(want_district):
            continue
        if want_zone is not None and str(
            dinfo.get("zone_code", "")
        ) != str(want_zone):
            continue
        out[dcode] = dinfo
    return out


def _district_at_risk_pct(dinfo: Dict[str, Any]) -> Optional[float]:
    """Compute one weighted-mean at-risk percent for a district.

    Aggregates across the diseases the MV exposes for the district. A
    district with no disease columns returns ``None`` so the priority
    ranking can skip it instead of treating it as "0% at-risk".
    """
    diseases = dinfo.get("diseases") or {}
    pcts: List[float] = []
    for payload in diseases.values():
        try:
            pcts.append(float(payload.get("pct_at_risk", 0)))
        except (TypeError, ValueError):
            continue
    if not pcts:
        return None
    return sum(pcts) / len(pcts)


def _aggregate_kpis(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the three exec KPI tile values from a filtered scope.

    ``at_risk_pct`` is the weighted-mean at-risk percentage across the
    whole scope (district pct weighted by district screened count).

    ``followup_pct`` is the share of *districts* whose weighted-mean
    at-risk pct exceeds the follow-up threshold. The exec audience reads
    this as "X% of zones need attention", which is the right
    operational framing.
    """
    total_screened = 0
    weighted_sum = 0.0
    weight = 0
    n_followup = 0
    n_eligible = 0
    for dinfo in scope.values():
        ts = int(dinfo.get("total_screened", 0) or 0)
        total_screened += ts
        d_pct = _district_at_risk_pct(dinfo)
        if d_pct is None:
            continue
        n_eligible += 1
        if ts > 0:
            weighted_sum += d_pct * ts
            weight += ts
        if d_pct >= _FOLLOWUP_PCT_THRESHOLD:
            n_followup += 1
    at_risk_pct = (weighted_sum / weight) if weight > 0 else 0.0
    followup_pct = (
        (100.0 * n_followup / n_eligible) if n_eligible > 0 else 0.0
    )
    return {
        "total_screened": total_screened,
        "at_risk_pct": round(at_risk_pct, 1),
        "followup_pct": round(followup_pct, 1),
    }


def _priority_districts(
    scope: Dict[str, Any], n: int = _PRIORITY_TOP_N
) -> List[Dict[str, Any]]:
    """Return the top-N districts by weighted-mean at-risk percentage."""
    rows: List[Dict[str, Any]] = []
    for dcode, dinfo in scope.items():
        pct = _district_at_risk_pct(dinfo)
        if pct is None:
            continue
        rows.append(
            {
                "district_code": str(dcode),
                "district_name": str(
                    dinfo.get("name_th") or dinfo.get("district_name") or dcode
                ),
                "at_risk_pct": round(pct, 1),
            }
        )
    rows.sort(key=lambda r: r["at_risk_pct"], reverse=True)
    return rows[:n]


def _build_recommendations(
    kpis: Dict[str, Any],
    priorities: List[Dict[str, Any]],
    target_pct: float,
) -> List[str]:
    """Render 1-line, data-driven action recommendations.

    Each recommendation is templated against the KPI tile values and
    the top-3 districts so the exec sees concrete next steps, not
    generic boilerplate.
    """
    lines: List[str] = []
    # Screening-coverage gap: only call out a recommendation when the
    # gap is non-trivial.
    # NOTE: ``total_screened`` is a count, not a percent. The gap
    # recommendation is gated on the priority list rather than a
    # percent-coverage value (which we don't have without population).
    if priorities:
        names = ", ".join(p["district_name"] for p in priorities[:2])
        lines.append(
            f"เพิ่มการคัดกรองในเขต {names} ซึ่งมีอัตราเสี่ยงสูงสุด"
        )
    # Follow-up gap.
    if kpis["followup_pct"] >= 25.0:
        lines.append(
            f"จัดทีมติดตามเชิงรุก: {kpis['followup_pct']:.0f}% ของเขต "
            "มีระดับความเสี่ยงเกินเกณฑ์"
        )
    # At-risk gap (citywide).
    if kpis["at_risk_pct"] > target_pct / 4.0:  # heuristic upper bound
        lines.append(
            "พิจารณาขยายงบประมาณการป้องกันโรคไม่ติดต่อ (NCD prevention)"
        )
    # Always end with a generic monitoring nudge — keeps the section
    # non-empty even if the data is calm enough not to trigger any of
    # the specific recommendations above.
    lines.append("ติดตามตัวชี้วัดรายไตรมาสเพื่อปรับนโยบายให้ทันสถานการณ์")
    return lines


def _delta_arrow(actual: float, target: float) -> str:
    """Return ``"▲"`` / ``"▼"`` / ``""`` depending on direction.

    The arrow is emitted in BOTH renderers — Unicode triangles render
    fine in HTML and in xelatex with the Sarabun font (S7 baseline).
    """
    if actual > target + 0.05:
        return "▲"
    if actual < target - 0.05:
        return "▼"
    return ""  # within ε of target — no arrow


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class AudienceSummaryExecutiveBlock(ContentBlock):
    """Exec-facing dashboard summary — KPIs, priority districts, actions."""

    block_id: ClassVar[str] = "audience_summary_executive"
    Parameters: ClassVar[type[BaseModel]] = _AudienceSummaryExecutiveParams
    audience_target: ClassVar[Optional[AudienceTarget]] = (
        AudienceTarget.EXECUTIVE
    )

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _AudienceSummaryExecutiveParams)
        getter = getattr(ctx.data_collector, "data", None)
        bag: Any = getter() if callable(getter) else (getter or {})
        district_data = self._read(bag, "district_data") or {}
        scope = _apply_filters(district_data, params.filters)
        kpis = _aggregate_kpis(scope)
        priorities = _priority_districts(scope)
        target_pct = _DEFAULT_SCREENING_TARGET_PCT
        # Gap-vs-target — we don't have population per district reliably,
        # so we surface the GAP between observed at-risk pct and the
        # 80% screening target as a coverage proxy. This is intentional:
        # exec dashboards normally show "X% to go", not absolute counts.
        # The arrow points the right way for at-risk (lower = better),
        # so we INVERT the comparison.
        screening_arrow = _delta_arrow(
            target_pct - kpis["at_risk_pct"],  # "headroom"
            target_pct,
        )
        recommendations = _build_recommendations(
            kpis, priorities, target_pct
        )
        return {
            "kpis": [
                {
                    "label_th": "ผู้คัดกรองทั้งหมด",
                    "value_str": f"{kpis['total_screened']:,}",
                    "raw": kpis["total_screened"],
                    "delta": "",
                    "kind": "count",
                },
                {
                    "label_th": "อัตราเสี่ยงเฉลี่ย",
                    "value_str": f"{kpis['at_risk_pct']:.1f}%",
                    "raw": kpis["at_risk_pct"],
                    "delta": screening_arrow,
                    "kind": "pct",
                },
                {
                    "label_th": "เขตต้องติดตาม",
                    "value_str": f"{kpis['followup_pct']:.1f}%",
                    "raw": kpis["followup_pct"],
                    "delta": "▲" if kpis["followup_pct"] > 0 else "",
                    "kind": "pct",
                },
            ],
            "priorities": priorities,
            "recommendations": recommendations,
            "target_pct": target_pct,
            "filters": params.filters or {},
        }

    @staticmethod
    def _read(bag: Any, key: str) -> Any:
        if isinstance(bag, dict):
            return bag.get(key)
        return getattr(bag, key, None)

    # ------------------------------------------------------------------
    # HTML — KPI row + priority list + recommendations
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        # KPI tiles
        tiles: List[str] = []
        for tile in data["kpis"]:
            label = _html_escape(str(tile["label_th"]))
            value = _html_escape(str(tile["value_str"]))
            delta = _html_escape(str(tile.get("delta") or ""))
            tiles.append(
                '<div class="exec-kpi-tile">'
                f'<div class="exec-kpi-value">{value} {delta}</div>'
                f'<div class="exec-kpi-label">{label}</div>'
                "</div>"
            )
        kpi_html = (
            '<div class="audience-summary-exec-kpis">'
            + "".join(tiles)
            + "</div>"
        )
        # Priority districts
        if data["priorities"]:
            items = "".join(
                f'<li><strong>{_html_escape(p["district_name"])}</strong>'
                f' &mdash; {p["at_risk_pct"]:.1f}%</li>'
                for p in data["priorities"]
            )
            prio_html = (
                '<div class="audience-summary-exec-priorities">'
                "<h4>เขตที่ต้องเร่งดำเนินการ</h4>"
                f"<ol>{items}</ol>"
                "</div>"
            )
        else:
            prio_html = (
                '<div class="audience-summary-exec-priorities">'
                "<h4>เขตที่ต้องเร่งดำเนินการ</h4>"
                "<p><em>ไม่มีข้อมูลเพียงพอ</em></p>"
                "</div>"
            )
        # Recommendations
        rec_items = "".join(
            f"<li>{_html_escape(r)}</li>"
            for r in data["recommendations"]
        )
        rec_html = (
            '<div class="audience-summary-exec-recommendations">'
            "<h4>ข้อเสนอแนะเชิงนโยบาย</h4>"
            f"<ul>{rec_items}</ul>"
            "</div>"
        )
        return (
            '<section class="audience-summary-executive">'
            + kpi_html
            + prio_html
            + rec_html
            + "</section>"
        )

    # ------------------------------------------------------------------
    # LaTeX — same content, no new packages required (matches the
    # whitepaper preamble that S7 stabilised).
    # ------------------------------------------------------------------

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        lines: List[str] = []
        # KPI row — 3 columns of a tabular, big bold value above small label.
        kpis = data["kpis"]
        if kpis:
            col_spec = "|" + "c|" * len(kpis)
            value_cells = " & ".join(
                r"\textbf{\Large "
                + latex_escape(str(t["value_str"]))
                + (" " + latex_escape(str(t["delta"])) if t.get("delta") else "")
                + "}"
                for t in kpis
            )
            label_cells = " & ".join(
                latex_escape(str(t["label_th"])) for t in kpis
            )
            lines.append(r"\begin{center}")
            lines.append(r"\begin{tabular}{" + col_spec + "}")
            lines.append(r"\hline")
            lines.append(value_cells + r" \\")
            lines.append(label_cells + r" \\ \hline")
            lines.append(r"\end{tabular}")
            lines.append(r"\end{center}")
        # Priority districts
        lines.append(r"\textbf{เขตที่ต้องเร่งดำเนินการ}")
        if data["priorities"]:
            lines.append(r"\begin{enumerate}")
            for p in data["priorities"]:
                lines.append(
                    r"\item \textbf{"
                    + latex_escape(str(p["district_name"]))
                    + r"} --- "
                    + f"{p['at_risk_pct']:.1f}\\%"
                )
            lines.append(r"\end{enumerate}")
        else:
            lines.append(r"\textit{ไม่มีข้อมูลเพียงพอ}\par")
        # Recommendations
        lines.append(r"\textbf{ข้อเสนอแนะเชิงนโยบาย}")
        lines.append(r"\begin{itemize}")
        for r in data["recommendations"]:
            lines.append(r"\item " + latex_escape(str(r)))
        lines.append(r"\end{itemize}")
        return "\n".join(lines) + "\n"


__all__ = [
    "AudienceSummaryExecutiveBlock",
    "_AudienceSummaryExecutiveParams",
]
