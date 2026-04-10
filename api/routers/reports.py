"""Report generation API router.

Provides endpoints to generate, download, and manage cached PDF reports
in multiple languages and formats (whitepaper / executive slides).

Ported from bma-health -- async def kept for FastAPI compatibility
(FastAPI runs sync defs in a thread pool automatically).
All internal calls are sync. Imports adjusted for flat layout.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

import config
from schemas.reports import ReportGenerateResponse, ReportInfo, ReportStatusResponse
from services.report_generator import LANGS, REPORT_TYPES, report_generator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])

REPORTS_DIR = Path(config.REPORTS_DIR)


def _report_url(report_type: str, lang: str) -> str:
    """Build the download URL for a report."""
    endpoint = "comprehensive" if report_type == "whitepaper" else "executive"
    return f"/api/reports/{endpoint}/{lang}"


# ------------------------------------------------------------------
# Download endpoints
# ------------------------------------------------------------------

@router.get("/comprehensive/{lang}")
async def download_comprehensive(lang: str):
    """Download the comprehensive whitepaper PDF for a language."""
    if lang not in LANGS:
        raise HTTPException(
            status_code=404,
            detail=f"Language '{lang}' not supported. Valid: {LANGS}",
        )
    path = report_generator.get_cache_path(lang, "whitepaper")
    if not path.exists():
        try:
            path = report_generator.generate(lang, "whitepaper")
        except FileNotFoundError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            logger.exception("Report generation failed for %s/whitepaper", lang)
            raise HTTPException(
                status_code=500,
                detail=f"Report generation failed: {e}",
            )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"bma_health_report_{lang}.pdf",
    )


@router.get("/executive/{lang}")
async def download_executive(lang: str):
    """Download the executive slides PDF for a language."""
    if lang not in LANGS:
        raise HTTPException(
            status_code=404,
            detail=f"Language '{lang}' not supported. Valid: {LANGS}",
        )
    path = report_generator.get_cache_path(lang, "slides")
    if not path.exists():
        try:
            path = report_generator.generate(lang, "slides")
        except FileNotFoundError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            logger.exception("Report generation failed for %s/slides", lang)
            raise HTTPException(
                status_code=500,
                detail=f"Report generation failed: {e}",
            )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"bma_health_slides_{lang}.pdf",
    )


@router.get("/disease/{disease}")
async def download_disease_slide(disease: str):
    """Download a disease-specific slide deck PDF."""
    if not re.match(r'^[a-z_]+$', disease):
        raise HTTPException(status_code=400, detail="Invalid disease key")
    cache_path = REPORTS_DIR / "disease" / f"{disease}_th.pdf"
    if not cache_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Disease slide for '{disease}' not found. Generate it first via POST /api/reports/generate.",
        )
    return FileResponse(
        cache_path,
        media_type="application/pdf",
        filename=f"bma_health_{disease}_th.pdf",
    )


@router.get("/adaptive/{filename}")
async def download_adaptive_report(filename: str):
    """Download an AI-generated adaptive report PDF."""
    if not re.match(r'^[\w\-]+\.pdf$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    cache_dir = REPORTS_DIR / "adaptive"
    cache_path = (cache_dir / filename).resolve()
    if not str(cache_path).startswith(str(cache_dir.resolve())):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not cache_path.exists():
        raise HTTPException(status_code=404, detail=f"Adaptive report '{filename}' not found")
    return FileResponse(cache_path, media_type="application/pdf", filename=filename)


@router.get("/zone/{zone_code}/{lang}")
async def download_zone_report(zone_code: str, lang: str = "th"):
    """Download a zone-specific report PDF."""
    cache_path = REPORTS_DIR / "zone" / f"zone{zone_code}_{lang}.pdf"
    if not cache_path.exists():
        try:
            from services.zone_report_generator import generate_zone_report
            cache_path = generate_zone_report(zone_code, lang)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Zone report generation failed: {e}")
    return FileResponse(
        cache_path,
        media_type="application/pdf",
        filename=f"bma_health_zone{zone_code}_{lang}.pdf",
    )


@router.get("/msd/{lang}")
async def download_msd_comprehensive(lang: str = "th"):
    """Download the comprehensive MSD report (100+ pages)."""
    cache_path = REPORTS_DIR / lang / "msd_comprehensive.pdf"
    if not cache_path.exists():
        try:
            from services.msd_report_generator import MSDReportGenerator
            gen = MSDReportGenerator()
            result = gen.generate(lang)
            if result is None:
                raise HTTPException(status_code=500, detail="MSD report compilation failed")
            cache_path = result
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("MSD report generation failed")
            raise HTTPException(status_code=500, detail=f"MSD report generation failed: {e}")
    return FileResponse(
        cache_path,
        media_type="application/pdf",
        filename=f"bma_health_msd_comprehensive_{lang}.pdf",
    )


@router.get("/public/{lang}")
async def download_public_infographic(lang: str = "th"):
    """Download the public health infographic (1-page)."""
    cache_path = REPORTS_DIR / f"public_{lang}.pdf"
    if not cache_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Public infographic not yet generated. Use POST /api/reports/generate to create it.",
        )
    return FileResponse(cache_path, media_type="application/pdf", filename=f"bma_health_public_{lang}.pdf")


@router.get("/catalog")
async def get_catalog():
    """List all available report types with download URLs and cache status."""
    from data.facts import HEALTH_ZONES

    base = REPORTS_DIR
    diseases = [
        "diabetes", "hypertension", "obesity", "dyslipidemia",
        "cardiovascular", "stroke", "ckd", "anemia", "respiratory",
    ]
    disease_names = {
        "diabetes": "เบาหวาน", "hypertension": "ความดันโลหิตสูง",
        "obesity": "โรคอ้วน", "dyslipidemia": "ไขมันในเลือดผิดปกติ",
        "cardiovascular": "โรคหลอดเลือดหัวใจ", "stroke": "โรคหลอดเลือดสมอง",
        "ckd": "โรคไตเรื้อรัง", "anemia": "โรคโลหิตจาง",
        "respiratory": "โรคระบบทางเดินหายใจ",
    }

    def _check(path: Path) -> dict:
        exists = path.exists()
        return {"cached": exists, "size": path.stat().st_size if exists else 0}

    categories = [
        {
            "id": "executive", "label": "สำหรับผู้บริหาร", "icon": "chart",
            "reports": [
                {"label": "Executive Slides (TH)", "url": "/api/reports/executive/th", **_check(base / "th" / "slides.pdf")},
                {"label": "Executive Slides (EN)", "url": "/api/reports/executive/en", **_check(base / "en" / "slides.pdf")},
            ],
        },
        {
            "id": "zones", "label": "สำหรับโรงพยาบาล/โซน", "icon": "hospital",
            "reports": [
                {"label": f"{z['name_th']} ({z['facilitator'].replace('โรงพยาบาล','รพ.')})",
                 "url": f"/api/reports/zone/{zc}/th",
                 **_check(base / "zone" / f"zone{zc}_th.pdf")}
                for zc, z in HEALTH_ZONES.items()
            ],
        },
        {
            "id": "public", "label": "สำหรับประชาชน", "icon": "users",
            "reports": [
                {"label": "สรุปสุขภาพ กทม. (TH)", "url": "/api/reports/public/th", **_check(base / "public_th.pdf")},
            ],
        },
        {
            "id": "msd", "label": "สำหรับสำนักการแพทย์", "icon": "hospital",
            "reports": [
                {"label": "รายงานฉบับเต็ม สนพ. 100+ หน้า (TH)", "url": "/api/reports/msd/th",
                 **_check(base / "th" / "msd_comprehensive.pdf")},
            ],
        },
        {
            "id": "whitepaper", "label": "รายงานฉบับเต็ม", "icon": "document",
            "reports": [
                {"label": "Comprehensive Report (TH)", "url": "/api/reports/comprehensive/th",
                 **_check(base / "th" / "whitepaper.pdf")},
                {"label": "Comprehensive Report (EN)", "url": "/api/reports/comprehensive/en",
                 **_check(base / "en" / "whitepaper.pdf")},
            ],
        },
        {
            "id": "diseases", "label": "รายโรค", "icon": "stethoscope",
            "reports": [
                {"label": disease_names.get(dk, dk), "url": f"/api/reports/disease/{dk}",
                 **_check(base / "disease" / f"{dk}_th.pdf")}
                for dk in diseases
            ],
        },
    ]

    return {"categories": categories}


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------

@router.get("/scheduler-status")
async def get_scheduler_status():
    """Get nightly report scheduler status."""
    from services.scheduler import get_scheduler_status
    return get_scheduler_status()


@router.get("/generation-progress")
async def get_generation_progress():
    """Get real-time progress of background report generation."""
    progress = report_generator.get_generation_progress()
    return progress


@router.get("/status", response_model=ReportStatusResponse)
async def get_status():
    """Get cache status of all report variants."""
    status = report_generator.get_status()
    report_infos = [
        ReportInfo(
            lang=r["lang"],
            report_type=r["report_type"],
            cached=r["cached"],
            size_bytes=r["size_bytes"],
            valid=r["valid"],
            url=_report_url(r["report_type"], r["lang"]),
        )
        for r in status["reports"]
    ]
    return ReportStatusResponse(
        reports=report_infos,
        data_hash=status["data_hash"],
        total_cached=status["total_cached"],
    )


# ------------------------------------------------------------------
# Generation triggers
# ------------------------------------------------------------------

@router.post("/generate", response_model=ReportGenerateResponse)
async def generate_all(background_tasks: BackgroundTasks):
    """Trigger generation of all reports for all languages in the background."""
    background_tasks.add_task(report_generator.generate_all)
    return ReportGenerateResponse(
        status="started",
        message="Report generation started in background for all languages and types",
        lang="all",
        report_type="all",
    )


@router.post("/generate/{lang}", response_model=ReportGenerateResponse)
async def generate_single(lang: str, background_tasks: BackgroundTasks):
    """Generate both whitepaper and slides for a single language in the background."""
    if lang not in LANGS:
        raise HTTPException(
            status_code=404,
            detail=f"Language '{lang}' not supported. Valid: {LANGS}",
        )
    background_tasks.add_task(report_generator.generate, lang, "whitepaper")
    background_tasks.add_task(report_generator.generate, lang, "slides")
    return ReportGenerateResponse(
        status="started",
        message=f"Generating whitepaper and slides for '{lang}'",
        lang=lang,
        report_type="both",
        url=_report_url("whitepaper", lang),
    )


@router.post("/generate/{lang}/{report_type}", response_model=ReportGenerateResponse)
async def generate_specific(lang: str, report_type: str, background_tasks: BackgroundTasks):
    """Generate a specific report variant in the background."""
    if lang not in LANGS:
        raise HTTPException(status_code=404, detail=f"Language '{lang}' not supported")
    if report_type not in REPORT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Report type '{report_type}' not supported. Valid: {REPORT_TYPES}",
        )
    background_tasks.add_task(report_generator.generate, lang, report_type)
    return ReportGenerateResponse(
        status="started",
        message=f"Generating {report_type} for '{lang}'",
        lang=lang,
        report_type=report_type,
        url=_report_url(report_type, lang),
    )


# ------------------------------------------------------------------
# Cache management
# ------------------------------------------------------------------

@router.post("/invalidate")
async def invalidate_cache():
    """Invalidate all cached reports, forcing regeneration on next request."""
    report_generator.invalidate_cache()
    return {"status": "ok", "message": "All cached reports invalidated"}
