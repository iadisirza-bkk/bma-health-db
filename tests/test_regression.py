"""
Regression tests for the 10 most critical API endpoints.
These form the safety net for the refactor — must stay green at all times.
"""
import pytest


@pytest.mark.anyio
async def test_health_check(public_client):
    resp = await public_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body
    assert body["status"] in ("ok", "degraded")


@pytest.mark.anyio
async def test_summary_overview(client):
    resp = await client.get("/api/v2/summary/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_screened" in body
    assert "by_zone" in body
    assert isinstance(body["by_zone"], list)


@pytest.mark.anyio
async def test_summary_zones(client):
    resp = await client.get("/api/v2/summary/zones")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    if body:
        assert "zone_code" in body[0]


@pytest.mark.anyio
async def test_summary_districts(client):
    resp = await client.get("/api/v2/summary/districts")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    if body:
        assert "district_code" in body[0]


@pytest.mark.anyio
async def test_summary_district_detail(client):
    # First get list to find a valid district code
    list_resp = await client.get("/api/v2/summary/districts")
    assert list_resp.status_code == 200
    districts = list_resp.json()
    if not districts:
        pytest.skip("No districts in database")
    dcode = districts[0]["district_code"]
    resp = await client.get(f"/api/v2/summary/districts/{dcode}")
    assert resp.status_code == 200
    body = resp.json()
    assert "district_code" in body or "disease" in body or "data" in body


@pytest.mark.anyio
async def test_summary_filtered(client):
    resp = await client.get("/api/v2/summary/filtered")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body or isinstance(body, list) or "total" in body


@pytest.mark.anyio
async def test_disease_lab_crosstab(client):
    resp = await client.get("/api/v2/epidemiology/disease-lab-crosstab")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body or isinstance(body, list)


@pytest.mark.anyio
async def test_multi_disease_matrix(client):
    resp = await client.get("/api/v2/epidemiology/multi-disease-matrix")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body or isinstance(body, list)


@pytest.mark.anyio
async def test_headline_kpi(client):
    resp = await client.get("/api/v2/executive/headline-kpi")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_screened" in body or "data" in body or isinstance(body, dict)


@pytest.mark.anyio
async def test_public_district_summary(client):
    # This endpoint requires a district query param
    list_resp = await client.get("/api/v2/summary/districts")
    districts = list_resp.json()
    if not districts:
        pytest.skip("No districts in database")
    dcode = districts[0]["district_code"]
    resp = await client.get(f"/api/v2/public/district-summary?district={dcode}")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
