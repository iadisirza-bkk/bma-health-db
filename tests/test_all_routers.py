"""
Smoke test — hit at least one endpoint per router group to verify routing works.
"""
import pytest


@pytest.mark.anyio
async def test_trends_screening(client):
    resp = await client.get("/api/v2/trends/screening")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_search_districts(client):
    resp = await client.get("/api/v2/search/districts?disease=diabetes")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_epidemiology_age_pyramid(client):
    resp = await client.get("/api/v2/epidemiology/age-pyramid")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_promotion_bmi(client):
    resp = await client.get("/api/v2/promotion/bmi-distribution")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_disease_control_screening_coverage(client):
    resp = await client.get("/api/v2/disease-control/screening-coverage")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_kpi_moph_targets(client):
    resp = await client.get("/api/v2/kpi/moph-targets")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_executive_headline(client):
    resp = await client.get("/api/v2/executive/headline-kpi")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_facility_performance(client):
    resp = await client.get("/api/v2/facility/performance")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_strategy_cost(client):
    resp = await client.get("/api/v2/strategy/cost-per-screening")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_research_dictionary(client):
    resp = await client.get("/api/v2/research/data-dictionary")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_public_health_tips(client):
    resp = await client.get("/api/v2/public/health-tips?disease=diabetes")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_monitoring_table_stats(client):
    resp = await client.get("/api/v2/monitoring/table-stats")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_monitoring_etl_status(client):
    resp = await client.get("/api/v2/monitoring/etl-status")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_zone_dashboard(client):
    resp = await client.get("/api/v2/zone/01/dashboard")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_disease_control_ncd_cascade(client):
    resp = await client.get("/api/v2/disease-control/ncd-cascade?disease=diabetes")
    assert resp.status_code == 200
