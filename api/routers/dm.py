"""DM Classification router — exposes mv_dm_classification (4-pattern Venn).

Pattern is a 4-char bitstring `c1c2c3c4`:
    c1 = risk     (DM risk per screening rules)
    c2 = diag     (already diagnosed DM)
    c3 = family   (family history of DM)
    c4 = fpg      (FPG-positive on this screening)

The MV is pre-aggregated and k-anonymity (n>=5) is already enforced at
build time via migration 213, so individual rows are safe to expose.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import execute_query, execute_scalar
from security import K_ANONYMITY_THRESHOLD
from cache import cache_get, cache_set, TTL_T3_FILTERED
from services.reports.blocks._stats_helpers import wilson_ci

router = APIRouter(prefix="/api/v2/dm", tags=["DM Classification"])

_VALID_SCOPES = {"city", "zone", "district", "region", "non_bkk"}
_VALID_REGIONS = {"N", "NE", "S", "C"}


def _build_where(scope: str, scope_id: Optional[str]) -> tuple[str, tuple]:
    """Return (where_clause, params) for the MV based on scope."""
    if scope == "city":
        return "", ()
    if scope == "zone":
        return (
            "WHERE district_code IN (SELECT dcode FROM public.ref_districts WHERE zone_code = %s)",
            (scope_id,),
        )
    # scope == "district"
    return "WHERE district_code = %s", (scope_id,)


@router.get("/classification")
def dm_classification(
    scope: str = Query(..., description="city | zone | district"),
    id: Optional[str] = Query(None, description="zone_code or district_code (required when scope != city)"),
    pattern: Optional[str] = Query(None, min_length=4, max_length=4, description="Optional 4-char pattern filter, e.g. '1010'"),
):
    """Aggregated DM 4-pattern classification + Wilson CIs + named groups."""
    if scope not in _VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope '{scope}'. Must be one of {sorted(_VALID_SCOPES)}",
        )
    if scope not in ("city", "non_bkk") and not id:
        raise HTTPException(
            status_code=422,
            detail=f"Query parameter 'id' is required when scope='{scope}'",
        )
    if scope == "region" and id not in _VALID_REGIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid region_code '{id}'. Must be one of {sorted(_VALID_REGIONS)}",
        )
    if pattern is not None and any(ch not in "01" for ch in pattern):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid pattern '{pattern}'. Must be a 4-char bitstring of 0/1.",
        )

    scope_id = id if scope not in ("city", "non_bkk") else None
    cache_key = f"dm:cls:{scope}:{scope_id}:{pattern}:s"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    if scope == "region":
        # Region scope reads from mv_dm_classification_region (mig 219).
        region_where = "WHERE region_code = %s"
        region_params: tuple = (scope_id,)
        if pattern is not None:
            region_where += " AND pattern = %s"
            region_params = region_params + (pattern,)
        rows = execute_query(
            f"""
            SELECT pattern, SUM(n_patients)::bigint AS n
            FROM public.mv_dm_classification_region
            {region_where}
            GROUP BY pattern
            ORDER BY pattern
            """,
            region_params,
        )
    elif scope == "non_bkk":
        # non_bkk: aggregate across ALL 4 regions (no id parameter).
        nb_where = ""
        nb_params: tuple = ()
        if pattern is not None:
            nb_where = "WHERE pattern = %s"
            nb_params = (pattern,)
        rows = execute_query(
            f"""
            SELECT pattern, SUM(n_patients)::bigint AS n
            FROM public.mv_dm_classification_region
            {nb_where}
            GROUP BY pattern
            ORDER BY pattern
            """,
            nb_params or None,
        )
    else:
        where, params = _build_where(scope, scope_id)
        extra = ""
        if pattern is not None:
            extra = (" AND pattern = %s" if where else "WHERE pattern = %s")
            params = params + (pattern,)

        rows = execute_query(
            f"""
            SELECT pattern, SUM(n_patients)::bigint AS n
            FROM public.mv_dm_classification
            {where}{extra}
            GROUP BY pattern
            ORDER BY pattern
            """,
            params or None,
        )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No rows for scope='{scope}', id='{scope_id}'",
        )

    # Per-district "newly found" (strict): patient self-said NO DM but
    # FPG ≥ 126. Sourced from mv_dm_new_findings (mig 215).
    nf_by_district: dict[str, int] = {}
    nf_rows = execute_query(
        "SELECT district_code, newly_found_strict FROM public.mv_dm_new_findings",
        None,
    )
    for r in nf_rows:
        nf_by_district[r["district_code"]] = int(r["newly_found_strict"] or 0)

    # Per-district breakdown (only for city scope, no pattern filter).
    # Lets the frontend recompute box plot / descriptive stats / zone ranking
    # for any pattern without re-fetching.
    districts_payload: list[dict] = []
    if scope == "city" and pattern is None:
        d_rows = execute_query(
            """
            SELECT m.district_code, m.pattern, SUM(m.n_patients)::bigint AS n,
                   d.zone_code, d.name_th AS district_name
            FROM public.mv_dm_classification m
            LEFT JOIN public.ref_districts d ON d.dcode = m.district_code
            GROUP BY m.district_code, m.pattern, d.zone_code, d.name_th
            ORDER BY m.district_code, m.pattern
            """,
            None,
        )
        # Pivot per district
        by_dc: dict[str, dict] = {}
        for r in d_rows:
            dc = r["district_code"]
            entry = by_dc.setdefault(dc, {
                "district_code": dc,
                "district_name": r.get("district_name"),
                "zone_code": r.get("zone_code"),
                "total": 0,
                "totals": {"c1_risk": 0, "c2_diag": 0, "c3_family": 0, "c4_fpg": 0},
                "named_groups": {"any_dm_signal": 0, "newly_found_dm": 0,
                                 "controlled_dm": 0, "uncontrolled_dm": 0,
                                 "preventive": 0},
            })
            n = int(r["n"])
            p = r["pattern"]
            entry["total"] += n
            if p[0] == "1": entry["totals"]["c1_risk"]   += n
            if p[1] == "1": entry["totals"]["c2_diag"]   += n
            if p[2] == "1": entry["totals"]["c3_family"] += n
            if p[3] == "1": entry["totals"]["c4_fpg"]    += n
            # any_dm_signal = c1 OR c2 OR c4 (family/c3 excluded — hereditary
            # risk, not active disease state). Including c3 made the factor
            # cross-tab tautological for "family history" group.
            if p[0] == "1" or p[1] == "1" or p[3] == "1":         entry["named_groups"]["any_dm_signal"]   += n
            if p[0] == "0" and p[1] == "1" and p[3] == "0":       entry["named_groups"]["controlled_dm"]   += n
            if p[1] == "1" and (p[0] == "1" or p[3] == "1"):      entry["named_groups"]["uncontrolled_dm"] += n
            if p == "0010":                                        entry["named_groups"]["preventive"]      += n
        # Inject strict newly-found per district (from mv_dm_new_findings).
        for dc, entry in by_dc.items():
            entry["named_groups"]["newly_found_dm"] = nf_by_district.get(dc, 0)
        # k-anon at district level — drop districts with total < K
        districts_payload = [
            v for v in by_dc.values() if v["total"] >= K_ANONYMITY_THRESHOLD
        ]
        districts_payload.sort(key=lambda v: v["district_code"])

    total = sum(int(r["n"]) for r in rows)

    # Edge-case k-anon suppression (MV already enforces per-row >=5).
    if total < K_ANONYMITY_THRESHOLD:
        raise HTTPException(
            status_code=404,
            detail=f"Suppressed: total_patients < {K_ANONYMITY_THRESHOLD}",
        )

    # Per-condition totals (sum n where the bit at index X is '1').
    c1_risk   = sum(int(r["n"]) for r in rows if r["pattern"][0] == "1")
    c2_diag   = sum(int(r["n"]) for r in rows if r["pattern"][1] == "1")
    c3_family = sum(int(r["n"]) for r in rows if r["pattern"][2] == "1")
    c4_fpg    = sum(int(r["n"]) for r in rows if r["pattern"][3] == "1")

    def _ci(k: int, n: int) -> dict:
        if n == 0:
            return {"p": 0.0, "lower": 0.0, "upper": 0.0}
        lo, hi = wilson_ci(k, n)
        return {"p": round(k / n, 4), "lower": round(lo, 4), "upper": round(hi, 4)}

    # Named clinical groups.
    # any_dm_signal: UNION of c1/c2/c4 (family/c3 excluded — see comment in
    # district loop). The "ทั้งหมด" chip uses this.
    any_dm_signal = sum(int(r["n"]) for r in rows
                        if r["pattern"][0] == "1" or r["pattern"][1] == "1" or r["pattern"][3] == "1")

    # newly_found_dm: STRICT — patient explicitly self-reported NO DM but
    # FPG ≥ 126 ("เจอใหม่ในโครงการ"). Sourced from mv_dm_new_findings (mig 215);
    # restrict to districts in scope. Region scope reads from
    # mv_dm_new_findings_region (mig 219).
    #
    # CITY scope MUST filter to BKK district codes (`^10\d\d$`) only —
    # mv_dm_new_findings includes non-BKK home districts (codes 11xx, 74xx, etc.)
    # of patients screened in BKK. Without this filter the city total inflates
    # past the per-zone sum (zones only contain BKK districts), and the tooltip
    # "ทั้ง BKK" / "All BKK" label becomes misleading.
    if scope == "city":
        newly_found_dm = sum(
            v for dc, v in nf_by_district.items()
            if re.match(r"^10\d\d$", dc)
        )
    elif scope == "zone":
        zone_dcodes = execute_query(
            "SELECT dcode FROM public.ref_districts WHERE zone_code = %s",
            (scope_id,),
        )
        newly_found_dm = sum(nf_by_district.get(r["dcode"], 0) for r in zone_dcodes)
    elif scope == "region":
        nf_region = execute_scalar(
            "SELECT newly_found_strict FROM public.mv_dm_new_findings_region WHERE region_code = %s",
            (scope_id,),
        )
        newly_found_dm = int(nf_region or 0)
    elif scope == "non_bkk":
        # Sum across all 4 regions (mig 219). 4 row aggregation, runs <5ms.
        nf_total = execute_scalar(
            "SELECT SUM(newly_found_strict)::bigint FROM public.mv_dm_new_findings_region",
            None,
        )
        newly_found_dm = int(nf_total or 0)
    else:  # district
        newly_found_dm = nf_by_district.get(scope_id or "", 0)

    controlled_dm   = sum(int(r["n"]) for r in rows if r["pattern"][0] == "0" and r["pattern"][1] == "1" and r["pattern"][3] == "0")
    uncontrolled_dm = sum(int(r["n"]) for r in rows if r["pattern"][1] == "1" and (r["pattern"][0] == "1" or r["pattern"][3] == "1"))
    preventive      = sum(int(r["n"]) for r in rows if r["pattern"] == "0010")

    patterns_payload = [
        {
            "pattern": r["pattern"],
            "n": int(r["n"]),
            "pct": round(int(r["n"]) * 100.0 / total, 2) if total else 0.0,
        }
        for r in rows
    ]

    # Per-subdistrict ranking (only for scope='district') from mv_dm_subdistrict.
    subdistricts_payload: list[dict] = []
    if scope == "district" and scope_id:
        sub_rows = execute_query(
            """
            SELECT subdistrict_code, n_total::bigint AS n_total,
                   n_signal::bigint AS n_signal, signal_pct
            FROM public.mv_dm_subdistrict
            WHERE district_code = %s
            ORDER BY signal_pct DESC
            """,
            (scope_id,),
        )
        subdistricts_payload = [
            {
                "subdistrict_code": r["subdistrict_code"],
                "n_total":    int(r["n_total"]),
                "n_signal":   int(r["n_signal"]),
                "signal_pct": float(r["signal_pct"] or 0),
            }
            for r in sub_rows
        ]

    result = {
        "data": {
            "scope": scope,
            "scope_id": scope_id,
            "total_patients": total,
            "totals": {
                "c1_risk":   c1_risk,
                "c2_diag":   c2_diag,
                "c3_family": c3_family,
                "c4_fpg":    c4_fpg,
            },
            "wilson_ci": {
                "c1_risk":   _ci(c1_risk,   total),
                "c2_diag":   _ci(c2_diag,   total),
                "c3_family": _ci(c3_family, total),
                "c4_fpg":    _ci(c4_fpg,    total),
            },
            "patterns": patterns_payload,
            "named_groups": {
                "any_dm_signal":   any_dm_signal,
                "newly_found_dm":  newly_found_dm,
                "controlled_dm":   controlled_dm,
                "uncontrolled_dm": uncontrolled_dm,
                "preventive":      preventive,
            },
            "districts": districts_payload,
            "subdistricts": subdistricts_payload,
        }
    }

    cache_set(cache_key, result, TTL_T3_FILTERED)
    return result


# =============================================================================
# /factors — DM × risk-factor cross-tab (mv_dm_factors, mig 217)
# =============================================================================

# Display labels for the 6 factors. Group-level Thai/English labels live
# inline on each MV row (factor_group_th / factor_group_en), so we only
# need the factor-level header labels here.
_FACTOR_LABELS: dict[str, dict[str, str]] = {
    "bmi_cat":   {"th": "ดัชนีมวลกาย (BMI)",        "en": "BMI category"},
    "smoke":     {"th": "การสูบบุหรี่",               "en": "Smoking status"},
    "alcohol":   {"th": "การดื่มแอลกอฮอล์",           "en": "Alcohol consumption"},
    "age_group": {"th": "กลุ่มอายุ",                  "en": "Age group"},
    "excercise": {"th": "การออกกำลังกาย",            "en": "Exercise frequency"},
    "family_dm": {"th": "ประวัติเบาหวานในครอบครัว",   "en": "Family history of DM"},
}

# Stable display order within each factor (ordinal, not by lift).
_GROUP_ORDER: dict[str, list[str]] = {
    "bmi_cat":   ["normal", "overweight", "obese", "severely_obese"],
    "smoke":     ["non_smoker", "former_smoker", "current_smoker"],
    "alcohol":   ["non_drinker", "former_drinker", "current_drinker"],
    "age_group": ["18-29", "30-44", "45-59", "60-74", "75+"],
    "excercise": ["regular", "sometimes", "never"],
    "family_dm": ["family_no", "family_yes"],
}


def _shape_factors(rows: list[dict], scope: str, scope_id: Optional[str]) -> dict:
    """Reshape raw MV rows (factor_key, factor_group, label_th/en, n, dm_n) into
    the canonical factors response payload. Used by both /factors and
    /factors/bulk. `rows` must already be filtered/aggregated to a single scope.
    """
    # Compute scope-level any_dm_signal % to use as the lift baseline.
    # Use the factor with max coverage (n) — typically bmi_cat (~99.9%
    # coverage) — as denominator anchor.
    factor_totals: dict[str, tuple[int, int]] = {}
    for r in rows:
        fk = r["factor_key"]
        n_acc, dm_acc = factor_totals.get(fk, (0, 0))
        factor_totals[fk] = (n_acc + int(r["n"]), dm_acc + int(r["dm_n"]))
    if not factor_totals:
        return {
            "scope":       scope,
            "scope_id":    scope_id,
            "zone_dm_pct": 0.0,
            "factors":     [],
        }
    baseline_fk = max(factor_totals, key=lambda k: factor_totals[k][0])
    base_n, base_dm = factor_totals[baseline_fk]
    zone_dm_pct = round(100.0 * base_dm / base_n, 2) if base_n > 0 else 0.0

    # Group rows by factor_key.
    by_factor: dict[str, list[dict]] = {}
    for r in rows:
        fk = r["factor_key"]
        n = int(r["n"])
        dm_n = int(r["dm_n"])
        dm_pct = round(100.0 * dm_n / n, 2) if n > 0 else 0.0
        lift = round(dm_pct / zone_dm_pct, 2) if zone_dm_pct > 0 else 0.0
        by_factor.setdefault(fk, []).append({
            "group":     r["factor_group"],
            "label_th":  r["factor_group_th"],
            "label_en":  r["factor_group_en"],
            "n":         n,
            "dm_n":      dm_n,
            "dm_pct":    dm_pct,
            "lift":      lift,
        })

    # Build factors list in stable order; sort groups within each factor by
    # the canonical order, then identify top_group (max lift, n>=5).
    factors_payload: list[dict] = []
    for fk in _FACTOR_LABELS:
        groups = by_factor.get(fk, [])
        if not groups:
            continue
        order = {g: i for i, g in enumerate(_GROUP_ORDER.get(fk, []))}
        groups.sort(key=lambda g: order.get(g["group"], 99))
        eligible = [g for g in groups if g["n"] >= K_ANONYMITY_THRESHOLD]
        top_group = max(eligible, key=lambda g: g["lift"])["group"] if eligible else None
        factors_payload.append({
            "factor_key":      fk,
            "factor_label_th": _FACTOR_LABELS[fk]["th"],
            "factor_label_en": _FACTOR_LABELS[fk]["en"],
            "groups":          groups,
            "top_group":       top_group,
        })

    return {
        "scope":       scope,
        "scope_id":    scope_id,
        "zone_dm_pct": zone_dm_pct,
        "factors":     factors_payload,
    }


@router.get("/factors")
def dm_factors(
    scope: str = Query(..., description="zone | city | district"),
    id: Optional[str] = Query(None, description="zone_code or district_code (required when scope != 'city')"),
):
    """DM × risk-factor cross-tab from public.mv_dm_factors (mig 217) /
    public.mv_dm_factors_district (mig 218).

    For each of 6 factors, return per-group (n, dm_n, dm_pct, lift) where
    `lift = dm_pct / zone_dm_pct`. `top_group` is the group with max lift
    (n>=5). City scope aggregates across all 8 zones.
    """
    if scope not in ("zone", "city", "district", "region", "non_bkk"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope '{scope}'. Must be 'zone', 'city', 'district', 'region', or 'non_bkk'.",
        )
    if scope == "zone" and not id:
        raise HTTPException(
            status_code=422,
            detail="Query parameter 'id' (zone_code) is required when scope='zone'",
        )
    if scope == "district":
        if not id:
            raise HTTPException(
                status_code=422,
                detail="Query parameter 'id' (district_code) is required when scope='district'",
            )
        if not re.match(r"^10\d\d$", id):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid district_code '{id}'. Must match ^10\\d\\d$ (BKK).",
            )
    if scope == "region":
        if not id:
            raise HTTPException(
                status_code=422,
                detail="Query parameter 'id' (region_code) is required when scope='region'",
            )
        if id not in _VALID_REGIONS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid region_code '{id}'. Must be one of {sorted(_VALID_REGIONS)}",
            )

    scope_id = id if scope not in ("city", "non_bkk") else None
    cache_key = f"dm:factors:{scope}:{scope_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # Pull cross-tab rows for the requested scope.
    if scope == "zone":
        rows = execute_query(
            """
            SELECT factor_key, factor_group, factor_group_th, factor_group_en,
                   n::bigint AS n, dm_n::bigint AS dm_n
            FROM public.mv_dm_factors
            WHERE zone_code = %s
            """,
            (scope_id,),
        )
    elif scope == "district":
        rows = execute_query(
            """
            SELECT factor_key, factor_group, factor_group_th, factor_group_en,
                   n::bigint AS n, dm_n::bigint AS dm_n
            FROM public.mv_dm_factors_district
            WHERE district_code = %s
            """,
            (scope_id,),
        )
    elif scope == "region":
        rows = execute_query(
            """
            SELECT factor_key, factor_group, factor_group_th, factor_group_en,
                   n::bigint AS n, dm_n::bigint AS dm_n
            FROM public.mv_dm_factors_region
            WHERE region_code = %s
            """,
            (scope_id,),
        )
    elif scope == "non_bkk":
        # non_bkk: aggregate factor counts across all 4 regions.
        rows = execute_query(
            """
            SELECT factor_key, factor_group,
                   MAX(factor_group_th) AS factor_group_th,
                   MAX(factor_group_en) AS factor_group_en,
                   SUM(n)::bigint    AS n,
                   SUM(dm_n)::bigint AS dm_n
            FROM public.mv_dm_factors_region
            GROUP BY factor_key, factor_group
            """,
            None,
        )
    else:
        rows = execute_query(
            """
            SELECT factor_key, factor_group,
                   MAX(factor_group_th) AS factor_group_th,
                   MAX(factor_group_en) AS factor_group_en,
                   SUM(n)::bigint    AS n,
                   SUM(dm_n)::bigint AS dm_n
            FROM public.mv_dm_factors
            GROUP BY factor_key, factor_group
            """,
            None,
        )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No rows for scope='{scope}', id='{scope_id}'",
        )

    result = {"data": _shape_factors(rows, scope, scope_id)}
    cache_set(cache_key, result, TTL_T3_FILTERED)
    return result


# =============================================================================
# /factors/bulk — pre-fetch ALL scopes for hover-tooltip frontend (mig 218)
# =============================================================================

@router.get("/factors/bulk")
def dm_factors_bulk():
    """Return city + every zone + every BKK district in one payload.

    The frontend uses this on map mount so subsequent hover tooltips are
    pure dictionary lookups by zone/district code (no re-fetch). Cached
    aggressively under a single static key since the data is identical for
    every reader.
    """
    cache_key = "dm:factors:bulk"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # Two scans — zone-level (for city aggregate + per-zone) and district-level.
    zone_rows = execute_query(
        """
        SELECT zone_code, factor_key, factor_group,
               factor_group_th, factor_group_en,
               n::bigint AS n, dm_n::bigint AS dm_n
        FROM public.mv_dm_factors
        """,
        None,
    )
    dist_rows = execute_query(
        """
        SELECT district_code, factor_key, factor_group,
               factor_group_th, factor_group_en,
               n::bigint AS n, dm_n::bigint AS dm_n
        FROM public.mv_dm_factors_district
        """,
        None,
    )
    region_rows = execute_query(
        """
        SELECT region_code, factor_key, factor_group,
               factor_group_th, factor_group_en,
               n::bigint AS n, dm_n::bigint AS dm_n
        FROM public.mv_dm_factors_region
        """,
        None,
    )

    # Bucket zone rows by zone_code; aggregate across zones for the city row.
    by_zone: dict[str, list[dict]] = {}
    city_acc: dict[tuple[str, str], dict] = {}
    for r in zone_rows:
        zc = r["zone_code"]
        by_zone.setdefault(zc, []).append(r)
        key = (r["factor_key"], r["factor_group"])
        agg = city_acc.get(key)
        if agg is None:
            city_acc[key] = {
                "factor_key":      r["factor_key"],
                "factor_group":    r["factor_group"],
                "factor_group_th": r["factor_group_th"],
                "factor_group_en": r["factor_group_en"],
                "n":               int(r["n"]),
                "dm_n":            int(r["dm_n"]),
            }
        else:
            agg["n"]    += int(r["n"])
            agg["dm_n"] += int(r["dm_n"])
    city_rows = list(city_acc.values())

    # Bucket district rows by district_code.
    by_district: dict[str, list[dict]] = {}
    for r in dist_rows:
        by_district.setdefault(r["district_code"], []).append(r)

    # Bucket region rows by region_code (mig 219, non-BKK).
    by_region: dict[str, list[dict]] = {}
    for r in region_rows:
        by_region.setdefault(r["region_code"], []).append(r)

    zones_payload: dict[str, dict] = {
        zc: _shape_factors(rs, "zone", zc) for zc, rs in by_zone.items()
    }
    districts_payload: dict[str, dict] = {
        dc: _shape_factors(rs, "district", dc) for dc, rs in by_district.items()
    }
    regions_payload: dict[str, dict] = {
        rc: _shape_factors(rs, "region", rc) for rc, rs in by_region.items()
    }
    city_payload = _shape_factors(city_rows, "city", None)

    # non_bkk: single bucket — sum the 4 region rows Python-side and shape
    # exactly like /factors?scope=non_bkk does.
    nb_acc: dict[tuple[str, str], dict] = {}
    for r in region_rows:
        key = (r["factor_key"], r["factor_group"])
        agg = nb_acc.get(key)
        if agg is None:
            nb_acc[key] = {
                "factor_key":      r["factor_key"],
                "factor_group":    r["factor_group"],
                "factor_group_th": r["factor_group_th"],
                "factor_group_en": r["factor_group_en"],
                "n":               int(r["n"]),
                "dm_n":            int(r["dm_n"]),
            }
        else:
            agg["n"]    += int(r["n"])
            agg["dm_n"] += int(r["dm_n"])
    non_bkk_payload = _shape_factors(list(nb_acc.values()), "non_bkk", None)

    result = {
        "data": {
            "city":      city_payload,
            "zones":     zones_payload,
            "districts": districts_payload,
            "regions":   regions_payload,
            "non_bkk":   non_bkk_payload,
        }
    }
    cache_set(cache_key, result, TTL_T3_FILTERED)
    return result
