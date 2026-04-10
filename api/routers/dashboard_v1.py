"""Dashboard API router providing role-based views of health screening data.

Sync port — uses load_district_data() from data_adapter (psycopg2).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException

from data.facts import DCODE_TO_ZONE as ZONE_MAPPING, ZONE_NAMES_EN as ZONE_NAMES
from services.data_adapter import load_district_data

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _zone_for_dcode(dcode: str) -> str:
    return ZONE_MAPPING.get(dcode, "1")


# ---------------------------------------------------------------------------
# 1. Governor Dashboard
# ---------------------------------------------------------------------------

@router.get("/governor")
def governor_dashboard():
    """City-wide overview for the Bangkok Governor.

    Includes KPIs per disease, zone comparisons, and top-5 risk districts.
    """
    data = load_district_data()
    total_screened = sum(d["total_screened"] for d in data.values())

    # --- Disease KPIs ---
    disease_agg: dict[str, dict[str, Any]] = {}
    for d in data.values():
        for dk, dd in d["diseases"].items():
            if dk not in disease_agg:
                disease_agg[dk] = {
                    "name_th": dd["name"],
                    "name_en": dd["name_en"],
                    "weighted_sum": 0.0,
                    "total_screened": 0,
                    "total_at_risk": 0,
                    "district_pcts": [],
                }
            w = dd["total_screened"]
            disease_agg[dk]["weighted_sum"] += dd["pct_at_risk"] * w
            disease_agg[dk]["total_screened"] += w
            disease_agg[dk]["total_at_risk"] += round(dd["pct_at_risk"] * w / 100)
            disease_agg[dk]["district_pcts"].append((d["dcode"], dd["pct_at_risk"]))

    disease_kpis: list[dict] = []
    top_risk: dict[str, list[dict]] = {}

    for dk in sorted(disease_agg.keys()):
        agg = disease_agg[dk]
        wpct = agg["weighted_sum"] / agg["total_screened"] if agg["total_screened"] > 0 else 0.0
        disease_kpis.append({
            "disease": dk,
            "name_th": agg["name_th"],
            "name_en": agg["name_en"],
            "pct_at_risk": round(wpct, 2),
            "total_at_risk": agg["total_at_risk"],
            "total_screened": agg["total_screened"],
        })

        # Top 5 risk districts
        sorted_dists = sorted(agg["district_pcts"], key=lambda x: x[1], reverse=True)[:5]
        top_risk[dk] = [
            {
                "rank": i + 1,
                "dcode": dcode,
                "name_th": data[dcode]["name_th"],
                "name_en": data[dcode]["name_en"],
                "pct_at_risk": pct,
                "total_screened": data[dcode]["total_screened"],
            }
            for i, (dcode, pct) in enumerate(sorted_dists)
        ]

    # --- Zone comparison ---
    zone_agg: dict[str, dict[str, Any]] = {}
    for dcode, d in data.items():
        zc = _zone_for_dcode(dcode)
        if zc not in zone_agg:
            zone_agg[zc] = {"total_screened": 0, "district_count": 0, "disease_weighted": {}, "disease_totals": {}}
        zone_agg[zc]["total_screened"] += d["total_screened"]
        zone_agg[zc]["district_count"] += 1
        for dk, dd in d["diseases"].items():
            w = dd["total_screened"]
            zone_agg[zc]["disease_weighted"].setdefault(dk, 0.0)
            zone_agg[zc]["disease_weighted"][dk] += dd["pct_at_risk"] * w
            zone_agg[zc]["disease_totals"].setdefault(dk, 0)
            zone_agg[zc]["disease_totals"][dk] += w

    zone_comparison: list[dict] = []
    for zc in sorted(zone_agg.keys()):
        za = zone_agg[zc]
        diseases_pct: dict[str, float] = {}
        for dk in za["disease_weighted"]:
            t = za["disease_totals"][dk]
            diseases_pct[dk] = round(za["disease_weighted"][dk] / t, 2) if t > 0 else 0.0
        zone_comparison.append({
            "zone_code": zc,
            "zone_name": ZONE_NAMES.get(zc, f"Zone {zc}"),
            "total_screened": za["total_screened"],
            "district_count": za["district_count"],
            "diseases": diseases_pct,
        })

    return {
        "total_districts": len(data),
        "total_screened": total_screened,
        "disease_kpis": disease_kpis,
        "zone_comparison": zone_comparison,
        "top_risk_districts": top_risk,
        "pm25_city_average": None,
        "generated_at": _timestamp(),
    }


# ---------------------------------------------------------------------------
# 2. District Director Dashboard
# ---------------------------------------------------------------------------

@router.get("/district/{dcode}")
def district_director_dashboard(dcode: str):
    """Detailed view for a specific district director.

    Shows the district's stats compared with zone and city averages,
    plus ranking position among all 50 districts.
    """
    data = load_district_data()
    if dcode not in data:
        raise HTTPException(status_code=404, detail=f"District {dcode} not found")

    district = data[dcode]
    zone_code = _zone_for_dcode(dcode)
    zone_name = ZONE_NAMES.get(zone_code, f"Zone {zone_code}")

    # Pre-compute city-wide and zone-wide aggregates
    city_disease_pcts: dict[str, list[tuple[str, float]]] = {}
    zone_dcodes = {dc for dc, zc in ZONE_MAPPING.items() if zc == zone_code}

    for dc, d in data.items():
        for dk, dd in d["diseases"].items():
            city_disease_pcts.setdefault(dk, []).append((dc, dd["pct_at_risk"]))

    diseases_detail: list[dict] = []
    rank_sum = 0
    rank_count = 0

    for dk, dd in district["diseases"].items():
        # City average
        all_pcts = city_disease_pcts.get(dk, [])
        city_avg = float(np.mean([p for _, p in all_pcts])) if all_pcts else 0.0

        # Zone average
        zone_pcts = [p for dc, p in all_pcts if dc in zone_dcodes]
        zone_avg = float(np.mean(zone_pcts)) if zone_pcts else 0.0

        # Rank (highest risk = rank 1)
        sorted_pcts = sorted(all_pcts, key=lambda x: x[1], reverse=True)
        rank = next((i + 1 for i, (dc, _) in enumerate(sorted_pcts) if dc == dcode), len(data))
        rank_sum += rank
        rank_count += 1

        # Indicator statuses
        indicators: list[dict] = []
        for ind_key, ind_data in dd["indicators"].items():
            cutoff = ind_data.get("cutoff")
            pct_above = ind_data.get("pct_above_cutoff")
            if pct_above is not None and cutoff is not None:
                if pct_above > 30:
                    status = "critical"
                elif pct_above > 15:
                    status = "warning"
                else:
                    status = "normal"
            else:
                status = "normal"

            indicators.append({
                "indicator": ind_key,
                "label": ind_data["label"],
                "unit": ind_data["unit"],
                "mean": ind_data["mean"],
                "cutoff": cutoff,
                "pct_above_cutoff": pct_above,
                "count_above": ind_data.get("count_above"),
                "status": status,
            })

        diseases_detail.append({
            "disease": dk,
            "name_th": dd["name"],
            "name_en": dd["name_en"],
            "pct_at_risk": dd["pct_at_risk"],
            "total_screened": dd["total_screened"],
            "zone_avg_pct": round(zone_avg, 2),
            "city_avg_pct": round(city_avg, 2),
            "rank_among_50": rank,
            "indicators": indicators,
        })

    overall_rank = round(rank_sum / rank_count) if rank_count > 0 else 0

    return {
        "dcode": dcode,
        "name_th": district["name_th"],
        "name_en": district["name_en"],
        "total_screened": district["total_screened"],
        "zone_code": zone_code,
        "zone_name": zone_name,
        "diseases": diseases_detail,
        "overall_rank": overall_rank,
        "generated_at": _timestamp(),
    }


# ---------------------------------------------------------------------------
# 3. Medical Dashboard
# ---------------------------------------------------------------------------

@router.get("/medical")
def medical_dashboard():
    """Clinical indicator focus view for the Medical Department.

    Provides per-indicator statistics across all districts,
    screening completion rates, and risk distribution data.
    """
    data = load_district_data()
    total_screened = sum(d["total_screened"] for d in data.values())

    # --- Per-indicator city-wide stats ---
    indicator_collector: dict[str, dict[str, Any]] = {}

    for d in data.values():
        for dk, dd in d["diseases"].items():
            for ind_key, ind_data in dd["indicators"].items():
                comp_key = f"{dk}:{ind_key}"
                if comp_key not in indicator_collector:
                    indicator_collector[comp_key] = {
                        "indicator": ind_key,
                        "label": ind_data["label"],
                        "unit": ind_data["unit"],
                        "disease": dk,
                        "disease_en": dd["name_en"],
                        "means": [],
                        "cutoff": ind_data.get("cutoff"),
                        "pct_above_values": [],
                        "count_above_total": 0,
                    }
                if ind_data.get("mean") is not None:
                    indicator_collector[comp_key]["means"].append(ind_data["mean"])
                if ind_data.get("pct_above_cutoff") is not None:
                    indicator_collector[comp_key]["pct_above_values"].append(ind_data["pct_above_cutoff"])
                if ind_data.get("count_above") is not None:
                    indicator_collector[comp_key]["count_above_total"] += ind_data["count_above"]

    indicator_stats: list[dict] = []
    for comp_key in sorted(indicator_collector.keys()):
        ic = indicator_collector[comp_key]
        arr = np.array(ic["means"]) if ic["means"] else np.array([0.0])
        avg_pct = float(np.mean(ic["pct_above_values"])) if ic["pct_above_values"] else None

        indicator_stats.append({
            "indicator": ic["indicator"],
            "label": ic["label"],
            "unit": ic["unit"],
            "disease": ic["disease"],
            "disease_en": ic["disease_en"],
            "mean_across_districts": round(float(np.mean(arr)), 2),
            "median_across_districts": round(float(np.median(arr)), 2),
            "std_across_districts": round(float(np.std(arr, ddof=1)), 2) if len(arr) > 1 else 0.0,
            "min_district_mean": round(float(np.min(arr)), 2),
            "max_district_mean": round(float(np.max(arr)), 2),
            "cutoff": ic["cutoff"],
            "avg_pct_above_cutoff": round(avg_pct, 2) if avg_pct is not None else None,
            "total_above_cutoff": ic["count_above_total"] if ic["count_above_total"] > 0 else None,
        })

    # --- Screening rates ---
    disease_screening: dict[str, dict[str, Any]] = {}
    for d in data.values():
        for dk, dd in d["diseases"].items():
            if dk not in disease_screening:
                disease_screening[dk] = {"name_en": dd["name_en"], "total": 0, "count": 0}
            disease_screening[dk]["total"] += dd["total_screened"]
            disease_screening[dk]["count"] += 1

    screening_rates: list[dict] = []
    for dk in sorted(disease_screening.keys()):
        ds = disease_screening[dk]
        screening_rates.append({
            "disease": dk,
            "name_en": ds["name_en"],
            "total_screened": ds["total"],
            "districts_reporting": ds["count"],
            "avg_screened_per_district": round(ds["total"] / ds["count"], 0) if ds["count"] > 0 else 0,
        })

    # --- Risk distributions ---
    risk_distributions: list[dict] = []
    disease_pcts: dict[str, list[float]] = {}
    for d in data.values():
        for dk, dd in d["diseases"].items():
            disease_pcts.setdefault(dk, []).append(dd["pct_at_risk"])

    for dk in sorted(disease_pcts.keys()):
        pcts = np.array(disease_pcts[dk])
        avg = float(np.mean(pcts))
        std = float(np.std(pcts, ddof=1)) if len(pcts) > 1 else 0.0

        low = int(np.sum(pcts < avg - std))
        high = int(np.sum(pcts > avg + std))
        medium = len(pcts) - low - high

        name_en = ""
        for d in data.values():
            if dk in d["diseases"]:
                name_en = d["diseases"][dk]["name_en"]
                break

        risk_distributions.append({
            "disease": dk,
            "name_en": name_en,
            "low_risk_districts": low,
            "medium_risk_districts": medium,
            "high_risk_districts": high,
            "city_average_pct": round(avg, 2),
            "city_std_pct": round(std, 2),
        })

    return {
        "total_districts": len(data),
        "total_screened": total_screened,
        "indicator_stats": indicator_stats,
        "screening_rates": screening_rates,
        "risk_distributions": risk_distributions,
        "generated_at": _timestamp(),
    }
