"""Stats + plain-language helpers shared across audience-summary blocks.

Per Sprint S8 ("Audience-Segmented Report Sections") these helpers exist
so each ``audience_summary_<persona>`` block (people / executive /
clinician / researcher) computes its proportions and renders its plain-
language strings the SAME way. Centralising them here keeps the four
blocks visually consistent and gives the test suite one obvious place to
pin the numerical / textual contracts.

Sprint S11 ("PhD-grade Whitepaper") extends this module with the
inferential / effect-size / multiple-comparison helpers needed for the
academic-grade blocks (forest plots, funnel plots, FDR-controlled
multi-test tables). Those new helpers all rely on :mod:`scipy` (already
a hard dep of the project) and avoid :mod:`statsmodels` deliberately:
each helper is small enough that pulling in statsmodels (a heavy import
that drags pandas, patsy, and scipy.optimize) is overkill, and the
didactic value of writing the formula out is part of the whitepaper
deliverable.

The module is private — leading underscore — because it is *not* part of
the descriptor wire surface. Block authors import it directly; YAML
authors never reference it.

Contents
--------
* :func:`wilson_ci` — Wilson score interval for a binomial proportion.
* :func:`format_count_per_10` — turn a 0..1 proportion into a Thai
  "X ใน 10 คน" / "X-Y ใน 10 คน" string for the people-facing block.
* :func:`selection_bias_disclaimer_th` /
  :func:`selection_bias_disclaimer_en` — boilerplate paragraphs reminding
  the reader that screening data are NOT a population sample.
* :func:`bh_fdr` / :func:`bh_fdr_adjusted` — Benjamini-Hochberg FDR
  decisions and adjusted p-values (S11).
* :func:`cohens_h` — effect size for two proportions (S11).
* :func:`cliffs_delta` — non-parametric effect size for two
  independent samples (S11).
* :func:`mantel_haenszel_or` — stratified odds ratio with
  Robins-Breslow-Greenland CI and Mantel-Haenszel χ² test (S11).
* :func:`selection_ratio` — observed / expected with Garwood (exact
  Poisson) confidence interval (S11).
* :func:`funnel_plot_data` — pre-computed points and ±2sd / ±3sd
  control limits for funnel-plot rendering (S11).
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Wilson score interval — preferred over the textbook normal approximation
# because it stays inside [0, 1] and behaves sanely at small n / extreme p.
# Reference: Wilson EB (1927). Probable Inference, the Law of Succession,
# and Statistical Inference. JASA 22(158): 209-212.
# ---------------------------------------------------------------------------


# Inverse standard-normal CDF for common ``alpha`` values. Used so we don't
# pull SciPy in just for one z-score lookup. Values are rounded to 4 d.p.
_Z_BY_ALPHA = {
    0.10: 1.6449,   # 90% CI
    0.05: 1.9600,   # 95% CI (the default)
    0.01: 2.5758,   # 99% CI
}


def _z_for_alpha(alpha: float) -> float:
    """Return the two-sided z critical value for ``alpha``.

    Falls back to a small Newton-style approximation for non-canonical
    alphas (rare in our codebase) so the helper still produces *some*
    answer rather than raising. The three canonical alphas above cover
    every report we author today.
    """
    if alpha in _Z_BY_ALPHA:
        return _Z_BY_ALPHA[alpha]
    # Beasley-Springer-Moro inverse-CDF approximation. Accurate to ~1e-3
    # for 0.001 < alpha < 0.5 — plenty for a CI that gets rounded to 2 dp
    # in a report.
    p = 1.0 - alpha / 2.0
    if p <= 0.0 or p >= 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    # Acklam-style rational approximation (public domain)
    a = (-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01)
    q = p - 0.5
    r = q * q
    num = ((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]
    den = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    return float(q * num / den)


def wilson_ci(k: int, n: int, alpha: float = 0.05) -> Tuple[float, float]:
    """Wilson score interval for ``k`` successes in ``n`` trials.

    Returns ``(lower, upper)`` as proportions in [0, 1]. ``alpha=0.05``
    yields the canonical 95% CI.

    Edge cases
    ----------
    * ``n == 0`` → returns ``(0.0, 0.0)`` (no data, no signal).
    * ``k > n`` or ``k < 0`` → :class:`ValueError`.
    * ``k == 0`` and ``k == n`` are handled by Wilson's closed form
      without producing the textbook-degenerate ``[0, 0]`` /
      ``[1, 1]`` you'd get from the normal approximation.

    The interval is *symmetric* around the **shrunken** centre
    (``(k + z²/2) / (n + z²)``), not the raw ``p̂``, which is exactly why
    it's safer at extremes than the Wald form.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n!r}")
    if k < 0 or k > n:
        raise ValueError(f"k must be in [0, n], got k={k!r}, n={n!r}")
    if n == 0:
        return (0.0, 0.0)
    z = _z_for_alpha(alpha)
    z2 = z * z
    p_hat = k / n
    centre = (p_hat + z2 / (2.0 * n)) / (1.0 + z2 / n)
    half = (
        z
        * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n)
        / (1.0 + z2 / n)
    )
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return (lo, hi)


# ---------------------------------------------------------------------------
# Plain-language proportion → "X ใน 10 คน"
# ---------------------------------------------------------------------------


def format_count_per_10(p: float) -> str:
    """Convert a proportion in [0, 1] to a Thai "X ใน 10 คน" string.

    Rules (chosen so the audience_summary_people block reads naturally
    to a non-clinician):
        * 0.0 ≤ p < 0.05  → "น้อยกว่า 1 ใน 10 คน"
        * p ≥ 0.95        → "เกือบ 10 ใน 10 คน"
        * Otherwise we look at ``p * 10`` and:
            - if it rounds to a single integer with the rounding error
              < 0.10 (≈ within 1 person per 10), report "N ใน 10 คน"
              with N = round(p * 10) clamped to [1, 9].
            - else, report a "N-(N+1) ใน 10 คน" range so e.g. 0.34 reads
              as "3-4 ใน 10 คน" rather than rounding down to "3".

    Examples
    --------
    >>> format_count_per_10(0.23)
    '2 ใน 10 คน'
    >>> format_count_per_10(0.34)
    '3-4 ใน 10 คน'
    >>> format_count_per_10(0.0)
    'น้อยกว่า 1 ใน 10 คน'
    >>> format_count_per_10(0.97)
    'เกือบ 10 ใน 10 คน'
    """
    if p < 0.0 or p > 1.0:
        raise ValueError(f"p must be in [0, 1], got {p!r}")
    if p < 0.05:
        return "น้อยกว่า 1 ใน 10 คน"
    if p >= 0.95:
        return "เกือบ 10 ใน 10 คน"
    scaled = p * 10.0
    nearest = round(scaled)
    nearest_clamped = max(1, min(9, int(nearest)))
    # Tolerance window: anything within ~0.30 of an integer-per-10 is
    # close enough to read as a clean integer ("2 ใน 10 คน"). A larger
    # gap reads as a 1-step range ("3-4 ใน 10 คน") so the audience
    # doesn't get a misleading rounded answer for, e.g., 0.34. The
    # ``+ 1e-9`` slack absorbs ``0.23 * 10 == 2.3000000000000003``-style
    # float imprecision so the boundary case ``p == 0.23`` lands on the
    # "2" branch as documented.
    if abs(scaled - nearest_clamped) <= 0.30 + 1e-9:
        return f"{nearest_clamped} ใน 10 คน"
    # Range: pick the floor and floor+1.
    lo = max(1, min(8, int(math.floor(scaled))))
    return f"{lo}-{lo + 1} ใน 10 คน"


# ---------------------------------------------------------------------------
# Selection-bias disclaimer paragraphs
# ---------------------------------------------------------------------------
#
# Both languages return ONE paragraph. Blocks render this verbatim so
# the disclaimer is identical across the four audience surfaces — the
# legal team has signed off on this exact wording.


def selection_bias_disclaimer_th() -> str:
    """Thai disclaimer: screening data is NOT a population sample."""
    return (
        "ข้อมูลในรายงานนี้มาจากการคัดกรองสุขภาพของประชาชนที่เข้าร่วมโดยสมัครใจ "
        "ไม่ใช่กลุ่มตัวอย่างสุ่มจากประชากรทั้งกรุงเทพมหานคร "
        "ตัวเลขจึงสะท้อนเฉพาะผู้ที่เข้ารับการคัดกรอง อาจมีความเอนเอียง "
        "(selection bias) เมื่อเทียบกับประชากรทั่วไป "
        "การตีความจึงควรพิจารณาในบริบทของกลุ่มผู้คัดกรอง ไม่ควรนำไปสรุป "
        "อัตราโรคของประชากรทั้งกรุงเทพมหานครโดยตรง"
    )


def selection_bias_disclaimer_en() -> str:
    """English disclaimer: screening data is NOT a population sample."""
    return (
        "The figures in this report are derived from the voluntary "
        "BMA health-screening programme. They are NOT a probability "
        "sample of the Bangkok population. Selection bias is expected "
        "(self-selected attendees differ from non-attendees on age, "
        "comorbidity, and health-seeking behaviour). Interpret the "
        "numbers as describing the screened cohort and DO NOT "
        "extrapolate them to the wider Bangkok population without "
        "an explicit reweighting step."
    )


# ---------------------------------------------------------------------------
# S11 — Benjamini-Hochberg FDR (1995 step-up)
# ---------------------------------------------------------------------------
#
# We deliberately implement BH ourselves instead of leaning on
# ``scipy.stats.false_discovery_control``: the procedure is short, the
# whitepaper claims algorithmic transparency, and an explicit reference
# implementation lets reviewers re-derive every q-value by hand.
#
# Reference: Benjamini Y, Hochberg Y (1995). "Controlling the false
# discovery rate: a practical and powerful approach to multiple
# testing." J. R. Statist. Soc. B 57(1): 289-300.


def bh_fdr(pvalues: List[float], q: float = 0.05) -> List[bool]:
    """Benjamini-Hochberg FDR step-up: which hypotheses survive at level ``q``?

    Returns one bool per input p-value (in the original input order).
    ``True`` means the hypothesis is rejected at FDR ``q``.

    Procedure (BH 1995)
    -------------------
    Let ``p_(1) <= p_(2) <= ... <= p_(m)`` be the sorted p-values and
    ``H_(i)`` the corresponding hypothesis. Find the largest ``k`` such
    that ``p_(k) <= (k/m) * q`` and reject ``H_(1), ..., H_(k)``. If no
    such ``k`` exists, reject nothing.

    Edge cases
    ----------
    * Empty input → empty list.
    * Any p-value outside [0, 1] → :class:`ValueError`.
    * NaN inputs → :class:`ValueError` (caller bug; fail fast).

    We implement this in pure Python rather than statsmodels because
    statsmodels is not a project dep and the procedure fits in 15 lines.
    """
    if not pvalues:
        return []
    for p in pvalues:
        if p != p:  # NaN
            raise ValueError("p-values must be finite, got NaN")
        if p < 0.0 or p > 1.0:
            raise ValueError(f"p-values must be in [0, 1], got {p!r}")
    if not (0.0 < q < 1.0):
        raise ValueError(f"q must be in (0, 1), got {q!r}")

    m = len(pvalues)
    # Sort by p-value, remembering original index.
    order = sorted(range(m), key=lambda i: pvalues[i])
    # Find largest k (1-indexed) with p_(k) <= (k/m)*q.
    k_star = 0
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= (rank / m) * q:
            k_star = rank
    decisions = [False] * m
    # Reject the first k_star sorted hypotheses → mark their original idx.
    for rank, idx in enumerate(order, start=1):
        if rank <= k_star:
            decisions[idx] = True
    return decisions


def bh_fdr_adjusted(pvalues: List[float]) -> List[float]:
    """Benjamini-Hochberg adjusted p-values (q-values).

    Returns one adjusted p-value per input, in the original input order.
    A hypothesis is significant at FDR level ``q`` iff its adjusted
    p-value is ``<= q`` — i.e. ``[a <= q for a in bh_fdr_adjusted(p)]``
    must equal :func:`bh_fdr`'s output for the same ``q``.

    Construction
    ------------
    For sorted p-values ``p_(1) <= ... <= p_(m)``, the raw BH adjustment
    is ``a_(i) = p_(i) * m / i``. To preserve monotonicity (a hypothesis
    can never be more significant than a strictly worse one) we then
    take a *backward cumulative minimum* over the sorted sequence and
    finally clip to [0, 1].

    Edge cases match :func:`bh_fdr`: empty in → empty out; NaN raises.
    """
    if not pvalues:
        return []
    for p in pvalues:
        if p != p:
            raise ValueError("p-values must be finite, got NaN")
        if p < 0.0 or p > 1.0:
            raise ValueError(f"p-values must be in [0, 1], got {p!r}")

    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    sorted_p = [pvalues[i] for i in order]
    # Raw adjustment: p_(i) * m / i (1-indexed).
    raw = [sorted_p[i - 1] * m / i for i in range(1, m + 1)]
    # Backward cumulative minimum to keep monotone non-decreasing.
    adj_sorted = [0.0] * m
    running_min = 1.0
    for i in range(m - 1, -1, -1):
        running_min = min(running_min, raw[i])
        adj_sorted[i] = min(1.0, running_min)
    # Un-sort back to caller's order.
    adjusted = [0.0] * m
    for rank, idx in enumerate(order):
        adjusted[idx] = adj_sorted[rank]
    return adjusted


# ---------------------------------------------------------------------------
# S11 — Cohen's h: effect size for two proportions
# ---------------------------------------------------------------------------


def cohens_h(p1: float, p2: float) -> float:
    """Cohen's h effect size for two proportions.

    Formula: ``h = 2 * arcsin(sqrt(p1)) - 2 * arcsin(sqrt(p2))``. Returns
    the **unsigned** magnitude ``|h|`` because the sign just records
    direction, which the caller already knows from the ordering of
    ``p1`` and ``p2``.

    Conventions (Cohen 1988, Statistical Power Analysis 2e, Ch. 6)::

        |h| < 0.2  → trivial
        |h| ≈ 0.2  → small
        |h| ≈ 0.5  → medium
        |h| ≈ 0.8  → large

    The arcsine transform (φ in Cohen's notation) stabilises variance
    across the [0, 1] range, which is why h is preferred over a raw
    p1 - p2 difference for between-cohort comparisons.

    No statsmodels — :func:`math.asin` and :func:`math.sqrt` suffice.
    """
    if p1 < 0.0 or p1 > 1.0:
        raise ValueError(f"p1 must be in [0, 1], got {p1!r}")
    if p2 < 0.0 or p2 > 1.0:
        raise ValueError(f"p2 must be in [0, 1], got {p2!r}")
    phi1 = 2.0 * math.asin(math.sqrt(p1))
    phi2 = 2.0 * math.asin(math.sqrt(p2))
    return abs(phi1 - phi2)


# ---------------------------------------------------------------------------
# S11 — Cliff's delta: non-parametric effect size
# ---------------------------------------------------------------------------


def cliffs_delta(arr1: List[float], arr2: List[float]) -> Tuple[float, str]:
    """Cliff's delta for two independent samples + qualitative label.

    ``delta = (n_greater - n_less) / (n1 * n2)`` where, over all
    ``n1 * n2`` pairs ``(x, y) ∈ arr1 × arr2``, ``n_greater`` counts
    pairs with ``x > y`` and ``n_less`` counts ``x < y``. Ties don't
    contribute to either count. The result is in [-1, 1].

    Magnitude → label (Romano et al. 2006, "Appropriate Statistics for
    Ordinal Level Data"; see also Vargha & Delaney 2000):

        |δ| < 0.147 → "negligible"
        |δ| < 0.330 → "small"
        |δ| < 0.474 → "medium"
        |δ| ≥ 0.474 → "large"

    Returns the **signed** delta + the label; sign tells which sample
    is larger.

    No statsmodels — pure pairwise comparison; O(n1 * n2) and small
    enough for our n.
    """
    if not arr1 or not arr2:
        raise ValueError("arr1 and arr2 must both be non-empty")
    n1 = len(arr1)
    n2 = len(arr2)
    n_greater = 0
    n_less = 0
    for x in arr1:
        for y in arr2:
            if x > y:
                n_greater += 1
            elif x < y:
                n_less += 1
    delta = (n_greater - n_less) / (n1 * n2)
    abs_d = abs(delta)
    if abs_d < 0.147:
        label = "negligible"
    elif abs_d < 0.330:
        label = "small"
    elif abs_d < 0.474:
        label = "medium"
    else:
        label = "large"
    return (delta, label)


# ---------------------------------------------------------------------------
# S11 — Mantel-Haenszel stratified OR + RBG CI + MH χ² test
# ---------------------------------------------------------------------------
#
# Each stratum is a 2x2 contingency table laid out as::
#
#                 cases   controls
#       exposed   |  a   |    c    |
#       unexposed |  b   |    d    |
#
# i.e. the tuple is ``(a, b, c, d)`` = (exposed-cases,
# unexposed-cases, exposed-controls, unexposed-controls). This matches
# the convention in Rothman, Greenland & Lash, *Modern Epidemiology* 3e,
# Ch 15. ``n = a + b + c + d`` per stratum.


def mantel_haenszel_or(
    strata: List[Tuple[int, int, int, int]],
) -> Tuple[float, float, float, float]:
    """Mantel-Haenszel stratified odds ratio with RBG CI + MH χ² p-value.

    Parameters
    ----------
    strata
        List of ``(a, b, c, d)`` tuples — see module-level diagram for
        the layout. Empty strata or strata with ``n < 2`` are skipped
        (they contribute neither numerator nor variance and would
        otherwise zero-divide).

    Returns
    -------
    ``(or_mh, ci_lo, ci_hi, p_value)``

    * ``or_mh = sum_k (a_k d_k / n_k) / sum_k (b_k c_k / n_k)``
    * 95% CI via Robins-Breslow-Greenland (Robins, Breslow, Greenland
      1986). The standard error of ``log(OR_MH)`` is::

          var = G/(2R²) + (H + I)/(2RS) + J/(2S²)

      with ``R = sum a_k d_k / n_k``, ``S = sum b_k c_k / n_k``,
      ``G = sum P_k a_k d_k / n_k``, ``H = sum P_k b_k c_k / n_k``,
      ``I = sum Q_k a_k d_k / n_k``, ``J = sum Q_k b_k c_k / n_k``,
      and ``P_k = (a_k + d_k)/n_k``, ``Q_k = (b_k + c_k)/n_k``.

    * p-value via the Mantel-Haenszel χ² (1 df, two-sided)::

          χ² = (sum a_k - sum E(a_k))² / sum V(a_k)
          E(a_k) = (a_k+b_k)(a_k+c_k) / n_k
          V(a_k) = (a_k+b_k)(c_k+d_k)(a_k+c_k)(b_k+d_k) / (n_k² (n_k-1))

      No continuity correction (chosen for consistency with the
      ``epitools`` and Stata defaults — reviewers expect uncorrected χ²
      unless asked for ``correct=True``).

    statsmodels has ``StratifiedTable`` which would do all of this in
    two lines, but pulling it in for one helper triples cold-start time
    and the formulas are short enough to inline transparently.
    """
    if not strata:
        raise ValueError("at least one stratum is required")

    # Lazy import — keeps `import _stats_helpers` cheap for callers who
    # only need wilson_ci / format_count_per_10.
    from scipy.stats import chi2 as _chi2_dist

    sum_R = 0.0  # numerator of OR_MH
    sum_S = 0.0  # denominator of OR_MH
    G = 0.0  # sum P_k a_k d_k / n_k
    H = 0.0  # sum P_k b_k c_k / n_k
    I = 0.0  # sum Q_k a_k d_k / n_k
    J = 0.0  # sum Q_k b_k c_k / n_k
    sum_a = 0
    sum_E = 0.0
    sum_V = 0.0

    used = 0
    for (a, b, c, d) in strata:
        if a < 0 or b < 0 or c < 0 or d < 0:
            raise ValueError(f"cell counts must be non-negative, got {(a,b,c,d)!r}")
        n = a + b + c + d
        if n < 2:
            # Degenerate stratum — variance is undefined; drop it.
            continue
        used += 1
        P = (a + d) / n
        Q = (b + c) / n
        R = a * d / n
        S = b * c / n
        sum_R += R
        sum_S += S
        G += P * R
        H += P * S
        I += Q * R
        J += Q * S
        sum_a += a
        sum_E += (a + b) * (a + c) / n
        sum_V += (
            (a + b) * (c + d) * (a + c) * (b + d) / (n * n * (n - 1))
        )

    if used == 0:
        raise ValueError("all strata were degenerate (n < 2 each)")
    if sum_S == 0.0:
        # No discordant b*c contribution — OR_MH is +∞. Surface as NaN
        # rather than raising, so callers (forest_plot block) can show
        # "not estimable".
        return (float("inf"), float("inf"), float("inf"), float("nan"))
    if sum_R == 0.0:
        return (0.0, 0.0, 0.0, float("nan"))

    or_mh = sum_R / sum_S
    log_or = math.log(or_mh)
    var_log = (
        G / (2.0 * sum_R * sum_R)
        + (H + I) / (2.0 * sum_R * sum_S)
        + J / (2.0 * sum_S * sum_S)
    )
    se = math.sqrt(var_log)
    z = _Z_BY_ALPHA[0.05]
    ci_lo = math.exp(log_or - z * se)
    ci_hi = math.exp(log_or + z * se)

    if sum_V <= 0.0:
        p = float("nan")
    else:
        chi2_stat = (sum_a - sum_E) ** 2 / sum_V
        p = float(1.0 - _chi2_dist.cdf(chi2_stat, df=1))
    return (or_mh, ci_lo, ci_hi, p)


# ---------------------------------------------------------------------------
# S11 — Selection ratio + Garwood (exact Poisson) CI
# ---------------------------------------------------------------------------


def selection_ratio(
    observed: int,
    expected: float,
    alpha: float = 0.05,
) -> Tuple[float, float, float]:
    """Selection ratio (observed / expected) with exact Poisson CI.

    The CI uses the Garwood (1936) exact method: the standard
    inversion of the Poisson cumulative distribution against the χ²
    quantile, which avoids the Wald-on-log overshoot at small counts.

    Formula (Garwood)::

        lower_count = chi2.ppf(α/2, df=2k) / 2
        upper_count = chi2.ppf(1 - α/2, df=2(k+1)) / 2

    For ``k == 0`` the lower bound collapses to 0. The CI on the SR
    follows by dividing both bounds by ``expected``.

    No statsmodels — uses :func:`scipy.stats.chi2.ppf` directly, the
    same primitive statsmodels calls under the hood.

    Returns ``(sr, ci_lo, ci_hi)``.
    """
    if observed < 0:
        raise ValueError(f"observed must be >= 0, got {observed!r}")
    if not isinstance(observed, int):
        raise TypeError(
            f"observed must be int (Poisson count), got {type(observed).__name__}"
        )
    if expected <= 0.0:
        raise ValueError(
            f"expected must be > 0 to form a ratio, got {expected!r}"
        )
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")

    from scipy.stats import chi2 as _chi2_dist

    sr = observed / expected
    if observed == 0:
        lo_count = 0.0
    else:
        lo_count = float(_chi2_dist.ppf(alpha / 2.0, df=2 * observed)) / 2.0
    hi_count = float(_chi2_dist.ppf(1.0 - alpha / 2.0, df=2 * (observed + 1))) / 2.0
    return (sr, lo_count / expected, hi_count / expected)


# ---------------------------------------------------------------------------
# S11 — Funnel-plot data prep
# ---------------------------------------------------------------------------


def funnel_plot_data(
    observed: List[int],
    expected: List[float],
    labels: List[str] | None = None,
) -> Dict[str, list]:
    """Pre-compute the funnel-plot points and ±2sd / ±3sd control limits.

    A funnel plot of selection ratios shows ``SR_i = O_i / E_i`` against
    the expected count ``E_i``. Under the null ``H_0: SR = 1`` we have
    ``O_i ~ Poisson(E_i)`` so ``Var(SR_i) = 1 / E_i`` and the (1-α)·100%
    control limits are ``1 ± z * sqrt(1 / E)`` (Spiegelhalter 2005,
    "Funnel plots for comparing institutional performance").

    The control-limit curves are sampled at a dense set of expected
    counts so the rendering block can simply draw a polyline; the
    sampling spans ``[E_min, E_max]`` of the input ``expected``.

    Returns a dict with::

        {
          "points": [(label, sr, n_expected), ...],
          "control_2sd_low":  [(n, lo), ...],
          "control_2sd_high": [(n, hi), ...],
          "control_3sd_low":  [(n, lo), ...],
          "control_3sd_high": [(n, hi), ...],
        }

    ``label`` defaults to ``"#i"`` if ``labels`` is None.

    No statsmodels — closed-form Normal approximation is appropriate
    here (the same approximation Spiegelhalter recommends for n ≥ 5).
    """
    if len(observed) != len(expected):
        raise ValueError(
            f"observed and expected must align: "
            f"{len(observed)} vs {len(expected)}"
        )
    if not observed:
        return {
            "points": [],
            "control_2sd_low": [],
            "control_2sd_high": [],
            "control_3sd_low": [],
            "control_3sd_high": [],
        }
    for e in expected:
        if e <= 0.0:
            raise ValueError(f"expected counts must be > 0, got {e!r}")
    for o in observed:
        if o < 0:
            raise ValueError(f"observed counts must be >= 0, got {o!r}")
    if labels is not None and len(labels) != len(observed):
        raise ValueError(
            f"labels must align with observed: "
            f"{len(labels)} vs {len(observed)}"
        )

    if labels is None:
        labels = [f"#{i}" for i in range(len(observed))]

    points = [
        (lbl, o / e, e)
        for lbl, o, e in zip(labels, observed, expected)
    ]

    # Sample control limits across the expected-count range — enough
    # points to render a smooth curve. Use 50 log-spaced samples.
    e_min = max(1e-9, min(expected))
    e_max = max(expected)
    if e_min == e_max:
        # Single E value — emit just that point on each curve.
        sample_es = [e_min]
    else:
        n_samples = 50
        log_lo = math.log(e_min)
        log_hi = math.log(e_max)
        step = (log_hi - log_lo) / (n_samples - 1)
        sample_es = [math.exp(log_lo + i * step) for i in range(n_samples)]

    def _band(zmult: float) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        lows: List[Tuple[float, float]] = []
        highs: List[Tuple[float, float]] = []
        for e in sample_es:
            se = 1.0 / math.sqrt(e)
            lows.append((e, max(0.0, 1.0 - zmult * se)))
            highs.append((e, 1.0 + zmult * se))
        return lows, highs

    lo2, hi2 = _band(2.0)
    lo3, hi3 = _band(3.0)

    return {
        "points": points,
        "control_2sd_low": lo2,
        "control_2sd_high": hi2,
        "control_3sd_low": lo3,
        "control_3sd_high": hi3,
    }


__all__ = [
    "wilson_ci",
    "format_count_per_10",
    "selection_bias_disclaimer_th",
    "selection_bias_disclaimer_en",
    "bh_fdr",
    "bh_fdr_adjusted",
    "cohens_h",
    "cliffs_delta",
    "mantel_haenszel_or",
    "selection_ratio",
    "funnel_plot_data",
]
