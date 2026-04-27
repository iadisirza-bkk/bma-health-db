"""Public router -- extracted from main.py."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import K_ANONYMITY_THRESHOLD
from cache import cache_get, cache_set, TTL_T3_FILTERED, TTL_T4_STATIC

router = APIRouter(prefix="/api/v2/public", tags=["Public"])


# ------------------------------------------------------------------ #
# GET /api/v2/public/district-summary
# ------------------------------------------------------------------ #

@router.get("/district-summary")
def public_district_summary(
    district: str = Query(..., description="District code"),
    lang: str = Query("th"),
) -> dict:
    """Simplified health summary for public. PDPA-safe, Thai language."""
    disease = execute_query(
        """SELECT district_code, district_name, total_screened,
                  risk_dm_count, pct_risk_dm,
                  risk_hpt_count, pct_risk_hpt,
                  risk_cvd_count, pct_risk_cvd,
                  found_obesity_count
           FROM summary_district_disease WHERE district_code = %s""",
        (district,),
    )
    if not disease:
        raise HTTPException(status_code=404, detail="District not found")

    d = disease[0]
    total = d.get("total_screened") or 0
    if total < K_ANONYMITY_THRESHOLD:
        raise HTTPException(status_code=403, detail="Data suppressed for privacy (k-anonymity)")

    name = d.get("district_name") or district

    # Suppress individual disease counts below threshold
    dm_count = d.get("risk_dm_count") or 0
    hpt_count = d.get("risk_hpt_count") or 0
    cvd_count = d.get("risk_cvd_count") or 0
    obesity_count = d.get("found_obesity_count") or 0

    dm_text = f"เบาหวาน {dm_count:,} คน ({d.get('pct_risk_dm') or 0}%)" if dm_count >= K_ANONYMITY_THRESHOLD else "เบาหวาน: ข้อมูลไม่เพียงพอ"
    hpt_text = f"ความดันสูง {hpt_count:,} คน ({d.get('pct_risk_hpt') or 0}%)" if hpt_count >= K_ANONYMITY_THRESHOLD else "ความดันสูง: ข้อมูลไม่เพียงพอ"
    cvd_text = f"หัวใจและหลอดเลือด {cvd_count:,} คน ({d.get('pct_risk_cvd') or 0}%)" if cvd_count >= K_ANONYMITY_THRESHOLD else "หัวใจและหลอดเลือด: ข้อมูลไม่เพียงพอ"
    obesity_text = f"โรคอ้วน {obesity_count:,} คน" if obesity_count >= K_ANONYMITY_THRESHOLD else "โรคอ้วน: ข้อมูลไม่เพียงพอ"

    summary = (
        f"สรุปผลการคัดกรองสุขภาพ เขต{name}\n"
        f"จำนวนผู้เข้ารับการคัดกรอง: {total:,} คน\n\n"
        f"ผลการคัดกรองโรคเรื้อรัง:\n"
        f"- {dm_text}\n"
        f"- {hpt_text}\n"
        f"- {cvd_text}\n"
        f"- {obesity_text}\n\n"
        f"หมายเหตุ: ข้อมูลนี้เป็นข้อมูลรวม ไม่มีข้อมูลส่วนบุคคล"
    )

    return {
        "district_code": district,
        "district_name": name,
        "total_screened": total,
        "summary_text": summary,
        "lang": lang,
    }


# ------------------------------------------------------------------ #
# GET /api/v2/public/screening-locations
# ------------------------------------------------------------------ #

@router.get("/screening-locations")
def screening_locations(district: Optional[str] = Query(None)) -> dict:
    """Health centers in district (from ref_facilities)."""
    conditions = []
    params = []
    if district:
        conditions.append("f.district_code = %s")
        params.append(district)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT f.code, f.name_th, f.name_en, f.facility_type,
               f.district_code, f.zone_code, f.latitude, f.longitude
        FROM ref_facilities f
        {where}
        ORDER BY f.code
    """, tuple(params) or None)

    if not rows:
        return {"locations": [], "note": "ยังไม่มีข้อมูลสถานที่คัดกรองใน ref_facilities — ต้องเพิ่ม seed data"}
    return {"locations": rows}


# ------------------------------------------------------------------ #
# GET /api/v2/public/health-tips
# ------------------------------------------------------------------ #

@router.get("/health-tips")
def health_tips(risk: str = Query("diabetes")) -> dict:
    """Health tips/recommendations based on risk factor."""
    tips = {
        "diabetes": {
            "disease_th": "เบาหวาน",
            "tips": [
                "ลดอาหารหวาน แป้ง น้ำตาล ข้าวขาว",
                "ออกกำลังกายอย่างน้อย 150 นาที/สัปดาห์",
                "ตรวจระดับน้ำตาลเป็นประจำทุก 6 เดือน",
                "รักษาน้ำหนักตัวให้อยู่ในเกณฑ์ BMI < 25",
                "งดสูบบุหรี่และลดแอลกอฮอล์",
            ],
            "warning_signs": ["ปัสสาวะบ่อย กระหายน้ำมาก", "น้ำหนักลดโดยไม่ทราบสาเหตุ", "แผลหายช้า ชาปลายมือปลายเท้า"],
            "where_to_go": "ศูนย์บริการสาธารณสุข กทม. ใกล้บ้านท่าน (69 แห่งทั่ว กทม.)",
        },
        "hypertension": {
            "disease_th": "ความดันโลหิตสูง",
            "tips": ["ลดเค็ม ลดโซเดียม", "ออกกำลังกายสม่ำเสมอ", "ลดน้ำหนักถ้า BMI > 25", "จัดการความเครียด", "วัดความดันเป็นประจำ"],
            "warning_signs": ["ปวดศีรษะรุนแรง", "ตาพร่ามัว", "เจ็บหน้าอก หายใจลำบาก"],
            "where_to_go": "ศูนย์บริการสาธารณสุข กทม.",
        },
        "obesity": {
            "disease_th": "โรคอ้วน",
            "tips": ["กินผักผลไม้เพิ่มขึ้น", "ลดอาหารทอด ของมัน", "เดิน 10,000 ก้าว/วัน", "ลดน้ำหวาน ชานม", "ลดบะหมี่กึ่งสำเร็จรูป"],
            "warning_signs": ["รอบเอว ≥ 90cm (ชาย) หรือ ≥ 80cm (หญิง)", "BMI ≥ 25", "หายใจลำบากเมื่อออกแรง"],
            "where_to_go": "ศูนย์บริการสาธารณสุข กทม.",
        },
    }

    if risk in tips:
        return tips[risk]
    return {"disease_th": risk, "tips": ["ปรึกษาแพทย์ที่ศูนย์บริการสาธารณสุข กทม. ใกล้บ้าน"], "where_to_go": "ศูนย์บริการสาธารณสุข กทม."}


# ------------------------------------------------------------------ #
# GET /api/v2/public/service-satisfaction
# ------------------------------------------------------------------ #

@router.get("/service-satisfaction")
def service_satisfaction(district: Optional[str] = Query(None)) -> dict:
    """Service satisfaction survey results."""
    return {"data_available": False,
            "message": "ยังไม่มีข้อมูลความพึงพอใจในระบบ — ต้องเชื่อมกับระบบสำรวจความพึงพอใจ กทม.",
            "suggestion": "เพิ่ม satisfaction_surveys table หรือเชื่อมกับระบบ Traffy Fondue"}


# ------------------------------------------------------------------ #
# GET /api/v2/public/complaint-status
# ------------------------------------------------------------------ #

@router.get("/complaint-status")
def complaint_status(ticket: Optional[str] = Query(None)) -> dict:
    """Complaint/service request status."""
    return {"data_available": False,
            "message": "ยังไม่มีระบบร้องเรียนในฐานข้อมูลสุขภาพ — ใช้ระบบ Traffy Fondue หรือ สายด่วน กทม. 1555",
            "links": {"traffy_fondue": "https://fondue.traffy.in.th", "hotline": "1555"}}


# ------------------------------------------------------------------ #
# GET /api/v2/public/open-data
# ------------------------------------------------------------------ #

@router.get("/open-data")
def open_data(format: str = Query("json")) -> dict:
    """Open data portal: aggregate health data for transparency."""
    # Return district-level aggregate data that's safe for public
    districts = execute_query("""
        SELECT s.district_code, s.district_name, s.zone_code, s.total_screened,
               s.pct_risk_dm, s.pct_risk_hpt, s.pct_risk_cvd,
               ROUND(100.0 * s.found_obesity_count / NULLIF(s.total_screened, 0), 1) as pct_obesity,
               ROUND(100.0 * s.found_dyslipidemia_count / NULLIF(s.total_screened, 0), 1) as pct_dyslipidemia
        FROM summary_district_disease s
        WHERE s.total_screened >= 5
        ORDER BY s.district_code
    """)

    return {
        "license": "Creative Commons Attribution 4.0 (CC BY 4.0)",
        "source": "สำนักการแพทย์ กรุงเทพมหานคร",
        "description": "ข้อมูลรวมผลการคัดกรองสุขภาพ ระดับเขต (aggregate, ไม่มีข้อมูลส่วนบุคคล)",
        "k_anonymity": 5,
        "last_updated": datetime.utcnow().isoformat(),
        "format": format,
        "records": len(districts),
        "data": districts,
    }


# =========================================================================== #
# Traffic-light health card (Doc 08 — ประชาชนจบ ม.6)
# =========================================================================== #

# National averages (approximate, from MOH 2024 data) for traffic-light grading
_NATIONAL_AVG = {
    "diabetes": 8.5,
    "hypertension": 15.0,
    "cardiovascular": 5.0,
    "obesity": 30.0,
    "dyslipidemia": 20.0,
    "depression_risk": 8.0,
}

_TRAFFIC_LIGHT_LABELS = {
    "green": {"th": "ดี", "en": "Good", "icon": "green_circle"},
    "yellow": {"th": "ต้องระวัง", "en": "Caution", "icon": "yellow_circle"},
    "red": {"th": "น่าห่วง", "en": "Concerning", "icon": "red_circle"},
}


def _traffic_light(value: float, national_avg: float, lower_is_better: bool = True) -> dict:
    """Classify a value as green/yellow/red compared to national average."""
    if value is None:
        return {"color": "gray", "label_th": "ไม่มีข้อมูล", "label_en": "No data"}
    if lower_is_better:
        if value <= national_avg * 0.8:
            color = "green"
        elif value <= national_avg * 1.2:
            color = "yellow"
        else:
            color = "red"
    else:
        if value >= national_avg * 1.2:
            color = "green"
        elif value >= national_avg * 0.8:
            color = "yellow"
        else:
            color = "red"
    labels = _TRAFFIC_LIGHT_LABELS[color]
    return {"color": color, "label_th": labels["th"], "label_en": labels["en"]}


@router.get("/district-health-card")
def district_health_card(
    district: str = Query(..., description="District code (e.g., 1001)"),
    lang: str = Query("th", description="Language: th or en"),
) -> dict:
    """Traffic-light health card for a district — designed for ม.6 education level.
    รายงานสุขภาพเขตแบบสัญญาณไฟจราจร (เขียว/เหลือง/แดง) สำหรับประชาชนทั่วไป
    เข้าใจง่าย ไม่ใช้ศัพท์แพทย์"""

    # Cache check
    cache_key = f"health_card:{district}:{lang}"
    hit = cache_get(cache_key)
    if hit is not None:
        return hit

    # Disease data
    disease = execute_query("""
        SELECT district_code, district_name, zone_code, total_screened,
               pct_risk_dm, pct_risk_hpt, pct_risk_cvd,
               ROUND(100.0 * found_obesity_count / NULLIF(total_screened, 0), 1) AS pct_obesity,
               ROUND(100.0 * found_dyslipidemia_count / NULLIF(total_screened, 0), 1) AS pct_dyslipidemia
        FROM summary_district_disease
        WHERE district_code = %s
    """, (district,))

    if not disease:
        return {"error": "ไม่พบข้อมูลเขตนี้", "district": district}
    d = disease[0]
    if (d.get("total_screened") or 0) < K_ANONYMITY_THRESHOLD:
        return {"error": "ข้อมูลไม่เพียงพอสำหรับแสดงผล (ต้องมีผู้ตรวจอย่างน้อย 5 คน)"}

    # Mental health
    mental = execute_query("""
        SELECT pct_depression_risk, pct_high_stress
        FROM summary_district_mental WHERE district_code = %s
    """, (district,))
    m = mental[0] if mental else {}

    # Lab
    lab = execute_query("""
        SELECT avg_fbs, avg_cholesterol, avg_bmi, pct_anemia
        FROM summary_district_lab l
        JOIN summary_bmi_waist b ON b.district_code = l.district_code
        WHERE l.district_code = %s LIMIT 1
    """, (district,))
    lb = lab[0] if lab else {}

    is_th = lang == "th"

    indicators = [
        {
            "name": "เบาหวาน" if is_th else "Diabetes",
            "description": "คนเสี่ยงเป็นเบาหวาน" if is_th else "Diabetes risk",
            "value": d.get("pct_risk_dm"),
            "unit": "%",
            **_traffic_light(d.get("pct_risk_dm"), _NATIONAL_AVG["diabetes"]),
        },
        {
            "name": "ความดันสูง" if is_th else "Hypertension",
            "description": "คนเสี่ยงเป็นความดัน" if is_th else "Hypertension risk",
            "value": d.get("pct_risk_hpt"),
            "unit": "%",
            **_traffic_light(d.get("pct_risk_hpt"), _NATIONAL_AVG["hypertension"]),
        },
        {
            "name": "โรคหัวใจ-หลอดเลือด" if is_th else "Cardiovascular",
            "description": "คนเสี่ยงเป็นโรคหัวใจ" if is_th else "CVD risk",
            "value": d.get("pct_risk_cvd"),
            "unit": "%",
            **_traffic_light(d.get("pct_risk_cvd"), _NATIONAL_AVG["cardiovascular"]),
        },
        {
            "name": "อ้วน" if is_th else "Obesity",
            "description": "คนน้ำหนักเกิน" if is_th else "Overweight/obese",
            "value": d.get("pct_obesity"),
            "unit": "%",
            **_traffic_light(d.get("pct_obesity"), _NATIONAL_AVG["obesity"]),
        },
        {
            "name": "ไขมันสูง" if is_th else "Dyslipidemia",
            "description": "คนมีไขมันในเลือดสูง" if is_th else "High cholesterol",
            "value": d.get("pct_dyslipidemia"),
            "unit": "%",
            **_traffic_light(d.get("pct_dyslipidemia"), _NATIONAL_AVG["dyslipidemia"]),
        },
        {
            "name": "ซึมเศร้า/เครียด" if is_th else "Depression/Stress",
            "description": "คนเสี่ยงซึมเศร้า" if is_th else "Depression risk",
            "value": m.get("pct_depression_risk"),
            "unit": "%",
            **_traffic_light(m.get("pct_depression_risk"), _NATIONAL_AVG["depression_risk"]),
        },
    ]

    # Count traffic lights
    colors = [i["color"] for i in indicators if i["color"] != "gray"]
    green_count = colors.count("green")
    red_count = colors.count("red")

    if red_count >= 3:
        overall = "red"
        summary = "เขตนี้มีหลายด้านที่น่าห่วง ควรเข้าตรวจสุขภาพ" if is_th else "Multiple concerning indicators"
    elif red_count >= 1:
        overall = "yellow"
        summary = "เขตนี้มีบางด้านที่ต้องระวัง" if is_th else "Some areas of concern"
    else:
        overall = "green"
        summary = "เขตนี้สุขภาพดีโดยรวม" if is_th else "Generally healthy district"

    result = {
        "district_code": d["district_code"],
        "district_name": d["district_name"],
        "total_screened": d["total_screened"],
        "overall_status": {
            "color": overall,
            "label_th": _TRAFFIC_LIGHT_LABELS[overall]["th"],
            "summary": summary,
        },
        "indicators": indicators,
        "green_count": green_count,
        "yellow_count": colors.count("yellow"),
        "red_count": red_count,
        "advice": {
            "th": "ตรวจสุขภาพฟรีได้ที่ศูนย์บริการสาธารณสุขใกล้บ้าน" if red_count > 0
                  else "รักษาสุขภาพดีๆ ต่อไป ออกกำลังกายสม่ำเสมอ",
            "en": "Free health screening at your nearest public health center" if red_count > 0
                  else "Keep up the healthy lifestyle!",
        },
        "lang": lang,
    }

    cache_set(cache_key, result, TTL_T3_FILTERED)
    return result
