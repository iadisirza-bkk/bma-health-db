"""GenerateReportTool — PDF report generation (comprehensive, executive, disease_focus).

SYNC. Adapted: report generators may not be available in this project yet.
Returns download URLs that the frontend/other routers can serve.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from agents.tools.base import BaseTool, ToolResult
from agents.tools.helpers import DISEASE_NAMES, normalize_disease

logger = logging.getLogger(__name__)


class GenerateReportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_type: Literal["comprehensive", "executive", "disease_focus"]
    disease: Optional[str] = None
    lang: Optional[Literal["th", "en"]] = None


class GenerateReportTool(BaseTool):
    name = "generate_report"
    description = "Generate PDF report/slides: comprehensive, executive, or disease_focus (6-slide deck)"
    Parameters = GenerateReportParams
    parameters_schema = {
        "type": "object",
        "properties": {
            "report_type": {"type": "string", "enum": ["comprehensive", "executive", "disease_focus"]},
            "disease": {"type": "string"},
            "lang": {"type": "string", "enum": ["th", "en"]},
        },
        "required": ["report_type"],
    }

    def execute(self, args: dict) -> ToolResult:
        args = self.Parameters(**args).model_dump(exclude_none=True)
        report_type = args.get("report_type", "comprehensive")
        disease = normalize_disease(args.get("disease"))
        lang = args.get("lang", "th")

        if report_type == "disease_focus":
            if not disease:
                return ToolResult(text="ต้องระบุโรคสำหรับ disease_focus")
            disease_name = DISEASE_NAMES.get(disease, disease)
            try:
                from services.disease_slide_generator import generate_disease_slide
                generate_disease_slide(disease)
                url = f"/api/reports/disease/{disease}"
                return ToolResult(text=f"สร้างสไลด์ {disease_name} สำเร็จ\nDownload URL: {url}")
            except ImportError:
                url = f"/api/reports/disease/{disease}"
                return ToolResult(text=f"ระบบสร้างสไลด์ยังไม่พร้อม กรุณาใช้ข้อมูลด้านล่างแทน\nDownload URL: {url}")
            except Exception as e:
                return ToolResult(text=f"สร้างสไลด์ไม่สำเร็จ: {e}")

        endpoint = "comprehensive" if report_type == "comprehensive" else "executive"
        label = "รายงานฉบับสมบูรณ์" if report_type == "comprehensive" else "สไลด์สรุปผู้บริหาร"
        url = f"/api/reports/{endpoint}/{lang}"
        try:
            from services.report_generator import report_generator
            rt = "whitepaper" if report_type == "comprehensive" else "slides"
            path = report_generator.get_cache_path(lang, rt)
            if not path.exists():
                report_generator.generate(lang, rt)
            return ToolResult(text=f"สร้าง {label} ({lang.upper()}) สำเร็จ\nDownload URL: {url}")
        except ImportError:
            return ToolResult(text=f"ระบบสร้างรายงานยังไม่พร้อม กรุณาใช้ข้อมูลด้านล่างแทน\nDownload URL: {url}")
        except Exception as e:
            return ToolResult(text=f"สร้างรายงานไม่สำเร็จ: {e}\nDownload URL: {url}")
