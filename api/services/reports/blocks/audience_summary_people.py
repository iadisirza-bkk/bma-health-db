"""``audience_summary_people`` block — plain-language summary for ประชาชน.

Sprint S8 ("Audience-Segmented Report Sections") block #1 of 4. The
block emits a 3-tile traffic-light card grid (green / yellow / red) plus
an action checklist, all in plain Thai with NO clinical jargon. The
target reader is a member of the public reading their district's report,
NOT a clinician or executive.

Data sources
------------
* ``ctx.data_collector.data()`` returns the legacy ``ReportData``
  dataclass (see :mod:`services.report_data_collector`). The fields the
  block reads are:
    - ``total_screened`` (int) — population of screened persons
    - ``district_data`` (dict[dcode → {total_screened, diseases}]) —
      per-district detail (used to apply ``filters.zone_code`` /
      ``filters.district_code`` scope)
    - ``city_disease_summary`` (list of {disease, name_th, avg_pct,
      district_count}) — sorted descending by avg_pct
    - ``top_diseases`` (list of top-3 from ``city_disease_summary``)

If ``params.filters`` narrows to a zone or district, we recompute the
top-3 + at-risk percentages from the filtered slice of
``district_data`` so the block matches the chart / table siblings'
behaviour for the same filter contract.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.latex_utils import latex_escape
from services.reports.blocks._stats_helpers import format_count_per_10
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger(
    "api.services.reports.blocks.audience_summary_people"
)


# ---------------------------------------------------------------------------
# Plain-language disease labels (NO jargon — "ความดันสูง" not "Hypertension").
# Keys mirror the legacy ``DISEASES`` keys in
# :mod:`services.report_data_collector` so we can join on them.
# ---------------------------------------------------------------------------

_PLAIN_LABEL_TH: Dict[str, str] = {
    "diabetes": "เบาหวาน",
    "hypertension": "ความดันสูง",
    "obesity": "อ้วน / น้ำหนักเกิน",
    "dyslipidemia": "ไขมันในเลือดสูง",
    "cardiovascular": "หัวใจและหลอดเลือด",
    "stroke": "อัมพาต / หลอดเลือดสมอง",
    "ckd": "ไต",
    "anemia": "โลหิตจาง",
    "respiratory": "ระบบหายใจ",
    "mental": "สุขภาพจิต",
}

# 1-line plain guidance per disease — what the reader should DO if their
# district is high-risk for it. Authored once here so all four audience
# blocks could reuse the same advice copy if they want to.
_PLAIN_GUIDANCE_TH: Dict[str, str] = {
    "diabetes": "ควรตรวจน้ำตาลในเลือดอย่างน้อยปีละ 1 ครั้ง",
    "hypertension": "ควรตรวจวัดความดันทุก 6 เดือน",
    "obesity": "ควบคุมอาหารและออกกำลังกายสม่ำเสมอ",
    "dyslipidemia": "ตรวจไขมันในเลือดทุก 1-2 ปี",
    "cardiovascular": "ปรึกษาแพทย์หากมีอาการเจ็บแน่นหน้าอก",
    "stroke": "หากแขนขาอ่อนแรงเฉียบพลัน รีบพบแพทย์",
    "ckd": "ดื่มน้ำให้พอ ลดเค็ม ตรวจการทำงานของไตปีละครั้ง",
    "anemia": "รับประทานอาหารธาตุเหล็ก หากมีอาการอ่อนเพลียควรพบแพทย์",
    "respiratory": "งดสูบบุหรี่ หลีกเลี่ยงควัน/ฝุ่น PM2.5",
    "mental": "ปรึกษาผู้เชี่ยวชาญหากรู้สึกเครียดต่อเนื่อง",
}

# Default action checklist when nothing district-specific applies. Kept
# short on purpose — the people block is supposed to feel like a
# pamphlet, not a clinical handout.
_DEFAULT_ACTIONS_TH: List[str] = [
    "เข้ารับการคัดกรองสุขภาพประจำปีที่ศูนย์บริการสาธารณสุขใกล้บ้าน",
    "วัดความดันโลหิต น้ำตาล และดัชนีมวลกาย (BMI) อย่างน้อยปีละ 1 ครั้ง",
    "ออกกำลังกายอย่างน้อย 150 นาทีต่อสัปดาห์",
    "ลดอาหารหวาน มัน เค็ม และเพิ่มผัก-ผลไม้",
    "หากมีอาการผิดปกติ พบแพทย์ทันที อย่ารอ",
]

# Traffic-light cutoffs (percent at-risk) — chosen to match the public-
# health convention used elsewhere in the codebase. ``green`` is "low
# concern"; ``red`` is "should be a personal priority".
_GREEN_MAX_PCT = 10.0
_YELLOW_MAX_PCT = 20.0


class _AudienceSummaryPeopleParams(BaseModel):
    """Parameters for the ``audience_summary_people`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ``None`` = citywide; pass ``{"zone_code": "03"}`` or
    # ``{"district_code": "1004"}`` to restrict the slice we summarise.
    filters: Optional[Dict[str, Any]] = Field(default=None)


# ---------------------------------------------------------------------------
# Filtering + aggregation
# ---------------------------------------------------------------------------


def _apply_filters(
    district_data: Dict[str, Any],
    filters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Restrict ``district_data`` to the dcodes matching ``filters``.

    Supported filter keys (others are ignored — same lenient contract as
    the chart block):
        * ``district_code`` (str) — exact match
        * ``zone_code`` (str) — district's ``zone_code`` field equals it
    """
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


def _summarise(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate ``district_data``-shaped scope into the figures the block needs.

    Returns
    -------
    dict
        ``{ "total_screened", "n_districts", "diseases":
            [ { "key", "name_th", "avg_pct" }, ... sorted desc ] }``.

    The block uses the top 3 diseases of ``diseases`` for the traffic-
    light card grid.
    """
    total_screened = 0
    accum: Dict[str, Dict[str, float]] = {}
    for dinfo in scope.values():
        ts = int(dinfo.get("total_screened", 0) or 0)
        total_screened += ts
        diseases = dinfo.get("diseases") or {}
        for key, payload in diseases.items():
            try:
                pct = float(payload.get("pct_at_risk", 0))
            except (TypeError, ValueError):
                continue
            row = accum.setdefault(
                key, {"weighted_sum": 0.0, "weight": 0}
            )
            row["weighted_sum"] += pct * ts
            row["weight"] += ts
    diseases_out: List[Dict[str, Any]] = []
    for key, row in accum.items():
        if row["weight"] <= 0:
            continue
        diseases_out.append(
            {
                "key": key,
                "name_th": _PLAIN_LABEL_TH.get(key, key),
                "avg_pct": row["weighted_sum"] / row["weight"],
            }
        )
    diseases_out.sort(key=lambda r: r["avg_pct"], reverse=True)
    return {
        "total_screened": total_screened,
        "n_districts": len(scope),
        "diseases": diseases_out,
    }


def _traffic_light(pct: float) -> str:
    """Map an at-risk percentage to a ``green / yellow / red`` bucket."""
    if pct < _GREEN_MAX_PCT:
        return "green"
    if pct < _YELLOW_MAX_PCT:
        return "yellow"
    return "red"


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class AudienceSummaryPeopleBlock(ContentBlock):
    """Plain-language traffic-light summary for ประชาชนทั่วไป."""

    block_id: ClassVar[str] = "audience_summary_people"
    Parameters: ClassVar[type[BaseModel]] = _AudienceSummaryPeopleParams
    audience_target: ClassVar[Optional[AudienceTarget]] = AudienceTarget.PEOPLE

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _AudienceSummaryPeopleParams)
        getter = getattr(ctx.data_collector, "data", None)
        bag: Any = getter() if callable(getter) else (getter or {})
        # Tolerate both the dataclass shape (``ReportData``) and a plain
        # dict. The legacy collector returns the dataclass; the test
        # harness in ``test_blocks.py`` passes a dict.
        district_data = self._read(bag, "district_data") or {}
        scope = _apply_filters(district_data, params.filters)
        agg = _summarise(scope)
        # Top-3 diseases for the traffic-light tiles.
        top3: List[Dict[str, Any]] = []
        for d in agg["diseases"][:3]:
            pct_value = d["avg_pct"]
            pct_proportion = max(0.0, min(1.0, pct_value / 100.0))
            top3.append(
                {
                    "key": d["key"],
                    "label_th": d["name_th"],
                    "pct": round(pct_value, 1),
                    "count_per_10": format_count_per_10(pct_proportion),
                    "guidance_th": _PLAIN_GUIDANCE_TH.get(d["key"], ""),
                    "tier": _traffic_light(pct_value),
                }
            )
        # Screening rate — proxy: total_screened / sum-of-population.
        # We don't have population in district_data reliably, so we
        # surface the raw count and let the renderer phrase it.
        return {
            "scope_total_screened": agg["total_screened"],
            "scope_n_districts": agg["n_districts"],
            "filters": params.filters or {},
            "top3": top3,
            "actions": list(_DEFAULT_ACTIONS_TH),
        }

    @staticmethod
    def _read(bag: Any, key: str) -> Any:
        """Tolerate dataclass / dict / object access for ``key``."""
        if isinstance(bag, dict):
            return bag.get(key)
        return getattr(bag, key, None)

    # ------------------------------------------------------------------
    # HTML — 3 traffic-light cards + action checklist
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        cards: List[str] = []
        for tile in data["top3"]:
            tier = _html_escape(str(tile["tier"]))
            label = _html_escape(str(tile["label_th"]))
            count = _html_escape(str(tile["count_per_10"]))
            pct = _html_escape(f"{tile['pct']:.1f}")
            guidance = _html_escape(str(tile.get("guidance_th") or ""))
            cards.append(
                f'<div class="card-{tier}">'
                f'<div class="big-number">{count}</div>'
                f'<div class="disease-label">เสี่ยง{label} '
                f'({pct}%)</div>'
                f'<div class="guidance">{guidance}</div>'
                f"</div>"
            )
        cards_html = (
            '<div class="audience-summary-people-cards">'
            + "".join(cards)
            + "</div>"
        )
        # Empty-state path — when there's no data, omit the cards but
        # still render an action checklist so the section never produces
        # zero markup.
        if not data["top3"]:
            cards_html = (
                '<div class="audience-summary-people-cards">'
                '<p><em>ยังไม่มีข้อมูลคัดกรองในพื้นที่นี้</em></p>'
                "</div>"
            )
        action_items = "".join(
            f"<li>{_html_escape(str(a))}</li>" for a in data["actions"]
        )
        actions_html = (
            '<div class="audience-summary-people-actions">'
            "<h4>สิ่งที่ควรทำ</h4>"
            f"<ul>{action_items}</ul>"
            "</div>"
        )
        return (
            '<section class="audience-summary-people">'
            + cards_html
            + actions_html
            + "</section>"
        )

    # ------------------------------------------------------------------
    # LaTeX — colour-boxed itemize, deliberately simple (no TikZ)
    # ------------------------------------------------------------------

    _TIER_LATEX_COLOR: ClassVar[Dict[str, str]] = {
        "green": "green!20",
        "yellow": "yellow!30",
        "red": "red!25",
    }

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        lines: List[str] = []
        if data["top3"]:
            lines.append(r"\begin{itemize}")
            for tile in data["top3"]:
                color = self._TIER_LATEX_COLOR.get(
                    str(tile["tier"]), "gray!20"
                )
                count = latex_escape(str(tile["count_per_10"]))
                label = latex_escape(str(tile["label_th"]))
                pct_str = f"{tile['pct']:.1f}\\%"
                guidance = latex_escape(str(tile.get("guidance_th") or ""))
                # \colorbox{<colour>}{<text>} — "simple, no fancy TikZ"
                # per the S8 task brief.
                lines.append(
                    r"\item \colorbox{" + color + r"}{\textbf{"
                    + count + r"}} เสี่ยง" + label + r" (" + pct_str + ")"
                )
                if guidance:
                    lines.append(r"\\\textit{" + guidance + r"}")
            lines.append(r"\end{itemize}")
        else:
            lines.append(
                r"\textit{ยังไม่มีข้อมูลคัดกรองในพื้นที่นี้}\par"
            )
        # Action checklist
        lines.append(r"\textbf{สิ่งที่ควรทำ}")
        lines.append(r"\begin{itemize}")
        for action in data["actions"]:
            lines.append(r"\item " + latex_escape(str(action)))
        lines.append(r"\end{itemize}")
        return "\n".join(lines) + "\n"


__all__ = [
    "AudienceSummaryPeopleBlock",
    "_AudienceSummaryPeopleParams",
]
