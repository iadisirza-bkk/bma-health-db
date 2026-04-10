"""Statistical utility functions for trend analysis and inferential statistics.

Implements non-parametric Mann-Kendall trend test, Sen's slope estimator,
chi-square test, odds ratio, ANOVA, logistic regression, and Fisher's exact test
without requiring scipy dependency.

Ported from bma-health/backend/app/services/statistics_utils.py — identical logic.
"""

import math
from collections import Counter


def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz and Stegun)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    p = d * math.exp(-x * x / 2.0) * (
        t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    )
    return 1.0 - p if x > 0 else p


def mann_kendall_test(data: list[float]) -> dict:
    """Non-parametric Mann-Kendall trend test.

    Returns: direction, tau, p_value, slope (Sen's slope estimator),
    and confidence level string.
    """
    n = len(data)
    if n < 4:
        return {
            "direction": "insufficient_data",
            "tau": 0,
            "p_value": 1.0,
            "slope": 0,
            "confidence": "N/A",
        }

    # Calculate S statistic
    s = 0
    for k in range(n - 1):
        for j in range(k + 1, n):
            diff = data[j] - data[k]
            if diff > 0:
                s += 1
            elif diff < 0:
                s -= 1

    # Variance of S -- account for ties
    ties = Counter(data)
    tie_sum = sum(t * (t - 1) * (2 * t + 5) for t in ties.values() if t > 1)
    var_s = (n * (n - 1) * (2 * n + 5) - tie_sum) / 18

    # Z statistic
    if s > 0:
        z = (s - 1) / math.sqrt(var_s) if var_s > 0 else 0
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s) if var_s > 0 else 0
    else:
        z = 0

    # Two-tailed p-value (normal approximation)
    p_value = 2 * (1 - _norm_cdf(abs(z)))

    # Kendall's tau
    tau = s / (n * (n - 1) / 2)

    # Sen's slope estimator
    slopes = []
    for k in range(n):
        for j in range(k + 1, n):
            if j != k:
                slopes.append((data[j] - data[k]) / (j - k))
    slope = sorted(slopes)[len(slopes) // 2] if slopes else 0

    # Direction with significance
    if p_value < 0.05:
        direction = "increasing" if tau > 0 else "decreasing"
    else:
        direction = "stable"

    # Confidence level
    if p_value < 0.01:
        confidence = "high (p<0.01)"
    elif p_value < 0.05:
        confidence = "moderate (p<0.05)"
    elif p_value < 0.10:
        confidence = "low (p<0.10)"
    else:
        confidence = "not significant (p>=0.10)"

    return {
        "direction": direction,
        "tau": round(tau, 4),
        "p_value": round(p_value, 4),
        "slope": round(slope, 4),
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Chi-square CDF approximation helpers
# ---------------------------------------------------------------------------

def _ln_gamma(x: float) -> float:
    """Log-gamma function using Lanczos approximation (g=7, n=9)."""
    if x <= 0:
        return float("inf")
    if x < 0.5:
        return math.log(math.pi / math.sin(math.pi * x)) - _ln_gamma(1.0 - x)
    x -= 1.0
    g = 7
    coefs = [
        0.99999999999980993,
        676.5203681218851,
        -1259.1392167224028,
        771.32342877765313,
        -176.61502916214059,
        12.507343278686905,
        -0.13857109526572012,
        9.9843695780195716e-6,
        1.5056327351493116e-7,
    ]
    s = coefs[0]
    for i in range(1, g + 2):
        s += coefs[i] / (x + i)
    t = x + g + 0.5
    return 0.5 * math.log(2.0 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(s)


def _gamma_lower_reg(a: float, x: float) -> float:
    """Regularized lower incomplete gamma function P(a, x) via series expansion."""
    if x < 0:
        return 0.0
    if x == 0:
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
        d = 1.0 / b
        h = d
        for i in range(1, 200):
            an = -i * (i - a)
            b += 2.0
            d = an * d + b
            if abs(d) < 1e-30:
                d = 1e-30
            c = b + an / c
            if abs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < 1e-10:
                break
        q = math.exp(-x + a * math.log(x) - _ln_gamma(a)) * h
        return 1.0 - q


def _chi2_cdf(x: float, k: int) -> float:
    """CDF of chi-square distribution with k degrees of freedom."""
    if x <= 0 or k <= 0:
        return 0.0
    return _gamma_lower_reg(k / 2.0, x / 2.0)


def _f_cdf(x: float, d1: int, d2: int) -> float:
    """CDF of F-distribution using regularized incomplete beta function."""
    if x <= 0:
        return 0.0
    z = d1 * x / (d1 * x + d2)
    return _reg_beta(d1 / 2.0, d2 / 2.0, z)


def _reg_beta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b) via continued fraction (Lentz)."""
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
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d

    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c

        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta

        if abs(delta - 1.0) < 1e-10:
            break

    return prefix * h


# ---------------------------------------------------------------------------
# Chi-Square Test of Independence
# ---------------------------------------------------------------------------

def chi_square_test(observed: list[list[int]]) -> dict:
    """Chi-square test for independence on a contingency table."""
    rows = len(observed)
    cols = len(observed[0]) if rows > 0 else 0

    if rows < 2 or cols < 2:
        return {"chi2": 0.0, "df": 0, "p_value": 1.0, "cramers_v": 0.0}

    row_sums = [sum(row) for row in observed]
    col_sums = [sum(observed[r][c] for r in range(rows)) for c in range(cols)]
    n = sum(row_sums)

    if n == 0:
        return {"chi2": 0.0, "df": 0, "p_value": 1.0, "cramers_v": 0.0}

    chi2 = 0.0
    for r in range(rows):
        for c in range(cols):
            expected = row_sums[r] * col_sums[c] / n
            if expected > 0:
                chi2 += (observed[r][c] - expected) ** 2 / expected

    df = (rows - 1) * (cols - 1)
    p_value = 1.0 - _chi2_cdf(chi2, df) if df > 0 else 1.0
    k = min(rows, cols)
    cramers_v = math.sqrt(chi2 / (n * (k - 1))) if n > 0 and k > 1 else 0.0

    return {
        "chi2": round(chi2, 4),
        "df": df,
        "p_value": round(p_value, 4),
        "cramers_v": round(cramers_v, 4),
    }


# ---------------------------------------------------------------------------
# Odds Ratio with 95% CI (Woolf's method)
# ---------------------------------------------------------------------------

def odds_ratio(a: int, b: int, c: int, d: int) -> dict:
    """Calculate odds ratio from 2x2 contingency table."""
    a0, b0, c0, d0 = a, b, c, d
    if a == 0 or b == 0 or c == 0 or d == 0:
        a0, b0, c0, d0 = a + 0.5, b + 0.5, c + 0.5, d + 0.5

    or_val = (a0 * d0) / (b0 * c0) if (b0 * c0) > 0 else float("inf")
    ln_or = math.log(or_val) if or_val > 0 and or_val != float("inf") else 0
    se = math.sqrt(1.0 / a0 + 1.0 / b0 + 1.0 / c0 + 1.0 / d0)

    ci_lower = math.exp(ln_or - 1.96 * se)
    ci_upper = math.exp(ln_or + 1.96 * se)

    z = ln_or / se if se > 0 else 0
    p_value = 2 * (1 - _norm_cdf(abs(z)))

    return {
        "odds_ratio": round(or_val, 4),
        "ci_lower": round(ci_lower, 4),
        "ci_upper": round(ci_upper, 4),
        "p_value": round(p_value, 4),
    }


# ---------------------------------------------------------------------------
# One-Way ANOVA (F-test)
# ---------------------------------------------------------------------------

def one_way_anova(groups: list[list[float]]) -> dict:
    """One-way ANOVA to compare means across groups."""
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
    ssw = sum(
        sum((x - means[i]) ** 2 for x in groups[i])
        for i in range(k)
    )

    df_between = k - 1
    df_within = n_total - k

    if df_within <= 0 or ssw == 0:
        return {
            "f_statistic": float("inf") if ssb > 0 else 0.0,
            "p_value": 0.0 if ssb > 0 else 1.0,
            "df_between": df_between,
            "df_within": max(df_within, 0),
            "eta_squared": 1.0 if ssb > 0 else 0.0,
        }

    msb = ssb / df_between
    msw = ssw / df_within
    f_stat = msb / msw if msw > 0 else 0.0

    p_value = 1.0 - _f_cdf(f_stat, df_between, df_within)

    sst = ssb + ssw
    eta_sq = ssb / sst if sst > 0 else 0.0

    return {
        "f_statistic": round(f_stat, 4),
        "p_value": round(p_value, 4),
        "df_between": df_between,
        "df_within": df_within,
        "eta_squared": round(eta_sq, 4),
    }
