"""``appendix_methodology`` block — static methodology appendix.

Per ADR-03 §3 this is a copy-only block: no parameters, no DB lookup.
It exists because every public-facing health report carries a fixed
boilerplate covering data sources, time window, k-anonymity rule,
non-imputation policy, and MSD criteria. Centralising it here means
descriptor authors can drop ``- block: appendix_methodology`` instead of
re-typing the legalese.
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks.base import ContentBlock
from services.reports.spec import RenderContext


class _AppendixParams(BaseModel):
    """No parameters — methodology copy is fixed."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# The Thai methodology copy used by every public report. Kept as a
# module-level constant so a future edit / translation lives in one file
# rather than scattered across descriptors.
_THAI_BODY = [
    "แหล่งข้อมูล: ระบบคัดกรองสุขภาพของสำนักการแพทย์ กทม. รวบรวมจาก 3 แอปพลิเคชัน "
    "ได้แก่ HHC (Home Health Care), CMU (Community Mobile Unit) และ "
    "Health Promotion Mobile Application (HPMA).",
    "ระยะเวลาเก็บข้อมูล: ครอบคลุมการคัดกรองตั้งแต่เริ่มเปิดให้บริการของแต่ละแอปจนถึง "
    "วันที่ดึงข้อมูลล่าสุด ซึ่งระบุไว้ในหน้าปก.",
    "เกณฑ์การปกป้องข้อมูลส่วนบุคคล (k-anonymity): "
    "เซลล์ใด ๆ ที่มีจำนวนผู้รับการคัดกรองน้อยกว่า 5 จะถูกระงับการแสดงผล "
    "(suppress) เพื่อป้องกันการระบุตัวบุคคล.",
    "นโยบายการไม่อนุมานค่าที่ขาดหาย (non-imputation): "
    "ตัวเลขที่นำเสนอเป็นจำนวนนับโดยตรง — หากผู้คัดกรองไม่มีผลตรวจในรายการใด "
    "จะไม่นับเป็น 0 และจะไม่ถูกประมาณค่าทดแทน.",
    "เกณฑ์ MSD (Mean Standard Deviation): "
    "ใช้สำหรับจัดอันดับเขต / กลุ่มประชากรที่มีอัตราเสี่ยงต่อโรคเรื้อรังสูงกว่าค่าเฉลี่ยอย่างมีนัยสำคัญ "
    "(เกินค่าเฉลี่ยเมือง + 1 SD) เพื่อใช้ในการจัดสรรทรัพยากรการคัดกรองเชิงรุก.",
]


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class AppendixMethodologyBlock(ContentBlock):
    """Static methodology appendix — no params, no DB lookup."""

    block_id: ClassVar[str] = "appendix_methodology"
    Parameters: ClassVar[type[BaseModel]] = _AppendixParams

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        # Returning the bullets as data lets a future i18n pass swap in
        # english copy without touching renderers.
        return {"title_th": "ภาคผนวก: ระเบียบวิธี", "bullets": list(_THAI_BODY)}

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        bullets = "\n".join(
            r"\item " + latex_escape(str(b)) for b in data["bullets"]
        )
        return (
            r"\section{" + latex_escape(str(data["title_th"])) + "}\n"
            r"\begin{itemize}" + "\n"
            + bullets + "\n"
            + r"\end{itemize}" + "\n"
        )

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        items = "".join(
            f"<li>{_html_escape(str(b))}</li>" for b in data["bullets"]
        )
        title = _html_escape(str(data["title_th"]))
        return (
            f'<section class="appendix">'
            f"<h2>{title}</h2>"
            f"<ul>{items}</ul>"
            f"</section>"
        )
