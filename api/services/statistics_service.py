"""Statistical analysis service for Bangkok health screening data.

Sync port of the source project's StatisticsService.
Uses load_district_data() from data_adapter (psycopg2/materialized views)
instead of reading JSON files.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
from scipy import stats as scipy_stats

from data.facts import DCODE_TO_ZONE as ZONE_MAPPING, ZONE_NAMES_EN as ZONE_NAMES, HEALTH_ZONES
from services.data_adapter import load_district_data

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lightweight result dicts (no Pydantic schemas — router serialises to JSON)
# ---------------------------------------------------------------------------

def descriptive_stats(dcode: str) -> dict:
    """Compute descriptive statistics for all indicators in a district."""
    data = load_district_data()
    if dcode not in data:
        raise ValueError(f"District {dcode} not found")

    district = data[dcode]
    disease_stats_list: list[dict] = []

    for disease_key, disease_data in district["diseases"].items():
        indicator_stats: list[dict] = []
        for ind_key, ind_data in disease_data["indicators"].items():
            all_means = [
                d["diseases"][disease_key]["indicators"][ind_key]["mean"]
                for d in data.values()
                if disease_key in d["diseases"]
                and ind_key in d["diseases"][disease_key]["indicators"]
                and d["diseases"][disease_key]["indicators"][ind_key].get("mean") is not None
            ]
            arr = np.array(all_means) if all_means else np.array([0.0])

            indicator_stats.append({
                "indicator": ind_key,
                "label": ind_data["label"],
                "unit": ind_data["unit"],
                "mean": float(ind_data["mean"]) if ind_data.get("mean") is not None else 0.0,
                "median": float(np.median(arr)),
                "std": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
                "cutoff": ind_data.get("cutoff"),
                "pct_above_cutoff": ind_data.get("pct_above_cutoff"),
                "count_above": ind_data.get("count_above"),
                "sample_size": disease_data["total_screened"],
            })

        disease_stats_list.append({
            "disease": disease_key,
            "name_en": disease_data["name_en"],
            "pct_at_risk": disease_data["pct_at_risk"],
            "total_screened": disease_data["total_screened"],
            "indicators": indicator_stats,
        })

    return {
        "dcode": district["dcode"],
        "name_th": district["name_th"],
        "name_en": district["name_en"],
        "total_screened": district["total_screened"],
        "diseases": disease_stats_list,
    }


def compare_districts(dcode1: str, dcode2: str) -> dict:
    """Compare two districts with statistical significance testing."""
    data = load_district_data()
    if dcode1 not in data:
        raise ValueError(f"District {dcode1} not found")
    if dcode2 not in data:
        raise ValueError(f"District {dcode2} not found")

    d1 = data[dcode1]
    d2 = data[dcode2]

    disease_comparisons: list[dict] = []
    all_diseases = set(d1["diseases"].keys()) | set(d2["diseases"].keys())

    for disease_key in sorted(all_diseases):
        if disease_key not in d1["diseases"] or disease_key not in d2["diseases"]:
            continue

        dd1 = d1["diseases"][disease_key]
        dd2 = d2["diseases"][disease_key]
        indicator_comparisons: list[dict] = []

        all_indicators = set(dd1["indicators"].keys()) | set(dd2["indicators"].keys())
        for ind_key in sorted(all_indicators):
            if ind_key not in dd1["indicators"] or ind_key not in dd2["indicators"]:
                continue

            ind1 = dd1["indicators"][ind_key]
            ind2 = dd2["indicators"][ind_key]
            mean1 = ind1.get("mean") or 0.0
            mean2 = ind2.get("mean") or 0.0
            diff = mean1 - mean2
            pct_diff = (diff / mean2 * 100) if mean2 != 0 else 0.0

            all_means = np.array([
                d["diseases"][disease_key]["indicators"][ind_key]["mean"]
                for d in data.values()
                if disease_key in d["diseases"]
                and ind_key in d["diseases"][disease_key]["indicators"]
                and d["diseases"][disease_key]["indicators"][ind_key].get("mean") is not None
            ])
            std_pop = float(np.std(all_means, ddof=1)) if len(all_means) > 1 else 1.0
            n1 = dd1["total_screened"]
            n2 = dd2["total_screened"]
            se = std_pop * np.sqrt(1.0 / max(n1, 1) + 1.0 / max(n2, 1)) if std_pop > 0 else 1.0
            z_stat = diff / se if se > 0 else 0.0
            p_value = float(2 * (1 - scipy_stats.norm.cdf(abs(z_stat))))

            indicator_comparisons.append({
                "indicator": ind_key,
                "label": ind1["label"],
                "unit": ind1["unit"],
                "district1_mean": mean1,
                "district2_mean": mean2,
                "difference": round(diff, 2),
                "pct_difference": round(pct_diff, 2),
                "statistically_significant": p_value < 0.05,
                "p_value": round(p_value, 6),
            })

        disease_comparisons.append({
            "disease": disease_key,
            "name_en": dd1["name_en"],
            "district1_pct_at_risk": dd1["pct_at_risk"],
            "district2_pct_at_risk": dd2["pct_at_risk"],
            "difference": round(dd1["pct_at_risk"] - dd2["pct_at_risk"], 2),
            "indicators": indicator_comparisons,
        })

    return {
        "district1": {"dcode": d1["dcode"], "name_th": d1["name_th"], "name_en": d1["name_en"], "total_screened": d1["total_screened"]},
        "district2": {"dcode": d2["dcode"], "name_th": d2["name_th"], "name_en": d2["name_en"], "total_screened": d2["total_screened"]},
        "diseases": disease_comparisons,
    }


def zone_summary(zone_code: str) -> dict:
    """Compute aggregated statistics for a zone (weighted by screened population)."""
    if zone_code not in ZONE_NAMES:
        raise ValueError(f"Zone {zone_code} not found. Valid zones: {list(ZONE_NAMES.keys())}")

    data = load_district_data()
    zone_dcodes = [dcode for dcode, zc in ZONE_MAPPING.items() if zc == zone_code]
    zone_districts = {dcode: data[dcode] for dcode in zone_dcodes if dcode in data}

    if not zone_districts:
        raise ValueError(f"No districts found for zone {zone_code}")

    total_screened = sum(d["total_screened"] for d in zone_districts.values())

    disease_agg: dict[str, dict[str, Any]] = {}
    for d in zone_districts.values():
        for disease_key, disease_data in d["diseases"].items():
            if disease_key not in disease_agg:
                disease_agg[disease_key] = {
                    "name_en": disease_data["name_en"],
                    "weighted_sum": 0.0,
                    "total_screened": 0,
                    "pcts": [],
                }
            weight = disease_data["total_screened"]
            disease_agg[disease_key]["weighted_sum"] += disease_data["pct_at_risk"] * weight
            disease_agg[disease_key]["total_screened"] += weight
            disease_agg[disease_key]["pcts"].append(disease_data["pct_at_risk"])

    zone_disease_stats: list[dict] = []
    for disease_key in sorted(disease_agg.keys()):
        agg = disease_agg[disease_key]
        weighted_pct = agg["weighted_sum"] / agg["total_screened"] if agg["total_screened"] > 0 else 0.0
        zone_disease_stats.append({
            "disease": disease_key,
            "name_en": agg["name_en"],
            "weighted_pct_at_risk": round(weighted_pct, 2),
            "min_pct": min(agg["pcts"]),
            "max_pct": max(agg["pcts"]),
            "total_screened": agg["total_screened"],
        })

    district_summaries: list[dict] = []
    for dcode, d in sorted(zone_districts.items()):
        pct_map = {dk: dd["pct_at_risk"] for dk, dd in d["diseases"].items()}
        district_summaries.append({
            "dcode": dcode,
            "name_en": d["name_en"],
            "total_screened": d["total_screened"],
            "pct_at_risk": pct_map,
        })

    return {
        "zone_code": zone_code,
        "zone_name": ZONE_NAMES[zone_code],
        "total_districts": len(zone_districts),
        "total_screened": total_screened,
        "diseases": zone_disease_stats,
        "districts": district_summaries,
    }


def city_overview() -> dict:
    """Compute Bangkok-wide summary statistics."""
    data = load_district_data()
    total_screened = sum(d["total_screened"] for d in data.values())

    disease_agg: dict[str, dict[str, Any]] = {}
    for d in data.values():
        for disease_key, disease_data in d["diseases"].items():
            if disease_key not in disease_agg:
                disease_agg[disease_key] = {
                    "name_en": disease_data["name_en"],
                    "weighted_sum": 0.0,
                    "total_screened": 0,
                    "pcts": [],
                    "total_at_risk": 0,
                }
            weight = disease_data["total_screened"]
            disease_agg[disease_key]["weighted_sum"] += disease_data["pct_at_risk"] * weight
            disease_agg[disease_key]["total_screened"] += weight
            disease_agg[disease_key]["pcts"].append(disease_data["pct_at_risk"])
            disease_agg[disease_key]["total_at_risk"] += round(disease_data["pct_at_risk"] * weight / 100)

    city_diseases: list[dict] = []
    for disease_key in sorted(disease_agg.keys()):
        agg = disease_agg[disease_key]
        pcts = np.array(agg["pcts"])
        weighted_pct = agg["weighted_sum"] / agg["total_screened"] if agg["total_screened"] > 0 else 0.0
        city_diseases.append({
            "disease": disease_key,
            "name_en": agg["name_en"],
            "weighted_pct_at_risk": round(weighted_pct, 2),
            "min_district_pct": float(np.min(pcts)),
            "max_district_pct": float(np.max(pcts)),
            "std_across_districts": round(float(np.std(pcts, ddof=1)), 2) if len(pcts) > 1 else 0.0,
            "total_at_risk": agg["total_at_risk"],
            "total_screened": agg["total_screened"],
        })

    return {
        "total_districts": len(data),
        "total_screened": total_screened,
        "diseases": city_diseases,
    }


def risk_ranking(disease: str) -> dict:
    """Rank all districts by pct_at_risk for a given disease."""
    data = load_district_data()

    valid_diseases: set[str] = set()
    for d in data.values():
        valid_diseases.update(d["diseases"].keys())
    if disease not in valid_diseases:
        raise ValueError(f"Disease '{disease}' not found. Valid diseases: {sorted(valid_diseases)}")

    entries: list[dict[str, Any]] = []
    for d in data.values():
        if disease in d["diseases"]:
            entries.append({
                "dcode": d["dcode"],
                "name_th": d["name_th"],
                "name_en": d["name_en"],
                "total_screened": d["total_screened"],
                "pct_at_risk": d["diseases"][disease]["pct_at_risk"],
            })

    entries.sort(key=lambda x: x["pct_at_risk"], reverse=True)

    pcts = [e["pct_at_risk"] for e in entries]
    city_avg = float(np.mean(pcts)) if pcts else 0.0

    rankings = []
    for i, entry in enumerate(entries):
        rankings.append({"rank": i + 1, **entry})

    disease_name_en = ""
    for d in data.values():
        if disease in d["diseases"]:
            disease_name_en = d["diseases"][disease]["name_en"]
            break

    return {
        "disease": disease,
        "name_en": disease_name_en,
        "rankings": rankings,
        "city_average": round(city_avg, 2),
    }


def trend_analysis(dcode: str, disease: str) -> dict:
    """Placeholder for future time-series trend analysis."""
    data = load_district_data()
    if dcode not in data:
        raise ValueError(f"District {dcode} not found")

    district = data[dcode]
    valid_diseases = set(district["diseases"].keys())
    if disease not in valid_diseases:
        raise ValueError(f"Disease '{disease}' not found for district {dcode}. Valid: {sorted(valid_diseases)}")

    disease_data = district["diseases"][disease]

    return {
        "dcode": dcode,
        "name_en": district["name_en"],
        "disease": disease,
        "disease_name_en": disease_data["name_en"],
        "data_points": [],
        "message": "Time-series data not yet available. Structure is ready for future integration.",
    }
