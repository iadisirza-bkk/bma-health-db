"""QueryStatisticalTestTool — chi-square, ANOVA, logistic regression, etc.

SYNC — all computations are local (no external API calls).
"""
from __future__ import annotations

import math
import random
from collections import Counter
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from agents.tools.base import BaseTool, ToolResult
from agents.tools.helpers import (
    load_data, normalize_disease, get_base_rates, get_total_screened, apply_modifier,
    resolve_filter, DISEASE_NAMES, DISEASE_ALIASES, ALL_DISEASES,
    FACTOR_CATEGORIES, FACTOR_MODIFIERS,
)


# ---------------------------------------------------------------------------
# Inlined statistics_utils (one_way_anova, mann_kendall_test)
# These were in app/services/statistics_utils.py in the source project.
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz and Stegun)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    p = d * math.exp(-x * x / 2.0) * (
        t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    )
    return 1.0 - p if x > 0 else p


def _ln_gamma(x: float) -> float:
    if x <= 0:
        return float("inf")
    if x < 0.5:
        return math.log(math.pi / math.sin(math.pi * x)) - _ln_gamma(1.0 - x)
    x -= 1.0
    g = 7
    coefs = [
        0.99999999999980993, 676.5203681218851, -1259.1392167224028,
        771.32342877765313, -176.61502916214059, 12.507343278686905,
        -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7,
    ]
    s = coefs[0]
    for i in range(1, g + 2):
        s += coefs[i] / (x + i)
    t = x + g + 0.5
    return 0.5 * math.log(2.0 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(s)


def _gamma_lower_reg(a: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x < a + 1:
        ap = a
        s = 1.0 / a
        delta = s
        for _ in range(200):
            ap += 1
            delta *= x / ap
            s += delta
            if abs(delta) < abs(s) * 1e-10:
                break
        return s * math.exp(-x + a * math.log(x) - _ln_gamma(a))
    else:
        b = x + 1.0 - a
        c = 1.0 / 1e-30
        d_val = 1.0 / b
        h = d_val
        for i in range(1, 200):
            an = -i * (i - a)
            b += 2.0
            d_val = an * d_val + b
            if abs(d_val) < 1e-30:
                d_val = 1e-30
            c = b + an / c
            if abs(c) < 1e-30:
                c = 1e-30
            d_val = 1.0 / d_val
            delta = d_val * c
            h *= delta
            if abs(delta - 1.0) < 1e-10:
                break
        q = math.exp(-x + a * math.log(x) - _ln_gamma(a)) * h
        return 1.0 - q


def _reg_beta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _reg_beta(b, a, 1.0 - x)
    ln_prefix = (
        a * math.log(max(x, 1e-300))
        + b * math.log(max(1.0 - x, 1e-300))
        - math.log(a)
        - _ln_gamma(a) - _ln_gamma(b) + _ln_gamma(a + b)
    )
    try:
        prefix = math.exp(ln_prefix)
    except OverflowError:
        return 0.0
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d_val = 1.0 - qab * x / qap
    if abs(d_val) < 1e-30:
        d_val = 1e-30
    d_val = 1.0 / d_val
    h = d_val
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d_val = 1.0 + aa * d_val
        if abs(d_val) < 1e-30:
            d_val = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d_val = 1.0 / d_val
        h *= d_val * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d_val = 1.0 + aa * d_val
        if abs(d_val) < 1e-30:
            d_val = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d_val = 1.0 / d_val
        delta = d_val * c
        h *= delta
        if abs(delta - 1.0) < 1e-10:
            break
    return prefix * h


def _f_cdf(x: float, d1: int, d2: int) -> float:
    if x <= 0:
        return 0.0
    z = d1 * x / (d1 * x + d2)
    return _reg_beta(d1 / 2.0, d2 / 2.0, z)


def one_way_anova(groups: list[list[float]]) -> dict:
    k = len(groups)
    if k < 2:
        return {"f_statistic": 0.0, "p_value": 1.0, "df_between": 0, "df_within": 0, "eta_squared": 0.0}
    groups = [g for g in groups if len(g) > 0]
    k = len(groups)
    if k < 2:
        return {"f_statistic": 0.0, "p_value": 1.0, "df_between": 0, "df_within": 0, "eta_squared": 0.0}
    ns = [len(g) for g in groups]
    n_total = sum(ns)
    means = [sum(g) / len(g) for g in groups]
    grand_mean = sum(sum(g) for g in groups) / n_total
    ssb = sum(ns[i] * (means[i] - grand_mean) ** 2 for i in range(k))
    ssw = sum(sum((x - means[i]) ** 2 for x in groups[i]) for i in range(k))
    df_between = k - 1
    df_within = n_total - k
    if df_within <= 0 or ssw == 0:
        return {"f_statistic": float("inf") if ssb > 0 else 0.0, "p_value": 0.0 if ssb > 0 else 1.0,
                "df_between": df_between, "df_within": max(df_within, 0), "eta_squared": 1.0 if ssb > 0 else 0.0}
    msb = ssb / df_between
    msw = ssw / df_within
    f_stat = msb / msw if msw > 0 else 0.0
    p_value = 1.0 - _f_cdf(f_stat, df_between, df_within)
    sst = ssb + ssw
    eta_sq = ssb / sst if sst > 0 else 0.0
    return {"f_statistic": round(f_stat, 4), "p_value": round(p_value, 4),
            "df_between": df_between, "df_within": df_within, "eta_squared": round(eta_sq, 4)}


def mann_kendall_test(data_series: list[float]) -> dict:
    n = len(data_series)
    if n < 4:
        return {"direction": "insufficient_data", "tau": 0, "p_value": 1.0, "slope": 0, "confidence": "N/A"}
    s = 0
    for k in range(n - 1):
        for j in range(k + 1, n):
            diff = data_series[j] - data_series[k]
            if diff > 0:
                s += 1
            elif diff < 0:
                s -= 1
    ties = Counter(data_series)
    tie_sum = sum(t * (t - 1) * (2 * t + 5) for t in ties.values() if t > 1)
    var_s = (n * (n - 1) * (2 * n + 5) - tie_sum) / 18
    if s > 0:
        z = (s - 1) / math.sqrt(var_s) if var_s > 0 else 0
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s) if var_s > 0 else 0
    else:
        z = 0
    p_value = 2 * (1 - _norm_cdf(abs(z)))
    tau = s / (n * (n - 1) / 2)
    slopes = []
    for k in range(n):
        for j in range(k + 1, n):
            if j != k:
                slopes.append((data_series[j] - data_series[k]) / (j - k))
    slope = sorted(slopes)[len(slopes) // 2] if slopes else 0
    if p_value < 0.05:
        direction = "increasing" if tau > 0 else "decreasing"
    else:
        direction = "stable"
    if p_value < 0.01:
        confidence = "high (p<0.01)"
    elif p_value < 0.05:
        confidence = "moderate (p<0.05)"
    elif p_value < 0.10:
        confidence = "low (p<0.10)"
    else:
        confidence = "not significant (p>=0.10)"
    return {"direction": direction, "tau": round(tau, 4), "p_value": round(p_value, 4),
            "slope": round(slope, 4), "confidence": confidence}


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class QueryStatisticalTestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test: Literal["chi_square", "odds_ratio", "anova", "logistic_regression", "correlation", "mann_kendall", "comorbidity"]
    disease: Optional[str] = None
    factor: Optional[Literal["sex", "age_group", "smoking", "alcohol", "exercise", "zone"]] = None
    exposed_value: Optional[str] = None
    disease2: Optional[str] = None


class QueryStatisticalTestTool(BaseTool):
    name = "query_statistical_test"
    description = "Run statistical test: chi_square, odds_ratio, anova, logistic_regression, correlation, mann_kendall, comorbidity"
    Parameters = QueryStatisticalTestParams
    parameters_schema = {
        "type": "object",
        "properties": {
            "test": {"type": "string", "enum": ["chi_square", "odds_ratio", "anova", "logistic_regression", "correlation", "mann_kendall", "comorbidity"]},
            "disease": {"type": "string"},
            "factor": {"type": "string", "enum": ["sex", "age_group", "smoking", "alcohol", "exercise", "zone"]},
            "exposed_value": {"type": "string"},
            "disease2": {"type": "string"},
        },
        "required": ["test"],
    }

    def execute(self, args: dict) -> ToolResult:
        args = self.Parameters(**args).model_dump(exclude_none=True)
        data = load_data()
        test = args.get("test", "chi_square")
        disease = normalize_disease(args.get("disease", "diabetes"))
        factor = args.get("factor", "sex")
        exposed = args.get("exposed_value")

        base_rates = get_base_rates(data)
        total = get_total_screened(data)
        disease_name = DISEASE_NAMES.get(disease, disease)

        if test in ("chi_square", "odds_ratio", "anova", "logistic_regression") and factor in FACTOR_CATEGORIES:
            cats = FACTOR_CATEGORIES[factor]
            mods = FACTOR_MODIFIERS[factor]
            base = base_rates.get(disease, 0)
            rows = []
            for cat_key, cat_th, prop in cats:
                n = round(total * prop)
                mod = mods.get(cat_key, {}).get(disease, 1.0)
                rate = apply_modifier(base, mod)
                at_risk = round(n * rate / 100)
                rows.append({"category": cat_key, "label": cat_th, "n": n, "at_risk": at_risk, "rate": rate})

            if test == "chi_square":
                return self._chi_square(rows, disease_name, factor)
            elif test == "odds_ratio" and exposed:
                return self._odds_ratio(rows, disease_name, factor, exposed)
            elif test == "anova":
                return self._anova(rows, disease_name, factor)
            elif test == "logistic_regression":
                return self._logistic(rows, disease_name, factor)

        if test == "correlation":
            return self._correlation(data, disease, args.get("disease2"), base_rates)
        if test == "mann_kendall":
            return self._mann_kendall(disease, disease_name, base_rates)
        if test == "comorbidity":
            return self._comorbidity(data)

        return ToolResult(text="ไม่รองรับ test type")

    def _chi_square(self, rows, disease_name, factor) -> ToolResult:
        """Pearson chi-square goodness-of-fit on a single-factor 2-way table.

        Improvements over the previous implementation:
          - Yates' continuity correction for 2x2 tables (df=1) — recommended
            when any expected cell count is < 10.
          - Small-sample warning when ANY expected cell count < 5 (Cochran's
            rule). chi-square distribution is unreliable here; Fisher's exact
            would be better but isn't computed yet.
          - Uses scipy's exact chi2 survival function instead of an erf-based
            normal approximation that was inaccurate for small df.
        """
        total_at_risk = sum(r["at_risk"] for r in rows)
        total_n = sum(r["n"] for r in rows)
        expected_rate = total_at_risk / total_n if total_n > 0 else 0
        df = len(rows) - 1

        # Compute observed/expected per cell so we can apply Yates correction
        # and check the small-sample condition cell-by-cell.
        small_expected = False
        chi2 = 0.0
        apply_yates = (df == 1)
        for r in rows:
            obs_at_risk = r["at_risk"]
            obs_not = r["n"] - r["at_risk"]
            exp_at_risk = r["n"] * expected_rate
            exp_not = r["n"] * (1 - expected_rate)

            # Cochran's rule of thumb: any expected cell < 5 → warn
            if min(exp_at_risk, exp_not) < 5:
                small_expected = True

            # Yates: subtract 0.5 from |O-E| before squaring (only for df=1)
            d_at_risk = abs(obs_at_risk - exp_at_risk)
            d_not = abs(obs_not - exp_not)
            if apply_yates:
                d_at_risk = max(0, d_at_risk - 0.5)
                d_not = max(0, d_not - 0.5)

            chi2 += (d_at_risk ** 2) / max(exp_at_risk, 1)
            chi2 += (d_not ** 2) / max(exp_not, 1)

        # Use scipy's exact chi2 survival function — accurate for any df,
        # unlike the previous erf-based normal approximation.
        from scipy import stats as _scipy_stats
        if df > 0 and chi2 > 0:
            p = float(_scipy_stats.chi2.sf(chi2, df))
        else:
            p = 1.0
        # Don't underflow to 0 in display
        p_display = max(0.0001, round(p, 4))

        warnings_list: list[str] = []
        if small_expected:
            warnings_list.append("expected cell count < 5; p-value unreliable (consider Fisher's exact)")
        if apply_yates:
            warnings_list.append("Yates continuity correction applied (df=1)")

        sig_label = "มีนัยสำคัญ" if (p < 0.05 and not small_expected) else "ไม่มีนัยสำคัญ"
        text = f"## Chi-Square: {disease_name} x {factor}\n"
        text += f"- chi2={chi2:.2f}, df={df}, p={p_display:.4f}\n"
        text += f"- {sig_label}\n"
        if warnings_list:
            text += "- ⚠️ " + " | ".join(warnings_list) + "\n"
        chart_data = [{"name": r["label"], "value": r["rate"]} for r in rows]
        for r in rows:
            text += f"- {r['label']}: {r['rate']}%\n"
        viz = [{"type": "bar", "title": f"Chi-Square: {disease_name} x {factor}", "data": chart_data, "xKey": "name", "yKey": "value", "color": "#3b82f6"}]
        return ToolResult(text=text, visualizations=viz)

    def _odds_ratio(self, rows, disease_name, factor, exposed) -> ToolResult:
        resolved = resolve_filter(factor, exposed)
        exp_row = next((r for r in rows if r["category"] == resolved), None)
        if not exp_row:
            return ToolResult(text=f"ไม่พบกลุ่ม '{exposed}'")
        ue = [r for r in rows if r["category"] != resolved]
        ue_n, ue_risk = sum(r["n"] for r in ue), sum(r["at_risk"] for r in ue)
        a, b = exp_row["at_risk"], exp_row["n"] - exp_row["at_risk"]
        c, d = ue_risk, ue_n - ue_risk
        or_val = (a * d) / max(b * c, 1)
        se = math.sqrt(1/max(a,1) + 1/max(b,1) + 1/max(c,1) + 1/max(d,1))
        ci_lo, ci_hi = round(or_val * math.exp(-1.96 * se), 2), round(or_val * math.exp(1.96 * se), 2)
        text = f"## OR: {exposed} -> {disease_name}\n- OR={or_val:.2f} (95%CI: {ci_lo}-{ci_hi})\n"
        return ToolResult(text=text)

    def _anova(self, rows, disease_name, factor) -> ToolResult:
        groups = [[r["rate"]] * max(1, r["n"] // 1000) for r in rows]
        result = one_way_anova(groups)
        text = f"## ANOVA: {disease_name} x {factor}\n- F={result.get('f_statistic',0):.2f}, p={result.get('p_value',1):.4f}, eta2={result.get('eta_squared',0):.3f}\n"
        chart_data = [{"name": r["label"], "value": r["rate"]} for r in rows]
        viz = [{"type": "bar", "title": f"ANOVA: {disease_name} x {factor}", "data": chart_data, "xKey": "name", "yKey": "value", "color": "#8b5cf6"}]
        return ToolResult(text=text, visualizations=viz)

    def _logistic(self, rows, disease_name, factor) -> ToolResult:
        ref = rows[0]
        forest_data = []
        text = (
            f"## Logistic Regression: {factor} -> {disease_name}\n"
            f"Note: OR from cross-tabulation, p-value is approximate\n\n"
        )
        for r in rows:
            a, b = r["at_risk"], r["n"] - r["at_risk"]
            c, d = ref["at_risk"], ref["n"] - ref["at_risk"]
            or_val = (a * d) / max(b * c, 1)
            se = math.sqrt(1/max(a,1) + 1/max(b,1) + 1/max(c,1) + 1/max(d,1))
            ci_lo, ci_hi = round(or_val * math.exp(-1.96 * se), 2), round(or_val * math.exp(1.96 * se), 2)
            z_val = abs(math.log(max(or_val, 0.001))) / max(se, 0.001)
            p = max(0.0001, round(2 * (1 - 0.5 * (1 + math.erf(z_val / math.sqrt(2)))), 4))
            forest_data.append({"name": r["label"], "value": round(or_val, 2), "ci_low": ci_lo, "ci_high": ci_hi, "pValue": p})
            sig = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "ns"
            text += f"- {r['label']}: OR={or_val:.2f} ({ci_lo}-{ci_hi}) p={p:.4f} {sig}\n"
        text += f"\n*Reference group: {ref['label']}*"
        viz = [{"type": "forest_plot", "title": f"Forest Plot: {factor} -> {disease_name}", "data": forest_data, "xKey": "name", "yKey": "value"}]
        return ToolResult(text=text, visualizations=viz)

    def _correlation(self, data, disease, disease2_raw, base_rates) -> ToolResult:
        disease2 = normalize_disease(disease2_raw) or "hypertension"
        d2name = DISEASE_NAMES.get(disease2, disease2)
        dname = DISEASE_NAMES.get(disease, disease)
        vals1, vals2, districts = [], [], list(data.values())
        for d in districts:
            d1 = d["diseases"].get(disease, d["diseases"].get(next((k for k,v in DISEASE_ALIASES.items() if v == disease), ""), {}))
            d2 = d["diseases"].get(disease2, d["diseases"].get(next((k for k,v in DISEASE_ALIASES.items() if v == disease2), ""), {}))
            v1 = d1.get("pct_at_risk", 0) if isinstance(d1, dict) else 0
            v2 = d2.get("pct_at_risk", 0) if isinstance(d2, dict) else 0
            vals1.append(v1); vals2.append(v2)
        n = len(vals1)
        if n < 3:
            return ToolResult(text="ข้อมูลไม่พอ")
        m1, m2 = sum(vals1)/n, sum(vals2)/n
        cov = sum((a-m1)*(b-m2) for a,b in zip(vals1,vals2))/n
        s1 = max((sum((a-m1)**2 for a in vals1)/n)**0.5, 0.001)
        s2 = max((sum((b-m2)**2 for b in vals2)/n)**0.5, 0.001)
        r = cov / (s1 * s2)
        text = f"## Correlation: {dname} x {d2name}\n- r={r:.3f}, n={n} districts\n"
        scatter = [{"name": districts[i]["name_th"].replace("เขต",""), "x": vals1[i], "value": vals2[i], "size": districts[i]["total_screened"]} for i in range(n)]
        viz = [{"type": "scatter", "title": f"{dname} vs {d2name}", "data": scatter, "xKey": "x", "yKey": "value"}]
        return ToolResult(text=text, visualizations=viz)

    def _mann_kendall(self, disease, disease_name, base_rates) -> ToolResult:
        base = base_rates.get(disease, 30)
        random.seed(hash(disease))
        # WARNING: This uses SIMULATED monthly data (not real time-series)
        monthly = [round(base + random.gauss(0, base*0.05) + 2*math.sin(i*math.pi/6), 1) for i in range(12)]
        result = mann_kendall_test(monthly)
        dir_th = {"increasing": "เพิ่มขึ้น", "decreasing": "ลดลง"}.get(result.get("direction", ""), "คงที่")
        text = (
            f"## Mann-Kendall Trend Test: {disease_name}\n"
            f"Note: Simulated monthly data, not real time-series\n\n"
            f"- tau={result.get('tau',0):.3f}, slope={result.get('slope',0):.3f}, trend: {dir_th}\n"
        )
        months = ["ม.ค.","ก.พ.","มี.ค.","เม.ย.","พ.ค.","มิ.ย.","ก.ค.","ส.ค.","ก.ย.","ต.ค.","พ.ย.","ธ.ค."]
        viz = [{"type": "line", "title": f"Trend {disease_name} (simulated)", "data": [{"name": months[i], "value": monthly[i]} for i in range(12)], "xKey": "name", "yKey": "value", "color": "#00744B"}]
        return ToolResult(text=text, visualizations=viz)

    def _comorbidity(self, data) -> ToolResult:
        diseases = ALL_DISEASES
        names = [DISEASE_NAMES.get(dk, dk) for dk in diseases]
        matrix = []
        for i, d1 in enumerate(diseases):
            v1 = [dist["diseases"].get(d1, {}).get("pct_at_risk", 0) if isinstance(dist["diseases"].get(d1), dict) else 0 for dist in data.values()]
            for j, d2 in enumerate(diseases):
                v2 = [dist["diseases"].get(d2, {}).get("pct_at_risk", 0) if isinstance(dist["diseases"].get(d2), dict) else 0 for dist in data.values()]
                n = len(v1)
                m1, m2 = sum(v1)/n, sum(v2)/n
                cov = sum((a-m1)*(b-m2) for a,b in zip(v1,v2))/n
                s1 = max((sum((a-m1)**2 for a in v1)/n)**0.5, 0.001)
                s2 = max((sum((b-m2)**2 for b in v2)/n)**0.5, 0.001)
                matrix.append({"name": names[j], "y": names[i], "value": round(cov/(s1*s2), 2)})
        viz = [{"type": "heatmap", "title": "Comorbidity Matrix", "data": matrix, "xKey": "name", "yKey": "value"}]
        return ToolResult(text="Comorbidity Matrix (Pearson r)", visualizations=viz)
