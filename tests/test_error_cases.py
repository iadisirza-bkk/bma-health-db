"""
Error case tests — invalid params, auth failures, edge cases.

Tests that the API returns proper error codes and messages
for bad requests, missing resources, and auth failures.
"""
import pytest


# ============================================================================
# AUTH ERRORS
# ============================================================================

class TestAuthErrors:

    @pytest.mark.anyio
    async def test_no_api_key(self, public_client):
        """Endpoints should reject requests without API key."""
        resp = await public_client.get("/api/v2/summary/overview")
        assert resp.status_code == 401
        data = resp.json()
        assert "detail" in data

    @pytest.mark.anyio
    async def test_wrong_api_key(self, app):
        from httpx import ASGITransport, AsyncClient
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.headers["X-API-Key"] = "totally-wrong-key"
            resp = await ac.get("/api/v2/summary/overview")
            assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_health_no_auth_required(self, public_client):
        """/health should work without API key."""
        resp = await public_client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_docs_no_auth_required(self, public_client):
        """/docs should work without API key."""
        resp = await public_client.get("/docs")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_admin_api_no_bearer(self, client):
        """Admin API should reject without Bearer token."""
        resp = await client.get("/api/admin/data-status")
        assert resp.status_code in (401, 403)

    @pytest.mark.anyio
    async def test_admin_api_wrong_bearer(self, client):
        resp = await client.get(
            "/api/admin/data-status",
            headers={"Authorization": "Bearer wrong-password"},
        )
        assert resp.status_code in (401, 403)


# ============================================================================
# INVALID PARAMETERS
# ============================================================================

class TestInvalidParams:

    @pytest.mark.anyio
    async def test_district_not_found(self, client):
        resp = await client.get("/api/v2/summary/districts/0000")
        assert resp.status_code in (403, 404)

    @pytest.mark.anyio
    async def test_zone_not_found(self, client):
        resp = await client.get("/api/v2/summary/zones/99")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_invalid_disease_key(self, client):
        resp = await client.get("/api/v2/summary/districts")
        districts = resp.json()
        if not districts:
            pytest.skip("No districts")
        dcode = districts[0]["district_code"]
        resp = await client.get(f"/api/v2/summary/districts/{dcode}/disease/not_a_disease")
        assert resp.status_code == 400
        data = resp.json()
        assert "detail" in data

    @pytest.mark.anyio
    async def test_invalid_granularity(self, client):
        resp = await client.get("/api/v2/trends/screening", params={"granularity": "yearly"})
        assert resp.status_code in (200, 400, 422)

    @pytest.mark.anyio
    async def test_search_missing_disease(self, client):
        """Search without required 'disease' param."""
        resp = await client.get("/api/v2/search/districts")
        assert resp.status_code in (400, 422)

    @pytest.mark.anyio
    async def test_report_invalid_language(self, client):
        resp = await client.get("/api/reports/comprehensive/xyz")
        assert resp.status_code in (400, 404)

    @pytest.mark.anyio
    async def test_report_adaptive_path_traversal(self, client):
        resp = await client.get("/api/reports/adaptive/../../../etc/passwd")
        assert resp.status_code in (400, 404, 422)

    @pytest.mark.anyio
    async def test_export_nonexistent_district(self, client):
        resp = await client.get("/api/export/district/0000/pdf")
        assert resp.status_code in (200, 404)

    @pytest.mark.anyio
    async def test_stats_nonexistent_district(self, client):
        resp = await client.get("/api/stats/district/0000")
        assert resp.status_code in (200, 404)


# ============================================================================
# RESPONSE FORMAT VALIDATION
# ============================================================================

class TestResponseFormat:

    @pytest.mark.anyio
    async def test_health_response_shape(self, public_client):
        resp = await public_client.get("/health")
        data = resp.json()
        assert "status" in data
        assert "database" in data
        assert "timestamp" in data
        assert data["status"] in ("ok", "degraded")

    @pytest.mark.anyio
    async def test_overview_response_shape(self, client):
        resp = await client.get("/api/v2/summary/overview")
        data = resp.json()
        assert "total_screened" in data
        assert "zones_count" in data
        assert "by_zone" in data
        assert "by_disease" in data

    @pytest.mark.anyio
    async def test_district_list_is_array(self, client):
        resp = await client.get("/api/v2/summary/districts")
        data = resp.json()
        assert isinstance(data, list)

    @pytest.mark.anyio
    async def test_zones_list_is_array(self, client):
        resp = await client.get("/api/v2/summary/zones")
        data = resp.json()
        assert isinstance(data, list)

    @pytest.mark.anyio
    async def test_openapi_schema(self, public_client):
        resp = await public_client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["info"]["version"] == "4.0.0"
        assert len(data["paths"]) >= 100

    @pytest.mark.anyio
    async def test_report_catalog_shape(self, client):
        resp = await client.get("/api/reports/catalog")
        data = resp.json()
        assert "categories" in data
        for cat in data["categories"]:
            assert "id" in cat
            assert "label" in cat
            assert "reports" in cat
            assert isinstance(cat["reports"], list)

    @pytest.mark.anyio
    async def test_generation_progress_shape(self, client):
        resp = await client.get("/api/reports/generation-progress")
        data = resp.json()
        assert "running" in data
        assert isinstance(data["running"], bool)
        assert "completed" in data
        assert "total" in data

    @pytest.mark.anyio
    async def test_governor_dashboard_shape(self, client):
        resp = await client.get("/api/dashboard/governor")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "total_screened" in data or "diseases" in data

    @pytest.mark.anyio
    async def test_factors_response_has_categories(self, client):
        resp = await client.get("/api/factors/sex")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))

    @pytest.mark.anyio
    async def test_screening_summary_response(self, client):
        resp = await client.get("/api/screening-tests/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))


# ============================================================================
# EDGE CASES
# ============================================================================

class TestEdgeCases:

    @pytest.mark.anyio
    async def test_filtered_with_all_params(self, client):
        """Filtered endpoint with all parameters set."""
        resp = await client.get("/api/v2/summary/filtered", params={
            "sex": "1", "age_group": "40-49", "smoking": "0", "exercise": "1",
        })
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_trends_quarterly(self, client):
        resp = await client.get("/api/v2/trends/screening", params={"granularity": "quarterly"})
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_trends_disease_with_district(self, client):
        resp = await client.get("/api/v2/summary/districts")
        districts = resp.json()
        if not districts:
            pytest.skip("No districts")
        dcode = districts[0]["district_code"]
        resp = await client.get(f"/api/v2/trends/disease/diabetes", params={"district": dcode})
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_chat_with_invalid_history_json(self, client):
        """Stream endpoint should handle malformed history gracefully."""
        resp = await client.get("/api/health/chat/stream", params={
            "message": "test",
            "history": "not-valid-json",
        })
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_gis_heatmap_all_diseases(self, client):
        """Test heatmap for each valid disease key."""
        diseases = ["diabetes", "hypertension", "cardiovascular", "obesity"]
        for d in diseases:
            resp = await client.get(f"/api/v2/gis/heatmap/disease/{d}")
            assert resp.status_code == 200, f"Failed for disease: {d}"

    @pytest.mark.anyio
    async def test_concurrent_overview_calls(self, client):
        """Multiple concurrent calls should not crash."""
        import asyncio
        tasks = [client.get("/api/v2/summary/overview") for _ in range(5)]
        results = await asyncio.gather(*tasks)
        for r in results:
            assert r.status_code == 200
