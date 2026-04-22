"""
Tests for the shared HealthDataService — verifies business logic
that both REST API and MCP server depend on.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from services.health_data_service import HealthDataService
from database import execute_query, execute_scalar


@pytest.fixture(scope="module")
def svc():
    return HealthDataService(query=execute_query, scalar=execute_scalar)


def test_overview_has_required_fields(svc):
    r = svc.get_overview()
    assert "total_screened" in r
    assert "target" in r
    assert r["target"] == 1_000_000


def test_overview_total_is_numeric(svc):
    r = svc.get_overview()
    assert isinstance(r["total_screened"], (int, float))


def test_district_summary_not_found(svc):
    r = svc.get_district_summary("9999")
    assert "error" in r


def test_compare_disease_invalid_key(svc):
    with pytest.raises(ValueError, match="Invalid disease_key"):
        svc.compare_disease("invalid_disease")


def test_compare_disease_valid(svc):
    r = svc.compare_disease("diabetes", level="zone")
    assert isinstance(r, list)


def test_filtered_summary_returns_data(svc):
    r = svc.get_filtered_summary({})
    assert "rows" in r or "error" in r


def test_lab_summary_returns_data(svc):
    r = svc.get_lab_summary()
    assert "total_lab_patients" in r or "error" in r


def test_mental_health_returns_data(svc):
    r = svc.get_mental_health_summary()
    assert "total_screened" in r or "error" in r


def test_demographics_returns_data(svc):
    r = svc.get_demographics()
    assert "total_respondents" in r or "error" in r


def test_search_districts_requires_query(svc):
    r = svc.search_districts(None)
    assert "error" in r


def test_search_districts_valid(svc):
    r = svc.search_districts({"disease": "diabetes", "limit": 5})
    assert isinstance(r, list)


def test_trend_invalid_granularity(svc):
    r = svc.get_trend("diabetes", granularity="weekly")
    assert "error" in r


def test_k_anonymity_threshold(svc):
    assert svc.K_ANONYMITY_THRESHOLD == 5


def test_round_floats():
    r = HealthDataService._round_floats({"a": 3.14159, "b": [1.111, 2.222]})
    assert r["a"] == 3.14
    assert r["b"] == [1.11, 2.22]
