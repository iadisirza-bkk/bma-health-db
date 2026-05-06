"""``audience_summary_clinician`` block — clinical-grade summary section.

Sprint S8 ("Audience-Segmented Report Sections") — this is the
clinician-facing slice of every audience-aware report. The reader is
assumed to be a physician / nurse / public-health practitioner who
wants:
    * Vital-sign distributions (mean, SD, p5/p50/p95, n)
    * Abnormal-rate matrices (sex × age × condition)
    * ICD-10 mapped prevalence of diagnosed conditions
    * Screening-protocol adherence

Data sources (per the S8 brief):
    * ``mv_visit_resolved``        — visit-level vitals/labs + dx flags
    * ``summary_disease_age_sex``  — pre-aggregated sex × age slices

The block reads from ``ctx.data_collector`` first; when the collector
does not pre-compute a particular field (most fields here are NOT
in the legacy ``ReportData`` dataclass since this block ships in S8
itself) it falls back to a tightly-scoped query via
:func:`database.execute_query`. The fallback path only fires when the
collector hasn't been pre-warmed with a matching key — this mirrors the
guidance in the S8 brief.

Render contract: ``audience_target = AudienceTarget.CLINICIAN`` so the
audience-routing layer (Task B3) drops this section when
``?audience=`` is set and excludes ``clinician``.
"""
from __future__ import annotations

import logging
import math
from typing import Any, ClassVar, Dict, List, Optional, Sequence

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks._stats_helpers import wilson_ci
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger(
    "api.services.reports.blocks.audience_summary_clinician"
)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _ClinicianParams(BaseModel):
    """Parameters for the ``audience_summary_clinician`` block.

    ``filters`` is a free-form dict (district, age range, sex, ...) that
    callers MAY pass through; the block treats unknown keys as opaque
    and only responds to documented filters. Today only ``dcode`` and
    ``sex`` are honoured — anything else is ignored with a debug log.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    filters: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# ICD-10 mapping (per S8 brief)
# ---------------------------------------------------------------------------
#
# Each tuple is (mv_visit_resolved column suffix, ICD-10 code, Thai term,
# English term). ``found_<suffix>`` is the canonical column on the visit
# MV — same suffix used by the disease_district_grid + chart blocks so
# the codes line up across the report.
_ICD10_MAP: Sequence[tuple[str, str, str, str]] = (
    ("dm",            "E11", "เบาหวานชนิดที่ 2",        "Type 2 diabetes mellitus"),
    ("hpt",           "I10", "ความดันโลหิตสูงไม่ทราบสาเหตุ", "Essential (primary) hypertension"),
    ("cvd",           "I25", "โรคหลอดเลือดหัวใจเรื้อรัง",   "Chronic ischaemic heart disease"),
    ("dyslipidemia",  "E78", "ภาวะไขมันในเลือดผิดปกติ",     "Disorders of lipoprotein metabolism"),
    ("stroke",        "I64", "โรคหลอดเลือดสมอง",        "Stroke, not specified"),
    ("obesity",       "E66", "โรคอ้วน",                "Obesity"),
)


# ---------------------------------------------------------------------------
# SQL helpers — used only when the collector doesn't pre-compute a field.
# Kept module-private + named so they're easy to mock from tests.
# ---------------------------------------------------------------------------


def _safe_execute_query(
    sql: str, params: Optional[tuple] = None
) -> List[Dict[str, Any]]:
    """``database.execute_query`` with a defensive empty-list fallback.

    Tests often mock the data collector and never expect us to reach the
    real DB; we don't want a bare ``ImportError`` / ``OperationalError``
    to crash the section render. Returning ``[]`` lets ``render_*`` emit
    a "no data" placeholder in that path instead of bubbling a 500.
    """
    try:
        from database import execute_query  # local import — DB may be absent in tests
        return execute_query(sql, params)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "audience_summary_clinician: SQL fallback failed (%s); "
            "rendering empty placeholder",
            exc,
        )
        return []


def _resolve_collector_field(collector: Any, key: str) -> Any:
    """Walk ``collector.data()`` (dict) or ``collector.<key>`` (attr).

    The S8 brief says "use ctx.data_collector — DO NOT write SQL
    directly". Some collectors expose a single ``data()`` dict; others
    are ReportData-like objects with attribute access. Try both before
    falling back to SQL. Returns ``None`` when the field is absent.
    """
    if collector is None:
        return None
    # 1. dict-shaped via ``data()``
    getter = getattr(collector, "data", None)
    if callable(getter):
        try:
            bag = getter()
        except Exception:  # pragma: no cover — defensive
            bag = None
        if isinstance(bag, dict) and key in bag:
            return bag[key]
        if hasattr(bag, key):
            return getattr(bag, key)
    # 2. attribute on the collector itself
    if hasattr(collector, key):
        return getattr(collector, key)
    return None


# ---------------------------------------------------------------------------
# Distribution computation — vitals (SBP, DBP, BMI, FBS) from mv_visit_resolved
# ---------------------------------------------------------------------------


# Display names for each metric. Keep keys identical to the column names
# on ``mv_visit_resolved`` so the SQL query can use them verbatim.
_METRIC_LABELS_TH: Dict[str, str] = {
    "sbp":  "ความดันตัวบน (SBP, mmHg)",
    "dbp":  "ความดันตัวล่าง (DBP, mmHg)",
    "bmi":  "ดัชนีมวลกาย (BMI, kg/m²)",
    "fbs":  "น้ำตาลในเลือด (FBS, mg/dL)",
}
_METRIC_LABELS_EN: Dict[str, str] = {
    "sbp":  "Systolic blood pressure (mmHg)",
    "dbp":  "Diastolic blood pressure (mmHg)",
    "bmi":  "Body mass index (kg/m²)",
    "fbs":  "Fasting blood sugar (mg/dL)",
}


def _distribution_sql(filters: Optional[Dict[str, Any]]) -> tuple[str, tuple]:
    """Return SQL + params for the vital-sign distribution query.

    We compute mean / stddev / count and the three percentiles
    (p5/p50/p95) per-metric in one PostgreSQL pass via
    ``percentile_cont`` for the medians and ``avg`` / ``stddev_samp``
    for mean / SD. Using one query keeps the round-trip to one DB pass
    even when the data collector hasn't pre-warmed any of these fields.
    """
    where_clauses: List[str] = []
    params: List[Any] = []
    if filters:
        if filters.get("dcode"):
            where_clauses.append("dcode = %s")
            params.append(str(filters["dcode"]))
        if filters.get("sex"):
            where_clauses.append("sex = %s")
            params.append(str(filters["sex"]))
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = f"""
        SELECT
            'sbp'::text  AS metric,
            COUNT(sbp)   AS n,
            AVG(sbp)::float    AS mean,
            STDDEV_SAMP(sbp)::float AS sd,
            PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY sbp)::float AS p5,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY sbp)::float AS p50,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY sbp)::float AS p95
        FROM public.mv_visit_resolved {where_sql}
        UNION ALL
        SELECT 'dbp', COUNT(dbp),
               AVG(dbp)::float, STDDEV_SAMP(dbp)::float,
               PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY dbp)::float,
               PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY dbp)::float,
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY dbp)::float
        FROM public.mv_visit_resolved {where_sql}
        UNION ALL
        SELECT 'bmi', COUNT(bmi),
               AVG(bmi)::float, STDDEV_SAMP(bmi)::float,
               PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY bmi)::float,
               PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY bmi)::float,
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY bmi)::float
        FROM public.mv_visit_resolved {where_sql}
        UNION ALL
        SELECT 'fbs', COUNT(fbs),
               AVG(fbs)::float, STDDEV_SAMP(fbs)::float,
               PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY fbs)::float,
               PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY fbs)::float,
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY fbs)::float
        FROM public.mv_visit_resolved {where_sql}
    """
    # The same params tuple is reused for all four UNION-ALL legs.
    return sql, tuple(params * 4)


# ---------------------------------------------------------------------------
# Abnormal-rate matrix — % FBS≥126, % BP≥140/90, % BMI≥30 by sex × age
# ---------------------------------------------------------------------------


def _abnormal_rate_sql(filters: Optional[Dict[str, Any]]) -> tuple[str, tuple]:
    """Return SQL+params for the % abnormal matrix per (sex, age_group).

    Pulls counts from ``summary_disease_age_sex`` (already gated on
    k-anonymity ≥ 5 by the MV definition). The ``risk_*`` and ``found_*``
    suffix conventions match the rest of the codebase
    (see api/services/data_adapter.py:_DISEASE_MAP).
    """
    where_clauses: List[str] = ["age_group != '__none__'", "sex != 'unknown'"]
    params: List[Any] = []
    if filters:
        if filters.get("dcode"):
            where_clauses.append("dcode = %s")
            params.append(str(filters["dcode"]))
        if filters.get("sex"):
            where_clauses.append("sex = %s")
            params.append(str(filters["sex"]))
    where_sql = "WHERE " + " AND ".join(where_clauses)
    sql = f"""
        SELECT sex,
               age_group,
               SUM(total_screened) AS total,
               SUM(risk_dm)        AS dm_count,
               SUM(risk_hpt)       AS hpt_count,
               SUM(found_obesity)  AS obesity_count
        FROM public.summary_disease_age_sex
        {where_sql}
        GROUP BY sex, age_group
        ORDER BY sex, age_group
    """
    return sql, tuple(params)


# ---------------------------------------------------------------------------
# ICD-10 prevalence — top 5 diagnosed conditions
# ---------------------------------------------------------------------------


def _icd10_sql(filters: Optional[Dict[str, Any]]) -> tuple[str, tuple]:
    """One scan of mv_visit_resolved, summing ``found_*`` flags."""
    where_clauses: List[str] = []
    params: List[Any] = []
    if filters:
        if filters.get("dcode"):
            where_clauses.append("dcode = %s")
            params.append(str(filters["dcode"]))
        if filters.get("sex"):
            where_clauses.append("sex = %s")
            params.append(str(filters["sex"]))
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    cols = ", ".join(
        f"SUM(CASE WHEN found_{suf} THEN 1 ELSE 0 END) AS {suf}_n"
        for suf, _, _, _ in _ICD10_MAP
    )
    sql = f"""
        SELECT COUNT(*) AS total, {cols}
        FROM public.mv_visit_resolved
        {where_sql}
    """
    return sql, tuple(params)


# ---------------------------------------------------------------------------
# Screening-protocol adherence — % of visits with full panel
# ---------------------------------------------------------------------------


def _adherence_sql(filters: Optional[Dict[str, Any]]) -> tuple[str, tuple]:
    """Adherence = visits with vitals AND labs AND behaviour all populated.

    The "full panel" definition follows the S8 brief: vitals (sbp+dbp+bmi)
    + labs (fbs at minimum) + behaviour (smoking_status). Each leg is a
    NOT NULL check on the corresponding column; we sum them up in one
    pass for the simple ratio.
    """
    where_clauses: List[str] = []
    params: List[Any] = []
    if filters:
        if filters.get("dcode"):
            where_clauses.append("dcode = %s")
            params.append(str(filters["dcode"]))
        if filters.get("sex"):
            where_clauses.append("sex = %s")
            params.append(str(filters["sex"]))
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN sbp IS NOT NULL
                        AND dbp IS NOT NULL
                        AND bmi IS NOT NULL
                        AND fbs IS NOT NULL
                        AND smoking_status IS NOT NULL
                        THEN 1 ELSE 0 END) AS full_panel
        FROM public.mv_visit_resolved
        {where_sql}
    """
    return sql, tuple(params)


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class AudienceSummaryClinicianBlock(ContentBlock):
    """Clinician-facing summary: distributions + abnormal rates + ICD-10."""

    block_id: ClassVar[str] = "audience_summary_clinician"
    Parameters: ClassVar[type[BaseModel]] = _ClinicianParams
    audience_target: ClassVar[Optional[AudienceTarget]] = AudienceTarget.CLINICIAN

    # ------------------------------------------------------------------
    # collect — one DB pass per "section" (4 sections, mostly small queries)
    # ------------------------------------------------------------------

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _ClinicianParams)
        filters = params.filters or {}

        # Section 1 — distributions. The collector almost never has these
        # pre-computed (the legacy ReportData has aggregate disease %, not
        # raw vital distributions), so we go to SQL via _safe_execute_query.
        dist_rows = _resolve_collector_field(
            ctx.data_collector, "vitals_distribution"
        )
        if not isinstance(dist_rows, list) or not dist_rows:
            sql, params_t = _distribution_sql(filters)
            dist_rows = _safe_execute_query(sql, params_t)
        distributions = self._normalise_distribution_rows(dist_rows)

        # Section 2 — abnormal rate matrix from summary_disease_age_sex.
        abn_rows = _resolve_collector_field(
            ctx.data_collector, "abnormal_rates_by_sex_age"
        )
        if not isinstance(abn_rows, list) or not abn_rows:
            sql, params_t = _abnormal_rate_sql(filters)
            abn_rows = _safe_execute_query(sql, params_t)
        abnormal_matrix = self._build_abnormal_matrix(abn_rows)

        # Section 3 — ICD-10 prevalence.
        icd_row: Optional[Dict[str, Any]] = None
        icd_collected = _resolve_collector_field(
            ctx.data_collector, "icd10_prevalence"
        )
        if isinstance(icd_collected, list) and icd_collected:
            icd_row = icd_collected[0]
        if icd_row is None:
            sql, params_t = _icd10_sql(filters)
            rows = _safe_execute_query(sql, params_t)
            icd_row = rows[0] if rows else None
        icd10 = self._build_icd10(icd_row)

        # Section 4 — adherence.
        adh_row: Optional[Dict[str, Any]] = None
        adh_collected = _resolve_collector_field(
            ctx.data_collector, "screening_adherence"
        )
        if isinstance(adh_collected, list) and adh_collected:
            adh_row = adh_collected[0]
        elif isinstance(adh_collected, dict):
            adh_row = adh_collected
        if adh_row is None:
            sql, params_t = _adherence_sql(filters)
            rows = _safe_execute_query(sql, params_t)
            adh_row = rows[0] if rows else None
        adherence = self._build_adherence(adh_row)

        return {
            "distributions": distributions,
            "abnormal_matrix": abnormal_matrix,
            "icd10": icd10,
            "adherence": adherence,
            "lang": ctx.lang,
        }

    # ------------------------------------------------------------------
    # Data shapers — pure functions on the SQL row shapes for testability
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_distribution_rows(
        rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Reshape raw rows → a stable list of dicts the renderers consume."""
        out: List[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            metric = str(row.get("metric") or "").lower()
            if not metric:
                continue
            n = int(row.get("n") or 0)
            mean = row.get("mean")
            sd = row.get("sd")
            out.append({
                "metric": metric,
                "label_th": _METRIC_LABELS_TH.get(metric, metric),
                "label_en": _METRIC_LABELS_EN.get(metric, metric),
                "n": n,
                "mean": float(mean) if mean is not None else None,
                "sd": float(sd) if sd is not None else None,
                "p5": float(row["p5"]) if row.get("p5") is not None else None,
                "p50": float(row["p50"]) if row.get("p50") is not None else None,
                "p95": float(row["p95"]) if row.get("p95") is not None else None,
            })
        return out

    @staticmethod
    def _build_abnormal_matrix(
        rows: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Sex × age × condition matrix with Wilson CIs.

        Output shape:
            {
              "sex_levels": [...],
              "age_levels": [...],
              "cells": {(sex, age): {"total": ..., "rates": {cond: {...}}}}
            }
        """
        sex_levels: List[str] = []
        age_levels: List[str] = []
        cells: Dict[tuple[str, str], Dict[str, Any]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            sex = str(row.get("sex") or "")
            age = str(row.get("age_group") or "")
            if not sex or not age:
                continue
            if sex not in sex_levels:
                sex_levels.append(sex)
            if age not in age_levels:
                age_levels.append(age)
            total = int(row.get("total") or 0)
            dm_n = int(row.get("dm_count") or 0)
            hpt_n = int(row.get("hpt_count") or 0)
            obesity_n = int(row.get("obesity_count") or 0)
            rates: Dict[str, Dict[str, Any]] = {}
            for cond_key, cond_count in (
                ("fbs_high", dm_n),       # % FBS≥126 ≡ %_at_risk_dm in MVs
                ("bp_high", hpt_n),       # % BP≥140/90 ≡ %_at_risk_hpt
                ("bmi_high", obesity_n),  # % BMI≥30 ≡ found_obesity
            ):
                if total >= 5:  # k-anonymity ≥ 5 — redact small cells
                    pct = (cond_count / total) if total else 0.0
                    lo, hi = wilson_ci(cond_count, total)
                    rates[cond_key] = {
                        "k": cond_count,
                        "n": total,
                        "pct": pct,
                        "ci_lo": lo,
                        "ci_hi": hi,
                    }
                else:
                    rates[cond_key] = {
                        "k": None,
                        "n": total,
                        "pct": None,
                        "ci_lo": None,
                        "ci_hi": None,
                        "redacted": True,
                    }
            cells[(sex, age)] = {"total": total, "rates": rates}
        # Sort age_levels lexicographically (matches '0-19', '20-29', ...)
        age_levels = sorted(age_levels)
        return {
            "sex_levels": sex_levels,
            "age_levels": age_levels,
            "cells": cells,
        }

    @staticmethod
    def _build_icd10(row: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Top-5 diagnosed conditions with prevalence + 95% Wilson CI."""
        out: List[Dict[str, Any]] = []
        if not isinstance(row, dict):
            return out
        total = int(row.get("total") or 0)
        for suffix, code, name_th, name_en in _ICD10_MAP:
            n_raw = row.get(f"{suffix}_n")
            n = int(n_raw or 0)
            if total >= 5:
                pct = (n / total) if total else 0.0
                lo, hi = wilson_ci(n, total)
            else:
                pct, lo, hi = (None, None, None)
            out.append({
                "code": code,
                "term_th": name_th,
                "term_en": name_en,
                "n": n,
                "total": total,
                "pct": pct,
                "ci_lo": lo,
                "ci_hi": hi,
                "redacted": total < 5,
            })
        # Top-5 by raw count.
        out.sort(key=lambda r: (-int(r.get("n") or 0), r.get("code") or ""))
        return out[:5]

    @staticmethod
    def _build_adherence(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Single-cell summary + 95% Wilson CI."""
        if not isinstance(row, dict):
            return {"total": 0, "full_panel": 0, "pct": None, "ci_lo": None,
                    "ci_hi": None, "redacted": True}
        total = int(row.get("total") or 0)
        full = int(row.get("full_panel") or 0)
        if total >= 5:
            pct = (full / total) if total else 0.0
            lo, hi = wilson_ci(full, total)
            return {"total": total, "full_panel": full, "pct": pct,
                    "ci_lo": lo, "ci_hi": hi, "redacted": False}
        return {"total": total, "full_panel": full, "pct": None,
                "ci_lo": None, "ci_hi": None, "redacted": True}

    # ------------------------------------------------------------------
    # render — HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        lang = data.get("lang", "th")
        parts: List[str] = []
        parts.append(self._html_distributions(data["distributions"], lang))
        parts.append(self._html_abnormal_matrix(data["abnormal_matrix"], lang))
        parts.append(self._html_icd10(data["icd10"], lang))
        parts.append(self._html_adherence(data["adherence"], lang))
        return '<section class="audience-summary clinician">' + "".join(parts) + "</section>"

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        lang = data.get("lang", "th")
        parts: List[str] = []
        parts.append(self._latex_distributions(data["distributions"], lang))
        parts.append(self._latex_abnormal_matrix(data["abnormal_matrix"], lang))
        parts.append(self._latex_icd10(data["icd10"], lang))
        parts.append(self._latex_adherence(data["adherence"], lang))
        return "\n\n".join(p for p in parts if p) + "\n"

    # ------------------------------------------------------------------
    # Render helpers — HTML
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_pct(p: Optional[float]) -> str:
        if p is None:
            return "—"
        return f"{p * 100:.1f}%"

    @staticmethod
    def _fmt_ci(
        pct: Optional[float], lo: Optional[float], hi: Optional[float]
    ) -> str:
        """Produce '23.4% (95% CI 22.8-24.0)' or em-dash when missing."""
        if pct is None or lo is None or hi is None:
            return "—"
        return (
            f"{pct * 100:.1f}% (95% CI "
            f"{lo * 100:.1f}–{hi * 100:.1f})"
        )

    @staticmethod
    def _fmt_meansd(mean: Optional[float], sd: Optional[float]) -> str:
        if mean is None:
            return "—"
        if sd is None or (isinstance(sd, float) and math.isnan(sd)):
            return f"{mean:.1f}"
        return f"{mean:.1f} ± {sd:.1f}"

    @staticmethod
    def _fmt_p(v: Optional[float]) -> str:
        return "—" if v is None else f"{v:.1f}"

    def _html_distributions(
        self, dists: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "1. การกระจายค่าทางคลินิก" if lang != "en" else "1. Clinical-value distributions"
        if not dists:
            return f'<h3>{title}</h3><p class="empty">ไม่มีข้อมูล</p>'
        head = (
            "<thead><tr>"
            "<th>Metric</th><th>mean ± SD</th>"
            "<th>p5</th><th>p50</th><th>p95</th><th>n</th>"
            "</tr></thead>"
        )
        rows: List[str] = []
        for d in dists:
            label = d.get("label_en" if lang == "en" else "label_th") or d["metric"]
            rows.append(
                "<tr>"
                f"<td>{_html_escape(str(label))}</td>"
                f"<td>{self._fmt_meansd(d.get('mean'), d.get('sd'))}</td>"
                f"<td>{self._fmt_p(d.get('p5'))}</td>"
                f"<td>{self._fmt_p(d.get('p50'))}</td>"
                f"<td>{self._fmt_p(d.get('p95'))}</td>"
                f"<td>{int(d.get('n') or 0):,}</td>"
                "</tr>"
            )
        return (
            f'<h3>{title}</h3>'
            f'<table class="audience-clinician distributions">'
            f'{head}<tbody>{"".join(rows)}</tbody></table>'
        )

    def _html_abnormal_matrix(
        self, matrix: Dict[str, Any], lang: str
    ) -> str:
        title = "2. อัตราผิดปกติ (Sex × Age × ภาวะ)" if lang != "en" else "2. Abnormal rates (Sex × Age × Condition)"
        sex_levels: List[str] = matrix.get("sex_levels", [])
        age_levels: List[str] = matrix.get("age_levels", [])
        cells: Dict[tuple[str, str], Dict[str, Any]] = matrix.get("cells", {})
        if not sex_levels or not age_levels:
            return f'<h3>{title}</h3><p class="empty">ไม่มีข้อมูล</p>'
        cond_keys = ("fbs_high", "bp_high", "bmi_high")
        cond_labels = {
            "fbs_high": "FBS≥126",
            "bp_high":  "BP≥140/90",
            "bmi_high": "BMI≥30",
        }
        head_th = "<th>Sex</th><th>Age</th>" + "".join(
            f"<th>{cond_labels[k]}</th>" for k in cond_keys
        )
        body_rows: List[str] = []
        for sex in sex_levels:
            for age in age_levels:
                cell = cells.get((sex, age))
                if not cell:
                    continue
                rates = cell.get("rates", {})
                tds = [
                    f"<td>{_html_escape(sex)}</td>",
                    f"<td>{_html_escape(age)}</td>",
                ]
                for k in cond_keys:
                    r = rates.get(k, {})
                    if r.get("redacted"):
                        tds.append("<td>—</td>")
                    else:
                        tds.append(
                            "<td>"
                            + _html_escape(
                                self._fmt_ci(
                                    r.get("pct"), r.get("ci_lo"), r.get("ci_hi")
                                )
                            )
                            + "</td>"
                        )
                body_rows.append("<tr>" + "".join(tds) + "</tr>")
        return (
            f'<h3>{title}</h3>'
            f'<table class="audience-clinician abnormal">'
            f"<thead><tr>{head_th}</tr></thead>"
            f'<tbody>{"".join(body_rows)}</tbody></table>'
        )

    def _html_icd10(self, icd10: List[Dict[str, Any]], lang: str) -> str:
        title = "3. ความชุกตามรหัส ICD-10 (Top 5)" if lang != "en" else "3. ICD-10 prevalence (Top 5)"
        if not icd10:
            return f'<h3>{title}</h3><p class="empty">ไม่มีข้อมูล</p>'
        rows: List[str] = []
        for r in icd10:
            term = r.get("term_en" if lang == "en" else "term_th") or ""
            rows.append(
                "<tr>"
                f"<td>{_html_escape(str(r.get('code') or ''))}</td>"
                f"<td>{_html_escape(str(term))}</td>"
                f"<td>{int(r.get('n') or 0):,}</td>"
                f"<td>{_html_escape(self._fmt_ci(r.get('pct'), r.get('ci_lo'), r.get('ci_hi')))}</td>"
                "</tr>"
            )
        return (
            f'<h3>{title}</h3>'
            f'<table class="audience-clinician icd10">'
            f"<thead><tr><th>ICD-10</th><th>Term</th><th>n</th><th>% (95% CI)</th></tr></thead>"
            f'<tbody>{"".join(rows)}</tbody></table>'
        )

    def _html_adherence(
        self, adherence: Dict[str, Any], lang: str
    ) -> str:
        title = "4. ความครบถ้วนของชุดคัดกรอง" if lang != "en" else "4. Screening-protocol adherence"
        n_full = int(adherence.get("full_panel") or 0)
        n_total = int(adherence.get("total") or 0)
        ci = self._fmt_ci(
            adherence.get("pct"), adherence.get("ci_lo"), adherence.get("ci_hi")
        )
        body = (
            f'<p>เคสครบชุด (vitals + labs + behaviour): '
            f'<strong>{n_full:,} / {n_total:,}</strong> '
            f'= <strong>{_html_escape(ci)}</strong></p>'
        )
        return f'<h3>{title}</h3>' + body

    # ------------------------------------------------------------------
    # Render helpers — LaTeX
    # ------------------------------------------------------------------

    @staticmethod
    def _ci_latex(
        pct: Optional[float], lo: Optional[float], hi: Optional[float]
    ) -> str:
        """LaTeX-safe CI string. ``%`` is escaped; en-dashes use ``--``."""
        if pct is None or lo is None or hi is None:
            return "---"
        return (
            f"{pct * 100:.1f}\\% (95\\% CI "
            f"{lo * 100:.1f}--{hi * 100:.1f})"
        )

    def _latex_distributions(
        self, dists: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "1. การกระจายค่าทางคลินิก" if lang != "en" else "1. Clinical-value distributions"
        if not dists:
            return r"\subsection*{" + latex_escape(title) + "}" + r"\textit{No data.}"
        out: List[str] = []
        out.append(r"\subsection*{" + latex_escape(title) + "}")
        out.append(r"\begin{tabular}{l|c|c|c|c|r}")
        out.append(r"\toprule")
        out.append(
            r"\textbf{Metric} & \textbf{mean $\pm$ SD} & "
            r"\textbf{p5} & \textbf{p50} & \textbf{p95} & \textbf{n} \\"
        )
        out.append(r"\midrule")
        for d in dists:
            label = d.get("label_en" if lang == "en" else "label_th") or d["metric"]
            out.append(
                latex_escape(str(label))
                + " & " + self._fmt_meansd(d.get("mean"), d.get("sd"))
                + " & " + self._fmt_p(d.get("p5"))
                + " & " + self._fmt_p(d.get("p50"))
                + " & " + self._fmt_p(d.get("p95"))
                + " & " + f"{int(d.get('n') or 0):,}"
                + r" \\"
            )
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        return "\n".join(out)

    def _latex_abnormal_matrix(
        self, matrix: Dict[str, Any], lang: str
    ) -> str:
        title = "2. อัตราผิดปกติ (Sex × Age × ภาวะ)" if lang != "en" else "2. Abnormal rates (Sex × Age × Condition)"
        sex_levels: List[str] = matrix.get("sex_levels", [])
        age_levels: List[str] = matrix.get("age_levels", [])
        cells: Dict[tuple[str, str], Dict[str, Any]] = matrix.get("cells", {})
        if not sex_levels or not age_levels:
            return r"\subsection*{" + latex_escape(title) + "}" + r"\textit{No data.}"
        cond_keys = ("fbs_high", "bp_high", "bmi_high")
        cond_labels = {
            "fbs_high": r"FBS$\geq$126",
            "bp_high":  r"BP$\geq$140/90",
            "bmi_high": r"BMI$\geq$30",
        }
        out: List[str] = []
        out.append(r"\subsection*{" + latex_escape(title) + "}")
        out.append(r"\begin{tabular}{l|l|c|c|c}")
        out.append(r"\toprule")
        head_cells = [r"\textbf{Sex}", r"\textbf{Age}"]
        for k in cond_keys:
            head_cells.append(r"\textbf{" + cond_labels[k] + "}")
        out.append(" & ".join(head_cells) + r" \\")
        out.append(r"\midrule")
        for sex in sex_levels:
            for age in age_levels:
                cell = cells.get((sex, age))
                if not cell:
                    continue
                rates = cell.get("rates", {})
                cells_row = [latex_escape(sex), latex_escape(age)]
                for k in cond_keys:
                    r = rates.get(k, {})
                    if r.get("redacted"):
                        cells_row.append("---")
                    else:
                        cells_row.append(
                            self._ci_latex(
                                r.get("pct"), r.get("ci_lo"), r.get("ci_hi")
                            )
                        )
                out.append(" & ".join(cells_row) + r" \\")
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        return "\n".join(out)

    def _latex_icd10(self, icd10: List[Dict[str, Any]], lang: str) -> str:
        title = "3. ความชุกตามรหัส ICD-10 (Top 5)" if lang != "en" else "3. ICD-10 prevalence (Top 5)"
        if not icd10:
            return r"\subsection*{" + latex_escape(title) + "}" + r"\textit{No data.}"
        out: List[str] = []
        out.append(r"\subsection*{" + latex_escape(title) + "}")
        out.append(r"\begin{tabular}{l|l|r|c}")
        out.append(r"\toprule")
        out.append(r"\textbf{ICD-10} & \textbf{Term} & \textbf{n} & \textbf{\% (95\% CI)} \\")
        out.append(r"\midrule")
        for r in icd10:
            term = r.get("term_en" if lang == "en" else "term_th") or ""
            out.append(
                latex_escape(str(r.get("code") or ""))
                + " & " + latex_escape(str(term))
                + " & " + f"{int(r.get('n') or 0):,}"
                + " & " + self._ci_latex(
                    r.get("pct"), r.get("ci_lo"), r.get("ci_hi")
                )
                + r" \\"
            )
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        return "\n".join(out)

    def _latex_adherence(
        self, adherence: Dict[str, Any], lang: str
    ) -> str:
        title = "4. ความครบถ้วนของชุดคัดกรอง" if lang != "en" else "4. Screening-protocol adherence"
        n_full = int(adherence.get("full_panel") or 0)
        n_total = int(adherence.get("total") or 0)
        ci = self._ci_latex(
            adherence.get("pct"), adherence.get("ci_lo"), adherence.get("ci_hi")
        )
        body = (
            f"เคสครบชุด: \\textbf{{{n_full:,} / {n_total:,}}} = "
            f"\\textbf{{{ci}}}"
        )
        return r"\subsection*{" + latex_escape(title) + "}\n" + body


# ---------------------------------------------------------------------------
# Local HTML helpers — keep block self-contained
# ---------------------------------------------------------------------------


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__ = ["AudienceSummaryClinicianBlock"]
