"""
Same-person discrepancy analysis — Phase 3 of the cross-source dashboard.

For every (source_a, source_b) pair, we find people who appear in BOTH systems
(via `idcard_hash`), take their LATEST visit from each, and measure:

1. **Continuous variables** → Bland-Altman statistics
     - mean difference (bias)
     - SD of differences
     - 95% limits of agreement (LoA) = bias ± 1.96·SD
     - Intraclass correlation (ICC)
     - PNG scatter plot (matplotlib)

2. **Categorical variables** → Cohen's kappa
     - Confusion matrix
     - κ value with Landis-Koch interpretation
     - Percent agreement

Important
---------
- This module JOINs raw_patients to itself via idcard_hash — expensive on large
  tables. Callers should cache results (per pair × metric) if hitting a busy
  endpoint. A simple @lru_cache on the public entry points is enough.
- "No agreement available" is a valid result — when one source has no data or
  the pair has fewer than `MIN_PAIRS` in common.
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

# Matplotlib (Agg backend — thread-safe, no GUI)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt   # noqa: E402

from sklearn.metrics import cohen_kappa_score, confusion_matrix   # noqa: E402

from database import execute_query   # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MIN_PAIRS_FOR_PLOT = 10    # fewer than this → skip plotting, just show N
MIN_PAIRS_FOR_STATS = 3    # fewer than this → skip computation entirely


# ---------------------------------------------------------------------------
# Metric catalog — (metric_key, label, table_alias, per-source column, unit)
# ---------------------------------------------------------------------------
# table_alias is used in the SQL (v = raw_vitalsigns, l = raw_lab_results).

@dataclass(frozen=True)
class ContinuousMetric:
    key: str
    label: str
    table: str          # 'v' (raw_vitalsigns) or 'l' (raw_lab_results)
    unit: str
    col_by_source: Dict[str, str]


@dataclass(frozen=True)
class CategoricalMetric:
    key: str
    label: str
    table: str
    col_by_source: Dict[str, str]
    labels: List[str]   # human-readable category labels


# Portal/App1 use the raw/ETL-derived column; App2 uses the *_src column.
CONTINUOUS_METRICS: List[ContinuousMetric] = [
    ContinuousMetric(
        key="bmi", label="BMI", table="v", unit="kg/m²",
        col_by_source={"portal": "v.bmi", "app1": "v.bmi", "app2": "v.bmi_src"},
    ),
    ContinuousMetric(
        key="sbp", label="SBP", table="v", unit="mmHg",
        col_by_source={"portal": "v.sbp", "app1": "v.sbp"},
    ),
    ContinuousMetric(
        key="dbp", label="DBP", table="v", unit="mmHg",
        col_by_source={"portal": "v.dbp", "app1": "v.dbp"},
    ),
    ContinuousMetric(
        key="height_cm", label="Height", table="v", unit="cm",
        col_by_source={"portal": "v.height_cm", "app1": "v.height_cm"},
    ),
    ContinuousMetric(
        key="weight_kg", label="Weight", table="v", unit="kg",
        col_by_source={"portal": "v.weight_kg", "app1": "v.weight_kg"},
    ),
    ContinuousMetric(
        key="waist_cm", label="Waist", table="v", unit="cm",
        col_by_source={"portal": "v.waist_cm", "app1": "v.waist_cm"},
    ),
    ContinuousMetric(
        key="hemoglobin", label="Hemoglobin", table="l", unit="g/dL",
        col_by_source={"portal": "l.hemoglobin", "app1": "l.hemoglobin",
                       "app2": "l.hemoglobin_src"},
    ),
    ContinuousMetric(
        key="cholesterol", label="Cholesterol", table="l", unit="mg/dL",
        col_by_source={"portal": "l.cholesterol", "app1": "l.cholesterol",
                       "app2": "l.cholesterol_src"},
    ),
    ContinuousMetric(
        key="egfr", label="eGFR", table="l", unit="mL/min",
        col_by_source={"portal": "l.egfr", "app1": "l.egfr", "app2": "l.egfr_src"},
    ),
    ContinuousMetric(
        key="fbs", label="FBS", table="l", unit="mg/dL",
        col_by_source={"portal": "l.fbs", "app1": "l.fbs"},
    ),
]


CATEGORICAL_METRICS: List[CategoricalMetric] = [
    CategoricalMetric(
        key="found_dm", label="DM found", table="v",
        col_by_source={"portal": "v.found_dm", "app1": "v.found_dm", "app2": "v.found_dm"},
        labels=["False", "True"],
    ),
    CategoricalMetric(
        key="found_hpt", label="HPT found", table="v",
        col_by_source={"portal": "v.found_hpt", "app1": "v.found_hpt", "app2": "v.found_hpt"},
        labels=["False", "True"],
    ),
    CategoricalMetric(
        key="found_cvd", label="CVD found", table="v",
        col_by_source={"portal": "v.found_cvd", "app1": "v.found_cvd", "app2": "v.found_cvd"},
        labels=["False", "True"],
    ),
    CategoricalMetric(
        key="found_obesity", label="Obesity found", table="v",
        col_by_source={"portal": "v.found_obesity", "app1": "v.found_obesity", "app2": "v.found_obesity"},
        labels=["False", "True"],
    ),
    CategoricalMetric(
        key="risk_dm", label="DM risk", table="v",
        col_by_source={"portal": "v.risk_dm", "app1": "v.risk_dm", "app2": "v.risk_dm"},
        labels=["False", "True"],
    ),
    CategoricalMetric(
        key="risk_hpt", label="HPT risk", table="v",
        col_by_source={"portal": "v.risk_hpt", "app1": "v.risk_hpt", "app2": "v.risk_hpt"},
        labels=["False", "True"],
    ),
    CategoricalMetric(
        key="sex", label="Sex", table="p",
        col_by_source={"portal": "p.sex", "app1": "p.sex", "app2": "p.sex"},
        labels=["10 (M)", "20 (F)"],
    ),
]


# ---------------------------------------------------------------------------
# Source pair presets
# ---------------------------------------------------------------------------

ALL_PAIRS = [
    ("portal", "app1"),
    ("portal", "app2"),
    ("app1", "app2"),
]


def normalize_pair(raw: Optional[str]) -> Tuple[str, str]:
    """Parse a 'a-b' pair string, fall back to portal-app1 if invalid."""
    if raw:
        parts = raw.strip().lower().split("-")
        if len(parts) == 2 and tuple(parts) in ALL_PAIRS:
            return (parts[0], parts[1])
    return ("portal", "app1")


# ---------------------------------------------------------------------------
# Pair extraction — find people in BOTH sources, take latest visit from each
# ---------------------------------------------------------------------------

def _extract_continuous_pairs(
    metric: ContinuousMetric, source_a: str, source_b: str
) -> List[Tuple[float, float]]:
    """Return list of (val_a, val_b) pairs for this metric on this source pair.

    Empty list if either source lacks the metric or no common people.
    """
    col_a = metric.col_by_source.get(source_a)
    col_b = metric.col_by_source.get(source_b)
    if col_a is None or col_b is None:
        return []

    # Table to join depends on metric.table ('v' = vitalsigns, 'l' = lab)
    if metric.table == "v":
        src_table = "raw_vitalsigns"
        date_col = "v.visit_date"
    elif metric.table == "l":
        src_table = "raw_lab_results"
        date_col = "l.visit_date"
    else:
        return []

    short = "v" if metric.table == "v" else "l"

    sql = f"""
        WITH a AS (
            SELECT DISTINCT ON (p.idcard_hash)
                p.idcard_hash, {col_a} AS val
            FROM raw_patients p
            JOIN {src_table} {short} ON {short}.patient_id = p.id
            WHERE p.data_source = %s
              AND {col_a} IS NOT NULL
              AND {short}.cancel_status IS DISTINCT FROM 1
            ORDER BY p.idcard_hash, {date_col} DESC NULLS LAST
        ),
        b AS (
            SELECT DISTINCT ON (p.idcard_hash)
                p.idcard_hash, {col_b} AS val
            FROM raw_patients p
            JOIN {src_table} {short} ON {short}.patient_id = p.id
            WHERE p.data_source = %s
              AND {col_b} IS NOT NULL
              AND {short}.cancel_status IS DISTINCT FROM 1
            ORDER BY p.idcard_hash, {date_col} DESC NULLS LAST
        )
        SELECT a.val::float AS val_a, b.val::float AS val_b
        FROM a INNER JOIN b USING (idcard_hash)
    """
    try:
        rows = execute_query(sql, (source_a, source_b)) or []
        return [(r["val_a"], r["val_b"]) for r in rows
                if r["val_a"] is not None and r["val_b"] is not None]
    except Exception as exc:
        logger.warning("Continuous pair query failed (%s %s↔%s): %s",
                       metric.key, source_a, source_b, exc)
        return []


def _extract_categorical_pairs(
    metric: CategoricalMetric, source_a: str, source_b: str
) -> List[Tuple[str, str]]:
    """Return list of (cat_a, cat_b) string pairs for this metric."""
    col_a = metric.col_by_source.get(source_a)
    col_b = metric.col_by_source.get(source_b)
    if col_a is None or col_b is None:
        return []

    # Which raw table? For now only 'v' (vitalsigns) and 'p' (patients).
    if metric.table == "v":
        src_table = "raw_vitalsigns"
        short = "v"
        date_col = "v.visit_date"
        patient_filter_needed = True
    elif metric.table == "p":
        src_table = None  # patient column directly — no join to vitalsigns needed
        short = "p"
        date_col = "p.updated_at"
        patient_filter_needed = False
    else:
        return []

    if patient_filter_needed:
        sql = f"""
            WITH a AS (
                SELECT DISTINCT ON (p.idcard_hash)
                    p.idcard_hash, {col_a}::text AS val
                FROM raw_patients p
                JOIN {src_table} {short} ON {short}.patient_id = p.id
                WHERE p.data_source = %s
                  AND {col_a} IS NOT NULL
                  AND {short}.cancel_status IS DISTINCT FROM 1
                ORDER BY p.idcard_hash, {date_col} DESC NULLS LAST
            ),
            b AS (
                SELECT DISTINCT ON (p.idcard_hash)
                    p.idcard_hash, {col_b}::text AS val
                FROM raw_patients p
                JOIN {src_table} {short} ON {short}.patient_id = p.id
                WHERE p.data_source = %s
                  AND {col_b} IS NOT NULL
                  AND {short}.cancel_status IS DISTINCT FROM 1
                ORDER BY p.idcard_hash, {date_col} DESC NULLS LAST
            )
            SELECT a.val AS val_a, b.val AS val_b
            FROM a INNER JOIN b USING (idcard_hash)
        """
    else:
        sql = f"""
            WITH a AS (
                SELECT p.idcard_hash, {col_a}::text AS val
                FROM raw_patients p
                WHERE p.data_source = %s AND {col_a} IS NOT NULL
            ),
            b AS (
                SELECT p.idcard_hash, {col_b}::text AS val
                FROM raw_patients p
                WHERE p.data_source = %s AND {col_b} IS NOT NULL
            )
            SELECT a.val AS val_a, b.val AS val_b
            FROM a INNER JOIN b USING (idcard_hash)
        """

    try:
        rows = execute_query(sql, (source_a, source_b)) or []
        return [(r["val_a"], r["val_b"]) for r in rows
                if r["val_a"] is not None and r["val_b"] is not None]
    except Exception as exc:
        logger.warning("Categorical pair query failed (%s %s↔%s): %s",
                       metric.key, source_a, source_b, exc)
        return []


# ---------------------------------------------------------------------------
# Bland-Altman
# ---------------------------------------------------------------------------

def _icc(values_a: np.ndarray, values_b: np.ndarray) -> Optional[float]:
    """Intraclass correlation (ICC), two-way agreement, absolute.

    Uses the standard formula for ICC(A,1):
        ICC = (BMS - EMS) / (BMS + (k-1)*EMS + k*(JMS-EMS)/n)
    with k=2 (two raters = two sources).

    Returns None on degenerate input (zero variance).
    """
    if len(values_a) < 3:
        return None
    n = len(values_a)
    k = 2
    try:
        # Total mean
        grand = np.mean(np.concatenate([values_a, values_b]))
        # Row (subject) means
        row_mean = (values_a + values_b) / 2.0
        # Column (rater) means
        col_mean = np.array([np.mean(values_a), np.mean(values_b)])
        # Mean squares
        ss_total = np.sum((values_a - grand) ** 2) + np.sum((values_b - grand) ** 2)
        ss_between_rows = k * np.sum((row_mean - grand) ** 2)
        ss_between_cols = n * np.sum((col_mean - grand) ** 2)
        ss_error = ss_total - ss_between_rows - ss_between_cols
        if ss_error < 0:
            ss_error = 0.0
        bms = ss_between_rows / (n - 1)
        jms = ss_between_cols / (k - 1)
        ems = ss_error / ((n - 1) * (k - 1))
        denom = bms + (k - 1) * ems + k * (jms - ems) / n
        if denom <= 0:
            return None
        icc = (bms - ems) / denom
        return float(np.clip(icc, -1.0, 1.0))
    except Exception:
        return None


def compute_bland_altman(
    pairs: List[Tuple[float, float]]
) -> Dict:
    """Given (a, b) pairs, return Bland-Altman statistics.

    Keys: n, bias, sd, loa_upper, loa_lower, icc, mean_a, mean_b,
          pearson_r, pct_within_loa.
    """
    n = len(pairs)
    if n < MIN_PAIRS_FOR_STATS:
        return {"n": n, "applicable": False,
                "reason": f"N={n} < {MIN_PAIRS_FOR_STATS} (ต้องมีอย่างน้อย {MIN_PAIRS_FOR_STATS} คู่)"}

    a = np.array([p[0] for p in pairs], dtype=float)
    b = np.array([p[1] for p in pairs], dtype=float)
    diff = a - b
    mean_pair = (a + b) / 2.0
    bias = float(np.mean(diff))
    sd = float(np.std(diff, ddof=1)) if n > 1 else 0.0
    loa_upper = bias + 1.96 * sd
    loa_lower = bias - 1.96 * sd
    within_loa = int(np.sum((diff >= loa_lower) & (diff <= loa_upper)))
    pct_within = 100.0 * within_loa / n if n else 0.0
    # Pearson r
    if sd == 0 or np.std(a) == 0 or np.std(b) == 0:
        pearson_r = None
    else:
        pearson_r = float(np.corrcoef(a, b)[0, 1])

    return {
        "n": n,
        "applicable": True,
        "bias": round(bias, 3),
        "sd": round(sd, 3),
        "loa_upper": round(loa_upper, 3),
        "loa_lower": round(loa_lower, 3),
        "pct_within_loa": round(pct_within, 1),
        "mean_a": round(float(np.mean(a)), 3),
        "mean_b": round(float(np.mean(b)), 3),
        "pearson_r": round(pearson_r, 3) if pearson_r is not None else None,
        "icc": _icc(a, b),
        "mean_pair": mean_pair.tolist(),
        "diff": diff.tolist(),
    }


def render_bland_altman_png(
    stats: Dict, label: str, unit: str, source_a: str, source_b: str
) -> Optional[str]:
    """Render Bland-Altman scatter as base64-encoded PNG data URL.

    Returns None if insufficient data.
    """
    if not stats.get("applicable") or stats["n"] < MIN_PAIRS_FOR_PLOT:
        return None

    mean_pair = np.array(stats["mean_pair"])
    diff = np.array(stats["diff"])
    bias = stats["bias"]
    loa_u = stats["loa_upper"]
    loa_l = stats["loa_lower"]

    fig, ax = plt.subplots(figsize=(6, 4), dpi=110)
    ax.scatter(mean_pair, diff, alpha=0.45, s=18, color="#00744B", edgecolors="none")
    ax.axhline(bias, linestyle="-", linewidth=1.5, color="#1f2937",
               label=f"Bias: {bias:.2f}")
    ax.axhline(loa_u, linestyle="--", linewidth=1.2, color="#dc2626",
               label=f"+1.96 SD: {loa_u:.2f}")
    ax.axhline(loa_l, linestyle="--", linewidth=1.2, color="#dc2626",
               label=f"−1.96 SD: {loa_l:.2f}")
    ax.set_xlabel(f"Mean of {source_a} & {source_b} ({unit})")
    ax.set_ylabel(f"Difference ({source_a} − {source_b}) ({unit})")
    ax.set_title(f"Bland-Altman: {label}  (N={stats['n']})", fontsize=11)
    ax.legend(fontsize=8, loc="upper right", framealpha=0.9)
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


# ---------------------------------------------------------------------------
# Cohen's kappa
# ---------------------------------------------------------------------------

def _kappa_interpretation(kappa: float) -> str:
    """Landis & Koch (1977) benchmark for interpreting κ."""
    if kappa < 0:
        return "แย่กว่าการเดา (poor)"
    if kappa < 0.20:
        return "น้อย (slight)"
    if kappa < 0.40:
        return "พอใช้ (fair)"
    if kappa < 0.60:
        return "ปานกลาง (moderate)"
    if kappa < 0.80:
        return "ดี (substantial)"
    return "เกือบสมบูรณ์ (almost perfect)"


def compute_kappa(pairs: List[Tuple[str, str]]) -> Dict:
    """Cohen's kappa + confusion matrix on categorical pairs."""
    n = len(pairs)
    if n < MIN_PAIRS_FOR_STATS:
        return {"n": n, "applicable": False,
                "reason": f"N={n} < {MIN_PAIRS_FOR_STATS}"}
    try:
        y1 = [p[0] for p in pairs]
        y2 = [p[1] for p in pairs]
        labels = sorted(set(y1) | set(y2))
        if len(labels) < 2:
            return {"n": n, "applicable": False,
                    "reason": "ค่าเดียวทั้ง 2 source — kappa ไม่มีความหมาย"}

        kappa = float(cohen_kappa_score(y1, y2, labels=labels))
        cm = confusion_matrix(y1, y2, labels=labels).tolist()
        agree = sum(1 for a, b in pairs if a == b)
        pct_agree = 100.0 * agree / n

        return {
            "n": n,
            "applicable": True,
            "kappa": round(kappa, 4),
            "pct_agreement": round(pct_agree, 1),
            "labels": labels,
            "confusion": cm,  # [[count_row_col, ...], ...]
            "interpretation": _kappa_interpretation(kappa),
        }
    except Exception as exc:
        return {"n": n, "applicable": False, "reason": f"คำนวณไม่ได้: {exc}"}


# ---------------------------------------------------------------------------
# Public entry point — build full report for a (source_a, source_b) pair
# ---------------------------------------------------------------------------

def build_agreement_report(source_a: str, source_b: str,
                           with_plots: bool = True) -> Dict:
    """Full report for a source-pair: continuous + categorical stats.

    with_plots: if False, skip matplotlib work (saves ~1s per metric).
    """
    continuous_results = []
    for metric in CONTINUOUS_METRICS:
        if source_a not in metric.col_by_source or source_b not in metric.col_by_source:
            continuous_results.append({
                "metric": metric.key, "label": metric.label, "unit": metric.unit,
                "applicable": False,
                "reason": f"Source '{source_a}' หรือ '{source_b}' ไม่มี field นี้",
            })
            continue
        pairs = _extract_continuous_pairs(metric, source_a, source_b)
        stats = compute_bland_altman(pairs)
        plot = (
            render_bland_altman_png(stats, metric.label, metric.unit, source_a, source_b)
            if with_plots else None
        )
        continuous_results.append({
            "metric": metric.key,
            "label": metric.label,
            "unit": metric.unit,
            **stats,
            "plot": plot,
        })

    categorical_results = []
    for metric in CATEGORICAL_METRICS:
        if source_a not in metric.col_by_source or source_b not in metric.col_by_source:
            categorical_results.append({
                "metric": metric.key, "label": metric.label,
                "applicable": False,
                "reason": f"Source '{source_a}' หรือ '{source_b}' ไม่มี field นี้",
            })
            continue
        pairs = _extract_categorical_pairs(metric, source_a, source_b)
        stats = compute_kappa(pairs)
        categorical_results.append({
            "metric": metric.key,
            "label": metric.label,
            **stats,
        })

    # Count people common to both sources (any table)
    try:
        n_common_rows = execute_query(
            """
            SELECT COUNT(DISTINCT p_a.idcard_hash) AS n
            FROM raw_patients p_a
            WHERE p_a.data_source = %s
              AND EXISTS (
                SELECT 1 FROM raw_patients p_b
                WHERE p_b.data_source = %s AND p_b.idcard_hash = p_a.idcard_hash
              )
            """,
            (source_a, source_b),
        )
        n_common = int(n_common_rows[0]["n"]) if n_common_rows else 0
    except Exception:
        n_common = 0

    return {
        "source_a": source_a,
        "source_b": source_b,
        "n_common_patients": n_common,
        "continuous": continuous_results,
        "categorical": categorical_results,
        "min_pairs_for_plot": MIN_PAIRS_FOR_PLOT,
        "min_pairs_for_stats": MIN_PAIRS_FOR_STATS,
    }
