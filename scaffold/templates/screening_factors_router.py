"""FastAPI router generator for screening factors bulk endpoint.

Emits an additional endpoint inside api/routers/<key>.py:
  GET /api/v2/<key>/screening/factors/bulk
Returns city + every zone + every BKK district in a single payload —
the frontend caches this on map mount so subsequent hover tooltips
are pure dictionary lookups.

Response shape:
{
  "data": {
    "city":      ScreeningFactors,
    "zones":     {<zoneCode>: ScreeningFactors, ...},
    "districts": {<dcode>:    ScreeningFactors, ...}
  }
}
where ScreeningFactors = {
  scope, scope_id,
  area_abnormal_pct: float,  // baseline for lift calculation
  factors: [{factor_key, factor_label_th, factor_label_en,
             groups: [{group, label_th, label_en,
                       n_total, n_abnormal, abnormal_pct, lift}, ...],
             top_group}]
}
"""
from __future__ import annotations

from ..diseases import ScreeningSpec


def gen_screening_factors_endpoint(spec: ScreeningSpec) -> str:
    """Emits the /screening/factors/bulk endpoint code (appended to the
    screening router by gen_screening_router_with_factors)."""
    k = spec.key
    K = spec.short_upper
    return f'''
# =============================================================================
# /screening/factors/bulk — demographic/lifestyle factor cross-tab (mig {spec.migration_number + 100})
# =============================================================================

# Display labels for the 5 factors. Group-level labels live inline on each
# MV row (factor_group_th / factor_group_en), so we only carry header
# labels here.
_{k.upper()}_FACTOR_LABELS: dict[str, dict[str, str]] = {{
    "sex":       {{"th": "เพศ",                    "en": "Sex"}},
    "age_group": {{"th": "กลุ่มอายุ",               "en": "Age group"}},
    "smoke":     {{"th": "การสูบบุหรี่",             "en": "Smoking status"}},
    "alcohol":   {{"th": "การดื่มแอลกอฮอล์",         "en": "Alcohol consumption"}},
    "excercise": {{"th": "การออกกำลังกาย",          "en": "Exercise frequency"}},
}}

# Stable display order within each factor (ordinal, not by lift).
_{k.upper()}_GROUP_ORDER: dict[str, list[str]] = {{
    "sex":       ["male", "female"],
    "age_group": ["18-29", "30-44", "45-59", "60-74", "75+"],
    "smoke":     ["non_smoker", "former_smoker", "current_smoker"],
    "alcohol":   ["non_drinker", "former_drinker", "current_drinker"],
    "excercise": ["regular", "sometimes", "never"],
}}


def _shape_{k}_screening_factors(rows: list[dict], scope: str, scope_id: object) -> dict:
    """Reshape raw MV rows into the canonical screening-factors response.

    Lift baseline: the scope-level abnormal_pct using the factor with
    largest coverage (n_total) — typically `sex` (~99% coverage).
    """
    factor_totals: dict[str, tuple[int, int]] = {{}}
    for r in rows:
        fk = r["factor_key"]
        n_acc, abn_acc = factor_totals.get(fk, (0, 0))
        factor_totals[fk] = (n_acc + int(r["n_total"]), abn_acc + int(r["n_abnormal"]))
    if not factor_totals:
        return {{
            "scope":             scope,
            "scope_id":          scope_id,
            "area_abnormal_pct": 0.0,
            "factors":           [],
        }}
    baseline_fk = max(factor_totals, key=lambda k: factor_totals[k][0])
    base_n, base_abn = factor_totals[baseline_fk]
    area_pct = round(100.0 * base_abn / base_n, 2) if base_n > 0 else 0.0

    by_factor: dict[str, list[dict]] = {{}}
    for r in rows:
        fk = r["factor_key"]
        n_total = int(r["n_total"])
        n_abnormal = int(r["n_abnormal"])
        abn_pct = round(100.0 * n_abnormal / n_total, 2) if n_total > 0 else 0.0
        lift = round(abn_pct / area_pct, 2) if area_pct > 0 else 0.0
        by_factor.setdefault(fk, []).append({{
            "group":        r["factor_group"],
            "label_th":     r["factor_group_th"],
            "label_en":     r["factor_group_en"],
            "n_total":      n_total,
            "n_abnormal":   n_abnormal,
            "abnormal_pct": abn_pct,
            "lift":         lift,
        }})

    factors_payload: list[dict] = []
    for fk in _{k.upper()}_FACTOR_LABELS:
        groups = by_factor.get(fk, [])
        if not groups:
            continue
        order = {{g: i for i, g in enumerate(_{k.upper()}_GROUP_ORDER.get(fk, []))}}
        groups.sort(key=lambda g: order.get(g["group"], 99))
        eligible = [g for g in groups if g["n_total"] >= K_ANONYMITY_THRESHOLD]
        top_group = max(eligible, key=lambda g: g["lift"])["group"] if eligible else None
        factors_payload.append({{
            "factor_key":      fk,
            "factor_label_th": _{k.upper()}_FACTOR_LABELS[fk]["th"],
            "factor_label_en": _{k.upper()}_FACTOR_LABELS[fk]["en"],
            "groups":          groups,
            "top_group":       top_group,
        }})

    return {{
        "scope":             scope,
        "scope_id":          scope_id,
        "area_abnormal_pct": area_pct,
        "factors":           factors_payload,
    }}


@router.get("/screening/factors/bulk")
def {k}_screening_factors_bulk():
    """One-shot: city + all zones + all districts of {K} factor cross-tab.

    Cached aggressively under a single static key — payload is identical
    for every reader.
    """
    cache_key = "{k}:screening:factors:bulk"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    rows = execute_query(
        """
        SELECT scope, scope_id, factor_key, factor_group,
               factor_group_th, factor_group_en,
               n_total::bigint AS n_total,
               n_abnormal::bigint AS n_abnormal
        FROM public.mv_{k}_screening_factors
        """,
        None,
    )

    by_zone: dict[str, list[dict]] = {{}}
    by_dist: dict[str, list[dict]] = {{}}
    city_rows: list[dict] = []
    for r in rows:
        if r["scope"] == "city":
            city_rows.append(r)
        elif r["scope"] == "zone":
            by_zone.setdefault(r["scope_id"], []).append(r)
        elif r["scope"] == "district":
            by_dist.setdefault(r["scope_id"], []).append(r)

    payload = {{
        "data": {{
            "city":      _shape_{k}_screening_factors(city_rows, "city", None),
            "zones":     {{zc: _shape_{k}_screening_factors(rs, "zone", zc) for zc, rs in by_zone.items()}},
            "districts": {{dc: _shape_{k}_screening_factors(rs, "district", dc) for dc, rs in by_dist.items()}},
        }}
    }}

    cache_set(cache_key, payload, TTL_T3_FILTERED)
    return payload
'''
