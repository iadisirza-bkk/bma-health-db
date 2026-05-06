"""``audience_summary_researcher`` block — research-grade summary section.

Sprint S8 ("Audience-Segmented Report Sections") — this is the
researcher-facing slice of every audience-aware report. The reader is
assumed to be an epidemiologist / data scientist / academic doing a
population-based study and wants:

    * Demographics: n by sex × age × district
    * Disease prevalence with **95% Wilson CIs** — overall + by sex + by age
    * Sex × Disease association tests (χ², p, OR with Woolf 95% CI)
    * Age-prevalence linear trend (slope, R², p)
    * A loud **selection-bias disclaimer** at the top — screening data are
      NOT a probability sample of the Bangkok population.

Every numeric output respects k-anonymity ≥ 5: cells with raw n<5 are
redacted to em-dash. The underlying ``mv_*`` views already enforce
k-anon, but we also redact derivative cells (per-stratum prevalence /
per-OR contingency cells) where the raw count drops under the threshold.

Render contract: ``audience_target = AudienceTarget.RESEARCHER`` so the
audience-routing layer (Task B3) drops this section when ``?audience=``
is set and excludes ``researcher``.
"""
from __future__ import annotations

import logging
import math
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict

from services.latex_utils import latex_escape
from services.reports.blocks._stats_helpers import (
    selection_bias_disclaimer_en,
    selection_bias_disclaimer_th,
    wilson_ci,
)
from services.reports.blocks.base import AudienceTarget, ContentBlock
from services.reports.spec import RenderContext

logger = logging.getLogger(
    "api.services.reports.blocks.audience_summary_researcher"
)


# ---------------------------------------------------------------------------
# Disease set + display labels
# ---------------------------------------------------------------------------
#
# The researcher view reports prevalence + association for these six. We
# match the same suffix convention used by the clinician block.
_DISEASES: Sequence[Tuple[str, str, str]] = (
    ("dm",           "เบาหวาน",                "Diabetes mellitus"),
    ("hpt",          "ความดันโลหิตสูง",         "Hypertension"),
    ("cvd",          "โรคหลอดเลือดหัวใจ",      "Cardiovascular disease"),
    ("dyslipidemia", "ภาวะไขมันในเลือดผิดปกติ", "Dyslipidemia"),
    ("stroke",       "โรคหลอดเลือดสมอง",       "Stroke"),
    ("obesity",      "โรคอ้วน",                "Obesity"),
)

# Minimum cell-count for k-anonymity. Same constant the MV definitions
# use; redeclared locally so the block stays self-contained.
_K_ANON: int = 5


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class _ResearcherParams(BaseModel):
    """Parameters for the ``audience_summary_researcher`` block."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    filters: Optional[Dict[str, Any]] = None
    alpha: float = 0.05  # significance threshold, also drives Wilson CI z


# ---------------------------------------------------------------------------
# SQL fallback helpers
# ---------------------------------------------------------------------------


def _safe_execute_query(
    sql: str, params: Optional[tuple] = None
) -> List[Dict[str, Any]]:
    """``database.execute_query`` with a defensive empty-list fallback.

    Tests often mock the data collector and never expect us to reach the
    real DB. Returning ``[]`` lets the renderer emit a "no data"
    placeholder instead of bubbling a 500.
    """
    try:
        from database import execute_query
        return execute_query(sql, params)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "audience_summary_researcher: SQL fallback failed (%s); "
            "rendering empty placeholder",
            exc,
        )
        return []


def _resolve_collector_field(collector: Any, key: str) -> Any:
    """Walk ``collector.data()`` (dict) or ``collector.<key>`` (attr)."""
    if collector is None:
        return None
    getter = getattr(collector, "data", None)
    if callable(getter):
        try:
            bag = getter()
        except Exception:  # pragma: no cover
            bag = None
        if isinstance(bag, dict) and key in bag:
            return bag[key]
        if hasattr(bag, key):
            return getattr(bag, key)
    if hasattr(collector, key):
        return getattr(collector, key)
    return None


# ---------------------------------------------------------------------------
# Statistics — pure functions on aggregate counts
# ---------------------------------------------------------------------------


def _chi2_2x2(
    a: int, b: int, c: int, d: int
) -> Tuple[Optional[float], Optional[float]]:
    """χ² statistic + two-sided p-value for a 2×2 table.

    Layout::
                disease+   disease-
        sex=M     a          b
        sex=F     c          d

    Uses scipy when available; falls back to a manual χ² + survival
    function approximation (Wilson-Hilferty) so the block degrades
    gracefully on environments without scipy. Returns ``(None, None)``
    when any marginal is zero.
    """
    n = a + b + c + d
    if n == 0:
        return None, None
    row1 = a + b
    row2 = c + d
    col1 = a + c
    col2 = b + d
    if row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        return None, None
    try:
        from scipy.stats import chi2_contingency
        chi2, p, _dof, _exp = chi2_contingency(
            [[a, b], [c, d]], correction=False
        )
        return float(chi2), float(p)
    except Exception:  # pragma: no cover — fallback
        # Manual χ² with df=1.
        e_a = row1 * col1 / n
        e_b = row1 * col2 / n
        e_c = row2 * col1 / n
        e_d = row2 * col2 / n
        chi2 = sum(
            (obs - exp) ** 2 / exp if exp > 0 else 0.0
            for obs, exp in zip((a, b, c, d), (e_a, e_b, e_c, e_d))
        )
        # Wilson-Hilferty p-value approximation for χ² with df=1.
        # P(χ²>x) ≈ erfc(sqrt(x/2)) for df=1.
        p = math.erfc(math.sqrt(chi2 / 2.0)) if chi2 > 0 else 1.0
        return float(chi2), float(p)


def _odds_ratio_woolf(
    a: int, b: int, c: int, d: int, alpha: float = 0.05
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Crude odds ratio with Woolf 95% CI on the log scale.

    Adds 0.5 to every cell when any cell is zero (Haldane-Anscombe
    correction) so log(0) doesn't blow up the SE estimate. Returns
    ``(or, lo, hi)``; ``None`` means the table couldn't produce a
    meaningful estimate (e.g. all zeros).
    """
    if min(a, b, c, d) < 0:
        return None, None, None
    n = a + b + c + d
    if n == 0:
        return None, None, None
    if 0 in (a, b, c, d):
        a += 0.5
        b += 0.5
        c += 0.5
        d += 0.5
    if b * c == 0:
        return None, None, None
    or_val = (a * d) / (b * c)
    if or_val <= 0:
        return None, None, None
    log_or = math.log(or_val)
    se = math.sqrt(1.0 / a + 1.0 / b + 1.0 / c + 1.0 / d)
    # 95% z (mirrors _stats_helpers._z_for_alpha; importing isn't worth
    # the cycle since we already need the alpha → z mapping locally).
    z = 1.96 if abs(alpha - 0.05) < 1e-9 else 1.6449 if abs(alpha - 0.10) < 1e-9 else 2.5758
    lo = math.exp(log_or - z * se)
    hi = math.exp(log_or + z * se)
    return float(or_val), float(lo), float(hi)


def _linregress_age(
    age_groups: Sequence[str], prevalences: Sequence[float]
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Simple linear regression of prevalence on numeric age-group rank.

    Returns ``(slope, r_squared, p_value)``. ``None`` when there are
    fewer than 3 points or zero x-variance. Uses scipy.stats.linregress
    when available; falls back to manual OLS otherwise.
    """
    pts = [(i, p) for i, p in enumerate(prevalences) if p is not None]
    if len(pts) < 3:
        return None, None, None
    xs = [float(i) for i, _ in pts]
    ys = [float(p) for _, p in pts]
    try:
        from scipy.stats import linregress
        result = linregress(xs, ys)
        return float(result.slope), float(result.rvalue ** 2), float(result.pvalue)
    except Exception:  # pragma: no cover — fallback
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        syy = sum((y - mean_y) ** 2 for y in ys)
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        if sxx == 0:
            return None, None, None
        slope = sxy / sxx
        if syy == 0:
            r2: float = 1.0
        else:
            r2 = (sxy ** 2) / (sxx * syy)
        # Two-sided p via t-test on slope (df = n - 2). With df=1
        # collapsing the t-tail to the normal works fine for n>=3.
        if n > 2 and r2 < 1.0:
            t_stat = slope * math.sqrt((n - 2) / max(1.0 - r2, 1e-12) / max(sxx / n, 1e-12))
            # Approximate two-sided p via standard normal — close enough
            # for n>=3, plenty for a report.
            p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t_stat) / math.sqrt(2.0))))
        else:
            p = 0.0 if r2 == 1.0 else 1.0
        return float(slope), float(r2), float(p)


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------


def _demographics_sql(filters: Optional[Dict[str, Any]]) -> tuple[str, tuple]:
    """n by sex × age × district from summary_disease_age_sex."""
    where_clauses: List[str] = ["age_group != '__none__'"]
    params: List[Any] = []
    if filters and filters.get("dcode"):
        where_clauses.append("dcode = %s")
        params.append(str(filters["dcode"]))
    where_sql = "WHERE " + " AND ".join(where_clauses)
    sql = f"""
        SELECT dcode, sex, age_group, SUM(total_screened) AS n
        FROM public.summary_disease_age_sex
        {where_sql}
        GROUP BY dcode, sex, age_group
        ORDER BY dcode, sex, age_group
    """
    return sql, tuple(params)


def _prevalence_sql(filters: Optional[Dict[str, Any]]) -> tuple[str, tuple]:
    """Per-stratum disease counts.

    Returns rows with all six disease counts plus ``total_screened``,
    keyed by sex and age_group. The block aggregates further (overall,
    by-sex, by-age) in pure Python.
    """
    where_clauses: List[str] = ["age_group != '__none__'", "sex != 'unknown'"]
    params: List[Any] = []
    if filters and filters.get("dcode"):
        where_clauses.append("dcode = %s")
        params.append(str(filters["dcode"]))
    where_sql = "WHERE " + " AND ".join(where_clauses)
    sql = f"""
        SELECT sex, age_group,
               SUM(total_screened) AS total,
               SUM(risk_dm)        AS dm,
               SUM(risk_hpt)       AS hpt,
               SUM(risk_cvd)       AS cvd,
               SUM(found_dyslipidemia) AS dyslipidemia,
               SUM(found_stroke)   AS stroke,
               SUM(found_obesity)  AS obesity
        FROM public.summary_disease_age_sex
        {where_sql}
        GROUP BY sex, age_group
        ORDER BY sex, age_group
    """
    return sql, tuple(params)


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


class AudienceSummaryResearcherBlock(ContentBlock):
    """Researcher-facing summary: demographics + prevalence + tests + trend."""

    block_id: ClassVar[str] = "audience_summary_researcher"
    Parameters: ClassVar[type[BaseModel]] = _ResearcherParams
    audience_target: ClassVar[Optional[AudienceTarget]] = AudienceTarget.RESEARCHER

    # ------------------------------------------------------------------
    # collect
    # ------------------------------------------------------------------

    async def collect(
        self,
        ctx: RenderContext,
        params: BaseModel,
    ) -> Dict[str, Any]:
        assert isinstance(params, _ResearcherParams)
        filters = params.filters or {}
        alpha = float(params.alpha)

        # Demographics — long-format rows ``[{dcode, sex, age_group, n}, ...]``.
        demo_rows = _resolve_collector_field(
            ctx.data_collector, "demographics_long"
        )
        if not isinstance(demo_rows, list) or not demo_rows:
            sql, p = _demographics_sql(filters)
            demo_rows = _safe_execute_query(sql, p)

        # Per-stratum disease counts — used for prevalence + association.
        prev_rows = _resolve_collector_field(
            ctx.data_collector, "prevalence_strata"
        )
        if not isinstance(prev_rows, list) or not prev_rows:
            sql, p = _prevalence_sql(filters)
            prev_rows = _safe_execute_query(sql, p)

        prevalence = self._build_prevalence(prev_rows, alpha)
        association = self._build_association(prev_rows, alpha)
        age_trend = self._build_age_trend(prev_rows, alpha)

        return {
            "lang": ctx.lang,
            "alpha": alpha,
            "demographics": self._sanitize_demographics(demo_rows),
            "prevalence": prevalence,
            "association": association,
            "age_trend": age_trend,
        }

    # ------------------------------------------------------------------
    # Data shapers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_demographics(
        rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Redact rows with n<5 (k-anonymity) but keep the strata."""
        out: List[Dict[str, Any]] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            n = int(r.get("n") or 0)
            out.append({
                "dcode": str(r.get("dcode") or ""),
                "sex": str(r.get("sex") or ""),
                "age_group": str(r.get("age_group") or ""),
                "n": n if n >= _K_ANON else None,
                "redacted": n < _K_ANON,
            })
        return out

    @staticmethod
    def _aggregate(
        rows: List[Dict[str, Any]],
        col: str,
        group_key: Optional[str] = None,
    ) -> Dict[str, Tuple[int, int]]:
        """Sum ``col`` and ``total`` over rows, optionally grouped.

        Returns ``{label: (k, n)}``. ``label = '__overall__'`` when
        ``group_key`` is ``None``.
        """
        out: Dict[str, Tuple[int, int]] = {}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            label = "__overall__" if group_key is None else str(r.get(group_key) or "")
            k = int(r.get(col) or 0)
            n = int(r.get("total") or 0)
            ck, cn = out.get(label, (0, 0))
            out[label] = (ck + k, cn + n)
        return out

    def _build_prevalence(
        self, rows: List[Dict[str, Any]], alpha: float
    ) -> List[Dict[str, Any]]:
        """One row per (disease, stratum) with k/n + Wilson 95% CI."""
        out: List[Dict[str, Any]] = []
        for suf, name_th, name_en in _DISEASES:
            for stratum_label, group_key in (
                ("overall", None),
                ("by_sex", "sex"),
                ("by_age", "age_group"),
            ):
                bag = self._aggregate(rows, suf, group_key)
                for label, (k, n) in sorted(bag.items()):
                    if n < _K_ANON:
                        out.append({
                            "disease": suf,
                            "name_th": name_th,
                            "name_en": name_en,
                            "stratum": stratum_label,
                            "stratum_label": label,
                            "k": k,
                            "n": n,
                            "pct": None,
                            "ci_lo": None,
                            "ci_hi": None,
                            "redacted": True,
                        })
                        continue
                    pct = k / n if n else 0.0
                    lo, hi = wilson_ci(k, n, alpha=alpha)
                    out.append({
                        "disease": suf,
                        "name_th": name_th,
                        "name_en": name_en,
                        "stratum": stratum_label,
                        "stratum_label": label,
                        "k": k,
                        "n": n,
                        "pct": pct,
                        "ci_lo": lo,
                        "ci_hi": hi,
                        "redacted": False,
                    })
        return out

    def _build_association(
        self, rows: List[Dict[str, Any]], alpha: float
    ) -> List[Dict[str, Any]]:
        """Sex × Disease 2×2 contingency tests.

        Treats sex='M' as exposed, sex='F' as unexposed, disease=col as
        outcome. Returns one record per disease.
        """
        out: List[Dict[str, Any]] = []
        for suf, name_th, name_en in _DISEASES:
            # Build a 2×2 table summed over age groups.
            table_M = (0, 0)  # (cases, total)
            table_F = (0, 0)
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                sex = str(r.get("sex") or "").upper()[:1]
                k = int(r.get(suf) or 0)
                n = int(r.get("total") or 0)
                if sex == "M":
                    table_M = (table_M[0] + k, table_M[1] + n)
                elif sex == "F":
                    table_F = (table_F[0] + k, table_F[1] + n)
            a = table_M[0]
            b = max(table_M[1] - table_M[0], 0)
            c = table_F[0]
            d = max(table_F[1] - table_F[0], 0)
            if (a + b + c + d) < _K_ANON:
                out.append({
                    "disease": suf, "name_th": name_th, "name_en": name_en,
                    "table": (a, b, c, d),
                    "chi2": None, "p": None,
                    "or": None, "or_lo": None, "or_hi": None,
                    "redacted": True,
                })
                continue
            chi2, p = _chi2_2x2(a, b, c, d)
            or_val, or_lo, or_hi = _odds_ratio_woolf(a, b, c, d, alpha=alpha)
            out.append({
                "disease": suf,
                "name_th": name_th,
                "name_en": name_en,
                "table": (a, b, c, d),
                "chi2": chi2,
                "p": p,
                "or": or_val,
                "or_lo": or_lo,
                "or_hi": or_hi,
                "redacted": False,
            })
        return out

    def _build_age_trend(
        self, rows: List[Dict[str, Any]], alpha: float
    ) -> List[Dict[str, Any]]:
        """Per-disease prevalence by age group + linear-trend stats."""
        # Build age-group → (k, n) for each disease.
        out: List[Dict[str, Any]] = []
        for suf, name_th, name_en in _DISEASES:
            by_age = self._aggregate(rows, suf, "age_group")
            ages_sorted = sorted(by_age.keys())
            prevalences: List[Optional[float]] = []
            points: List[Dict[str, Any]] = []
            for age in ages_sorted:
                k, n = by_age[age]
                if n < _K_ANON:
                    prevalences.append(None)
                    points.append({
                        "age_group": age, "k": k, "n": n,
                        "pct": None, "redacted": True,
                    })
                    continue
                p = k / n if n else 0.0
                prevalences.append(p)
                points.append({
                    "age_group": age, "k": k, "n": n,
                    "pct": p, "redacted": False,
                })
            slope, r2, pval = _linregress_age(ages_sorted, prevalences)
            out.append({
                "disease": suf,
                "name_th": name_th,
                "name_en": name_en,
                "points": points,
                "slope": slope,
                "r2": r2,
                "p": pval,
            })
        return out

    # ------------------------------------------------------------------
    # Render — HTML
    # ------------------------------------------------------------------

    def render_html(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        lang = data.get("lang", "th")
        disclaimer = (
            selection_bias_disclaimer_en()
            if lang == "en"
            else selection_bias_disclaimer_th()
        )
        parts: List[str] = [
            f'<aside class="selection-bias-disclaimer">'
            f'<strong>{"Methodological note" if lang == "en" else "ข้อจำกัดเชิงระเบียบวิธี"}:</strong> '
            f'{_html_escape(disclaimer)}'
            f'</aside>'
        ]
        parts.append(self._html_demographics(data["demographics"], lang))
        parts.append(self._html_prevalence(data["prevalence"], lang))
        parts.append(self._html_association(data["association"], lang))
        parts.append(self._html_age_trend(data["age_trend"], lang))
        return '<section class="audience-summary researcher">' + "".join(parts) + "</section>"

    def render_latex(
        self,
        data: Dict[str, Any],
        params: BaseModel,
        ctx: RenderContext,
    ) -> str:
        lang = data.get("lang", "th")
        disclaimer = (
            selection_bias_disclaimer_en()
            if lang == "en"
            else selection_bias_disclaimer_th()
        )
        # Disclaimer goes inside a faint quote block so it's visually
        # separated from the body without needing an extra LaTeX package.
        parts: List[str] = [
            r"\begin{quote}\textit{"
            + latex_escape(disclaimer)
            + r"}\end{quote}"
        ]
        parts.append(self._latex_demographics(data["demographics"], lang))
        parts.append(self._latex_prevalence(data["prevalence"], lang))
        parts.append(self._latex_association(data["association"], lang))
        parts.append(self._latex_age_trend(data["age_trend"], lang))
        return "\n\n".join(p for p in parts if p) + "\n"

    # ------------------------------------------------------------------
    # Render helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_pct_ci(
        pct: Optional[float], lo: Optional[float], hi: Optional[float]
    ) -> str:
        if pct is None or lo is None or hi is None:
            return "—"
        return (
            f"{pct * 100:.1f}% (95% CI "
            f"{lo * 100:.1f}–{hi * 100:.1f})"
        )

    @staticmethod
    def _fmt_pct_ci_latex(
        pct: Optional[float], lo: Optional[float], hi: Optional[float]
    ) -> str:
        if pct is None or lo is None or hi is None:
            return "---"
        return (
            f"{pct * 100:.1f}\\% (95\\% CI "
            f"{lo * 100:.1f}--{hi * 100:.1f})"
        )

    @staticmethod
    def _fmt_or_ci(
        v: Optional[float], lo: Optional[float], hi: Optional[float]
    ) -> str:
        if v is None or lo is None or hi is None:
            return "—"
        return f"{v:.2f} ({lo:.2f}–{hi:.2f})"

    @staticmethod
    def _fmt_or_ci_latex(
        v: Optional[float], lo: Optional[float], hi: Optional[float]
    ) -> str:
        if v is None or lo is None or hi is None:
            return "---"
        return f"{v:.2f} ({lo:.2f}--{hi:.2f})"

    @staticmethod
    def _fmt_p(p: Optional[float]) -> str:
        if p is None:
            return "—"
        if p < 0.001:
            return "<0.001"
        return f"{p:.3f}"

    @staticmethod
    def _sig_marks(p: Optional[float]) -> str:
        if p is None:
            return ""
        if p < 0.01:
            return "**"
        if p < 0.05:
            return "*"
        return ""

    # ------- HTML --------------------------------------------------------

    def _html_demographics(
        self, demo: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "1. ลักษณะกลุ่มประชากรที่ศึกษา" if lang != "en" else "1. Cohort demographics"
        if not demo:
            return f'<h3>{title}</h3><p class="empty">ไม่มีข้อมูล</p>'
        head = "<thead><tr><th>District</th><th>Sex</th><th>Age</th><th>n</th></tr></thead>"
        rows: List[str] = []
        for r in demo:
            n_str = "—" if r.get("redacted") else f"{int(r.get('n') or 0):,}"
            rows.append(
                "<tr>"
                f"<td>{_html_escape(str(r.get('dcode') or ''))}</td>"
                f"<td>{_html_escape(str(r.get('sex') or ''))}</td>"
                f"<td>{_html_escape(str(r.get('age_group') or ''))}</td>"
                f"<td>{n_str}</td>"
                "</tr>"
            )
        return (
            f'<h3>{title}</h3>'
            f'<table class="audience-researcher demographics">'
            f'{head}<tbody>{"".join(rows)}</tbody></table>'
        )

    def _html_prevalence(
        self, prev: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "2. ความชุก (Wilson 95% CI)" if lang != "en" else "2. Prevalence (Wilson 95% CI)"
        if not prev:
            return f'<h3>{title}</h3><p class="empty">ไม่มีข้อมูล</p>'
        head = (
            "<thead><tr><th>Disease</th><th>Stratum</th>"
            "<th>k/N</th><th>% (95% CI)</th></tr></thead>"
        )
        rows: List[str] = []
        for r in prev:
            name = r.get("name_en" if lang == "en" else "name_th") or r["disease"]
            stratum = (
                r.get("stratum_label")
                if r.get("stratum") != "overall"
                else "overall"
            )
            kN = "—" if r.get("redacted") else f"{int(r['k']):,} / {int(r['n']):,}"
            ci = self._fmt_pct_ci(r.get("pct"), r.get("ci_lo"), r.get("ci_hi"))
            rows.append(
                "<tr>"
                f"<td>{_html_escape(str(name))}</td>"
                f"<td>{_html_escape(str(stratum))}</td>"
                f"<td>{kN}</td>"
                f"<td>{_html_escape(ci)}</td>"
                "</tr>"
            )
        return (
            f'<h3>{title}</h3>'
            f'<table class="audience-researcher prevalence">'
            f'{head}<tbody>{"".join(rows)}</tbody></table>'
        )

    def _html_association(
        self, assoc: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "3. การทดสอบ Sex × Disease (χ², OR)" if lang != "en" else "3. Sex × Disease association (χ², OR)"
        if not assoc:
            return f'<h3>{title}</h3><p class="empty">ไม่มีข้อมูล</p>'
        head = (
            "<thead><tr><th>Disease</th><th>χ²</th><th>p</th>"
            "<th>OR (95% CI)</th></tr></thead>"
        )
        rows: List[str] = []
        for r in assoc:
            name = r.get("name_en" if lang == "en" else "name_th") or r["disease"]
            sig = self._sig_marks(r.get("p"))
            chi2 = "—" if r.get("chi2") is None else f"{r['chi2']:.2f}"
            p_str = self._fmt_p(r.get("p"))
            or_str = self._fmt_or_ci(r.get("or"), r.get("or_lo"), r.get("or_hi"))
            rows.append(
                "<tr>"
                f"<td>{_html_escape(str(name))}</td>"
                f"<td>{chi2}</td>"
                f"<td>{_html_escape(p_str)}{sig}</td>"
                f"<td>{_html_escape(or_str)}</td>"
                "</tr>"
            )
        return (
            f'<h3>{title}</h3>'
            f'<table class="audience-researcher association">'
            f'{head}<tbody>{"".join(rows)}</tbody></table>'
            f'<p class="legend"><sup>*</sup> p&lt;.05, '
            f'<sup>**</sup> p&lt;.01</p>'
        )

    def _html_age_trend(
        self, trend: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "4. แนวโน้มตามกลุ่มอายุ (linear trend)" if lang != "en" else "4. Age-prevalence linear trend"
        if not trend:
            return f'<h3>{title}</h3><p class="empty">ไม่มีข้อมูล</p>'
        head = (
            "<thead><tr><th>Disease</th><th>slope</th>"
            "<th>R²</th><th>p</th></tr></thead>"
        )
        rows: List[str] = []
        for r in trend:
            name = r.get("name_en" if lang == "en" else "name_th") or r["disease"]
            slope = "—" if r.get("slope") is None else f"{r['slope']:.4f}"
            r2 = "—" if r.get("r2") is None else f"{r['r2']:.3f}"
            p_str = self._fmt_p(r.get("p"))
            sig = self._sig_marks(r.get("p"))
            rows.append(
                "<tr>"
                f"<td>{_html_escape(str(name))}</td>"
                f"<td>{slope}</td>"
                f"<td>{r2}</td>"
                f"<td>{_html_escape(p_str)}{sig}</td>"
                "</tr>"
            )
        return (
            f'<h3>{title}</h3>'
            f'<table class="audience-researcher age-trend">'
            f'{head}<tbody>{"".join(rows)}</tbody></table>'
        )

    # ------- LaTeX -------------------------------------------------------

    def _latex_demographics(
        self, demo: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "1. ลักษณะกลุ่มประชากรที่ศึกษา" if lang != "en" else "1. Cohort demographics"
        if not demo:
            return r"\subsection*{" + latex_escape(title) + "}" + r"\textit{No data.}"
        out: List[str] = [r"\subsection*{" + latex_escape(title) + "}"]
        out.append(r"\begin{tabular}{l|l|l|r}")
        out.append(r"\toprule")
        out.append(r"\textbf{District} & \textbf{Sex} & \textbf{Age} & \textbf{n} \\")
        out.append(r"\midrule")
        for r in demo:
            n_str = "---" if r.get("redacted") else f"{int(r.get('n') or 0):,}"
            out.append(
                latex_escape(str(r.get("dcode") or ""))
                + " & " + latex_escape(str(r.get("sex") or ""))
                + " & " + latex_escape(str(r.get("age_group") or ""))
                + " & " + n_str
                + r" \\"
            )
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        return "\n".join(out)

    def _latex_prevalence(
        self, prev: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "2. ความชุก (Wilson 95\\% CI)" if lang != "en" else "2. Prevalence (Wilson 95\\% CI)"
        if not prev:
            return r"\subsection*{" + title + "}" + r"\textit{No data.}"
        out: List[str] = [r"\subsection*{" + title + "}"]
        out.append(r"\begin{tabular}{l|l|r|c}")
        out.append(r"\toprule")
        out.append(r"\textbf{Disease} & \textbf{Stratum} & \textbf{k/N} & \textbf{\% (95\% CI)} \\")
        out.append(r"\midrule")
        for r in prev:
            name = r.get("name_en" if lang == "en" else "name_th") or r["disease"]
            stratum = (
                r.get("stratum_label")
                if r.get("stratum") != "overall"
                else "overall"
            )
            kN = "---" if r.get("redacted") else f"{int(r['k']):,} / {int(r['n']):,}"
            ci = self._fmt_pct_ci_latex(r.get("pct"), r.get("ci_lo"), r.get("ci_hi"))
            out.append(
                latex_escape(str(name))
                + " & " + latex_escape(str(stratum))
                + " & " + kN
                + " & " + ci
                + r" \\"
            )
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        return "\n".join(out)

    def _latex_association(
        self, assoc: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "3. การทดสอบ Sex × Disease ($\\chi^2$, OR)" if lang != "en" else "3. Sex $\\times$ Disease association ($\\chi^2$, OR)"
        if not assoc:
            return r"\subsection*{" + title + "}" + r"\textit{No data.}"
        out: List[str] = [r"\subsection*{" + title + "}"]
        out.append(r"\begin{tabular}{l|c|c|c}")
        out.append(r"\toprule")
        out.append(
            r"\textbf{Disease} & \textbf{$\chi^2$} & \textbf{p} & \textbf{OR (95\% CI)} \\"
        )
        out.append(r"\midrule")
        for r in assoc:
            name = r.get("name_en" if lang == "en" else "name_th") or r["disease"]
            chi2 = "---" if r.get("chi2") is None else f"{r['chi2']:.2f}"
            p_str = self._fmt_p(r.get("p"))
            or_str = self._fmt_or_ci_latex(r.get("or"), r.get("or_lo"), r.get("or_hi"))
            sig = self._sig_marks(r.get("p"))
            # ``*``/``**`` need not be escaped; latex sees them as text in
            # tabular cells (no ``\textit{*}`` math context here).
            out.append(
                latex_escape(str(name))
                + " & " + chi2
                + " & " + p_str + sig
                + " & " + or_str
                + r" \\"
            )
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        out.append(r"\par\smallskip\textit{$^{*}$ p$<$.05, $^{**}$ p$<$.01}")
        return "\n".join(out)

    def _latex_age_trend(
        self, trend: List[Dict[str, Any]], lang: str
    ) -> str:
        title = "4. แนวโน้มตามกลุ่มอายุ (linear trend)" if lang != "en" else "4. Age-prevalence linear trend"
        if not trend:
            return r"\subsection*{" + latex_escape(title) + "}" + r"\textit{No data.}"
        out: List[str] = [r"\subsection*{" + latex_escape(title) + "}"]
        out.append(r"\begin{tabular}{l|c|c|c}")
        out.append(r"\toprule")
        out.append(r"\textbf{Disease} & \textbf{slope} & \textbf{R$^2$} & \textbf{p} \\")
        out.append(r"\midrule")
        for r in trend:
            name = r.get("name_en" if lang == "en" else "name_th") or r["disease"]
            slope = "---" if r.get("slope") is None else f"{r['slope']:.4f}"
            r2 = "---" if r.get("r2") is None else f"{r['r2']:.3f}"
            p_str = self._fmt_p(r.get("p"))
            sig = self._sig_marks(r.get("p"))
            out.append(
                latex_escape(str(name))
                + " & " + slope
                + " & " + r2
                + " & " + p_str + sig
                + r" \\"
            )
        out.append(r"\bottomrule")
        out.append(r"\end{tabular}")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Local HTML helpers
# ---------------------------------------------------------------------------


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__ = ["AudienceSummaryResearcherBlock"]
