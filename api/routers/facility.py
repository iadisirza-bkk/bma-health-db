"""Facility router -- extracted from main.py."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import K_ANONYMITY_THRESHOLD

router = APIRouter(prefix="/api/v2/facility", tags=["Facility"])


# ------------------------------------------------------------------ #
# GET /api/v2/facility/performance
# ------------------------------------------------------------------ #

@router.get("/performance")
def facility_performance(
    district: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Screening performance per health facility (HPTCODE)."""
    conditions = []
    params = []
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT s.facility_code, s.district_code, s.total_screened,
               s.risk_dm, s.risk_hpt, s.found_dm, s.found_hpt,
               s.found_obesity, s.found_dyslipidemia,
               s.lab_completed, s.first_screening, s.last_screening
        FROM summary_facility s
        LEFT JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.total_screened DESC
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]

    # Convert dates to strings
    for r in rows:
        for k in ("first_screening", "last_screening"):
            if r.get(k) and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()

    return {"facilities": rows}


# ------------------------------------------------------------------ #
# GET /api/v2/facility/workload
# ------------------------------------------------------------------ #

@router.get("/workload")
def facility_workload(zone_code: Optional[str] = Query(None)):
    """Workload per facility: screenings per facility."""
    conditions = ["s.total_screened >= 5"]
    params = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT s.facility_code, s.district_code, d.name_th as district_name,
               s.total_screened, s.lab_completed,
               ROUND(100.0 * s.lab_completed / NULLIF(s.total_screened, 0), 1) as lab_completion_pct
        FROM summary_facility s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY s.total_screened DESC
    """, tuple(params) or None)

    return {"facilities": rows, "note": "ข้อมูลจำนวนเจ้าหน้าที่ (staffing) ยังไม่มีในระบบ — ต้องเพิ่ม ref_staff table"}


# ------------------------------------------------------------------ #
# GET /api/v2/facility/screening-yield-rank
# ------------------------------------------------------------------ #

@router.get("/screening-yield-rank")
def facility_screening_yield_rank(zone_code: Optional[str] = Query(None), disease: str = Query("diabetes")):
    """Rank facilities by screening yield (risk found / screened)."""
    risk_col = {"diabetes": "risk_dm", "hypertension": "risk_hpt"}.get(disease, "risk_dm")
    conditions = ["s.total_screened >= 5"]
    params = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = "WHERE " + " AND ".join(conditions)

    rows = execute_query(f"""
        SELECT s.facility_code, s.district_code, d.name_th,
               s.total_screened, s.{risk_col} as risk_count,
               ROUND(100.0 * s.{risk_col} / NULLIF(s.total_screened, 0), 1) as yield_pct
        FROM summary_facility s
        JOIN ref_districts d ON s.district_code = d.dcode
        {where}
        ORDER BY yield_pct DESC
    """, tuple(params) or None)

    # Add rank
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    return {"disease": disease, "facilities": rows}


# ------------------------------------------------------------------ #
# GET /api/v2/facility/staff-performance
# ------------------------------------------------------------------ #

@router.get("/staff-performance")
def staff_performance():
    """Staff-level screening performance."""
    return {"data_available": False,
            "message": "ข้อมูลผลงานรายเจ้าหน้าที่ไม่สามารถเปิดเผยผ่าน API ได้ — ต้องเข้าผ่าน Admin Panel",
            "reason": "PDPA: staff_code (FIRSTSTF) เป็นข้อมูลส่วนบุคคลของเจ้าหน้าที่"}


# ------------------------------------------------------------------ #
# GET /api/v2/facility/capacity-planning
# ------------------------------------------------------------------ #

@router.get("/capacity-planning")
def capacity_planning(zone_code: Optional[str] = Query(None), target_coverage: int = Query(80)):
    """Capacity planning: how many more screenings needed to reach target."""
    conditions = []
    params = []
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT d.dcode, d.name_th, d.population,
               COALESCE(s.total_screened, 0) as screened,
               ROUND(100.0 * COALESCE(s.total_screened, 0) / NULLIF(d.population, 0), 1) as current_coverage_pct,
               GREATEST(0, ROUND(d.population * %s / 100.0 - COALESCE(s.total_screened, 0))) as additional_needed
        FROM ref_districts d
        LEFT JOIN summary_district_disease s ON d.dcode = s.district_code
        {where}
        ORDER BY additional_needed DESC
    """, tuple([target_coverage] + params))

    total_needed = sum(r.get("additional_needed", 0) or 0 for r in rows)
    return {"target_coverage_pct": target_coverage, "districts": rows, "total_additional_needed": total_needed}


# ------------------------------------------------------------------ #
# GET /api/v2/facility/comparison
# ------------------------------------------------------------------ #

@router.get("/comparison")
def facility_comparison(facility1: str = Query(...), facility2: str = Query(...)):
    """Compare 2 facilities side by side."""
    f1 = execute_query("SELECT * FROM summary_facility WHERE facility_code = %s", (facility1,))
    f2 = execute_query("SELECT * FROM summary_facility WHERE facility_code = %s", (facility2,))

    if not f1 or not f2:
        raise HTTPException(status_code=404, detail="One or both facilities not found")

    return {"facility_1": f1[0], "facility_2": f2[0]}
