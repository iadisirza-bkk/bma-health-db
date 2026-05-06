"""Summary router — overview, filtered, lab, mental-health, demographics."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query

from database import execute_query, execute_scalar
from security import enforce_k_anonymity, K_ANONYMITY_THRESHOLD
from cache import cache_get, cache_set, TTL_T2_AGGREGATE, TTL_T3_FILTERED, TTL_T4_STATIC
from services.unified_screening import UNIFIED_CTE, build_unified_cte, parse_sources

router = APIRouter(prefix="/api/v2/summary", tags=["Summary"])

TARGET_SCREENED = 1_000_000


# =========================================================================== #
# Overview
# =========================================================================== #

@router.get("/overview")
def overview(sources: Optional[str] = Query(None, description="Comma-separated subset of {portal,app1,app2}; default = all")):
    """Top-level screening overview with zone and disease breakdowns.

    Aggregation = per-source dispatch via UNIFIED_CTE (see fact/aggregation-base.md).
    Optional `sources` filter restricts to a subset, e.g. ?sources=portal,app1.
    """
    parsed_sources = parse_sources(sources)
    cache_key = f"summary:overview:{','.join(parsed_sources) if parsed_sources else 'all'}"
    hit = cache_get(cache_key)
    if hit is not None:
        return hit

    # ── Fast path: no source filter → read pre-aggregated MVs ──
    # `mv_summary_global` (1 row) + `mv_summary_zones` (8 rows) refreshed by
    # refresh_all_mvs after each ETL. ~38s → <100ms.
    if parsed_sources is None:
        global_row = execute_query("SELECT * FROM public.mv_summary_global LIMIT 1") or [{}]
        g = global_row[0]
        total = int(g.get("total_screened") or 0)
        total_visits = int(g.get("total_visits") or 0)
        bkk_screened = int(g.get("bkk_screened") or 0)
        bkk_visits = int(g.get("bkk_visits") or 0)
        non_bkk_screened = int(g.get("non_bkk_screened") or 0)
        non_bkk_visits = int(g.get("non_bkk_visits") or 0)
        unknown_screened = int(g.get("unknown_screened") or 0)
        unknown_visits = int(g.get("unknown_visits") or 0)

        by_zone = execute_query("""
            SELECT zone_code, name_th, total_screened, total_visits
            FROM public.mv_summary_zones ORDER BY zone_code
        """) or []

        by_disease = []
        ts = total or 1
        # NCD pipeline: keys come from scaffold/templates/summary_global.py
        # (single source of truth — also drives the MV column projection).
        try:
            import sys as _sys
            from pathlib import Path as _P
            _root = _P(__file__).resolve().parents[2]
            if str(_root) not in _sys.path:
                _sys.path.insert(0, str(_root))
            from scaffold.templates.summary_global import (  # type: ignore
                disease_keys_for_overview,
                screening_keys_for_overview,
            )
            ncd_keys = disease_keys_for_overview()
            screening_keys = screening_keys_for_overview()
        except Exception:
            ncd_keys = ["diabetes", "hypertension", "cardiovascular", "obesity", "dyslipidemia"]
            screening_keys = []

        for key in ncd_keys:
            cnt = int(g.get(key) or 0)
            by_disease.append({
                "disease_key": key,
                "total_at_risk": cnt,
                "pct": round(100.0 * cnt / ts, 2) if ts else 0,
            })

        # Screening-pipeline diseases: aggregated from mv_<key>_screening
        # rather than mv_summary_global (different denominator — only
        # patients who had the test performed). One small SELECT per disease.
        # Skip diseases already counted in the NCD list to avoid duplicates
        # (obesity + dyslipidemia appear in both registries).
        ncd_seen = set(ncd_keys)
        for skey in screening_keys:
            if skey in ncd_seen:
                continue
            try:
                row = execute_query(
                    f"SELECT SUM(n_total)::bigint AS n, SUM(n_abnormal)::bigint AS k "
                    f"FROM public.mv_{skey}_screening"
                ) or [{}]
                rr = row[0]
                n = int(rr.get("n") or 0)
                k = int(rr.get("k") or 0)
                by_disease.append({
                    "disease_key": skey,
                    "total_at_risk": k,
                    "pct": round(100.0 * k / n, 2) if n > 0 else 0.0,
                    "screening_total": n,  # tested-cohort denominator (≠ total_screened)
                })
            except Exception:
                continue

        zone_count = execute_scalar("SELECT COUNT(*) FROM ref_health_zones") or 0
        district_count = execute_scalar("SELECT COUNT(*) FROM ref_districts") or 0
        last_updated = execute_scalar("SELECT MAX(refreshed_at) FROM public.mv_summary_global")

        # Audit counts (cancelled rows + retry dedup) — kept identical to slow path.
        audit_row = execute_query("""
            SELECT
              COUNT(*) FILTER (WHERE v.data_source IN ('portal','app1') AND v.cancel_status = 1)
                AS vital_cancelled,
              COUNT(*) FILTER (WHERE v.data_source IN ('portal','app1') AND v.cancel_status IS DISTINCT FROM 1)
                AS vital_after_cancel
            FROM raw_vitalsigns v
        """) or [{}]
        a = audit_row[0]
        app2_audit = execute_query("""
            SELECT COUNT(*) FILTER (WHERE cancel_status = 1) AS cancelled,
                   COUNT(*) FILTER (WHERE cancel_status IS DISTINCT FROM 1) AS after_cancel
            FROM raw_homehealth WHERE data_source = 'app2'
        """) or [{}]
        a2 = app2_audit[0] if app2_audit else {}
        cancelled_total = int(a.get("vital_cancelled") or 0) + int(a2.get("cancelled") or 0)
        raw_after_cancel = int(a.get("vital_after_cancel") or 0) + int(a2.get("after_cancel") or 0)
        dropped_retry = max(0, raw_after_cancel - total_visits)

        result = {
            "total_screened": total,
            "total_visits": total_visits,
            "target": TARGET_SCREENED,
            "zones_count": zone_count,
            "districts_count": district_count,
            "last_updated": str(last_updated) if last_updated else None,
            "by_zone": by_zone,
            "by_disease": by_disease,
            "breakdown": {
                "bkk":         {"total_screened": bkk_screened,     "total_visits": bkk_visits},
                "non_bangkok": {"total_screened": non_bkk_screened, "total_visits": non_bkk_visits},
                "unknown":     {"total_screened": unknown_screened, "total_visits": unknown_visits},
            },
            "audit": {
                "dropped_cancelled": cancelled_total,
                "dropped_retry_30d": dropped_retry,
                "raw_after_cancel":  raw_after_cancel,
            },
        }
        cache_set(cache_key, result, TTL_T2_AGGREGATE)
        return result

    # ── Slow path: source-filtered queries fall back to the unified CTE ──
    cte = build_unified_cte(parsed_sources)

    # Per-source aggregation via UNIFIED_CTE (built dynamically by `cte`).
    # `unified` = all rows (for person + risk-flag counts).
    # `unified_visits` = distinct (source, patient_id, day) with >30-day
    # dedup applied (for visit counts) — see services/unified_screening.py.

    # Audit counts — visits dropped at each stage of the pipeline.
    # Used by the OverviewBoard info tooltip ("วิธีนับ" / "ตัดออก ...").
    audit_row = execute_query("""
        SELECT
          -- Cancelled rows: raw_vitalsigns has Portal + App1, App2 lives on raw_homehealth
          COUNT(*) FILTER (WHERE v.data_source IN ('portal', 'app1') AND v.cancel_status = 1) AS vital_cancelled,
          -- Total raw rows after cancel filter, per the project's row-level
          -- definition (same as user's `WHERE CANCELST != 1` count). This is
          -- what `dropped_retry_30d` is measured against.
          COUNT(*) FILTER (WHERE v.data_source IN ('portal', 'app1') AND v.cancel_status IS DISTINCT FROM 1) AS vital_after_cancel
        FROM raw_vitalsigns v
    """) or [{}]
    a = audit_row[0]
    app2_audit = execute_query("""
        SELECT
          COUNT(*) FILTER (WHERE cancel_status = 1)                  AS cancelled,
          COUNT(*) FILTER (WHERE cancel_status IS DISTINCT FROM 1)   AS after_cancel
        FROM raw_homehealth WHERE data_source = 'app2'
    """) or [{}]
    a2 = app2_audit[0] if app2_audit else {}
    cancelled_total = int(a.get("vital_cancelled") or 0) + int(a2.get("cancelled") or 0)
    raw_after_cancel = int(a.get("vital_after_cancel") or 0) + int(a2.get("after_cancel") or 0)

    # Totals span ALL buckets (bkk + non_bkk + unknown) so the headline
    # number reconciles with the project total. Per-bucket counts are
    # returned in `breakdown` so the dashboard can show the split as a
    # footnote ("กทม X | ตจว Y | ไม่ระบุ Z").
    totals_row = execute_query(cte + """
        SELECT
          (SELECT COUNT(DISTINCT patient_id) FROM unified)        AS total_screened,
          (SELECT COUNT(*)                   FROM unified_visits) AS total_visits,
          -- BKK bucket
          (SELECT COUNT(DISTINCT patient_id) FROM unified        WHERE bucket = 'bkk')      AS bkk_screened,
          (SELECT COUNT(*)                   FROM unified_visits WHERE bucket = 'bkk')      AS bkk_visits,
          -- Non-BKK bucket (other provinces; detail in /non-bangkok-overview)
          (SELECT COUNT(DISTINCT patient_id) FROM unified        WHERE bucket = 'non_bkk')  AS non_bkk_screened,
          (SELECT COUNT(*)                   FROM unified_visits WHERE bucket = 'non_bkk')  AS non_bkk_visits,
          -- Unknown bucket (no district info anywhere)
          (SELECT COUNT(DISTINCT patient_id) FROM unified        WHERE bucket = 'unknown')  AS unknown_screened,
          (SELECT COUNT(*)                   FROM unified_visits WHERE bucket = 'unknown')  AS unknown_visits
    """) or [{}]
    r0 = totals_row[0]
    total = int(r0.get("total_screened") or 0)
    total_visits = int(r0.get("total_visits") or 0)
    bkk_screened = int(r0.get("bkk_screened") or 0)
    bkk_visits = int(r0.get("bkk_visits") or 0)
    non_bkk_screened = int(r0.get("non_bkk_screened") or 0)
    non_bkk_visits = int(r0.get("non_bkk_visits") or 0)
    unknown_screened = int(r0.get("unknown_screened") or 0)
    unknown_visits = int(r0.get("unknown_visits") or 0)
    # `dropped_retry_30d` = rows after CANCEL filter minus the deduped visit
    # count (matches the user's row-level definition: rows-in-vital with
    # CANCELST=0 minus the rn=1 final count).
    dropped_retry = max(0, raw_after_cancel - total_visits)

    zone_count = execute_scalar("SELECT COUNT(*) FROM ref_health_zones") or 0
    district_count = execute_scalar("SELECT COUNT(*) FROM ref_districts") or 0

    last_updated = execute_scalar(
        "SELECT MAX(refreshed_at) FROM summary_district_disease"
    )

    # by_zone = BKK-only; non-BKK and unknown are auto-excluded because
    # `u.dc = d.dcode` only matches BKK district codes 1001..1050.
    by_zone = execute_query(cte + """
        SELECT z.zone_code, z.name_th,
               COALESCE(COUNT(DISTINCT u.patient_id), 0) AS total_screened,
               COALESCE((
                 SELECT COUNT(*) FROM unified_visits uv
                 JOIN ref_districts d2 ON d2.dcode = uv.dc
                 WHERE d2.zone_code = z.zone_code
               ), 0) AS total_visits
        FROM ref_health_zones z
        LEFT JOIN ref_districts d ON d.zone_code = z.zone_code
        LEFT JOIN unified u ON u.dc = d.dcode
        GROUP BY z.zone_code, z.name_th
        ORDER BY z.zone_code
    """)

    # by_disease aggregates over ALL records so prevalence pct matches the
    # ALL-buckets headline denominator. Frontend already has a separate
    # /non-bangkok-overview if a BKK-only view is needed.
    # Obesity uses `found_obesity` (= msd_obese, measured BMI ≥ 23 per
    # Asia-Pacific NHES) — not `risk_bmi` (self-reported). Stroke removed
    # from the dashboard list per user request 2026-05-05.
    disease_rows = execute_query(cte + """
        SELECT
          (SELECT COUNT(DISTINCT patient_id) FROM unified)        AS total_screened,
          (SELECT COUNT(*)                   FROM unified_visits) AS total_visits,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)            AS diabetes,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)           AS hypertension,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)           AS cardiovascular,
          COUNT(DISTINCT patient_id) FILTER (WHERE found_obesity)      AS obesity,
          COUNT(DISTINCT patient_id) FILTER (WHERE found_dyslipidemia) AS dyslipidemia
        FROM unified
    """)
    d = disease_rows[0] if disease_rows else {}
    ts = d.get("total_screened") or 1
    by_disease = []
    for key in ("diabetes", "hypertension", "cardiovascular", "obesity", "dyslipidemia"):
        cnt = d.get(key) or 0
        by_disease.append({
            "disease_key": key,
            "total_at_risk": cnt,
            "pct": round(100.0 * cnt / ts, 2) if ts else 0,
        })

    result = {
        # Headline = ALL buckets (project total). Frontend can show as
        # "X คน · กทม Y · ตจว Z · ไม่ระบุ W" using the breakdown below.
        "total_screened": total,
        "total_visits": total_visits,
        "target": TARGET_SCREENED,
        "zones_count": zone_count,
        "districts_count": district_count,
        "last_updated": str(last_updated) if last_updated else None,
        "by_zone": by_zone,    # 8 BKK zones only — sums to bkk bucket
        "by_disease": by_disease,
        # Per-bucket split for the dashboard footnote. The three buckets
        # are mutually exclusive and exhaustive — total = sum of all three.
        "breakdown": {
            "bkk":         {"total_screened": bkk_screened,     "total_visits": bkk_visits},
            "non_bangkok": {"total_screened": non_bkk_screened, "total_visits": non_bkk_visits},
            "unknown":     {"total_screened": unknown_screened, "total_visits": unknown_visits},
        },
        # Audit / methodology counts — surfaced via the OverviewBoard info
        # tooltip ("วิธีนับ ... ตัดออก ..."). Project-wide totals.
        "audit": {
            "dropped_cancelled": cancelled_total,
            "dropped_retry_30d": dropped_retry,
            "raw_after_cancel":  raw_after_cancel,
        },
    }
    cache_set(cache_key, result, TTL_T2_AGGREGATE)
    return result


# =========================================================================== #
# Filtered query (k-anonymity enforced)
# =========================================================================== #

# Bucket boundaries used by summary_disease_age_sex. Used to map an arbitrary
# age range from the frontend (e.g. Gen X = 46-61) onto the closest set of
# overlapping buckets.
_AGE_BUCKETS: list[tuple[str, int, int]] = [
    ("18-29", 18, 29),
    ("30-44", 30, 44),
    ("45-59", 45, 59),
    ("60-74", 60, 74),
    ("75+",  75, 120),
]

_SEX_MAP = {"Male": "M", "Female": "F", "M": "M", "F": "F", "1": "M", "2": "F"}


@router.get("/filtered")
def filtered_summary(
    district: Optional[str] = Query(None),
    sex: Optional[str] = Query(None, description="'Male'/'Female' or 'M'/'F'"),
    age_group: Optional[str] = Query(None, description="Direct bucket ('18-29','30-44','45-59','60-74','75+')"),
    age_min: Optional[int] = Query(None, ge=0, le=120, description="Minimum age (inclusive)"),
    age_max: Optional[int] = Query(None, ge=0, le=120, description="Maximum age (inclusive)"),
    fiscal_year: Optional[int] = Query(None, ge=2550, le=2700,
        description="Thai fiscal year (BE). Currently no-op — pre-aggregated MV is FY-agnostic."),
    date_from: Optional[str] = Query(None, description="Currently no-op."),
    date_to: Optional[str] = Query(None, description="Currently no-op."),
    smoking: Optional[str] = Query(None, description="Currently no-op — needs disease×lifestyle MV."),
    exercise: Optional[str] = Query(None, description="Currently no-op."),
    alcohol: Optional[str] = Query(None, description="Currently no-op."),
):
    """Per-district disease summary, optionally filtered by sex and/or age range.

    Backed by `summary_disease_age_sex` (district × sex × age_group), which
    matches `mv_summary_districts` totals when summed across all sex/age.

    Sex is mapped from the frontend's English label to the DB single-letter code
    ('Male'→'M', 'Female'→'F'). Age range is mapped onto overlapping buckets;
    e.g. age_min=46&age_max=61 → buckets '45-59' and '60-74'.

    Lifestyle filters (smoking/alcohol/exercise) and time filters
    (fiscal_year/date_from/date_to) are accepted for forward-compat but currently
    do not affect the query — those need a disease×lifestyle MV that doesn't
    exist yet.
    """
    sex_norm = _SEX_MAP.get(sex) if sex else None

    # Resolve age filter → list of bucket strings.
    # age_explicit: user actively picked a range/bucket. If they did but no
    # bucket overlaps (e.g. Gen Alpha 2-13 vs DB buckets that start at 18),
    # we must return zero rows rather than fall through to "no filter".
    age_explicit = age_min is not None or age_max is not None or bool(age_group)
    if age_min is not None or age_max is not None:
        lo = age_min if age_min is not None else 0
        hi = age_max if age_max is not None else 120
        if lo > hi:
            lo, hi = hi, lo
        target_buckets = [b[0] for b in _AGE_BUCKETS if not (b[2] < lo or b[1] > hi)]
    elif age_group:
        target_buckets = [age_group]
    else:
        target_buckets = []

    # Always exclude rollup rows (sex='all') to prevent double-counting.
    conditions = ["s.sex <> 'all'", "s.age_group <> 'all'"]
    params: list = []

    if sex_norm:
        conditions.append("s.sex = %s")
        params.append(sex_norm)
    if target_buckets:
        placeholders = ",".join(["%s"] * len(target_buckets))
        conditions.append(f"s.age_group IN ({placeholders})")
        params.extend(target_buckets)
    elif age_explicit:
        # User picked an age range that maps to no DB bucket → no rows match.
        conditions.append("1=0")
    if district:
        conditions.append("s.district_code = %s")
        params.append(district)

    where = "WHERE " + " AND ".join(conditions)

    # INNER JOIN with mv_summary_districts so we only return Bangkok's 50
    # districts (summary_disease_age_sex contains other provinces too).
    rows = execute_query(f"""
        SELECT
          s.district_code,
          d.name_th                        AS district_name,
          d.name_en                        AS district_name_en,
          d.zone_code,
          SUM(s.total_screened)::int       AS patient_count,
          SUM(s.risk_dm)::int              AS risk_dm_count,
          SUM(s.risk_hpt)::int             AS risk_hpt_count,
          SUM(s.risk_cvd)::int             AS risk_cvd_count,
          SUM(s.risk_bmi)::int             AS risk_bmi_count,
          SUM(s.found_obesity)::int        AS found_obesity_count,
          SUM(s.found_dyslipidemia)::int   AS found_dyslipidemia_count,
          SUM(s.found_stroke)::int         AS found_stroke_count
        FROM summary_disease_age_sex s
        JOIN ref_districts d        ON d.dcode = s.district_code
        JOIN mv_summary_districts m ON m.district_code = s.district_code
        {where}
        GROUP BY s.district_code, d.name_th, d.name_en, d.zone_code
        ORDER BY s.district_code
    """, tuple(params))

    rows = enforce_k_anonymity(rows, count_field="patient_count")

    return {
        "filters_applied": {
            "district": district,
            "sex": sex_norm,
            "age_group": age_group,
            "age_min": age_min,
            "age_max": age_max,
            "age_buckets_used": target_buckets,
            "fiscal_year": fiscal_year,
            "date_from": date_from,
            "date_to": date_to,
            "smoking": smoking,
            "exercise": exercise,
            "alcohol": alcohol,
        },
        "lifestyle_filters_active": False,
        "k_anonymity_threshold": K_ANONYMITY_THRESHOLD,
        "data": rows,
    }


# =========================================================================== #
# Lab summary
# =========================================================================== #

@router.get("/lab")
def lab_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Lab summary, optionally filtered by district or zone."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("l.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Read from pre-aggregated MV (≥migration 110); ~7.5s → <100ms.
    rows = execute_query(f"""
        SELECT
          l.district_code,
          l.total_lab_patients,
          l.avg_hemoglobin,
          l.avg_fbs,
          l.avg_cholesterol,
          l.avg_triglyceride,
          l.avg_hdl,
          l.avg_ldl,
          l.avg_creatinine,
          l.avg_egfr,
          l.pct_anemia,
          l.pct_ckd
        FROM public.mv_summary_lab l
        JOIN ref_districts d ON l.district_code = d.dcode
        {where}
        ORDER BY l.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_lab_patients") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Mental health summary
# =========================================================================== #

@router.get("/mental-health")
def mental_health_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Mental health screening summary."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("m.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Read from pre-aggregated MV (≥migration 110); ~4.7s → <100ms.
    rows = execute_query(f"""
        SELECT
          m.district_code,
          m.total_screened,
          m.pct_depression_risk,
          m.pct_phq9_moderate,
          m.pct_high_stress
        FROM public.mv_summary_mental m
        JOIN ref_districts d ON m.district_code = d.dcode
        {where}
        ORDER BY m.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_screened") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Demographics summary
# =========================================================================== #

@router.get("/demographics")
def demographics_summary(
    dcode: Optional[str] = Query(None),
    zone_code: Optional[str] = Query(None),
):
    """Demographic breakdown by district."""
    conditions: list[str] = []
    params: list = []

    if dcode:
        conditions.append("dm.district_code = %s")
        params.append(dcode)
    if zone_code:
        conditions.append("d.zone_code = %s")
        params.append(zone_code)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = execute_query(f"""
        SELECT
          dm.district_code,
          dm.total_respondents,
          dm.edu_none, dm.edu_primary, dm.edu_secondary,
          dm.edu_high_school, dm.edu_vocational, dm.edu_bachelor, dm.edu_postgrad,
          dm.occ_government, dm.occ_private, dm.occ_self_employed,
          dm.occ_agriculture, dm.occ_unemployed, dm.occ_student, dm.occ_retired,
          dm.priv_ucs, dm.priv_sso, dm.priv_csmbs, dm.priv_other,
          dm.house_owned, dm.house_rented, dm.house_condo, dm.house_other
        FROM summary_district_demographics dm
        JOIN ref_districts d ON dm.district_code::text = d.dcode
        {where}
        ORDER BY dm.district_code
    """, tuple(params) or None)

    rows = [r for r in rows if (r.get("total_respondents") or 0) >= K_ANONYMITY_THRESHOLD]
    return rows


# =========================================================================== #
# Non-Bangkok overview
# Aggregates screening records where the patient's district_code is outside
# Bangkok (not in the 1001–1050 range). These are patients who self-reported
# a home district outside BKK yet came for BMA screening.
# =========================================================================== #

@router.get("/non-bangkok-overview")
def non_bangkok_overview():
    """Aggregated health stats for patients whose home is outside Bangkok.

    Headline numbers (total_screened, by_disease) come from the same unified
    CTE as /summary/overview — specifically `bucket = 'non_bkk'` — so the
    "ตจว" footnote on the dashboard and the "คนต่างจังหวัด" virtual zone
    in StatisticsBoard always show the same number.

    Per-province drill-down (by_home_province) and lab/mental aggregates
    use the legacy `home_province <> 10` query because those are the only
    rows where the upstream province code is filled in. Sum of
    by_home_province may therefore be < total_screened when records have
    a non-BKK district code but missing home_province — that's a known
    data-quality gap.
    """

    # Cache check (TTL 15 min)
    hit = cache_get("summary:non_bangkok_overview")
    if hit is not None:
        return hit

    # ── Headline counts via unified CTE — same source as /summary/overview ──
    from services.unified_screening import build_unified_cte
    cte = build_unified_cte(include_visits=True)
    headline_row = execute_query(cte + """
        SELECT
          (SELECT COUNT(DISTINCT patient_id) FROM unified
             WHERE bucket = 'non_bkk')                              AS total_screened,
          (SELECT COUNT(*) FROM unified_visits
             WHERE bucket = 'non_bkk')                              AS total_visits,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_dm)            AS risk_dm_count,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_hpt)           AS risk_hpt_count,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_cvd)           AS risk_cvd_count,
          COUNT(DISTINCT patient_id) FILTER (WHERE risk_bmi)           AS risk_bmi_count,
          COUNT(DISTINCT patient_id) FILTER (WHERE found_dyslipidemia) AS found_dyslipidemia_count,
          COUNT(DISTINCT patient_id) FILTER (WHERE found_stroke)       AS found_stroke_count
        FROM unified
        WHERE bucket = 'non_bkk'
    """) or [{}]
    h = headline_row[0]
    headline_total = int(h.get("total_screened") or 0)
    headline_visits = int(h.get("total_visits") or 0)

    # Step 1: Find k-anonymity-safe provinces (n >= threshold). All overall
    # aggregates below restrict to these provinces so every number on every
    # UI level (overall, region, province) stays mathematically consistent —
    # i.e. sum of provinces = sum of regions = overall.
    safe_provinces_rows = execute_query("""
        SELECT hv.home_province AS pc
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province IS NOT NULL
          AND hv.home_province <> 10
        GROUP BY hv.home_province
        HAVING COUNT(DISTINCT v.patient_id) >= %s
    """, (K_ANONYMITY_THRESHOLD,)) or []
    safe_provinces = [r["pc"] for r in safe_provinces_rows]

    # If both unified and safe_provinces are empty, suppress whole payload
    if headline_total == 0 and not safe_provinces:
        result = {
            "total_screened": 0,
            "total_visits": 0,
            "suppressed": True,
            "reason": f"k-anonymity: no province with n >= {K_ANONYMITY_THRESHOLD}",
            "by_disease": [],
            "by_home_province": [],
            "disease_counts": {},
            "physical": {},
            "lab": {},
            "mental": {},
            "last_updated": None,
        }
        cache_set("summary:non_bangkok_overview", result, TTL_T2_AGGREGATE)
        return result

    # ── Province-based detail query (lab / vitals / mental) ──
    # Uses the legacy `home_province <> 10` filter because those columns
    # only exist on the raw join. Sum may differ from headline_total —
    # that gap is the data-quality issue documented above.
    rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id)                                     AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.smoking = 1)        AS smoking_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.smoking IS NOT NULL) AS smoking_answered,
          AVG(v.sbp)        AS avg_sbp,
          AVG(v.dbp)        AS avg_dbp,
          AVG(v.weight_kg)  AS avg_weight_kg,
          AVG(v.waist_cm)   AS avg_waist_cm,
          AVG(CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) AS avg_bmi,
          MAX(v.visit_date) AS last_visit
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = ANY(%s)
    """, (safe_provinces,)) if safe_provinces else [{}]

    row = rows[0] if rows else {}
    # Headline replaced with unified — but keep `row` for province-based stats below.
    # Local helper: merge headline counts into `row` so disease_map (which reads
    # from `row`) picks up unified-derived numbers, while smoking/lab/mental
    # detail stays from the province-based row.
    row = {**row,
           "total_screened":              headline_total,
           "risk_dm_count":               int(h.get("risk_dm_count") or 0),
           "risk_hpt_count":              int(h.get("risk_hpt_count") or 0),
           "risk_cvd_count":              int(h.get("risk_cvd_count") or 0),
           "risk_bmi_count":              int(h.get("risk_bmi_count") or 0),
           "found_dyslipidemia_count":    int(h.get("found_dyslipidemia_count") or 0),
           "found_stroke_count":          int(h.get("found_stroke_count") or 0),
          }
    total = headline_total

    # k-anonymity guard: don't expose anything if too few patients
    if total < K_ANONYMITY_THRESHOLD:
        result = {
            "total_screened": 0,
            "suppressed": True,
            "reason": f"k-anonymity: n < {K_ANONYMITY_THRESHOLD}",
            "by_disease": [],
            "by_home_province": [],
            "last_updated": None,
        }
        cache_set("summary:non_bangkok_overview", result, TTL_T2_AGGREGATE)
        return result

    # Per-disease breakdown (mirrors /overview shape)
    disease_map = [
        ("diabetes",       "risk_dm_count"),
        ("hypertension",   "risk_hpt_count"),
        ("cardiovascular", "risk_cvd_count"),
        ("obesity",        "risk_bmi_count"),
        ("dyslipidemia",   "found_dyslipidemia_count"),
        ("stroke",         "found_stroke_count"),
    ]
    by_disease = []
    for key, col in disease_map:
        cnt = int(row.get(col) or 0)
        by_disease.append({
            "disease_key": key,
            "total_at_risk": cnt,
            "pct": round(100.0 * cnt / total, 2) if total else 0,
        })

    # Top home-provinces with per-disease breakdown. Uses the same k-anon
    # safe_provinces set as the overall aggregates so sum(province.count)
    # == overall.total_screened.
    by_home_province_rows = execute_query("""
        SELECT
          hv.home_province AS province_code,
          COUNT(DISTINCT v.patient_id) AS count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)            AS dm_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt)           AS hpt_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd)           AS cvd_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi)           AS bmi_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS dys_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_stroke)       AS stroke_count
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = ANY(%s)
        GROUP BY hv.home_province
        ORDER BY count DESC
    """, (safe_provinces,)) or []

    def _disease_breakdown(r: dict) -> dict:
        n = int(r.get("count") or 0)
        pairs = [
            ("diabetes",       "dm_count"),
            ("hypertension",   "hpt_count"),
            ("cardiovascular", "cvd_count"),
            ("obesity",        "bmi_count"),
            ("dyslipidemia",   "dys_count"),
            ("stroke",         "stroke_count"),
        ]
        out = {}
        for key, col in pairs:
            c = int(r.get(col) or 0)
            out[key] = {"count": c, "pct": round(100.0 * c / n, 2) if n else 0}
        return out

    by_home_province = [
        {
            "province_code": r["province_code"],
            "count": int(r["count"]),
            "diseases": _disease_breakdown(r),
        }
        for r in by_home_province_rows
    ]

    # Lab aggregates — same k-anon safe set
    lab_rows = execute_query("""
        SELECT
          COUNT(DISTINCT l.patient_id) AS total_lab_patients,
          AVG(l.hemoglobin)   AS avg_hemoglobin,
          AVG(l.hematocrit)   AS avg_hematocrit,
          AVG(l.fbs)          AS avg_fbs,
          AVG(l.cholesterol)  AS avg_cholesterol,
          AVG(l.triglyceride) AS avg_triglyceride,
          AVG(l.hdl)          AS avg_hdl,
          AVG(l.ldl)          AS avg_ldl,
          AVG(l.creatinine)   AS avg_creatinine,
          AVG(l.egfr)         AS avg_egfr,
          AVG(l.uric_acid)    AS avg_uric_acid,
          AVG(l.sgot)         AS avg_sgot,
          AVG(l.sgpt)         AS avg_sgpt,
          ROUND(100.0 * COUNT(*) FILTER (WHERE l.hemoglobin < 12)
                      / NULLIF(COUNT(*) FILTER (WHERE l.hemoglobin IS NOT NULL), 0), 2) AS pct_anemia,
          ROUND(100.0 * COUNT(*) FILTER (WHERE l.egfr < 60)
                      / NULLIF(COUNT(*) FILTER (WHERE l.egfr IS NOT NULL), 0), 2) AS pct_ckd
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        JOIN raw_lab_results l ON l.patient_id = v.patient_id
          AND l.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = ANY(%s)
    """, (safe_provinces,)) or []
    lab_row = lab_rows[0] if lab_rows else {}

    # Exercise / lifestyle — same k-anon safe set
    hh_rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise = 0) AS no_exercise_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise IS NOT NULL) AS exercise_answered
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        LEFT JOIN raw_homehealth h ON h.patient_id = v.patient_id
          AND h.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = ANY(%s)
    """, (safe_provinces,)) or []
    hh_row = hh_rows[0] if hh_rows else {}

    # Mental health percentages — same k-anon safe set
    mental_rows = execute_query("""
        SELECT
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
            WHERE v.depression_2q_1 >= 1 OR v.depression_2q_2 >= 1
          ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_depression_risk,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
            WHERE (COALESCE(v.phq9_q1,0) + COALESCE(v.phq9_q2,0) + COALESCE(v.phq9_q3,0)
                 + COALESCE(v.phq9_q4,0) + COALESCE(v.phq9_q5,0) + COALESCE(v.phq9_q6,0)
                 + COALESCE(v.phq9_q7,0) + COALESCE(v.phq9_q8,0) + COALESCE(v.phq9_q9,0)) >= 10
          ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_phq9_moderate,
          ROUND(100.0 * COUNT(DISTINCT v.patient_id) FILTER (
            WHERE (COALESCE(v.st5_q1,0) + COALESCE(v.st5_q2,0) + COALESCE(v.st5_q3,0)
                 + COALESCE(v.st5_q4,0) + COALESCE(v.st5_q5,0)) >= 7
          ) / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS pct_high_stress
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = ANY(%s)
    """, (safe_provinces,)) or []
    mental_row = mental_rows[0] if mental_rows else {}

    # Rates
    smoking_count = int(row.get("smoking_count") or 0)
    smoking_answered = int(row.get("smoking_answered") or 0)
    smoking_rate = round(100.0 * smoking_count / smoking_answered, 2) if smoking_answered else 0

    no_ex = int(hh_row.get("no_exercise_count") or 0)
    ex_answered = int(hh_row.get("exercise_answered") or 0)
    no_exercise_rate = round(100.0 * no_ex / ex_answered, 2) if ex_answered else 0

    last_visit = row.get("last_visit")

    # Helper: safely convert numeric or None to float
    def _f(v):
        return float(v) if v is not None else None

    result = {
        "total_screened": total,
        "total_visits": headline_visits,  # ตัวเลขครั้ง — same source as /overview.breakdown.non_bangkok
        "smoking_rate": smoking_rate,
        "no_exercise_rate": no_exercise_rate,
        "last_updated": str(last_visit) if last_visit else None,
        "by_disease": by_disease,
        "by_home_province": by_home_province,
        # Disease counts (raw) — used to build ZoneHealthData.diseases shape
        "disease_counts": {
            "diabetes":       int(row.get("risk_dm_count") or 0),
            "hypertension":   int(row.get("risk_hpt_count") or 0),
            "cardiovascular": int(row.get("risk_cvd_count") or 0),
            "obesity":        int(row.get("risk_bmi_count") or 0),
            "dyslipidemia":   int(row.get("found_dyslipidemia_count") or 0),
            "stroke":         int(row.get("found_stroke_count") or 0),
        },
        # Physical vitals (averages)
        "physical": {
            "avg_sbp":       _f(row.get("avg_sbp")),
            "avg_dbp":       _f(row.get("avg_dbp")),
            "avg_weight_kg": _f(row.get("avg_weight_kg")),
            "avg_waist_cm":  _f(row.get("avg_waist_cm")),
            "avg_bmi":       _f(row.get("avg_bmi")),
        },
        # Lab averages (same shape as /summary/lab row)
        "lab": {
            "total_lab_patients": int(lab_row.get("total_lab_patients") or 0),
            "avg_hemoglobin":   _f(lab_row.get("avg_hemoglobin")),
            "avg_hematocrit":   _f(lab_row.get("avg_hematocrit")),
            "avg_fbs":          _f(lab_row.get("avg_fbs")),
            "avg_cholesterol":  _f(lab_row.get("avg_cholesterol")),
            "avg_triglyceride": _f(lab_row.get("avg_triglyceride")),
            "avg_hdl":          _f(lab_row.get("avg_hdl")),
            "avg_ldl":          _f(lab_row.get("avg_ldl")),
            "avg_creatinine":   _f(lab_row.get("avg_creatinine")),
            "avg_egfr":         _f(lab_row.get("avg_egfr")),
            "avg_uric_acid":    _f(lab_row.get("avg_uric_acid")),
            "avg_sgot":         _f(lab_row.get("avg_sgot")),
            "avg_sgpt":         _f(lab_row.get("avg_sgpt")),
            "pct_anemia":       _f(lab_row.get("pct_anemia")),
            "pct_ckd":          _f(lab_row.get("pct_ckd")),
        },
        # Mental health (%s already computed)
        "mental": {
            "pct_depression_risk": _f(mental_row.get("pct_depression_risk")),
            "pct_phq9_moderate":   _f(mental_row.get("pct_phq9_moderate")),
            "pct_high_stress":     _f(mental_row.get("pct_high_stress")),
        },
    }
    cache_set("summary:non_bangkok_overview", result, TTL_T2_AGGREGATE)
    return result


# =========================================================================== #
# Non-Bangkok per-province detail
# =========================================================================== #

@router.get("/non-bangkok-province/{province_code}")
def non_bangkok_province(
    province_code: int = Path(..., ge=11, le=96, description="Thai province code (TIS 1099)"),
):
    """Full health stats for patients whose home_province equals the given code.

    Same response shape as /non-bangkok-overview but filtered to one province.
    Returns 404 if the province has no qualifying records.
    k-anonymity: suppressed flag set when total < threshold.
    """
    if province_code == 10:
        raise HTTPException(status_code=400, detail="use /summary/overview for Bangkok")

    cache_key = f"summary:non_bangkok_province:{province_code}"
    hit = cache_get(cache_key)
    if hit is not None:
        return hit

    rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id)                                     AS total_screened,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_dm)            AS risk_dm_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_hpt)           AS risk_hpt_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_cvd)           AS risk_cvd_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.risk_bmi)           AS risk_bmi_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_stroke)       AS found_stroke_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.found_dyslipidemia) AS found_dyslipidemia_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.smoking = 1)        AS smoking_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE v.smoking IS NOT NULL) AS smoking_answered,
          AVG(v.sbp)        AS avg_sbp,
          AVG(v.dbp)        AS avg_dbp,
          AVG(v.weight_kg)  AS avg_weight_kg,
          AVG(v.waist_cm)   AS avg_waist_cm,
          AVG(CASE WHEN v.height_cm > 0 THEN v.weight_kg / POWER(v.height_cm / 100.0, 2) END) AS avg_bmi,
          MAX(v.visit_date) AS last_visit
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = %s
    """, (province_code,))
    row = rows[0] if rows else {}
    total = int(row.get("total_screened") or 0)

    if total < K_ANONYMITY_THRESHOLD:
        result = {
            "province_code": province_code,
            "total_screened": 0,
            "suppressed": True,
            "reason": f"k-anonymity: n < {K_ANONYMITY_THRESHOLD}",
            "by_disease": [],
        }
        cache_set(cache_key, result, TTL_T2_AGGREGATE)
        return result

    disease_map = [
        ("diabetes",       "risk_dm_count"),
        ("hypertension",   "risk_hpt_count"),
        ("cardiovascular", "risk_cvd_count"),
        ("obesity",        "risk_bmi_count"),
        ("dyslipidemia",   "found_dyslipidemia_count"),
        ("stroke",         "found_stroke_count"),
    ]
    by_disease = []
    for key, col in disease_map:
        cnt = int(row.get(col) or 0)
        by_disease.append({
            "disease_key": key,
            "total_at_risk": cnt,
            "pct": round(100.0 * cnt / total, 2) if total else 0,
        })

    # Lab aggregates
    lab_rows = execute_query("""
        SELECT
          COUNT(DISTINCT l.patient_id) AS total_lab_patients,
          AVG(l.fbs)          AS avg_fbs,
          AVG(l.cholesterol)  AS avg_cholesterol,
          AVG(l.triglyceride) AS avg_triglyceride,
          AVG(l.hdl)          AS avg_hdl,
          AVG(l.ldl)          AS avg_ldl,
          AVG(l.creatinine)   AS avg_creatinine,
          AVG(l.egfr)         AS avg_egfr,
          AVG(l.hemoglobin)   AS avg_hemoglobin
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        JOIN raw_lab_results l ON l.patient_id = v.patient_id
          AND l.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = %s
    """, (province_code,)) or []
    lab_row = lab_rows[0] if lab_rows else {}

    # No-exercise rate
    hh_rows = execute_query("""
        SELECT
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise = 0) AS no_exercise_count,
          COUNT(DISTINCT v.patient_id) FILTER (WHERE h.exercise IS NOT NULL) AS exercise_answered
        FROM raw_vitalsigns v
        JOIN raw_homevisit hv ON hv.patient_id = v.patient_id
        LEFT JOIN raw_homehealth h ON h.patient_id = v.patient_id
          AND h.cancel_status IS DISTINCT FROM 1
        WHERE v.cancel_status IS DISTINCT FROM 1
          AND hv.home_province = %s
    """, (province_code,)) or []
    hh_row = hh_rows[0] if hh_rows else {}

    def _f(v):
        return float(v) if v is not None else None

    smoking_count = int(row.get("smoking_count") or 0)
    smoking_answered = int(row.get("smoking_answered") or 0)
    smoking_rate = round(100.0 * smoking_count / smoking_answered, 2) if smoking_answered else 0

    no_ex = int(hh_row.get("no_exercise_count") or 0)
    ex_answered = int(hh_row.get("exercise_answered") or 0)
    no_exercise_rate = round(100.0 * no_ex / ex_answered, 2) if ex_answered else 0

    result = {
        "province_code": province_code,
        "total_screened": total,
        "smoking_rate": smoking_rate,
        "no_exercise_rate": no_exercise_rate,
        "last_updated": str(row.get("last_visit")) if row.get("last_visit") else None,
        "by_disease": by_disease,
        "physical": {
            "avg_sbp":       _f(row.get("avg_sbp")),
            "avg_dbp":       _f(row.get("avg_dbp")),
            "avg_weight_kg": _f(row.get("avg_weight_kg")),
            "avg_waist_cm":  _f(row.get("avg_waist_cm")),
            "avg_bmi":       _f(row.get("avg_bmi")),
        },
        "lab": {
            "total_lab_patients": int(lab_row.get("total_lab_patients") or 0),
            "avg_fbs":          _f(lab_row.get("avg_fbs")),
            "avg_cholesterol":  _f(lab_row.get("avg_cholesterol")),
            "avg_triglyceride": _f(lab_row.get("avg_triglyceride")),
            "avg_hdl":          _f(lab_row.get("avg_hdl")),
            "avg_ldl":          _f(lab_row.get("avg_ldl")),
            "avg_creatinine":   _f(lab_row.get("avg_creatinine")),
            "avg_egfr":         _f(lab_row.get("avg_egfr")),
            "avg_hemoglobin":   _f(lab_row.get("avg_hemoglobin")),
        },
    }
    cache_set(cache_key, result, TTL_T2_AGGREGATE)
    return result


# =========================================================================== #
# Fiscal year catalog
# =========================================================================== #

@router.get("/fiscal-years")
def list_fiscal_years(min_records: int = Query(100, ge=0, description="Suppress FYs with fewer than N records")):
    """List Thai fiscal years present in raw_vitalsigns with record counts.

    Thai FY X starts Oct 1 (Buddhist year X - 544 → Gregorian X - 544) and
    ends Sep 30 (Buddhist year X - 543). Used by the frontend timeline filter
    to enumerate selectable periods.

    k-anonymity: suppresses FYs with < min_records entries (default 100).
    """
    hit = cache_get(f"summary:fiscal_years:{min_records}")
    if hit is not None:
        return hit

    rows = execute_query("""
        WITH fy AS (
          SELECT
            (EXTRACT(YEAR FROM visit_date)::int + 543 +
              CASE WHEN EXTRACT(MONTH FROM visit_date) >= 10 THEN 1 ELSE 0 END
            ) AS fiscal_year,
            patient_id,
            visit_date
          FROM raw_vitalsigns
          WHERE cancel_status IS DISTINCT FROM 1
            AND visit_date IS NOT NULL
        )
        SELECT
          fiscal_year,
          COUNT(*)::int                         AS records,
          COUNT(DISTINCT patient_id)::int       AS unique_patients,
          MIN(visit_date)::date                 AS first_visit,
          MAX(visit_date)::date                 AS last_visit
        FROM fy
        GROUP BY fiscal_year
        HAVING COUNT(*) >= %s
        ORDER BY fiscal_year DESC
    """, (min_records,)) or []

    result = [
        {
            "fiscal_year": int(r["fiscal_year"]),
            "records": int(r["records"]),
            "unique_patients": int(r["unique_patients"]),
            "first_visit": str(r["first_visit"]) if r["first_visit"] else None,
            "last_visit": str(r["last_visit"]) if r["last_visit"] else None,
            # ISO date range of the FY (for frontend labels)
            "fy_start": f"{int(r['fiscal_year']) - 544}-10-01",
            "fy_end":   f"{int(r['fiscal_year']) - 543}-09-30",
        }
        for r in rows
    ]
    cache_set(f"summary:fiscal_years:{min_records}", result, TTL_T4_STATIC)
    return result
