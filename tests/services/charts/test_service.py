"""Unit tests for ChartService + ChartRegistry + ChartSpec.

Async tests use the ``pytest.mark.anyio`` convention already used by the
rest of this repo (see tests/test_data_adapter.py). The repo dependency
is faked via a tiny canned-rows mock so these tests touch zero SQL.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Make api/ importable for `services.charts.*` (mirrors tests/conftest.py).
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "api")
sys.path.insert(0, os.path.abspath(_API_DIR))

from services.charts.registry import ChartRegistry  # noqa: E402
from services.charts.service import ChartService, _apply_k_anon  # noqa: E402
from services.charts.spec import (  # noqa: E402
    AxesSpec,
    ChartSpec,
    FilterParam,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeRepo:
    """In-memory canned-rows mock implementing the run_query Protocol."""

    def __init__(self, canned: List[Dict[str, Any]]) -> None:
        self.canned = canned
        self.calls: list[tuple[str, Dict[str, Any]]] = []

    async def run_query(
        self,
        query_id: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        self.calls.append((query_id, dict(params)))
        return [dict(r) for r in self.canned]


@pytest.fixture
def mock_repository() -> _FakeRepo:
    return _FakeRepo(
        canned=[
            {"district": "บางรัก", "rate": 12.5, "n": 47},
            {"district": "ดุสิต", "rate": 9.8, "n": 30},
            {"district": "ปทุมวัน", "rate": 0.0, "n": 2},  # below k=5
        ]
    )


def _bar_spec() -> ChartSpec:
    return ChartSpec(
        spec_id="risk_factor_profile",
        kind="bar",
        title_th="โรคติดเชื้อสะสมรายเขต",
        query_id="district_disease_counts",
        query_params={"year": 2024},
        accepts=[
            FilterParam(name="district", kind="district"),
            FilterParam(name="sex", kind="sex"),
        ],
        axes=AxesSpec(x="district", y="rate"),
        k_anon_threshold=5,
    )


@pytest.fixture
def bar_spec() -> ChartSpec:
    return _bar_spec()


@pytest.fixture
def registry_with_bar(bar_spec: ChartSpec) -> ChartRegistry:
    return ChartRegistry({"risk_factor_profile": bar_spec})


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------

def test_registry_discover_loads_yaml(tmp_path: Path) -> None:
    """Registry.discover scans `*.yaml` and loads each as a ChartSpec."""
    yaml_body = (
        "spec_id: risk_factor_profile\n"
        "kind: bar\n"
        "title_th: โรคติดเชื้อสะสมรายเขต\n"
        "query_id: district_disease_counts\n"
        "query_params:\n"
        "  year: 2024\n"
        "accepts:\n"
        "  - {name: district, kind: district, required: false}\n"
        "axes:\n"
        "  x: district\n"
        "  y: rate\n"
        "k_anon_threshold: 5\n"
    )
    (tmp_path / "risk_factor_profile.yaml").write_text(yaml_body, encoding="utf-8")

    reg = ChartRegistry.discover(tmp_path)

    assert reg.list_ids() == ["risk_factor_profile"]
    spec = reg.get("risk_factor_profile")
    assert spec.kind == "bar"
    assert spec.query_params == {"year": 2024}
    assert spec.axes.x == "district"


def test_registry_discover_fails_on_stem_mismatch(tmp_path: Path) -> None:
    """Filename stem must match spec_id field."""
    (tmp_path / "wrong_name.yaml").write_text(
        "spec_id: risk_factor_profile\n"
        "kind: bar\n"
        "title_th: t\n"
        "query_id: q\n"
        "axes: {x: a, y: b}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match spec_id"):
        ChartRegistry.discover(tmp_path)


def test_registry_get_unknown_raises_keyerror(
    registry_with_bar: ChartRegistry,
) -> None:
    with pytest.raises(KeyError, match="unknown chart spec_id"):
        registry_with_bar.get("does_not_exist")


# ---------------------------------------------------------------------------
# Service.render
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_render_returns_correct_shape(
    registry_with_bar: ChartRegistry,
    mock_repository: _FakeRepo,
) -> None:
    """Render returns kind/spec_id/data/meta with the right shape."""
    svc = ChartService(registry_with_bar, mock_repository)

    resp = await svc.render(
        "risk_factor_profile",
        filters={"district": "1024"},
    )

    assert resp.kind == "bar"
    assert resp.spec_id == "risk_factor_profile"
    # k=5 drops the n=2 row, so we expect 2 of 3 input rows.
    assert len(resp.data) == 2
    xs = {row.x for row in resp.data}
    assert xs == {"บางรัก", "ดุสิต"}
    # Repo was called with merged static + caller filters.
    assert mock_repository.calls == [
        ("district_disease_counts", {"year": 2024, "district": "1024"})
    ]


@pytest.mark.anyio
async def test_render_k_anon_drops_rows_below_threshold(
    registry_with_bar: ChartRegistry,
    mock_repository: _FakeRepo,
) -> None:
    """k_anon_dropped reflects how many rows fell below the threshold."""
    svc = ChartService(registry_with_bar, mock_repository)
    resp = await svc.render("risk_factor_profile", filters={})
    assert resp.meta.n_total == 3
    assert resp.meta.k_anon_threshold == 5
    assert resp.meta.k_anon_dropped == 1
    # No row at or below k=5 survives in 'drop' mode.
    assert all(row.n >= 5 for row in resp.data)


@pytest.mark.anyio
async def test_render_rejects_unknown_filter_keys(
    registry_with_bar: ChartRegistry,
    mock_repository: _FakeRepo,
) -> None:
    svc = ChartService(registry_with_bar, mock_repository)
    with pytest.raises(ValueError, match="does not accept filters"):
        await svc.render(
            "risk_factor_profile",
            filters={"this_is_not_real": "x"},
        )


@pytest.mark.anyio
async def test_render_rejects_missing_required_filter(
    bar_spec: ChartSpec,
) -> None:
    """If accepts has required=True, missing the filter is an error."""
    spec_required = bar_spec.model_copy(
        update={
            "accepts": [
                FilterParam(name="district", kind="district", required=True)
            ]
        }
    )
    reg = ChartRegistry({spec_required.spec_id: spec_required})
    svc = ChartService(reg, _FakeRepo(canned=[]))
    with pytest.raises(ValueError, match="requires filter"):
        await svc.render(spec_required.spec_id, filters={})


@pytest.mark.anyio
async def test_render_defense_in_depth_assert_raises_on_pii(
    registry_with_bar: ChartRegistry,
) -> None:
    """If the repo returns a row with `pid`, render must blow up loud."""
    leaky_repo = _FakeRepo(
        canned=[{"district": "บางรัก", "rate": 1.0, "n": 99, "pid": "abc"}]
    )
    svc = ChartService(registry_with_bar, leaky_repo)
    with pytest.raises(ValueError, match="forbidden individual-level field"):
        await svc.render("risk_factor_profile", filters={})


# ---------------------------------------------------------------------------
# _apply_k_anon (pure function)
# ---------------------------------------------------------------------------

def test_apply_k_anon_drop_strategy_counts_dropped() -> None:
    rows = [
        {"x": "a", "n": 10},
        {"x": "b", "n": 3},
        {"x": "c", "n": 1},
    ]
    out, dropped = _apply_k_anon(rows, threshold=5)
    assert dropped == 2
    assert out == [{"x": "a", "n": 10}]


def test_apply_k_anon_mask_strategy_blanks_numerics() -> None:
    rows = [
        {"x": "a", "n": 10, "rate": 4.5},
        {"x": "b", "n": 3, "rate": 9.0},
    ]
    out, dropped = _apply_k_anon(rows, threshold=5, strategy="mask")
    assert dropped == 1
    assert out[0] == {"x": "a", "n": 10, "rate": 4.5}
    assert out[1] == {"x": "b", "n": "<5", "rate": None}
