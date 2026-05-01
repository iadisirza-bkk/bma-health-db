"""NCD Diagnostic Report tool — exposes 11-disease × 4-metric breakdown.

For doctor-facing analytics: per-disease counts of
  - คนเสี่ยง (at_risk)         — RISK_* screening flag
  - คนป่วย (sick_clinical)     — FOUND_* clinical/self-report
  - คนใหม่จากการตรวจ (new_clinical) — found_* AND lab not over threshold
  - คนใหม่จากผลแลป (new_from_lab)   — lab over threshold AND NOT found_*

Reads from `public.mv_ncd_diagnostic_report` (≥ migration 113).
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from agents.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


def _query(sql: str, params: tuple = None) -> list[dict]:
    from database import execute_query
    return execute_query(sql, params)


def _scalar(sql: str, params: tuple = None):
    from database import execute_scalar
    return execute_scalar(sql, params)


def get_ncd_diagnostic_report() -> dict:
    """Return all 11 NCD rows + city-wide screened denominator."""
    rows = _query("""
        SELECT disease_key, disease_name_th, lab_threshold,
               at_risk, sick_clinical, new_clinical, new_from_lab,
               has_lab_threshold
        FROM public.mv_ncd_diagnostic_report
        ORDER BY
          CASE disease_key
            WHEN 'diabetes' THEN 1 WHEN 'hypertension' THEN 2
            WHEN 'dyslipidemia' THEN 3 WHEN 'obesity' THEN 4
            WHEN 'kidney' THEN 5 WHEN 'liver' THEN 6
            WHEN 'anemia' THEN 7 WHEN 'cardiovascular' THEN 8
            WHEN 'stroke' THEN 9 WHEN 'cervical_cancer' THEN 10
            WHEN 'colorectal_cancer' THEN 11
          END
    """)
    total = int(_scalar("""
        SELECT COUNT(DISTINCT patient_id)
        FROM public.mv_visit_resolved
        WHERE bucket = 'bkk' AND is_dedup_kept
    """) or 0)

    # Format as readable Thai summary so the LLM has clean text to synthesize.
    lines = [
        f"รายงานการคัดกรองโรคไม่ติดต่อ (NCD) — ครอบคลุมผู้มาตรวจ {total:,} คน (กทม. 50 เขต)",
        "",
        f"{'โรค':<26}{'เสี่ยง':>10}{'ป่วย':>10}{'ใหม่จากตรวจ':>14}{'ใหม่จากแลป':>14}",
        "-" * 75,
    ]
    for r in rows:
        def f(v): return f"{v:,}" if v is not None else "—"
        lines.append(
            f"{r['disease_name_th']:<26}"
            f"{f(r['at_risk']):>10}"
            f"{f(r['sick_clinical']):>10}"
            f"{f(r['new_clinical']):>14}"
            f"{f(r['new_from_lab']):>14}"
        )

    return {
        "total_screened": total,
        "summary": "\n".join(lines),
        "data": rows,
        "methodology": {
            "at_risk":       "RISK_* flag จากเกณฑ์คัดกรอง (BMI/age/family history)",
            "sick_clinical": "FOUND_* — ผู้ป่วยที่ตรวจพบหรือรายงานว่าเป็น",
            "new_clinical":  "FOUND_* AND lab ไม่ผิดปกติ — clinically detected",
            "new_from_lab":  "Lab ผิดปกติ AND NOT FOUND_* — lab จับโรคที่ self-report ไม่ได้",
        },
    }


class NcdDiagnosticReportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NcdDiagnosticReportTool(BaseTool):
    """Doctor-facing 11-disease × 4-metric report."""

    name = "query_ncd_diagnostic_report"
    description = (
        "รายงานคัดกรอง NCD แบบ 4 มิติ × 11 โรค "
        "(คนเสี่ยง / คนป่วย / ใหม่จากการตรวจ / ใหม่จากผลแลป) "
        "— เรียกเมื่อแพทย์ขอภาพรวมการคัดกรองโรคไม่ติดต่อ "
        "หรือถามว่าคัดกรองเจอโรคใหม่กี่คน"
    )
    Parameters = NcdDiagnosticReportParams
    parameters_schema: dict = {"type": "object", "properties": {}, "required": []}

    def execute(self, args: dict) -> ToolResult:
        args = self.Parameters(**args).model_dump(exclude_none=True)
        try:
            payload = get_ncd_diagnostic_report()
            return ToolResult(text=payload.get("summary", ""), metadata=payload)
        except Exception as exc:
            logger.exception("NcdDiagnosticReportTool failed")
            return ToolResult(text=f"ไม่สามารถสร้างรายงาน NCD: {exc}")
