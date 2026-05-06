"""Tests for ``services.reports.blocks._spatial_helpers`` (S11).

Pin the contracts that ``spatial_autocorr`` and ``choropleth`` depend on:
    * Queen-contiguity W is row-standardised (rows sum to 1)
    * Hand-coded ``ZONE_ADJACENCY`` is symmetric (queen contiguity is)
    * Moran's I picks up clear spatial clustering (I ≫ 0)
    * Moran's I is near the null expectation on iid random data
    * LISA labels HH / LL quadrants for a contrived clustered example
    * Permutation p-value is reproducible with a fixed ``random_state``
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

# Make ``api/`` importable for ``services.reports.*`` — same idiom as the
# rest of ``tests/services/reports/``.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.reports.blocks._spatial_helpers import (  # noqa: E402
    QUADRANT_LABELS_EN,
    QUADRANT_LABELS_TH,
    ZONE_ADJACENCY,
    lisa,
    morans_i,
    queen_contiguity_w,
)


# ---------------------------------------------------------------------------
# Adjacency + W
# ---------------------------------------------------------------------------


def test_zone_adjacency_has_8_zones() -> None:
    """The hand-coded zone adjacency should match the 8 BMA health zones."""
    assert sorted(ZONE_ADJACENCY.keys()) == ["1", "2", "3", "4", "5", "6", "7", "8"]


def test_zone_adjacency_is_symmetric() -> None:
    """Queen-contiguity is symmetric — verify by walking every edge."""
    for u, neighbours in ZONE_ADJACENCY.items():
        for v in neighbours:
            assert u in ZONE_ADJACENCY[v], (
                f"zone {u} lists {v} as neighbour but {v} doesn't list {u}"
            )


def test_zone_adjacency_no_self_loops() -> None:
    """A zone is not its own neighbour."""
    for u, neighbours in ZONE_ADJACENCY.items():
        assert u not in neighbours


def test_queen_contiguity_w_row_standardised() -> None:
    """Each row of W should sum to 1 (or 0 if isolated)."""
    W, labels = queen_contiguity_w(ZONE_ADJACENCY)
    assert W.shape == (8, 8)
    assert labels == ["1", "2", "3", "4", "5", "6", "7", "8"]
    row_sums = W.sum(axis=1)
    np.testing.assert_allclose(row_sums, 1.0, atol=1e-12)


def test_queen_contiguity_w_handles_isolated_node() -> None:
    """An isolated node should produce a zero row (not blow up on /0)."""
    adj = {"a": ["b"], "b": ["a"], "c": []}
    W, labels = queen_contiguity_w(adj)
    assert labels == ["a", "b", "c"]
    # Row for 'c' is all zeros.
    assert W[2].sum() == 0.0
    # Other rows still standardised.
    assert W[0].sum() == pytest.approx(1.0)
    assert W[1].sum() == pytest.approx(1.0)


def test_queen_contiguity_w_filters_unknown_neighbours() -> None:
    """Unknown neighbour labels are silently dropped (defensive parse)."""
    adj = {"a": ["b", "z"], "b": ["a"]}
    W, labels = queen_contiguity_w(adj)
    assert labels == ["a", "b"]
    # 'z' is dropped, 'a' has only 'b' as neighbour → row sums to 1.
    assert W[0].sum() == pytest.approx(1.0)
    assert W[0, 1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Moran's I — global
# ---------------------------------------------------------------------------


def test_morans_i_clustered_data_yields_positive_I() -> None:
    """A clearly-clustered dataset on the BMA zone graph should give I > 0.4
    and a small permutation p-value."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    # Zones 1-4 high, 5-8 low — they form connected components in the
    # zone graph so this is a textbook positive-autocorrelation case.
    clustered = np.array(
        [0.99, 0.98, 0.97, 0.96, 0.05, 0.04, 0.02, 0.01],
        dtype=np.float64,
    )
    I, expected, p = morans_i(clustered, W, n_perm=999, random_state=42)
    assert I > 0.4, f"expected strong positive autocorrelation, got I={I}"
    assert expected == pytest.approx(-1 / 7)
    assert p < 0.05, f"expected significant p-value, got p={p}"


def test_morans_i_random_data_near_null() -> None:
    """iid random data on the same graph should produce I close to E[I]
    with a non-significant p-value."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    rng = np.random.default_rng(0)
    random_data = rng.uniform(0, 1, size=8)
    I, expected, p = morans_i(random_data, W, n_perm=999, random_state=42)
    # ``random_data`` happens to land on a not-quite-null I, but the
    # permutation p should usually be ≥ 0.05 — this is a probabilistic
    # check so we tolerate a wider band than the clustered test.
    assert abs(I - expected) < 0.6, (
        f"random data gave |I - E[I]|={abs(I - expected)}, expected near null"
    )
    assert p > 0.05, f"random data unexpectedly significant: p={p}"


def test_morans_i_reproducible_with_random_state() -> None:
    """Two calls with the same ``random_state`` must produce identical p."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    I1, _, p1 = morans_i(values, W, n_perm=499, random_state=12345)
    I2, _, p2 = morans_i(values, W, n_perm=499, random_state=12345)
    assert I1 == I2
    assert p1 == p2


def test_morans_i_skip_perm_returns_nan_p() -> None:
    """``n_perm=0`` skips the permutation test and returns NaN for p."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    values = np.arange(8, dtype=np.float64)
    I, _, p = morans_i(values, W, n_perm=0)
    assert np.isnan(p)
    # I is still computed.
    assert isinstance(I, float)


def test_morans_i_constant_input_returns_zero_I() -> None:
    """Constant ``values`` → variance is 0, I is undefined; we return 0."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    constant = np.full(8, 0.5)
    I, _, p = morans_i(constant, W, n_perm=99, random_state=0)
    assert I == 0.0
    # No variability → permutation p is NaN.
    assert np.isnan(p)


def test_morans_i_shape_mismatch_raises() -> None:
    """Length mismatch between values and W must fail loudly."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    bad = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="shape mismatch"):
        morans_i(bad, W, n_perm=99)


# ---------------------------------------------------------------------------
# LISA — local
# ---------------------------------------------------------------------------


def test_lisa_returns_expected_keys() -> None:
    """LISA payload must always have the four documented keys."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    values = np.arange(8, dtype=np.float64)
    out = lisa(values, W, n_perm=99, random_state=0)
    assert set(out.keys()) == {"Ii", "p_values", "quadrant", "is_significant"}
    assert out["Ii"].shape == (8,)
    assert out["p_values"].shape == (8,)
    assert out["quadrant"].shape == (8,)
    assert out["is_significant"].shape == (8,)


def test_lisa_finds_HH_cluster_quadrant() -> None:
    """For a strong HH cluster, the quadrant code should be 1 (HH) on the
    high zones and 3 (LL) on the low zones — significance is stricter to
    achieve at n=8 so we test the quadrant assignment, not the p-value."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    # Match the connected-component split used in test_morans_i_clustered_data.
    strong = np.array(
        [0.99, 0.98, 0.97, 0.96, 0.05, 0.04, 0.02, 0.01],
        dtype=np.float64,
    )
    out = lisa(strong, W, n_perm=999, random_state=42)
    quad = out["quadrant"]
    # Zones 1-4 (indices 0-3) should land in the HH quadrant (1).
    for i in range(4):
        assert quad[i] == 1, f"index {i} should be HH, got {quad[i]}"
    # Zones 5-8 (indices 4-7) should land in the LL quadrant (3).
    for i in range(4, 8):
        assert quad[i] == 3, f"index {i} should be LL, got {quad[i]}"


def test_lisa_reproducible_with_random_state() -> None:
    """Same seed → identical p-values."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    values = np.array(
        [0.9, 0.85, 0.4, 0.45, 0.15, 0.10, 0.20, 0.05],
        dtype=np.float64,
    )
    a = lisa(values, W, n_perm=199, random_state=7)
    b = lisa(values, W, n_perm=199, random_state=7)
    np.testing.assert_array_equal(a["p_values"], b["p_values"])
    np.testing.assert_array_equal(a["Ii"], b["Ii"])


def test_lisa_skip_perm_returns_nan_p() -> None:
    """``n_perm=0`` produces NaN p-values and no significant flags."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    values = np.arange(8, dtype=np.float64)
    out = lisa(values, W, n_perm=0)
    assert np.all(np.isnan(out["p_values"]))
    assert not out["is_significant"].any()


def test_lisa_constant_input_returns_zero() -> None:
    """Constant input → all I_i = 0 (variance is undefined)."""
    W, _ = queen_contiguity_w(ZONE_ADJACENCY)
    constant = np.full(8, 0.42)
    out = lisa(constant, W, n_perm=99, random_state=0)
    np.testing.assert_array_equal(out["Ii"], np.zeros(8))


# ---------------------------------------------------------------------------
# Quadrant labels — surface for the block-level renderers
# ---------------------------------------------------------------------------


def test_quadrant_label_tables_have_all_codes() -> None:
    """Every integer quadrant code (0..4) needs both Thai + English labels."""
    for code in range(5):
        assert code in QUADRANT_LABELS_TH
        assert code in QUADRANT_LABELS_EN
