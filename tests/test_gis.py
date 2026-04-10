"""
Tests for GIS endpoints — facility locations, heatmaps, diet-disease.
"""
import pytest


@pytest.mark.anyio
async def test_facilities_list(client):
    resp = await client.get("/api/v2/gis/facilities?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body
    assert "data" in body
    assert body["total"] > 0
    if body["data"]:
        fac = body["data"][0]
        assert "latitude" in fac
        assert "longitude" in fac
        assert "code" in fac


@pytest.mark.anyio
async def test_facilities_total_count(client):
    resp = await client.get("/api/v2/gis/facilities?limit=1")
    body = resp.json()
    assert body["total"] > 10000, "Should have >10K facilities from clinic_latlong.xls"


@pytest.mark.anyio
async def test_facility_types(client):
    resp = await client.get("/api/v2/gis/facility-types")
    assert resp.status_code == 200
    body = resp.json()
    assert "types" in body
    assert len(body["types"]) > 5


@pytest.mark.anyio
async def test_facilities_by_zone(client):
    resp = await client.get("/api/v2/gis/facilities/zone/1")
    assert resp.status_code == 200
    body = resp.json()
    assert "facilities" in body


@pytest.mark.anyio
async def test_facilities_by_district(client):
    # Get a valid district code first
    districts = await client.get("/api/v2/summary/districts")
    dcode = districts.json()[0]["district_code"]
    resp = await client.get(f"/api/v2/gis/facilities/district/{dcode}")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_disease_heatmap_diabetes(client):
    resp = await client.get("/api/v2/gis/heatmap/disease/diabetes")
    assert resp.status_code == 200
    body = resp.json()
    assert "disease_key" in body
    assert body["disease_key"] == "diabetes"
    assert "data" in body


@pytest.mark.anyio
async def test_disease_heatmap_invalid_key(client):
    resp = await client.get("/api/v2/gis/heatmap/disease/invalid")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_disease_environment_overlay(client):
    resp = await client.get("/api/v2/gis/overlay/disease-environment?disease_key=diabetes")
    assert resp.status_code == 200
    body = resp.json()
    assert "disease_key" in body
    assert "disease_data" in body


@pytest.mark.anyio
async def test_diet_disease_sweet(client):
    resp = await client.get("/api/v2/promotion/diet-disease-correlation?diet=sweet&disease=diabetes")
    assert resp.status_code == 200
    body = resp.json()
    # Data may not be available (NULL in DB) — that's expected
    assert "data_available" in body or "data" in body


@pytest.mark.anyio
async def test_diet_disease_invalid(client):
    resp = await client.get("/api/v2/promotion/diet-disease-correlation?diet=invalid&disease=diabetes")
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body


@pytest.mark.anyio
async def test_pm25_graceful_fallback(client):
    """PM2.5 endpoint should return gracefully even if ArcGIS is unreachable."""
    resp = await client.get("/api/v2/gis/pm25/current")
    assert resp.status_code == 200
    body = resp.json()
    # Either has data or graceful fallback
    assert "data_available" in body or "data" in body or "message" in body
