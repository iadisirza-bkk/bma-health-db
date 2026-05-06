"""Tests for ``services.reports.blocks._stats_helpers`` (S8 + S11).

These pin the contract used by every ``audience_summary_*`` block so
the four blocks render the same number / phrase from the same input.

S11 (PhD-grade Whitepaper) extends the helper module with FDR
correction, effect sizes, stratified ORs, and selection-ratio /
funnel-plot primitives. Each new helper has at least three tests:
one against a textbook / scipy reference value, one edge case, and
one realistic-ish numerical scenario.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

# Make ``api/`` importable for ``services.reports.*`` — same idiom as the
# rest of ``tests/services/reports/``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks._stats_helpers import (  # noqa: E402
    bh_fdr,
    bh_fdr_adjusted,
    cliffs_delta,
    cohens_h,
    format_count_per_10,
    funnel_plot_data,
    mantel_haenszel_or,
    selection_bias_disclaimer_en,
    selection_bias_disclaimer_th,
    selection_ratio,
    wilson_ci,
)


# ---------------------------------------------------------------------------
# wilson_ci
# ---------------------------------------------------------------------------


def test_wilson_ci_n_zero_returns_zero_zero() -> None:
    """No data → no signal — interval collapses to (0, 0)."""
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_ci_50_50_centred() -> None:
    """k=50/n=100 → CI centred near 0.50, lo < 0.5 < hi, width ~0.10."""
    lo, hi = wilson_ci(50, 100, alpha=0.05)
    assert 0.0 <= lo < 0.5 < hi <= 1.0
    # Wilson 95% CI for 50/100 is approx (0.404, 0.596) — width ~0.19.
    assert abs((lo + hi) / 2 - 0.5) < 0.01
    assert 0.15 < (hi - lo) < 0.25


def test_wilson_ci_extreme_zero_successes() -> None:
    """k=0 — Wilson keeps the interval inside [0, 1] and ABOVE 0
    (unlike the textbook normal approximation which would degenerate)."""
    lo, hi = wilson_ci(0, 100, alpha=0.05)
    assert lo == 0.0
    # For 0/100, Wilson upper bound is approx 0.0370.
    assert 0.0 < hi < 0.10


def test_wilson_ci_invalid_inputs_raise() -> None:
    """Out-of-range k / n / alpha raise loudly so the caller notices."""
    with pytest.raises(ValueError):
        wilson_ci(-1, 10)
    with pytest.raises(ValueError):
        wilson_ci(11, 10)
    with pytest.raises(ValueError):
        wilson_ci(5, -1)


def test_wilson_ci_alphas_change_width() -> None:
    """Tighter alpha → wider CI. The 99% CI must be wider than the 95%."""
    lo95, hi95 = wilson_ci(50, 200, alpha=0.05)
    lo99, hi99 = wilson_ci(50, 200, alpha=0.01)
    assert (hi99 - lo99) > (hi95 - lo95)


# ---------------------------------------------------------------------------
# format_count_per_10
# ---------------------------------------------------------------------------


def test_format_count_per_10_clean_integer() -> None:
    """0.20 sits within ε of an integer-per-10 → no range shown."""
    assert format_count_per_10(0.20) == "2 ใน 10 คน"


def test_format_count_per_10_close_integer() -> None:
    """0.23 is close enough to 2 to round (within the 0.15 tolerance)."""
    assert format_count_per_10(0.23) == "2 ใน 10 คน"


def test_format_count_per_10_midway_uses_range() -> None:
    """0.34 is too far from an integer-per-10 → render as a range."""
    assert format_count_per_10(0.34) == "3-4 ใน 10 คน"


def test_format_count_per_10_below_threshold() -> None:
    """Anything below 0.05 collapses to "less than 1 in 10"."""
    assert format_count_per_10(0.0) == "น้อยกว่า 1 ใน 10 คน"
    assert format_count_per_10(0.04) == "น้อยกว่า 1 ใน 10 คน"


def test_format_count_per_10_above_threshold() -> None:
    """≥0.95 collapses to "almost 10 in 10"."""
    assert format_count_per_10(0.97) == "เกือบ 10 ใน 10 คน"
    assert format_count_per_10(1.0) == "เกือบ 10 ใน 10 คน"


def test_format_count_per_10_invalid_raises() -> None:
    """Out-of-range probability raises so a caller bug fails loud."""
    with pytest.raises(ValueError):
        format_count_per_10(-0.1)
    with pytest.raises(ValueError):
        format_count_per_10(1.1)


# ---------------------------------------------------------------------------
# Disclaimer paragraphs — pin a few load-bearing phrases so a careless
# rewrite (e.g. dropping the "selection bias" warning) trips the suite.
# ---------------------------------------------------------------------------


def test_selection_bias_disclaimer_th_has_required_phrases() -> None:
    text = selection_bias_disclaimer_th()
    assert "selection bias" in text  # English term included verbatim
    assert "สมัครใจ" in text  # voluntary screening
    assert "ประชากร" in text  # population-level reference
    assert len(text) > 100  # not an empty / placeholder string


def test_selection_bias_disclaimer_en_has_required_phrases() -> None:
    text = selection_bias_disclaimer_en()
    assert "selection bias" in text.lower()
    assert "voluntary" in text.lower()
    assert "Bangkok" in text
    assert "DO NOT" in text  # explicit caveat against extrapolation
    assert len(text) > 100


# ---------------------------------------------------------------------------
# S11 — bh_fdr / bh_fdr_adjusted
# ---------------------------------------------------------------------------


def test_bh_fdr_textbook_benjamini_hochberg_1995() -> None:
    """Textbook example: Benjamini & Hochberg 1995, Table 1
    (15 p-values from the Needleman et al. lead/IQ study).

    At q=0.05 the original paper rejects the first 4 hypotheses
    (the four smallest p-values). Reference: B&H 1995, p. 295."""
    pvals = [
        0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344,
        0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.000,
    ]
    decisions = bh_fdr(pvals, q=0.05)
    assert decisions[:4] == [True, True, True, True]
    assert all(d is False for d in decisions[4:])
    assert sum(decisions) == 4


def test_bh_fdr_empty_input_returns_empty() -> None:
    """Edge case: empty p-value list → empty decision list."""
    assert bh_fdr([], q=0.05) == []
    assert bh_fdr_adjusted([]) == []


def test_bh_fdr_realistic_three_pvalues_at_q05() -> None:
    """Realistic small case: [0.01, 0.04, 0.30] at q=0.05.
    Step-up: rank 3, p_(3)=0.30 > 1*0.05; rank 2, p_(2)=0.04 ≤ (2/3)*0.05?
    (2/3)*0.05 ≈ 0.0333, so 0.04 > 0.0333 → fail; rank 1, p=0.01 ≤ (1/3)*0.05
    ≈ 0.01667, pass. So only the smallest is rejected."""
    decisions = bh_fdr([0.01, 0.04, 0.30], q=0.05)
    assert decisions == [True, False, False]


def test_bh_fdr_preserves_input_order_when_unsorted() -> None:
    """The output order MUST match the original input order, not the
    sorted order — otherwise downstream blocks would mislabel results."""
    # Unsorted input; smallest is in the middle.
    pvals = [0.50, 0.001, 0.30]
    decisions = bh_fdr(pvals, q=0.05)
    # Only the 0.001 should pass; it's at index 1.
    assert decisions == [False, True, False]


def test_bh_fdr_adjusted_matches_scipy_reference() -> None:
    """Sanity-check our implementation against scipy 1.13's
    ``false_discovery_control`` on the B&H 1995 example."""
    from scipy.stats import false_discovery_control
    pvals = [
        0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344,
        0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.000,
    ]
    ours = bh_fdr_adjusted(pvals)
    ref = list(false_discovery_control(pvals, method="bh"))
    for a, b in zip(ours, ref):
        assert math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)


def test_bh_fdr_adjusted_monotone_after_resort() -> None:
    """Adjusted p-values, when re-sorted by raw p-value, must be
    monotone non-decreasing. This is the load-bearing property that
    distinguishes BH from a naive Bonferroni-by-rank step."""
    pvals = [0.001, 0.05, 0.02, 0.20, 0.5]
    adj = bh_fdr_adjusted(pvals)
    # Pair (raw, adjusted) and sort by raw.
    paired = sorted(zip(pvals, adj))
    sorted_adj = [a for _, a in paired]
    for i in range(1, len(sorted_adj)):
        assert sorted_adj[i] >= sorted_adj[i - 1] - 1e-12


def test_bh_fdr_invalid_pvalue_raises() -> None:
    """Out-of-range p-values are caller bugs and should fail loud."""
    with pytest.raises(ValueError):
        bh_fdr([0.1, 1.5], q=0.05)
    with pytest.raises(ValueError):
        bh_fdr([0.1, -0.01], q=0.05)
    with pytest.raises(ValueError):
        bh_fdr([0.1, 0.2], q=1.5)


# ---------------------------------------------------------------------------
# S11 — cohens_h
# ---------------------------------------------------------------------------


def test_cohens_h_textbook_small_effect() -> None:
    """Textbook value: Cohen 1988, Statistical Power Analysis 2e Ch 6.
    h(0.5, 0.4) ≈ 0.2014 — the canonical "small" effect-size benchmark."""
    h = cohens_h(0.5, 0.4)
    assert math.isclose(h, 0.2014, abs_tol=1e-3)


def test_cohens_h_identical_proportions_is_zero() -> None:
    """Edge case: identical proportions → h=0 (no effect)."""
    assert cohens_h(0.3, 0.3) == 0.0
    assert cohens_h(0.0, 0.0) == 0.0
    assert cohens_h(1.0, 1.0) == 0.0


def test_cohens_h_returns_unsigned_magnitude() -> None:
    """The convention is to report |h|: h(p1,p2) and h(p2,p1) match."""
    h_ab = cohens_h(0.7, 0.3)
    h_ba = cohens_h(0.3, 0.7)
    assert h_ab >= 0
    assert math.isclose(h_ab, h_ba, abs_tol=1e-12)


def test_cohens_h_large_effect_canonical() -> None:
    """h(0.8, 0.2) is the canonical "large" anchor; per Cohen 1988
    Table 6.2.1, it sits well above 0.8."""
    h = cohens_h(0.8, 0.2)
    # arcsine(sqrt(0.8))*2 - arcsine(sqrt(0.2))*2 ≈ 1.287
    assert h > 0.8
    assert math.isclose(h, 1.287, abs_tol=0.01)


def test_cohens_h_invalid_proportion_raises() -> None:
    with pytest.raises(ValueError):
        cohens_h(-0.1, 0.5)
    with pytest.raises(ValueError):
        cohens_h(0.5, 1.1)


# ---------------------------------------------------------------------------
# S11 — cliffs_delta
# ---------------------------------------------------------------------------


def test_cliffs_delta_perfect_separation_is_one() -> None:
    """Edge case: every element of arr1 > every element of arr2 →
    delta = +1, label "large"."""
    delta, label = cliffs_delta([10, 11, 12], [1, 2, 3])
    assert delta == 1.0
    assert label == "large"


def test_cliffs_delta_identical_groups_is_zero() -> None:
    """Identical groups → delta = 0, label "negligible"."""
    delta, label = cliffs_delta([1, 2, 3], [1, 2, 3])
    assert delta == 0.0
    assert label == "negligible"


def test_cliffs_delta_textbook_romano_2006_threshold() -> None:
    """Textbook (Romano et al. 2006): a delta of 0.20 is "small",
    0.40 is "medium", 0.50 is "large". Build a known case.

    For arr1=[2,3,4], arr2=[1,2,3]:
      pairs: 2>1=Y; 2>2=N; 2>3=N
             3>1=Y; 3>2=Y; 3>3=N
             4>1=Y; 4>2=Y; 4>3=Y
      n_greater = 6, n_less = 1, delta = (6-1)/9 ≈ 0.5556 → "large"
    """
    delta, label = cliffs_delta([2, 3, 4], [1, 2, 3])
    assert math.isclose(delta, 5.0 / 9.0, abs_tol=1e-12)
    assert label == "large"


def test_cliffs_delta_empty_input_raises() -> None:
    """Edge case: empty arrays raise — there is no defined delta."""
    with pytest.raises(ValueError):
        cliffs_delta([], [1, 2])
    with pytest.raises(ValueError):
        cliffs_delta([1, 2], [])


def test_cliffs_delta_negative_signed_when_arr1_smaller() -> None:
    """The sign is preserved (caller looks at sign for direction)."""
    delta, label = cliffs_delta([1, 2, 3], [10, 20, 30])
    assert delta == -1.0
    assert label == "large"


# ---------------------------------------------------------------------------
# S11 — mantel_haenszel_or
# ---------------------------------------------------------------------------


def test_mantel_haenszel_or_two_strata_realistic() -> None:
    """Realistic 2-stratum case. Hand-computed:
    Stratum 1 (n=100): a=10,b=20,c=30,d=40 → ad/n=4, bc/n=6
    Stratum 2 (n=100): a=20,b=10,c=15,d=55 → ad/n=11, bc/n=1.5
    OR_MH = (4 + 11) / (6 + 1.5) = 15 / 7.5 = 2.0
    """
    or_mh, lo, hi, p = mantel_haenszel_or([(10, 20, 30, 40), (20, 10, 15, 55)])
    assert math.isclose(or_mh, 2.0, abs_tol=1e-12)
    assert 0.0 < lo < or_mh < hi
    # Hand-checked against the RBG formula and chi^2 from scipy:
    # CI ≈ (1.103, 3.627), p ≈ 0.0172
    assert math.isclose(lo, 1.1029, abs_tol=1e-3)
    assert math.isclose(hi, 3.6268, abs_tol=1e-3)
    assert math.isclose(p, 0.01724, abs_tol=1e-3)


def test_mantel_haenszel_or_collapses_to_single_stratum_or() -> None:
    """Edge case: a single stratum should give the same OR as
    ``scipy.stats.fisher_exact`` returns for that 2x2 table."""
    from scipy.stats import fisher_exact
    # Table: cases (a,b)=(15,5); controls (c,d)=(10,20)
    # OR = (a*d)/(b*c) = (15*20)/(5*10) = 6.0
    or_mh, lo, hi, p = mantel_haenszel_or([(15, 5, 10, 20)])
    expected_or, _ = fisher_exact([[15, 5], [10, 20]])
    assert math.isclose(or_mh, float(expected_or), abs_tol=1e-9)
    assert lo < or_mh < hi


def test_mantel_haenszel_or_empty_strata_raises() -> None:
    """Edge case: empty list → ValueError (no data)."""
    with pytest.raises(ValueError):
        mantel_haenszel_or([])


def test_mantel_haenszel_or_negative_cells_raise() -> None:
    """Negative cell counts are a caller bug → fail loud."""
    with pytest.raises(ValueError):
        mantel_haenszel_or([(-1, 2, 3, 4)])


# ---------------------------------------------------------------------------
# S11 — selection_ratio (Garwood Poisson CI)
# ---------------------------------------------------------------------------


def test_selection_ratio_textbook_garwood_k10() -> None:
    """Textbook value: for k=10 the 95% Garwood Poisson CI on the count
    is (4.795, 18.39) (Ulm 1990, Tabelle 1; reproduced in Newcombe 2012
    "Confidence Intervals for Proportions and Related Measures of
    Effect Size", Table 5.1). With expected=10 the SR = 1.0 and the CI
    is (0.4795, 1.839)."""
    sr, lo, hi = selection_ratio(observed=10, expected=10.0)
    assert sr == 1.0
    assert math.isclose(lo, 0.4795, abs_tol=1e-3)
    assert math.isclose(hi, 1.8390, abs_tol=1e-3)


def test_selection_ratio_zero_observed_collapses_lower_bound() -> None:
    """Edge case: 0 observed → SR=0 with lower bound exactly 0
    (Garwood explicitly defines the lower bound at k=0 to be 0 — the
    chi^2 ppf at df=0 is undefined)."""
    sr, lo, hi = selection_ratio(observed=0, expected=5.0)
    assert sr == 0.0
    assert lo == 0.0
    assert hi > 0.0


def test_selection_ratio_realistic_overrepresentation() -> None:
    """Realistic case: O=120, E=100 — slight over-representation.
    SR=1.2; CI must straddle or sit just above 1.0 depending on n."""
    sr, lo, hi = selection_ratio(observed=120, expected=100.0)
    assert math.isclose(sr, 1.2, abs_tol=1e-12)
    # 95% CI on 120 counts is roughly (99.5, 143.6) (Garwood); divided by
    # 100 → roughly (0.995, 1.436). Loose check just to lock the shape.
    assert lo < sr < hi
    assert 0.9 < lo < 1.05
    assert 1.3 < hi < 1.6


def test_selection_ratio_invalid_inputs_raise() -> None:
    """Negative observed or non-positive expected fail loud."""
    with pytest.raises(ValueError):
        selection_ratio(-1, 5.0)
    with pytest.raises(ValueError):
        selection_ratio(5, 0.0)
    with pytest.raises(ValueError):
        selection_ratio(5, -3.0)


# ---------------------------------------------------------------------------
# S11 — funnel_plot_data
# ---------------------------------------------------------------------------


def test_funnel_plot_data_basic_three_units() -> None:
    """Basic case: three units, well-formed output dict."""
    fp = funnel_plot_data(
        observed=[10, 20, 30],
        expected=[12.0, 18.0, 25.0],
        labels=["A", "B", "C"],
    )
    assert set(fp) == {
        "points",
        "control_2sd_low",
        "control_2sd_high",
        "control_3sd_low",
        "control_3sd_high",
    }
    assert len(fp["points"]) == 3
    # Point format: (label, sr, n_expected)
    assert fp["points"][0] == ("A", 10 / 12.0, 12.0)
    # Control-limit curves are dense (50 points each) for smooth render.
    assert len(fp["control_2sd_low"]) == 50
    assert len(fp["control_3sd_high"]) == 50


def test_funnel_plot_data_2sd_inside_3sd() -> None:
    """Sanity property: at every sample point, the 3sd band must
    enclose the 2sd band — otherwise the funnel is malformed."""
    fp = funnel_plot_data(observed=[5, 15], expected=[10.0, 20.0])
    for (e2, lo2), (e3, lo3) in zip(fp["control_2sd_low"], fp["control_3sd_low"]):
        assert e2 == e3
        assert lo3 <= lo2 + 1e-12  # 3sd low is at-or-below 2sd low
    for (e2, hi2), (e3, hi3) in zip(fp["control_2sd_high"], fp["control_3sd_high"]):
        assert hi3 >= hi2 - 1e-12  # 3sd high is at-or-above 2sd high


def test_funnel_plot_data_empty_returns_empty_curves() -> None:
    """Edge case: empty input → empty output (no crash, no labels)."""
    fp = funnel_plot_data(observed=[], expected=[])
    assert fp["points"] == []
    assert fp["control_2sd_low"] == []
    assert fp["control_3sd_high"] == []


def test_funnel_plot_data_default_labels() -> None:
    """If no labels provided, default to ``"#0"``, ``"#1"``, ..."""
    fp = funnel_plot_data(observed=[3, 6], expected=[5.0, 5.0])
    labels = [pt[0] for pt in fp["points"]]
    assert labels == ["#0", "#1"]


def test_funnel_plot_data_mismatched_lengths_raise() -> None:
    """Caller bug: ``observed`` and ``expected`` must align."""
    with pytest.raises(ValueError):
        funnel_plot_data(observed=[1, 2, 3], expected=[1.0])
    with pytest.raises(ValueError):
        funnel_plot_data(observed=[1, 2], expected=[1.0, 2.0], labels=["A"])
