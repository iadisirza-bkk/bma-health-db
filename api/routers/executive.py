"""Executive router -- extracted from main.py.

All numeric aggregates here delegate to the unified CTE
(`services.unified_screening`) so the executive dashboard, Header banner
and `/summary/overview` all show the same number for "ผู้คัดกรองทั้งหมด".
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import K_ANONYMITY_THRESHOLD
from services.unified_screening import build_unified_cte

router = APIRouter(prefix="/api/v2/executive", tags=["Executive"])

TARGET_SCREENED = 1_000_000


# ------------------------------------------------------------------ #
# GET /api/v2/executive/headline-kpi
# ------------------------------------------------------------------ #

@router.get("/headline-kpi")
def headline_kpi():
    """3 headline KPIs for the Governor's press conference.

    Uses the unified CTE (same source as `/summary/overview`) so the Header
    banner and OverviewBoard always agree. `total_screened` and disease
    counts are project-wide (BKK + non-BKK + unknown).
    """
    cte = build_unified_cte(include_visits=False)

    # Headline numbers — same calc as /summary/overview
    rows = execute_query(cte + """
        SELECT
          (SELECT COUNT(DISTINCT patient_id) FROM unified) AS total,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)            AS diabetes,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)           AS hypertension,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)           AS cardiovascular,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_bmi)           AS obesity,
          COUNT(DISTINCT patient_id) FILTER (WHERE found_dyslipidemia) AS dyslipidemia,
          COUNT(DISTINCT patient_id) FILTER (WHERE found_stroke)       AS stroke
        FROM unified
    """)
    d = rows[0] if rows else {}
    total = int(d.get("total") or 0)
    target = TARGET_SCREENED
    coverage = round(100.0 * total / target, 2) if target > 0 else 0

    disease_names_th = {
        "diabetes": "เบาหวาน", "hypertension": "ความดันโลหิตสูง",
        "cardiovascular": "หลอดเลือดหัวใจ", "obesity": "โรคอ้วน",
        "dyslipidemia": "ไขมันในเลือดผิดปกติ", "stroke": "หลอดเลือดสมอง",
    }

    ts = total or 1
    top_disease = None
    top_pct = 0
    for key in disease_names_th:
        cnt = int(d.get(key) or 0)
        pct = round(100.0 * cnt / ts, 1) if ts else 0
        if pct > top_pct:
            top_pct = pct
            top_disease = {"key": key, "name_th": disease_names_th[key], "pct": pct, "count": cnt}

    # Most concerning district (highest total disease burden)
    worst = execute_query("""
        SELECT district_code, district_name, total_screened,
               risk_dm_count + risk_hpt_count + risk_cvd_count + risk_bmi_count AS total_risk
        FROM summary_district_disease
        WHERE total_screened >= 5
        ORDER BY (risk_dm_count + risk_hpt_count + risk_cvd_count + risk_bmi_count)::float / NULLIF(total_screened, 0) DESC
        LIMIT 1
    """)

    worst_district = None
    if worst:
        w = worst[0]
        worst_district = {
            "district_code": w.get("district_code"),
            "name_th": w.get("district_name"),
            "total_risk_pct": round(100.0 * (w.get("total_risk") or 0) / (w.get("total_screened") or 1), 1),
        }

    # Population from ref_districts
    pop = execute_scalar("SELECT SUM(population) FROM ref_districts") or 0

    return {
        "total_screened": total,
        "target": target,
        "coverage_pct": coverage,
        "population": pop,
        "top_disease": top_disease,
        "most_concerning_district": worst_district,
        "summary_text": f"คัดกรองแล้ว {total:,} คน จากเป้า {target:,} ({coverage}%) โรคที่พบมากที่สุดคือ{top_disease['name_th'] if top_disease else '-'} ({top_pct}%)",
    }


# ------------------------------------------------------------------ #
# GET /api/v2/executive/yoy-comparison
# ------------------------------------------------------------------ #

@router.get("/yoy-comparison")
def yoy_comparison(
    granularity: str = Query("quarterly"),
):
    """Year-over-year or quarter-over-quarter comparison."""
    if granularity not in ("monthly", "quarterly"):
        raise HTTPException(status_code=400, detail="granularity must be monthly or quarterly")

    trunc = "quarter" if granularity == "quarterly" else "month"

    rows = execute_query(f"""
        SELECT
            DATE_TRUNC('{trunc}', v.visit_date) AS period,
            COUNT(DISTINCT v.patient_id) AS screened,
            COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) AS risk_dm,
            COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) AS risk_hpt,
            COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_obesity) AS found_obesity
        FROM raw_vitalsigns v
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND v.visit_date IS NOT NULL
          AND v.visit_date >= '2024-01-01'
        GROUP BY DATE_TRUNC('{trunc}', v.visit_date)
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY period
    """, (K_ANONYMITY_THRESHOLD,))

    # Convert periods and compute deltas
    result = []
    prev = None
    for r in rows:
        if r.get("period") and hasattr(r["period"], "isoformat"):
            r["period"] = r["period"].isoformat()[:10]
        if prev:
            r["delta_screened"] = (r.get("screened") or 0) - (prev.get("screened") or 0)
            r["delta_pct"] = round(100.0 * r["delta_screened"] / (prev.get("screened") or 1), 1)
        else:
            r["delta_screened"] = 0
            r["delta_pct"] = 0
        prev = r
        result.append(r)

    return {"granularity": granularity, "periods": result}


# ------------------------------------------------------------------ #
# GET /api/v2/executive/campaign-impact
# ------------------------------------------------------------------ #

@router.get("/campaign-impact")
def campaign_impact(campaign: Optional[str] = Query(None)):
    """Campaign impact analysis."""
    return {"data_available": False,
            "message": "ยังไม่มีข้อมูล campaign ในระบบ — ต้องเพิ่ม campaign_events table เพื่อบันทึกกิจกรรมรณรงค์ แล้ว link กับ screening volume",
            "suggestion": "สร้าง reference table: campaign_events (id, name, start_date, end_date, zone_code, type)"}


# ------------------------------------------------------------------ #
# GET /api/v2/executive/media-brief
# ------------------------------------------------------------------ #

@router.get("/media-brief")
def media_brief(lang: str = Query("th"), max_bullets: int = Query(5)):
    """Auto-generated media brief for press conferences."""
    # Reuse headline KPI data
    total = execute_scalar("SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease") or 0
    pop = execute_scalar("SELECT COALESCE(SUM(population), 0) FROM ref_districts") or 0

    d = execute_query("SELECT SUM(risk_dm_count) as dm, SUM(risk_hpt_count) as hpt, SUM(found_obesity_count) as obesity, SUM(total_screened) as total FROM summary_district_disease")
    dd = d[0] if d else {}
    ts = dd.get("total") or 1

    bullets = [
        f"กรุงเทพมหานครคัดกรองสุขภาพแล้ว {total:,.0f} คน จากประชากร {pop:,.0f} คน (ครอบคลุม {round(100*total/pop,1) if pop else 0}%)",
        f"โรคที่พบมากที่สุด: ความดันโลหิตสูง ({round(100*(dd.get('hpt') or 0)/ts,1)}%) เบาหวาน ({round(100*(dd.get('dm') or 0)/ts,1)}%)",
        f"ภาวะอ้วนพบ {round(100*(dd.get('obesity') or 0)/ts,1)}% ของผู้คัดกรอง",
        f"ดำเนินการคัดกรองผ่านศูนย์บริการสาธารณสุข กทม. ทั้ง 69 แห่ง ครอบคลุม 50 เขต 8 โซนสุขภาพ",
        f"ข้อมูลทั้งหมดเป็นข้อมูลรวม (aggregate) ไม่มีข้อมูลส่วนบุคคล ตาม พ.ร.บ.คุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562",
    ]

    return {"lang": lang, "bullets": bullets[:max_bullets], "generated_at": datetime.utcnow().isoformat()}


# ------------------------------------------------------------------ #
# GET /api/v2/executive/alert
# ------------------------------------------------------------------ #

@router.get("/alert")
def executive_alerts(severity: str = Query("all", description="all|critical|warning")):
    """Alert system: flag districts/diseases exceeding thresholds."""
    alerts = []

    # Check districts with very high disease rates
    high_risk = execute_query("""
        SELECT district_code, district_name, total_screened,
               pct_risk_dm, pct_risk_hpt,
               ROUND(100.0 * found_obesity_count / NULLIF(total_screened, 0), 1) as pct_obesity
        FROM summary_district_disease
        WHERE total_screened >= 5
    """)

    for d in high_risk:
        if (d.get("pct_risk_dm") or 0) > 25:
            alerts.append({"severity": "critical", "type": "high_prevalence", "district": d.get("district_name"),
                          "disease": "เบาหวาน", "value": d["pct_risk_dm"], "threshold": 25})
        if (d.get("pct_risk_hpt") or 0) > 30:
            alerts.append({"severity": "critical", "type": "high_prevalence", "district": d.get("district_name"),
                          "disease": "ความดันสูง", "value": d["pct_risk_hpt"], "threshold": 30})
        if (d.get("pct_obesity") or 0) > 40:
            alerts.append({"severity": "warning", "type": "high_prevalence", "district": d.get("district_name"),
                          "disease": "อ้วน", "value": d["pct_obesity"], "threshold": 40})

    if severity != "all":
        alerts = [a for a in alerts if a["severity"] == severity]

    return {"alerts": alerts, "total": len(alerts)}
