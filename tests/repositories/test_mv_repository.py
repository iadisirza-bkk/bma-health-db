"""Smoke tests for the MVRepository data-access layer (ADR-01).

These tests connect to the real `bma_health` database (port 5433,
postgres/bma_health_dev). They are read-only and idempotent — they assert
that every registered chart query (a) returns without error, (b) returns
rows that pass through the Pydantic v2 row models without parse failure.

If the MVs are empty (e.g. fresh schema) the repository still has to
return an empty list cleanly — that's covered too.
"""
from __future__ import annotations

import os
import sys

import pytest

# Make `api/` importable the same way tests/conftest.py does.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))

# Ensure the test environment-variable defaults are in place before importing
# `database` / `config` so we don't blow up on `validate_production_config`.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("API_KEY", "dev-api-key-for-tests-only")
os.environ.setdefault("ADMIN_PASSWORD", "test-only-admin-password-not-for-production")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production-32+chars")
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-for-production-32+chars")
os.environ.setdefault("IDCARD_HASH_SECRET", "test-only-not-for-production-fixed-for-reproducibility")

from repositories import MVRepository, QueryNotFound  # noqa: E402
from repositories.rows import (  # noqa: E402
    AgePyramidRow,
    BehaviorDiseaseRow,
    DiseaseAgeSexRow,
    DistrictDiseaseRow,
    FacilityRow,
    LabDistributionRow,
    MentalRow,
    RepeatScreeningRow,
    RiskFactorRow,
    ScreeningCoverageRow,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def repo() -> MVRepository:
    """A single MVRepository instance is fine — `Repository.fetch_all`
    acquires its own pooled connection per call."""
    return MVRepository()


# --------------------------------------------------------------------------- #
# Registry sanity
# --------------------------------------------------------------------------- #

def test_registry_lists_all_chart_query_ids() -> None:
    """Every chart query named in the spec must be registered."""
    expected = {
        "district_disease_counts",
        "facility_screening",
        "disease_age_sex",
        "risk_factor_profile",
        "behavior_disease_correlation",
        "age_pyramid",
        "screening_coverage",
        "repeat_screening",
        "lab_distribution",
        "mental_health_distribution",
    }
    assert expected.issubset(MVRepository._queries)


@pytest.mark.anyio
async def test_run_query_unknown_id_raises(repo: MVRepository) -> None:
    with pytest.raises(QueryNotFound):
        await repo.run_query("does_not_exist")


@pytest.mark.anyio
async def test_run_query_dispatches_correctly(repo: MVRepository) -> None:
    """run_query() should route to the same method as a direct call."""
    direct = await repo.district_disease_counts()
    via_dispatch = await repo.run_query("district_disease_counts")
    assert len(direct) == len(via_dispatch)


# --------------------------------------------------------------------------- #
# Smoke tests — every chart query method
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_district_disease_counts(repo: MVRepository) -> None:
    rows = await repo.district_disease_counts()
    assert isinstance(rows, list)
    assert all(isinstance(r, DistrictDiseaseRow) for r in rows)
    assert len(rows) <= 10000


@pytest.mark.anyio
async def test_district_disease_counts_with_district_filter(repo: MVRepository) -> None:
    rows = await repo.district_disease_counts(district="1024")
    assert all(r.district_code == "1024" for r in rows)


@pytest.mark.anyio
async def test_district_disease_counts_with_zone_filter(repo: MVRepository) -> None:
    rows = await repo.district_disease_counts(zone="01")
    # zone filter shouldn't error even if no data matches in test fixture
    assert isinstance(rows, list)


@pytest.mark.anyio
async def test_facility_screening(repo: MVRepository) -> None:
    rows = await repo.facility_screening()
    assert isinstance(rows, list)
    assert all(isinstance(r, FacilityRow) for r in rows)


@pytest.mark.anyio
async def test_disease_age_sex(repo: MVRepository) -> None:
    rows = await repo.disease_age_sex()
    assert isinstance(rows, list)
    assert all(isinstance(r, DiseaseAgeSexRow) for r in rows)


@pytest.mark.anyio
async def test_risk_factor_profile(repo: MVRepository) -> None:
    rows = await repo.risk_factor_profile()
    assert isinstance(rows, list)
    assert all(isinstance(r, RiskFactorRow) for r in rows)
    # mv_summary_districts always carries 50 BKK districts, but we don't
    # assert on count to stay robust to fixture changes.


@pytest.mark.anyio
async def test_behavior_disease_correlation_default_smoke(repo: MVRepository) -> None:
    rows = await repo.behavior_disease_correlation()
    assert isinstance(rows, list)
    assert all(isinstance(r, BehaviorDiseaseRow) for r in rows)
    assert all(r.variable_key == "smoking" for r in rows)


@pytest.mark.anyio
async def test_behavior_disease_correlation_alcohol(repo: MVRepository) -> None:
    rows = await repo.behavior_disease_correlation(behavior="alcohol")
    assert all(r.variable_key == "alcohol" for r in rows)


@pytest.mark.anyio
async def test_behavior_disease_correlation_invalid_raises(repo: MVRepository) -> None:
    """Even though Literal[...] makes type checkers complain, the runtime
    guard must reject unknown values too — defense in depth."""
    with pytest.raises(ValueError):
        await repo.behavior_disease_correlation(behavior="bogus")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_age_pyramid(repo: MVRepository) -> None:
    rows = await repo.age_pyramid()
    assert isinstance(rows, list)
    assert all(isinstance(r, AgePyramidRow) for r in rows)


@pytest.mark.anyio
async def test_screening_coverage(repo: MVRepository) -> None:
    rows = await repo.screening_coverage()
    assert isinstance(rows, list)
    assert all(isinstance(r, ScreeningCoverageRow) for r in rows)


@pytest.mark.anyio
async def test_repeat_screening(repo: MVRepository) -> None:
    rows = await repo.repeat_screening()
    assert isinstance(rows, list)
    assert all(isinstance(r, RepeatScreeningRow) for r in rows)
    # visits ≥ persons by definition (each person ≥ 1 visit when present)
    for r in rows:
        if r.persons > 0:
            assert r.visits >= r.persons


@pytest.mark.anyio
async def test_lab_distribution_default_fbs(repo: MVRepository) -> None:
    rows = await repo.lab_distribution()
    assert isinstance(rows, list)
    assert all(isinstance(r, LabDistributionRow) for r in rows)
    assert all(r.lab_marker == "fbs" for r in rows)


@pytest.mark.anyio
async def test_lab_distribution_other_marker(repo: MVRepository) -> None:
    rows = await repo.lab_distribution(lab_marker="cholest")
    assert all(r.lab_marker == "cholest" for r in rows)


@pytest.mark.anyio
async def test_lab_distribution_invalid_raises(repo: MVRepository) -> None:
    with pytest.raises(ValueError):
        await repo.lab_distribution(lab_marker="not_a_marker")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_mental_health_distribution(repo: MVRepository) -> None:
    rows = await repo.mental_health_distribution()
    assert isinstance(rows, list)
    assert all(isinstance(r, MentalRow) for r in rows)
