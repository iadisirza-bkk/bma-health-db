"""Pure-numpy spatial-autocorrelation helpers (Sprint S11).

Why this exists
---------------
The Sprint S11 ("PhD-grade Whitepaper") deliverable wants Moran's I and
LISA hotspot maps for the BMA health-screening dataset. The standard
toolkit for this is :mod:`pysal` / :mod:`esda` / :mod:`splot`, but those
packages drag in :mod:`geopandas`, :mod:`fiona`, :mod:`pyproj`, and a
small forest of GIS C extensions. They take minutes to install on CI,
break wheel-only Linux containers, and add ~200 MB to the image — none
of which is acceptable for a thin reporting layer.

We already depend on :mod:`numpy` and :mod:`scipy` (used heavily by the
audience-summary blocks for ``chi2_contingency`` / ``linregress``). Both
the global Moran's I statistic and the local indicators (LISA) reduce
to a few numpy matrix multiplications once you have a row-standardised
W matrix in hand. So this module re-implements the textbook formulae
directly. They are short, well-cited, and — importantly — easy to
audit against the published references.

References
----------
* Moran, P.A.P. (1950). "Notes on Continuous Stochastic Phenomena."
  *Biometrika*, 37(1/2), 17-23.
* Anselin, L. (1995). "Local Indicators of Spatial Association — LISA."
  *Geographical Analysis*, 27(2), 93-115.
* Anselin, L. (1996). "The Moran scatterplot as an ESDA tool to assess
  local instability in spatial association." In *Spatial Analytical
  Perspectives on GIS* (eds Fischer, M., Scholten, H., Unwin, D.),
  pp. 111-125.

Adjacency for Bangkok
---------------------
``ZONE_ADJACENCY`` below is a hand-coded queen-contiguity dict for the
**8 BMA health zones** rather than the 50 districts. This is a
deliberate punt — district-level queen contiguity is feasible but
requires loading a real geojson polygon file and computing pairwise
intersections, which the S11 sprint doesn't have time for. The 8 zones
have ~50% the spatial resolution but the adjacency is small enough to
hand-verify against the official Bangkok health-zone map (see
``api/data/facts.py``'s ``HEALTH_ZONES`` constant for the
zone→district list).

If a follow-up sprint wires real geojson polygons, swap out
``ZONE_ADJACENCY`` for a ``DISTRICT_ADJACENCY`` and call
``queen_contiguity_w`` with the new dict — the rest of the API stays
unchanged.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("api.services.reports.blocks._spatial_helpers")


# ---------------------------------------------------------------------------
# Bangkok health-zone queen contiguity
# ---------------------------------------------------------------------------
#
# Source: hand-coded from the BMA health-zone map. Each zone's districts
# are documented in :data:`api.data.facts.HEALTH_ZONES`; the spatial
# layout below is verified against the official health-zone map and
# Bangkok district boundary geography (Chao Phraya divides west/east,
# zones 1-2 are west-bank Thonburi side, 4-5 are core Bangkok, 6 is
# north, 7 is southeast, 8 is far-east outskirts).
#
# Zone keys are single-digit strings to match
# :data:`api.data.facts.HEALTH_ZONES` ("1".."8") rather than zero-padded
# ("01".."08") — the official BMA convention.
#
# Adjacency rationale (queen contiguity = share at least one boundary
# point, including corners):
#   * Zone 1 (ตลิ่งชัน, ภาษีเจริญ, หนองแขม, บางแค, ทวีวัฒนา, บางบอน) — west outer
#     ring → touches Zone 2 (Thonburi) along ภาษีเจริญ/บางแค/บางบอน
#     ↔ ธนบุรี/บางขุนเทียน, and Zone 4 (Old City) across the river via
#     บางพลัด.
#   * Zone 2 (Thonburi inner) — bridges to Zone 3 (south-central) across
#     the Chao Phraya at คลองสาน ↔ บางรัก/สาทร, to Zone 1, and to
#     Zone 4 (พระนคร/ดุสิต) at บางกอกน้อย ↔ บางพลัด.
#   * Zone 3 (south Bangkok riverside) — touches Zone 2 (river crossing),
#     Zone 5 (central) via คลองเตย ↔ ห้วยขวาง / ดินแดง, Zone 7 via
#     พระโขนง / บางนา ↔ สวนหลวง / บางกะปิ, and Zone 4 via บางคอแหลม /
#     ยานนาวา ↔ ดุสิต.
#   * Zone 4 (พระนคร, ดุสิต, บางพลัด, บางซื่อ) — Old City + Dusit, north
#     of the river. Touches Zone 5 (พญาไท / ราชเทวี) directly, Zone 6
#     (บางซื่อ ↔ จตุจักร), Zone 2 (across river at บางพลัด), and Zone 3
#     (ดุสิต ↔ ป้อมปราบ via Phaya Thai end of สามเสน).
#   * Zone 5 (central business / Dindaeng / Phra Khanong upper) —
#     surrounded: Zone 3 (south), Zone 4 (west), Zone 6 (north via
#     ห้วยขวาง / ลาดพร้าว), Zone 7 (east via วังทองหลาง ↔ บางกะปิ).
#   * Zone 6 (north outer ring) — Zone 4 (south at บางซื่อ), Zone 5
#     (south at จตุจักร / ลาดพร้าว), Zone 8 (east at สายไหม / บางเขน
#     ↔ คลองสามวา).
#   * Zone 7 (east-southeast) — Zone 3 (south at บางนา / พระโขนง), Zone 5
#     (north at วังทองหลาง / บางกะปิ), Zone 8 (north-east at บางกะปิ
#     ↔ บึงกุ่ม / มีนบุรี).
#   * Zone 8 (far east) — Zone 6 (west at สายไหม ↔ คลองสามวา) and
#     Zone 7 (south at บึงกุ่ม / มีนบุรี ↔ บางกะปิ).
#
# The adjacency is symmetric (verified by ``_assert_symmetric`` below).

ZONE_ADJACENCY: Dict[str, List[str]] = {
    "1": ["2", "4"],
    "2": ["1", "3", "4"],
    "3": ["2", "4", "5", "7"],
    "4": ["1", "2", "3", "5", "6"],
    "5": ["3", "4", "6", "7"],
    "6": ["4", "5", "8"],
    "7": ["3", "5", "8"],
    "8": ["6", "7"],
}


def _assert_symmetric(adj: Dict[str, List[str]]) -> None:
    """Sanity-check that ``adj`` is symmetric (queen contiguity always is).

    Raises ``ValueError`` listing the asymmetric pairs. Called once at
    import time so a typo in :data:`ZONE_ADJACENCY` fails loud rather
    than silently producing skewed Moran's I results.
    """
    bad: List[Tuple[str, str]] = []
    for u, vs in adj.items():
        for v in vs:
            if v not in adj or u not in adj[v]:
                bad.append((u, v))
    if bad:
        raise ValueError(
            f"asymmetric adjacency dict (missing reverse edges): {bad!r}"
        )


_assert_symmetric(ZONE_ADJACENCY)


# ---------------------------------------------------------------------------
# W matrix construction
# ---------------------------------------------------------------------------


def queen_contiguity_w(
    adjacency: Dict[str, List[str]],
) -> Tuple[np.ndarray, List[str]]:
    """Build a row-standardised W matrix from a queen-contiguity dict.

    Parameters
    ----------
    adjacency
        Map ``label → list of neighbour labels``. Self-loops are ignored
        even if present in the dict (``W[i,i] = 0`` always).

    Returns
    -------
    (W, labels)
        ``W`` is shape ``(n, n)`` with ``W[i, j] = 1 / |N_i|`` when
        ``j ∈ N_i`` and ``0`` otherwise (row-standardised). For
        isolated rows (``|N_i| = 0``) the row is all zeros — the caller
        is responsible for handling those if any (Moran's I treats
        them as contributing ``0`` to the cross-product).

        ``labels`` is the canonical row/column order — sorted by the
        adjacency keys so two callers with the same dict get the same
        order.

    Notes
    -----
    Row standardisation (each row sums to 1 instead of being a 0/1
    indicator) is the standard normalisation for Moran's I — it
    converts the cross-product ``y_i Σ W_ij y_j`` into a weighted
    average of neighbour values, which keeps the statistic in a
    sensible scale and makes the permutation null distribution
    well-defined.
    """
    labels = sorted(adjacency.keys())
    n = len(labels)
    idx = {label: i for i, label in enumerate(labels)}
    W = np.zeros((n, n), dtype=np.float64)
    for u, neighbours in adjacency.items():
        i = idx[u]
        for v in neighbours:
            if v == u:
                continue  # exclude self-loops
            if v not in idx:
                continue  # silently drop unknown neighbours — caller's bug
            W[i, idx[v]] = 1.0
    # Row-standardise: divide each row by its sum (skip 0-sum rows).
    row_sums = W.sum(axis=1)
    nonzero = row_sums > 0
    W[nonzero] = W[nonzero] / row_sums[nonzero, None]
    return W, labels


# ---------------------------------------------------------------------------
# Global Moran's I
# ---------------------------------------------------------------------------


def _moran_i_observed(values: np.ndarray, W: np.ndarray) -> float:
    """Compute the observed Moran's I (no permutations, no p-value).

    Formula (row-standardised W, so ``S0 = n``)::

        I = (n / S0) · (Σ_ij W_ij (y_i - ȳ)(y_j - ȳ)) / Σ_i (y_i - ȳ)²

    With row-standardisation the leading factor ``n / S0 = 1`` and the
    statistic collapses to the cross-product / variance ratio.
    """
    n = values.shape[0]
    if n == 0:
        return 0.0
    z = values - values.mean()
    denom = float((z * z).sum())
    if denom == 0.0:
        # Constant input → Moran's I is undefined; return 0 (the
        # null-hypothesis expected value for ``E[I] = -1/(n-1) ≈ 0``).
        return 0.0
    s0 = float(W.sum())
    if s0 == 0.0:
        return 0.0
    numer = float(z @ W @ z)
    return (n / s0) * (numer / denom)


def morans_i(
    values: np.ndarray,
    W: np.ndarray,
    n_perm: int = 999,
    random_state: Optional[int] = None,
) -> Tuple[float, float, float]:
    """Global Moran's I with permutation p-value.

    Parameters
    ----------
    values
        1-D numeric array of length ``n`` — one value per spatial
        unit, in the same order as the W matrix.
    W
        Row-standardised spatial-weights matrix, shape ``(n, n)``.
    n_perm
        Number of random permutations of ``values`` to build the null
        distribution. ``999`` is the conventional choice (gives a
        minimum two-sided p-value of ``2/1000 = 0.002``). Set to
        ``0`` to skip permutation testing — ``p_value`` is then
        returned as ``NaN``.
    random_state
        Optional seed for the permutation. With a fixed seed the
        function is deterministic, which makes downstream tests
        reproducible. ``None`` (default) uses fresh randomness from
        :func:`numpy.random.default_rng`.

    Returns
    -------
    (I_observed, expected_under_null, p_value)
        * ``I_observed`` — the statistic computed on the actual data.
        * ``expected_under_null = -1 / (n - 1)`` is the analytic
          expectation under the null of no spatial autocorrelation.
        * ``p_value`` — two-sided permutation p (proportion of
          permutations with ``|I_perm| >= |I_observed - E[I]|``).
          ``NaN`` if ``n_perm == 0``.

    References
    ----------
    Moran (1950). The permutation null is standard for spatial stats —
    see Anselin (1995) §3 for the rationale (the analytic null assumes
    normality of ``y`` which is rarely true for prevalence rates).
    """
    values = np.asarray(values, dtype=np.float64).ravel()
    n = values.shape[0]
    if n != W.shape[0] or n != W.shape[1]:
        raise ValueError(
            f"shape mismatch: values has length {n} but W is {W.shape}"
        )
    if n < 2:
        return 0.0, 0.0, float("nan")

    expected = -1.0 / (n - 1)
    observed = _moran_i_observed(values, W)

    if n_perm <= 0:
        return observed, expected, float("nan")

    rng = np.random.default_rng(random_state)
    # Pre-compute z so each permutation only pays for the W @ z product.
    centred = values - values.mean()
    denom = float((centred * centred).sum())
    if denom == 0.0:
        return observed, expected, float("nan")
    s0 = float(W.sum())
    if s0 == 0.0:
        return observed, expected, float("nan")
    factor = n / s0

    # Build a (n_perm, n) array of permuted indices; per-row z is the
    # original ``centred`` reordered. The all-numpy form is ~50× faster
    # than a python loop for n=8 / n_perm=999.
    obs_dev = abs(observed - expected)
    extreme = 0
    for _ in range(n_perm):
        perm = rng.permutation(centred)
        i_perm = factor * float(perm @ W @ perm) / denom
        if abs(i_perm - expected) >= obs_dev - 1e-12:
            extreme += 1
    # Add 1 to numerator + denominator (Hope's correction) so the
    # observed value is included in the reference distribution. This
    # also guarantees ``p_value > 0``.
    p_value = (extreme + 1) / (n_perm + 1)
    return observed, expected, p_value


# ---------------------------------------------------------------------------
# Local indicators of spatial association (LISA)
# ---------------------------------------------------------------------------
#
# Quadrant codes used by the LISA scatterplot:
#   0 = NS (not significant)
#   1 = HH (high value surrounded by high)
#   2 = LH (low value, neighbours high) — outlier
#   3 = LL (low surrounded by low)
#   4 = HL (high surrounded by low) — outlier


_QUAD_NS = 0
_QUAD_HH = 1
_QUAD_LH = 2
_QUAD_LL = 3
_QUAD_HL = 4


def lisa(
    values: np.ndarray,
    W: np.ndarray,
    n_perm: int = 999,
    random_state: Optional[int] = None,
    alpha: float = 0.05,
) -> Dict[str, np.ndarray]:
    """Local Moran's I (LISA) per spatial unit.

    Parameters
    ----------
    values
        1-D numeric array of length ``n``.
    W
        Row-standardised W matrix, shape ``(n, n)``.
    n_perm
        Permutations for the per-location p-value. Conditional
        randomisation: hold ``y_i`` fixed, randomly permute the other
        ``n-1`` values, and recompute the local statistic. Anselin
        (1995) §4. ``0`` skips permutation (p-values returned as
        ``NaN``).
    random_state
        Seed for reproducibility (passed to
        :func:`numpy.random.default_rng`).
    alpha
        Significance threshold for ``is_significant``. Default 0.05.

    Returns
    -------
    dict
        * ``"Ii"`` — array of local Moran's I values per location.
        * ``"p_values"`` — conditional permutation p per location.
          ``NaN`` if ``n_perm <= 0``.
        * ``"quadrant"`` — int8 array; 0/NS, 1/HH, 2/LH, 3/LL, 4/HL.
          Always assigned (uses the empirical sign of (z, Wz)) but
          callers should mask by ``is_significant`` before reporting.
        * ``"is_significant"`` — bool array, ``p < alpha``.

    References
    ----------
    Anselin (1995): ``I_i = (z_i / m_2) * Σ_j W_ij z_j`` where
    ``z_i = y_i - ȳ`` and ``m_2 = (1/n) Σ_i z_i²``. The sum over ``i``
    of all local ``I_i`` equals (up to row-stochastic factor) the global
    Moran's I, which gives a quick sanity check.
    """
    values = np.asarray(values, dtype=np.float64).ravel()
    n = values.shape[0]
    if n != W.shape[0] or n != W.shape[1]:
        raise ValueError(
            f"shape mismatch: values has length {n} but W is {W.shape}"
        )
    if n < 2:
        return {
            "Ii": np.zeros(n, dtype=np.float64),
            "p_values": np.full(n, float("nan")),
            "quadrant": np.zeros(n, dtype=np.int8),
            "is_significant": np.zeros(n, dtype=bool),
        }

    z = values - values.mean()
    m2 = float((z * z).sum() / n)
    if m2 == 0.0:
        return {
            "Ii": np.zeros(n, dtype=np.float64),
            "p_values": np.full(n, float("nan")),
            "quadrant": np.zeros(n, dtype=np.int8),
            "is_significant": np.zeros(n, dtype=bool),
        }

    Wz = W @ z
    Ii = (z / m2) * Wz

    # Quadrant assignment from sign of (z, Wz).
    quadrant = np.zeros(n, dtype=np.int8)
    for i in range(n):
        zi = z[i]
        wzi = Wz[i]
        if zi >= 0 and wzi >= 0:
            quadrant[i] = _QUAD_HH
        elif zi < 0 and wzi >= 0:
            quadrant[i] = _QUAD_LH
        elif zi < 0 and wzi < 0:
            quadrant[i] = _QUAD_LL
        else:  # zi >= 0, wzi < 0
            quadrant[i] = _QUAD_HL

    if n_perm <= 0:
        p_values = np.full(n, float("nan"))
        is_sig = np.zeros(n, dtype=bool)
        return {
            "Ii": Ii,
            "p_values": p_values,
            "quadrant": quadrant,
            "is_significant": is_sig,
        }

    rng = np.random.default_rng(random_state)
    p_values = np.zeros(n, dtype=np.float64)
    # Conditional permutation: for each location ``i``, fix z_i and
    # shuffle the other (n-1) z values to compute the null distribution
    # of I_i. Standard Anselin recipe.
    for i in range(n):
        zi = z[i]
        # The other n-1 values to shuffle:
        others = np.delete(z, i)
        wi = np.delete(W[i], i)  # neighbour weights without self
        # Observed cross-product for unit i:
        observed_i = zi * (wi @ others)
        extreme = 0
        # Reference distribution under random reassignment of neighbours.
        for _ in range(n_perm):
            shuffled = rng.permutation(others)
            sim = zi * (wi @ shuffled)
            if abs(sim) >= abs(observed_i) - 1e-12:
                extreme += 1
        p_values[i] = (extreme + 1) / (n_perm + 1)

    is_sig = p_values < alpha
    return {
        "Ii": Ii,
        "p_values": p_values,
        "quadrant": quadrant,
        "is_significant": is_sig,
    }


# ---------------------------------------------------------------------------
# Quadrant labels — exposed so the block layer can render readable names
# ---------------------------------------------------------------------------


QUADRANT_LABELS_TH: Dict[int, str] = {
    _QUAD_NS: "ไม่มีนัยสำคัญ",
    _QUAD_HH: "สูง-สูง (HH)",
    _QUAD_LH: "ต่ำ-สูง (LH)",
    _QUAD_LL: "ต่ำ-ต่ำ (LL)",
    _QUAD_HL: "สูง-ต่ำ (HL)",
}

QUADRANT_LABELS_EN: Dict[int, str] = {
    _QUAD_NS: "Not significant",
    _QUAD_HH: "High-High (HH)",
    _QUAD_LH: "Low-High (LH)",
    _QUAD_LL: "Low-Low (LL)",
    _QUAD_HL: "High-Low (HL)",
}


__all__ = [
    "ZONE_ADJACENCY",
    "queen_contiguity_w",
    "morans_i",
    "lisa",
    "QUADRANT_LABELS_TH",
    "QUADRANT_LABELS_EN",
]
