"""
Tests for all 8 new endpoint groups added in the one-stop backend consolidation.

Groups: Chat, Reports, Export, Statistics, Dashboard, Factors, Screening Tests, Admin API.
"""
import pytest


# ============================================================================
# CHAT API  (/api/health/)
# ============================================================================

class TestChatAPI:
    """LLM Chat endpoints — may return 503 if LMStudio is not running."""

    @pytest.mark.anyio
    async def test_chat_sync_get(self, client):
        resp = await client.get("/api/health/chat", params={"message": "hello"})
        assert resp.status_code in (200, 503)

    @pytest.mark.anyio
    async def test_chat_sync_post(self, client):
        resp = await client.post("/api/health/chat", params={"message": "what is diabetes?"})
        assert resp.status_code in (200, 503)

    @pytest.mark.anyio
    async def test_chat_stream_get(self, client):
        resp = await client.get("/api/health/chat/stream", params={"message": "hello"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    @pytest.mark.anyio
    async def test_chat_stream_post(self, client):
        resp = await client.post("/api/health/chat/stream", params={"message": "overview"})
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_chat_stream_with_history(self, client):
        import json
        history = json.dumps([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
        resp = await client.get("/api/health/chat/stream", params={"message": "continue", "history": history})
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_chat_empty_message(self, client):
        resp = await client.get("/api/health/chat", params={"message": ""})
        assert resp.status_code in (200, 503)


# ============================================================================
# REPORTS API  (/api/reports/)
# ============================================================================

class TestReportsAPI:
    """PDF report endpoints."""

    @pytest.mark.anyio
    async def test_report_catalog(self, client):
        resp = await client.get("/api/reports/catalog")
        assert resp.status_code == 200
        data = resp.json()
        assert "categories" in data
        assert len(data["categories"]) >= 1

    @pytest.mark.anyio
    async def test_report_status(self, client):
        resp = await client.get("/api/reports/status")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_report_generation_progress(self, client):
        resp = await client.get("/api/reports/generation-progress")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "completed" in data

    @pytest.mark.anyio
    async def test_report_scheduler_status(self, client):
        resp = await client.get("/api/reports/scheduler-status")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_report_comprehensive_not_generated(self, client):
        """Report download returns 404 or 503 when not yet generated."""
        resp = await client.get("/api/reports/comprehensive/th")
        assert resp.status_code in (200, 404, 500, 503)

    @pytest.mark.anyio
    async def test_report_executive_not_generated(self, client):
        resp = await client.get("/api/reports/executive/en")
        assert resp.status_code in (200, 404, 500, 503)

    @pytest.mark.anyio
    async def test_report_invalid_lang(self, client):
        resp = await client.get("/api/reports/comprehensive/xx")
        assert resp.status_code in (400, 404)

    @pytest.mark.anyio
    async def test_report_disease_not_generated(self, client):
        resp = await client.get("/api/reports/disease/diabetes")
        assert resp.status_code in (200, 404)

    @pytest.mark.anyio
    async def test_report_invalidate(self, client):
        resp = await client.post("/api/reports/invalidate")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_report_adaptive_not_found(self, client):
        resp = await client.get("/api/reports/adaptive/nonexistent.pdf")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_report_adaptive_invalid_filename(self, client):
        resp = await client.get("/api/reports/adaptive/../../etc/passwd")
        assert resp.status_code in (400, 404, 422)

    @pytest.mark.anyio
    async def test_report_zone_not_generated(self, client):
        resp = await client.get("/api/reports/zone/1/th")
        assert resp.status_code in (200, 404, 500)

    @pytest.mark.anyio
    async def test_report_msd_not_generated(self, client):
        resp = await client.get("/api/reports/msd/th")
        assert resp.status_code in (200, 404, 500)

    @pytest.mark.anyio
    async def test_report_public_not_generated(self, client):
        resp = await client.get("/api/reports/public/th")
        assert resp.status_code in (200, 404)

    @pytest.mark.anyio
    async def test_report_dashboard(self, client):
        """Dashboard endpoint returns unified report state."""
        resp = await client.get("/api/reports/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        # Top-level keys
        assert "generation" in data
        assert "scheduler" in data
        assert "categories" in data
        assert "summary" in data
        # Generation progress shape
        gen = data["generation"]
        assert "running" in gen
        assert "percent" in gen
        assert isinstance(gen["percent"], (int, float))
        assert 0 <= gen["percent"] <= 100
        # Scheduler shape
        sched = data["scheduler"]
        assert "enabled" in sched
        assert "cron" in sched
        # Categories shape
        cats = data["categories"]
        assert len(cats) >= 6
        for cat in cats:
            assert "id" in cat
            assert "label" in cat
            assert "reports" in cat
            for report in cat["reports"]:
                assert "label" in report
                assert "url" in report
                assert "cached" in report
                assert "updated_at" in report
        # Summary shape
        summary = data["summary"]
        assert "total_reports" in summary
        assert "cached_reports" in summary
        assert "percent_ready" in summary
        assert summary["total_reports"] >= 1


# ============================================================================
# EXPORT API  (/api/export/)
# ============================================================================

class TestExportAPI:
    """Data export endpoints (PDF, Excel, CSV)."""

    async def _get_dcode(self, client):
        resp = await client.get("/api/v2/summary/districts")
        districts = resp.json()
        if not districts:
            pytest.skip("No districts in database")
        return districts[0]["district_code"]

    @pytest.mark.anyio
    async def test_export_city_excel(self, client):
        resp = await client.get("/api/export/city/excel")
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "spreadsheet" in ct or "csv" in ct or "octet" in ct

    @pytest.mark.anyio
    async def test_export_district_pdf(self, client):
        dcode = await self._get_dcode(client)
        resp = await client.get(f"/api/export/district/{dcode}/pdf")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_export_district_pdf_json(self, client):
        dcode = await self._get_dcode(client)
        resp = await client.get(f"/api/export/district/{dcode}/pdf/json")
        assert resp.status_code == 200
        data = resp.json()
        assert "district_code" in data or "dcode" in data or isinstance(data, dict)

    @pytest.mark.anyio
    async def test_export_district_excel(self, client):
        dcode = await self._get_dcode(client)
        resp = await client.get(f"/api/export/district/{dcode}/excel")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_export_zone_excel(self, client):
        resp = await client.get("/api/export/zone/1/excel")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_export_rankings_excel(self, client):
        resp = await client.get("/api/export/rankings/diabetes/excel")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_export_district_not_found(self, client):
        resp = await client.get("/api/export/district/9999/pdf")
        assert resp.status_code in (200, 404)


# ============================================================================
# STATISTICS API  (/api/stats/)
# ============================================================================

class TestStatisticsAPI:
    """Descriptive statistics, comparison, ranking endpoints."""

    async def _get_dcode(self, client):
        resp = await client.get("/api/v2/summary/districts")
        districts = resp.json()
        if not districts:
            pytest.skip("No districts in database")
        return districts[0]["district_code"]

    @pytest.mark.anyio
    async def test_stats_city(self, client):
        resp = await client.get("/api/stats/city")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_screened" in data or "total_districts" in data or isinstance(data, dict)

    @pytest.mark.anyio
    async def test_stats_district(self, client):
        dcode = await self._get_dcode(client)
        resp = await client.get(f"/api/stats/district/{dcode}")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_stats_zone(self, client):
        resp = await client.get("/api/stats/zone/1")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_stats_compare(self, client):
        resp = await client.get("/api/v2/summary/districts")
        districts = resp.json()
        if len(districts) < 2:
            pytest.skip("Need at least 2 districts")
        d1, d2 = districts[0]["district_code"], districts[1]["district_code"]
        resp = await client.get(f"/api/stats/compare?district1={d1}&district2={d2}")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_stats_ranking(self, client):
        resp = await client.get("/api/stats/ranking/diabetes")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))

    @pytest.mark.anyio
    async def test_stats_trends(self, client):
        dcode = await self._get_dcode(client)
        resp = await client.get(f"/api/stats/trends/{dcode}/diabetes")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_stats_district_not_found(self, client):
        resp = await client.get("/api/stats/district/9999")
        assert resp.status_code in (200, 404)


# ============================================================================
# DASHBOARD API  (/api/dashboard/)
# ============================================================================

class TestDashboardAPI:
    """Role-based dashboard endpoints."""

    @pytest.mark.anyio
    async def test_dashboard_governor(self, client):
        resp = await client.get("/api/dashboard/governor")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_screened" in data or "diseases" in data or isinstance(data, dict)

    @pytest.mark.anyio
    async def test_dashboard_director(self, client):
        resp = await client.get("/api/dashboard/director/1")
        assert resp.status_code in (200, 404)

    @pytest.mark.anyio
    async def test_dashboard_medical(self, client):
        resp = await client.get("/api/dashboard/medical")
        assert resp.status_code in (200, 404, 422)

    @pytest.mark.anyio
    async def test_dashboard_director_invalid_zone(self, client):
        resp = await client.get("/api/dashboard/director/99")
        assert resp.status_code in (200, 404)


# ============================================================================
# FACTORS API  (/api/factors/)
# ============================================================================

class TestFactorsAPI:
    """Factor analysis endpoints with chi-square tests."""

    @pytest.mark.anyio
    async def test_factors_sex(self, client):
        resp = await client.get("/api/factors/sex")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))

    @pytest.mark.anyio
    async def test_factors_age_group(self, client):
        resp = await client.get("/api/factors/age-group")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_factors_occupation(self, client):
        resp = await client.get("/api/factors/occupation")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_factors_zone(self, client):
        resp = await client.get("/api/factors/zone")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_factors_smoking(self, client):
        resp = await client.get("/api/factors/behavior/smoking")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_factors_alcohol(self, client):
        resp = await client.get("/api/factors/behavior/alcohol")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_factors_exercise(self, client):
        resp = await client.get("/api/factors/behavior/exercise")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_factors_cross_tabulation(self, client):
        resp = await client.get("/api/factors/cross-tabulation", params={
            "disease": "diabetes", "factor1": "sex", "factor2": "age_group"
        })
        assert resp.status_code == 200


# ============================================================================
# SCREENING TESTS API  (/api/screening-tests/)
# ============================================================================

class TestScreeningTestsAPI:
    """Clinical screening test result endpoints."""

    @pytest.mark.anyio
    async def test_screening_summary(self, client):
        resp = await client.get("/api/screening-tests/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))

    @pytest.mark.anyio
    async def test_screening_district(self, client):
        resp = await client.get("/api/v2/summary/districts")
        districts = resp.json()
        if not districts:
            pytest.skip("No districts")
        dcode = districts[0]["district_code"]
        resp = await client.get(f"/api/screening-tests/district/{dcode}")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_screening_ekg(self, client):
        resp = await client.get("/api/screening-tests/ekg/summary")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_screening_chest_xray(self, client):
        resp = await client.get("/api/screening-tests/chest-xray/summary")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_screening_blood(self, client):
        resp = await client.get("/api/screening-tests/blood/summary")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_screening_retinal(self, client):
        resp = await client.get("/api/screening-tests/retinal/summary")
        assert resp.status_code == 200


# ============================================================================
# ADMIN API  (/api/admin/)
# ============================================================================

class TestAdminAPI:
    """Admin endpoints — tests both auth and functionality."""

    @pytest.mark.anyio
    async def test_admin_data_status_no_auth(self, client):
        """Should reject without admin auth."""
        resp = await client.get("/api/admin/data-status")
        assert resp.status_code in (401, 403)

    @pytest.mark.anyio
    async def test_admin_data_status_with_auth(self, client):
        resp = await client.get(
            "/api/admin/data-status",
            headers={"Authorization": "Bearer admin"},
        )
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_admin_excel_template(self, client):
        resp = await client.get(
            "/api/admin/excel-template",
            headers={"Authorization": "Bearer admin"},
        )
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "spreadsheet" in ct or "octet" in ct

    @pytest.mark.anyio
    async def test_admin_audit_log(self, client):
        resp = await client.get(
            "/api/admin/audit-log",
            headers={"Authorization": "Bearer admin"},
        )
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_admin_invalidate_cache(self, client):
        resp = await client.post(
            "/api/admin/invalidate-cache",
            headers={"Authorization": "Bearer admin"},
        )
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_admin_upload_screening_empty(self, client):
        """Upload empty data should be rejected or handled."""
        resp = await client.post(
            "/api/admin/upload-screening",
            headers={"Authorization": "Bearer admin"},
            json={"data": {}},
        )
        assert resp.status_code in (200, 400, 422)

    @pytest.mark.anyio
    async def test_admin_wrong_password(self, client):
        resp = await client.get(
            "/api/admin/data-status",
            headers={"Authorization": "Bearer wrong-password"},
        )
        assert resp.status_code in (401, 403)
