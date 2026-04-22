"""KPI router -- extracted from main.py."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import enforce_k_anonymity, K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/kpi", tags=["KPI"])

TARGET_SCREENED = 1_000_000

DISEASE_KEYS = {
    "diabetes":       {"risk": "risk_dm",  "found": "found_dm",            "pct": "pct_risk_dm"},
    "hypertension":   {"risk": "risk_hpt", "found": "found_hpt",           "pct": "pct_risk_hpt"},
    "cardiovascular": {"risk": "risk_cvd", "found": "found_cvd",           "pct": "pct_risk_cvd"},
    "obesity":        {"risk": "risk_bmi", "found": "found_obesity",        "pct": None},
    "dyslipidemia":   {"risk": None,       "found": "found_dyslipidemia",  "pct": None},
    "stroke":         {"risk": None,       "found": "found_stroke",         "pct": None},
    "ckd":            {"risk": None,       "found": None,                   "pct": None},
    "anemia":         {"risk": None,       "found": None,                   "pct": None},
}


def _validate_disease_key(disease_key: str) -> None:
    if disease_key not in DISEASE_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid disease_key '{disease_key}'. Valid keys: {sorted(DISEASE_KEYS)}",
        )


# ------------------------------------------------------------------ #
# GET /api/v2/kpi/moph-targets
# ------------------------------------------------------------------ #

@router.get("/moph-targets")
def moph_targets():
    """Compare actual performance against Ministry of Public Health targets."""
    total = execute_scalar("SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease") or 0
    pop = execute_scalar("SELECT COALESCE(SUM(population), 0) FROM ref_districts") or 0

    disease_totals = execute_query("""
        SELECT
            SUM(total_screened) as total,
            SUM(risk_dm_count) as dm, SUM(risk_hpt_count) as hpt,
            SUM(found_obesity_count) as obesity
        FROM summary_district_disease
    """)
    d = disease_totals[0] if disease_totals else {}
    ts = d.get("total") or 1

    # MoPH KPI targets (standard Thai public health KPIs)
    kpis = [
        {
            "kpi_code": "NCD-01",
            "name_th": "ความครอบคลุมการคัดกรองสุขภาพ",
            "name_en": "Screening Coverage",
            "target_pct": 60.0,
            "actual_pct": round(100.0 * total / pop, 1) if pop > 0 else 0,
            "unit": "% ของประชากร",
        },
        {
            "kpi_code": "NCD-02",
            "name_th": "อัตราการพบผู้ป่วยเบาหวานรายใหม่",
            "name_en": "New DM Detection Rate",
            "target_pct": 5.0,
            "actual_pct": round(100.0 * (d.get("dm") or 0) / ts, 1),
            "unit": "% ของผู้คัดกรอง",
        },
        {
            "kpi_code": "NCD-03",
            "name_th": "อัตราการพบผู้ป่วยความดันรายใหม่",
            "name_en": "New HPT Detection Rate",
            "target_pct": 10.0,
            "actual_pct": round(100.0 * (d.get("hpt") or 0) / ts, 1),
            "unit": "% ของผู้คัดกรอง",
        },
        {
            "kpi_code": "NCD-04",
            "name_th": "อัตราภาวะอ้วน",
            "name_en": "Obesity Rate",
            "target_pct": 30.0,
            "actual_pct": round(100.0 * (d.get("obesity") or 0) / ts, 1),
            "unit": "% ของผู้คัดกรอง",
            "direction": "lower_is_better",
        },
    ]

    for kpi in kpis:
        direction = kpi.pop("direction", "higher_is_better")
        if direction == "lower_is_better":
            kpi["status"] = "ผ่าน" if kpi["actual_pct"] <= kpi["target_pct"] else "ไม่ผ่าน"
        else:
            kpi["status"] = "ผ่าน" if kpi["actual_pct"] >= kpi["target_pct"] else "ไม่ผ่าน"

    return {"total_screened": total, "population": pop, "kpis": kpis}


# ------------------------------------------------------------------ #
# GET /api/v2/kpi/screening-yield
# ------------------------------------------------------------------ #

@router.get("/screening-yield")
def screening_yield(
    disease: str = Query("diabetes"),
    zone_code: Optional[str] = Query(None),
):
    """Screening yield (% risk found / total screened) per district."""
    _validate_disease_key(disease)
    dk = DISEASE_KEYS[disease]

    risk_col = dk.get("risk")
    found_col = dk.get("found")
    count_col = None
    if risk_col:
        count_col = f"{risk_col}_count"
    elif found_col:
        count_col = f"{found_col}_count"

    if not count_col:
        raise HTTPException(
            status_code=400,
            detail=f"No risk/found column available for '{disease}'.",
        )

    conditions = ["s.total_screened > 0"]
    params: list = []
    if zone_code:
        conditions.append("s.zone_code = %s")
        params.append(zone_code)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT s.district_code, s.district_name, s.zone_code,
               s.total_screened,
               s.{count_col} AS risk_found,
               ROUND(100.0 * s.{count_col} / NULLIF(s.total_screened, 0), 2) AS yield_pct
        FROM summary_district_disease s
        {where}
        ORDER BY yield_pct DESC
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return {"disease": disease, "data": rows}


# ------------------------------------------------------------------ #
# GET /api/v2/kpi/control-rates
# ------------------------------------------------------------------ #

@router.get("/control-rates")
def control_rates(disease: str = Query("diabetes")):
    """Disease control rates (e.g., FBS < 126 for DM, BP < 140/90 for HPT).

    Reads from summary_disease_control (migration 016) — pre-aggregated per
    district, refreshed after every ETL. O(50) lookup instead of full scans
    of raw_vitalsigns + raw_lab_results.
    """
    # One row per district + sum across — single fast query.
    totals = execute_query("""
        SELECT COALESCE(SUM(lab_patients), 0)   AS lab_patients,
               COALESCE(SUM(dm_with_lab), 0)    AS dm_with_lab,
               COALESCE(SUM(dm_controlled), 0)  AS dm_controlled,
               COALESCE(SUM(hpt_with_bp), 0)    AS hpt_with_bp,
               COALESCE(SUM(hpt_controlled), 0) AS hpt_controlled
        FROM summary_disease_control
    """)
    t = totals[0] if totals else {}
    lab_count = int(t.get("lab_patients") or 0)

    if disease == "diabetes" and lab_count > 0:
        denom = int(t.get("dm_with_lab") or 0)
        num = int(t.get("dm_controlled") or 0)
        pct = round(100.0 * num / denom, 1) if denom > 0 else 0
        return {"disease": disease, "control_metric": "FBS < 126 mg/dL",
                "control_rate_pct": pct, "lab_patients": lab_count,
                "note": "Proxy metric. HbA1c not available in current dataset."}

    if disease == "hypertension":
        denom = int(t.get("hpt_with_bp") or 0)
        num = int(t.get("hpt_controlled") or 0)
        pct = round(100.0 * num / denom, 1) if denom > 0 else 0
        return {"disease": disease, "control_metric": "BP < 140/90 mmHg",
                "control_rate_pct": pct}

    return {"data_available": False, "message": f"ไม่มี control rate metric สำหรับ {disease} — ต้องรอข้อมูล lab เพิ่มเติม"}


# ------------------------------------------------------------------ #
# GET /api/v2/kpi/zone-comparison
# ------------------------------------------------------------------ #

@router.get("/zone-comparison")
def zone_comparison(kpi: str = Query("screening_coverage")):
    """Compare 8 zones on a single KPI."""
    if kpi == "screening_coverage":
        rows = execute_query("""
            SELECT z.zone_code, z.name_th,
                   COALESCE(SUM(s.total_screened), 0) as screened,
                   SUM(d.population) as population,
                   ROUND(100.0 * COALESCE(SUM(s.total_screened), 0) / NULLIF(SUM(d.population), 0), 2) as value
            FROM ref_health_zones z
            JOIN ref_districts d ON d.zone_code = z.zone_code
            LEFT JOIN summary_district_disease s ON s.zone_code = z.zone_code AND s.district_code = d.dcode
            GROUP BY z.zone_code, z.name_th
            ORDER BY value DESC
        """)
    elif kpi in ("dm_risk", "hpt_risk", "obesity_risk"):
        col_map = {"dm_risk": "risk_dm_count", "hpt_risk": "risk_hpt_count", "obesity_risk": "found_obesity_count"}
        col = col_map[kpi]
        rows = execute_query(f"""
            SELECT z.zone_code, z.name_th,
                   COALESCE(SUM(s.total_screened), 0) as screened,
                   COALESCE(SUM(s.{col}), 0) as risk_count,
                   ROUND(100.0 * COALESCE(SUM(s.{col}), 0) / NULLIF(SUM(s.total_screened), 0), 2) as value
            FROM ref_health_zones z
            LEFT JOIN summary_district_disease s ON s.zone_code = z.zone_code
            GROUP BY z.zone_code, z.name_th
            ORDER BY value DESC
        """)
    else:
        return {"error": f"Unknown KPI: {kpi}. Valid: screening_coverage, dm_risk, hpt_risk, obesity_risk"}

    return {"kpi": kpi, "zones": rows}


# ------------------------------------------------------------------ #
# GET /api/v2/kpi/progress-tracker
# ------------------------------------------------------------------ #

@router.get("/progress-tracker")
def progress_tracker(
    year: Optional[int] = Query(None),
    quarter: Optional[str] = Query(None, description="Q1, Q2, Q3, Q4"),
):
    """Quarterly progress vs targets."""
    # Query trends grouped by quarter
    rows = execute_query("""
        SELECT DATE_TRUNC('quarter', v.visit_date) as period,
               COUNT(DISTINCT v.patient_id) as screened,
               COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm) as dm_risk,
               COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt) as hpt_risk
        FROM raw_vitalsigns v
        WHERE v.cancel_status IS DISTINCT FROM 1 AND v.visit_date IS NOT NULL
        GROUP BY DATE_TRUNC('quarter', v.visit_date)
        HAVING COUNT(DISTINCT v.patient_id) >= %s
        ORDER BY period
    """, (K_ANONYMITY_THRESHOLD,))

    target_per_quarter = TARGET_SCREENED // 4
    for r in rows:
        if r.get("period") and hasattr(r["period"], "isoformat"):
            r["period"] = r["period"].isoformat()[:10]
        r["target"] = target_per_quarter
        r["progress_pct"] = round(100.0 * (r.get("screened") or 0) / target_per_quarter, 1)

    return {"target_annual": TARGET_SCREENED, "target_quarterly": target_per_quarter, "quarters": rows}


# ------------------------------------------------------------------ #
# GET /api/v2/kpi/benchmark
# ------------------------------------------------------------------ #

@router.get("/benchmark")
def benchmark():
    """Benchmark BMA against national averages (reference data)."""
    total = execute_scalar("SELECT COALESCE(SUM(total_screened), 0) FROM summary_district_disease") or 0
    pop = execute_scalar("SELECT COALESCE(SUM(population), 0) FROM ref_districts") or 0

    d = execute_query("""
        SELECT SUM(risk_dm_count) as dm, SUM(risk_hpt_count) as hpt,
               SUM(found_obesity_count) as obesity, SUM(total_screened) as total
        FROM summary_district_disease
    """)
    dd = d[0] if d else {}
    ts = dd.get("total") or 1

    # National reference values (from MoPH Health Survey 2024)
    benchmarks = [
        {"indicator": "DM prevalence", "indicator_th": "ความชุกเบาหวาน",
         "bma_pct": round(100.0 * (dd.get("dm") or 0) / ts, 1),
         "national_pct": 9.5, "source": "สำรวจสุขภาพ ครั้งที่ 6 (2562)"},
        {"indicator": "HPT prevalence", "indicator_th": "ความชุกความดันสูง",
         "bma_pct": round(100.0 * (dd.get("hpt") or 0) / ts, 1),
         "national_pct": 24.7, "source": "สำรวจสุขภาพ ครั้งที่ 6"},
        {"indicator": "Obesity prevalence", "indicator_th": "ความชุกโรคอ้วน",
         "bma_pct": round(100.0 * (dd.get("obesity") or 0) / ts, 1),
         "national_pct": 37.5, "source": "สำรวจสุขภาพ ครั้งที่ 6"},
        {"indicator": "Screening coverage", "indicator_th": "ความครอบคลุมการคัดกรอง",
         "bma_pct": round(100.0 * total / pop, 1) if pop else 0,
         "national_pct": 45.0, "source": "เป้าหมาย สปสช. 2567"},
    ]
    for b in benchmarks:
        b["vs_national"] = "สูงกว่า" if b["bma_pct"] > b["national_pct"] else "ต่ำกว่า"
        b["delta"] = round(b["bma_pct"] - b["national_pct"], 1)

    return {"benchmarks": benchmarks}


# =========================================================================== #
# Gap analysis (Doc 02 — รองผู้ว่า กทม.)
# =========================================================================== #

_KPI_TARGETS = {
    "screening_coverage_pct": 50.0,
    "pct_risk_dm": 8.0,
    "pct_risk_hpt": 12.0,
    "pct_risk_cvd": 5.0,
}


@router.get("/gap-analysis")
def gap_analysis(
    zone_code: Optional[str] = Query(None, description="Filter by zone"),
    kpi: str = Query("screening_coverage_pct", description="KPI to analyze: screening_coverage_pct, pct_risk_dm, pct_risk_hpt, pct_risk_cvd"),
):
    """Gap analysis: actual vs target KPI per district, ranked by urgency.
    วิเคราะห์ช่องว่าง: ผลงานจริง vs เป้า KPI ต่อเขต เรียงตามความเร่งด่วน
    ตอบโจทย์รองผู้ว่า กทม. (Doc 02)"""

    if kpi not in _KPI_TARGETS:
        return {"error": f"Invalid KPI. Valid: {sorted(_KPI_TARGETS.keys())}"}

    target_value = _KPI_TARGETS[kpi]

    conditions = ["s.total_screened >= %s"]
    params: list = [K_ANONYMITY_THRESHOLD]

    if zone_code:
        conditions.append("s.zone_code = %s")
        params.append(zone_code)

    where = " AND ".join(conditions)

    if kpi == "screening_coverage_pct":
        value_expr = "ROUND(100.0 * s.total_screened / NULLIF(d.population, 0), 1)"
    else:
        value_expr = f"s.{kpi}"

    rows = execute_query(f"""
        SELECT s.district_code, s.district_name, s.zone_code,
               s.total_screened,
               d.population,
               {value_expr} AS actual_value
        FROM summary_district_disease s
        JOIN ref_districts d ON d.dcode = s.district_code
        WHERE {where}
        ORDER BY {value_expr} ASC NULLS LAST
    """, tuple(params))

    districts = []
    for r in rows:
        actual = r.get("actual_value") or 0
        gap = round(target_value - actual, 1) if kpi == "screening_coverage_pct" else round(actual - target_value, 1)
        meets_target = actual >= target_value if kpi == "screening_coverage_pct" else actual <= target_value

        districts.append({
            **r,
            "target": target_value,
            "gap": gap,
            "meets_target": meets_target,
            "urgency": "critical" if abs(gap) > target_value * 0.5 else ("moderate" if abs(gap) > target_value * 0.2 else "on_track"),
        })

    # Sort: worst gap first
    districts.sort(key=lambda x: x["gap"], reverse=(kpi == "screening_coverage_pct"))

    critical_count = sum(1 for d in districts if d["urgency"] == "critical")
    on_track_count = sum(1 for d in districts if d["meets_target"])

    return {
        "kpi": kpi,
        "target": target_value,
        "zone_code": zone_code,
        "total_districts": len(districts),
        "on_track": on_track_count,
        "needs_attention": len(districts) - on_track_count,
        "critical": critical_count,
        "districts": districts,
    }
