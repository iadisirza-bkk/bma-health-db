"""
Tests for Phase 4: Traffic-light health card, gap analysis, final verification.
"""
import pytest


# ---------------------------------------------------------------------------
# Traffic-light health card (Doc 08 — ประชาชนจบ ม.6)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health_card_valid_district(client):
    # Get a valid district
    districts = await client.get("/api/v2/summary/districts")
    dcode = districts.json()[0]["district_code"]

    resp = await client.get(f"/api/v2/public/district-health-card?district={dcode}")
    assert resp.status_code == 200
    body = resp.json()
    assert "indicators" in body or "error" in body

    if "indicators" in body:
        assert "overall_status" in body
        assert body["overall_status"]["color"] in ("green", "yellow", "red")
        assert "green_count" in body
        assert "red_count" in body
        assert "advice" in body
        # Verify traffic-light classification
        for ind in body["indicators"]:
            assert "color" in ind
            assert ind["color"] in ("green", "yellow", "red", "gray")
            assert "name" in ind
            assert "label_th" in ind


@pytest.mark.anyio
async def test_health_card_english(client):
    districts = await client.get("/api/v2/summary/districts")
    dcode = districts.json()[0]["district_code"]
    resp = await client.get(f"/api/v2/public/district-health-card?district={dcode}&lang=en")
    assert resp.status_code == 200
    body = resp.json()
    if "indicators" in body:
        assert body["lang"] == "en"


@pytest.mark.anyio
async def test_health_card_invalid_district(client):
    resp = await client.get("/api/v2/public/district-health-card?district=9999")
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body


# ---------------------------------------------------------------------------
# Gap analysis (Doc 02 — รองผู้ว่า กทม.)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gap_analysis_default(client):
    resp = await client.get("/api/v2/kpi/gap-analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert "kpi" in body
    assert "target" in body
    assert "districts" in body
    assert "on_track" in body
    assert "critical" in body


@pytest.mark.anyio
async def test_gap_analysis_by_zone(client):
    resp = await client.get("/api/v2/kpi/gap-analysis?zone_code=1&kpi=pct_risk_dm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kpi"] == "pct_risk_dm"
    if body["districts"]:
        d = body["districts"][0]
        assert "gap" in d
        assert "meets_target" in d
        assert "urgency" in d
        assert d["urgency"] in ("critical", "moderate", "on_track")


@pytest.mark.anyio
async def test_gap_analysis_invalid_kpi(client):
    resp = await client.get("/api/v2/kpi/gap-analysis?kpi=invalid")
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body


# ---------------------------------------------------------------------------
# Final verification — all router groups still work
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_openapi_schema_loads(public_client):
    resp = await public_client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert schema["info"]["version"] == "4.0.0"
    # Should have many paths (100+ after one-stop consolidation)
    assert len(schema["paths"]) >= 100


@pytest.mark.anyio
async def test_docs_page_loads(public_client):
    resp = await public_client.get("/docs")
    assert resp.status_code == 200
