"""Strategy router -- extracted from main.py."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from database import execute_query, execute_scalar

router = APIRouter(prefix="/api/v2/strategy", tags=["Strategy"])

# Standard Thai healthcare cost references (สปสช. 2567)
_SCREENING_COST_PER_PERSON = 350  # THB per person
_DM_TREATMENT_COST_YEAR = 15_000  # THB/year
_HPT_TREATMENT_COST_YEAR = 8_000  # THB/year
_STROKE_TREATMENT_COST = 200_000  # THB/episode
_EARLY_DETECTION_SAVING_PCT = 0.40  # 30-50%, use midpoint


# ------------------------------------------------------------------ #
# GET /api/v2/strategy/cost-per-screening
# ------------------------------------------------------------------ #

@router.get("/cost-per-screening")
def cost_per_screening():
    """Cost per screening by district using standard cost reference (350 THB/person)."""
    rows = execute_query("""
        SELECT district_code, district_name, zone_code, total_screened,
               risk_dm_count, risk_hpt_count, found_obesity_count
        FROM summary_district_disease
        WHERE total_screened > 0
        ORDER BY total_screened DESC
    """)

    for r in rows:
        screened = r.get("total_screened") or 0
        r["screening_cost_thb"] = screened * _SCREENING_COST_PER_PERSON
        r["cost_per_person"] = _SCREENING_COST_PER_PERSON
        # Cost per risk case found (efficiency metric)
        total_risk = (r.get("risk_dm_count") or 0) + (r.get("risk_hpt_count") or 0)
        r["cost_per_risk_found"] = round(r["screening_cost_thb"] / total_risk, 0) if total_risk > 0 else None

    total_screened = sum(r.get("total_screened") or 0 for r in rows)
    total_cost = total_screened * _SCREENING_COST_PER_PERSON

    return {
        "cost_reference": {"screening_per_person_thb": _SCREENING_COST_PER_PERSON, "source": "สปสช. 2567"},
        "total_screened": total_screened,
        "total_cost_thb": total_cost,
        "districts": rows,
    }


# ------------------------------------------------------------------ #
# GET /api/v2/strategy/budget-allocation-model
# ------------------------------------------------------------------ #

@router.get("/budget-allocation-model")
def budget_allocation_model(total_budget: float = Query(560_000_000, description="Total budget in THB")):
    """Allocate budget proportional to population x risk level per district."""
    rows = execute_query("""
        SELECT s.district_code, s.district_name, s.zone_code,
               s.total_screened, s.risk_dm_count, s.risk_hpt_count,
               d.population
        FROM summary_district_disease s
        JOIN ref_districts d ON d.dcode = s.district_code
        WHERE s.total_screened > 0
    """)

    # Score = population * (1 + risk_rate). Higher risk districts get more budget.
    for r in rows:
        screened = r.get("total_screened") or 1
        risk_rate = ((r.get("risk_dm_count") or 0) + (r.get("risk_hpt_count") or 0)) / screened
        r["risk_rate"] = round(risk_rate, 4)
        r["score"] = (r.get("population") or 0) * (1 + risk_rate)

    total_score = sum(r["score"] for r in rows) or 1
    for r in rows:
        r["allocation_pct"] = round(100.0 * r["score"] / total_score, 2)
        r["allocated_budget_thb"] = round(total_budget * r["score"] / total_score, 0)
        r["per_capita_thb"] = round(r["allocated_budget_thb"] / (r.get("population") or 1), 0)

    rows.sort(key=lambda x: x["allocated_budget_thb"], reverse=True)

    return {
        "total_budget_thb": total_budget,
        "model": "population_x_risk_weighted",
        "districts": rows,
    }


# ------------------------------------------------------------------ #
# GET /api/v2/strategy/roi-analysis
# ------------------------------------------------------------------ #

@router.get("/roi-analysis")
def roi_analysis():
    """ROI = (prevented_treatment_cost - screening_cost) / screening_cost."""
    d = execute_query("""
        SELECT SUM(total_screened) as total, SUM(risk_dm_count) as dm,
               SUM(risk_hpt_count) as hpt, SUM(found_obesity_count) as obesity
        FROM summary_district_disease
    """)
    dd = d[0] if d else {}
    total = dd.get("total") or 0
    dm = dd.get("dm") or 0
    hpt = dd.get("hpt") or 0

    screening_cost = total * _SCREENING_COST_PER_PERSON

    # Early detection prevents progression: estimated savings
    dm_savings = dm * _DM_TREATMENT_COST_YEAR * _EARLY_DETECTION_SAVING_PCT
    hpt_savings = hpt * _HPT_TREATMENT_COST_YEAR * _EARLY_DETECTION_SAVING_PCT
    # Prevented strokes (estimate 5% of HPT would have stroke without intervention)
    stroke_prevented = int(hpt * 0.05)
    stroke_savings = stroke_prevented * _STROKE_TREATMENT_COST * _EARLY_DETECTION_SAVING_PCT

    total_savings = dm_savings + hpt_savings + stroke_savings
    net_benefit = total_savings - screening_cost
    roi = round(net_benefit / screening_cost, 2) if screening_cost > 0 else 0

    return {
        "screening_cost_thb": screening_cost,
        "prevented_costs": {
            "dm_early_treatment_savings": round(dm_savings, 0),
            "hpt_early_treatment_savings": round(hpt_savings, 0),
            "stroke_prevention_savings": round(stroke_savings, 0),
            "strokes_potentially_prevented": stroke_prevented,
        },
        "total_savings_thb": round(total_savings, 0),
        "net_benefit_thb": round(net_benefit, 0),
        "roi_ratio": roi,
        "roi_interpretation": f"ทุก 1 บาทที่ลงทุนคัดกรอง ได้ผลตอบแทน {roi} บาท",
        "assumptions": {
            "screening_cost_per_person": _SCREENING_COST_PER_PERSON,
            "dm_treatment_cost_year": _DM_TREATMENT_COST_YEAR,
            "hpt_treatment_cost_year": _HPT_TREATMENT_COST_YEAR,
            "stroke_treatment_cost": _STROKE_TREATMENT_COST,
            "early_detection_saving_pct": _EARLY_DETECTION_SAVING_PCT,
            "stroke_risk_in_hpt_pct": 5,
        },
    }


# ------------------------------------------------------------------ #
# GET /api/v2/strategy/resource-optimization
# ------------------------------------------------------------------ #

@router.get("/resource-optimization")
def resource_optimization():
    """Rank districts by risk/resource ratio to identify under-served areas."""
    rows = execute_query("""
        SELECT s.district_code, s.district_name, s.zone_code,
               s.total_screened, s.risk_dm_count, s.risk_hpt_count,
               d.population,
               COUNT(c.id) as clinic_count
        FROM summary_district_disease s
        JOIN ref_districts d ON d.dcode = s.district_code
        LEFT JOIN ref_clinics c ON c.district_code = s.district_code
        WHERE s.total_screened > 0
        GROUP BY s.district_code, s.district_name, s.zone_code,
                 s.total_screened, s.risk_dm_count, s.risk_hpt_count, d.population
    """)

    for r in rows:
        pop = r.get("population") or 1
        screened = r.get("total_screened") or 0
        clinics = r.get("clinic_count") or 1
        total_risk = (r.get("risk_dm_count") or 0) + (r.get("risk_hpt_count") or 0)

        r["coverage_pct"] = round(100.0 * screened / pop, 2)
        r["risk_rate"] = round(100.0 * total_risk / screened, 2) if screened > 0 else 0
        r["population_per_clinic"] = round(pop / clinics, 0)
        r["screened_per_clinic"] = round(screened / clinics, 0)
        # Priority score: high risk + low coverage = needs more resources
        r["priority_score"] = round(r["risk_rate"] * (100 - r["coverage_pct"]) / 100, 2)

    rows.sort(key=lambda x: x["priority_score"], reverse=True)

    return {
        "model": "risk_adjusted_coverage_gap",
        "description": "Districts ranked by (risk_rate * coverage_gap). Higher = needs more resources.",
        "districts": rows,
    }


# ------------------------------------------------------------------ #
# GET /api/v2/strategy/projected-savings
# ------------------------------------------------------------------ #

@router.get("/projected-savings")
def projected_savings(
    target_coverage_pct: float = Query(80.0, description="Target screening coverage %"),
    years: int = Query(5, ge=1, le=10, description="Projection horizon in years"),
):
    """Estimate savings from early detection at target coverage levels."""
    pop = execute_scalar("SELECT COALESCE(SUM(population), 0) FROM ref_districts") or 0
    d = execute_query("""
        SELECT SUM(total_screened) as total, SUM(risk_dm_count) as dm,
               SUM(risk_hpt_count) as hpt
        FROM summary_district_disease
    """)
    dd = d[0] if d else {}
    current_screened = dd.get("total") or 0
    current_dm = dd.get("dm") or 0
    current_hpt = dd.get("hpt") or 0

    # Current risk rates
    dm_rate = current_dm / current_screened if current_screened > 0 else 0.10
    hpt_rate = current_hpt / current_screened if current_screened > 0 else 0.20

    target_screened = int(pop * target_coverage_pct / 100)
    additional_screened = max(0, target_screened - current_screened)

    # Project new cases found at current risk rates
    new_dm_found = int(additional_screened * dm_rate)
    new_hpt_found = int(additional_screened * hpt_rate)

    additional_screening_cost = additional_screened * _SCREENING_COST_PER_PERSON

    # Annual savings from early detection of new cases
    annual_dm_savings = new_dm_found * _DM_TREATMENT_COST_YEAR * _EARLY_DETECTION_SAVING_PCT
    annual_hpt_savings = new_hpt_found * _HPT_TREATMENT_COST_YEAR * _EARLY_DETECTION_SAVING_PCT
    strokes_prevented_annual = int(new_hpt_found * 0.05)
    annual_stroke_savings = strokes_prevented_annual * _STROKE_TREATMENT_COST * _EARLY_DETECTION_SAVING_PCT
    annual_total_savings = annual_dm_savings + annual_hpt_savings + annual_stroke_savings

    projections = []
    cumulative_savings = 0
    for y in range(1, years + 1):
        cumulative_savings += annual_total_savings
        net = cumulative_savings - additional_screening_cost
        projections.append({
            "year": y,
            "cumulative_savings_thb": round(cumulative_savings, 0),
            "net_benefit_thb": round(net, 0),
            "breakeven": net >= 0,
        })

    return {
        "current_coverage_pct": round(100.0 * current_screened / pop, 1) if pop else 0,
        "target_coverage_pct": target_coverage_pct,
        "additional_screenings_needed": additional_screened,
        "additional_screening_cost_thb": additional_screening_cost,
        "projected_new_cases": {"dm": new_dm_found, "hpt": new_hpt_found},
        "annual_savings_thb": round(annual_total_savings, 0),
        "projections": projections,
        "assumptions": {
            "screening_cost_per_person": _SCREENING_COST_PER_PERSON,
            "dm_treatment_cost_year": _DM_TREATMENT_COST_YEAR,
            "hpt_treatment_cost_year": _HPT_TREATMENT_COST_YEAR,
            "stroke_treatment_cost": _STROKE_TREATMENT_COST,
            "early_detection_saving_pct": _EARLY_DETECTION_SAVING_PCT,
            "stroke_risk_in_hpt_pct": 5,
        },
    }
