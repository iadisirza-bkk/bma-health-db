"""
Full coverage tests for V2 API endpoints that were previously untested.

Covers: Summary (lab, mental-health, demographics), Zones (detail),
Epidemiology, KPI, Executive, Promotion, Disease Control, Facility,
Strategy, Research, Public, Monitoring, GIS.
"""
import pytest


# ============================================================================
# Helper
# ============================================================================

async def _first_dcode(client):
    resp = await client.get("/api/v2/summary/districts")
    districts = resp.json()
    if not districts:
        pytest.skip("No districts in database")
    return districts[0]["district_code"]


# ============================================================================
# SUMMARY — remaining endpoints
# ============================================================================

class TestSummaryFull:

    @pytest.mark.anyio
    async def test_summary_lab(self, client):
        resp = await client.get("/api/v2/summary/lab")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_summary_lab_by_zone(self, client):
        resp = await client.get("/api/v2/summary/lab", params={"zone_code": "01"})
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_summary_mental_health(self, client):
        resp = await client.get("/api/v2/summary/mental-health")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_summary_mental_health_by_zone(self, client):
        resp = await client.get("/api/v2/summary/mental-health", params={"zone_code": "02"})
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_summary_demographics(self, client):
        resp = await client.get("/api/v2/summary/demographics")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_summary_demographics_by_dcode(self, client):
        dcode = await _first_dcode(client)
        resp = await client.get("/api/v2/summary/demographics", params={"dcode": dcode})
        assert resp.status_code in (200, 403)

    @pytest.mark.anyio
    async def test_summary_zone_detail(self, client):
        resp = await client.get("/api/v2/summary/zones/01")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_summary_zone_not_found(self, client):
        resp = await client.get("/api/v2/summary/zones/99")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_summary_district_disease(self, client):
        dcode = await _first_dcode(client)
        resp = await client.get(f"/api/v2/summary/districts/{dcode}/disease/diabetes")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_summary_district_disease_invalid(self, client):
        dcode = await _first_dcode(client)
        resp = await client.get(f"/api/v2/summary/districts/{dcode}/disease/fake_disease")
        assert resp.status_code == 400


# ============================================================================
# EPIDEMIOLOGY — full coverage
# ============================================================================

class TestEpidemiologyFull:

    @pytest.mark.anyio
    async def test_age_group_prevalence(self, client):
        resp = await client.get("/api/v2/epidemiology/age-group-prevalence")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_incidence_rate(self, client):
        resp = await client.get("/api/v2/epidemiology/incidence-rate")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_outbreak_detection(self, client):
        resp = await client.get("/api/v2/epidemiology/outbreak-detection")
        assert resp.status_code == 200


# ============================================================================
# KPI — full coverage
# ============================================================================

class TestKPIFull:

    @pytest.mark.anyio
    async def test_screening_yield(self, client):
        resp = await client.get("/api/v2/kpi/screening-yield")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_control_rates(self, client):
        resp = await client.get("/api/v2/kpi/control-rates")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_zone_comparison(self, client):
        resp = await client.get("/api/v2/kpi/zone-comparison")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_progress_tracker(self, client):
        resp = await client.get("/api/v2/kpi/progress-tracker")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_benchmark(self, client):
        resp = await client.get("/api/v2/kpi/benchmark")
        assert resp.status_code == 200


# ============================================================================
# EXECUTIVE — full coverage
# ============================================================================

class TestExecutiveFull:

    @pytest.mark.anyio
    async def test_yoy_comparison(self, client):
        resp = await client.get("/api/v2/executive/yoy-comparison")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_campaign_impact(self, client):
        resp = await client.get("/api/v2/executive/campaign-impact")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_media_brief(self, client):
        resp = await client.get("/api/v2/executive/media-brief")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_executive_alert(self, client):
        resp = await client.get("/api/v2/executive/alert")
        assert resp.status_code == 200


# ============================================================================
# PROMOTION — full coverage
# ============================================================================

class TestPromotionFull:

    @pytest.mark.anyio
    async def test_behavior_disease_correlation(self, client):
        resp = await client.get("/api/v2/promotion/behavior-disease-correlation")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_risk_factor_profile(self, client):
        resp = await client.get("/api/v2/promotion/risk-factor-profile")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_exercise_frequency(self, client):
        resp = await client.get("/api/v2/promotion/exercise-frequency")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_waist_risk_analysis(self, client):
        resp = await client.get("/api/v2/promotion/waist-risk-analysis")
        assert resp.status_code == 200


# ============================================================================
# DISEASE CONTROL — full coverage
# ============================================================================

class TestDiseaseControlFull:

    @pytest.mark.anyio
    async def test_repeat_screening(self, client):
        resp = await client.get("/api/v2/disease-control/repeat-screening")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_progression(self, client):
        resp = await client.get("/api/v2/disease-control/progression")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_referral_outcome(self, client):
        resp = await client.get("/api/v2/disease-control/referral-outcome")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_treatment_compliance(self, client):
        resp = await client.get("/api/v2/disease-control/treatment-compliance")
        assert resp.status_code == 200


# ============================================================================
# FACILITY — full coverage
# ============================================================================

class TestFacilityFull:

    @pytest.mark.anyio
    async def test_workload(self, client):
        resp = await client.get("/api/v2/facility/workload")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_screening_yield_rank(self, client):
        resp = await client.get("/api/v2/facility/screening-yield-rank")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_capacity_planning(self, client):
        resp = await client.get("/api/v2/facility/capacity-planning")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_comparison(self, client):
        resp = await client.get("/api/v2/facility/comparison", params={"code1": "00001", "code2": "00002"})
        assert resp.status_code in (200, 404, 422)


# ============================================================================
# STRATEGY — full coverage
# ============================================================================

class TestStrategyFull:

    @pytest.mark.anyio
    async def test_budget_allocation(self, client):
        resp = await client.get("/api/v2/strategy/budget-allocation-model")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_roi_analysis(self, client):
        resp = await client.get("/api/v2/strategy/roi-analysis")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_resource_optimization(self, client):
        resp = await client.get("/api/v2/strategy/resource-optimization")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_projected_savings(self, client):
        resp = await client.get("/api/v2/strategy/projected-savings")
        assert resp.status_code == 200


# ============================================================================
# RESEARCH — full coverage
# ============================================================================

class TestResearchFull:

    @pytest.mark.anyio
    async def test_statistical_test(self, client):
        resp = await client.get("/api/v2/research/statistical-test")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_correlation_matrix(self, client):
        resp = await client.get("/api/v2/research/correlation-matrix")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_sample_size_calculator(self, client):
        resp = await client.get("/api/v2/research/sample-size-calculator")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_export(self, client):
        resp = await client.get("/api/v2/research/export")
        assert resp.status_code == 200


# ============================================================================
# PUBLIC — full coverage
# ============================================================================

class TestPublicFull:

    @pytest.mark.anyio
    async def test_screening_locations(self, client):
        resp = await client.get("/api/v2/public/screening-locations")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_service_satisfaction(self, client):
        resp = await client.get("/api/v2/public/service-satisfaction")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_complaint_status(self, client):
        resp = await client.get("/api/v2/public/complaint-status")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_open_data(self, client):
        resp = await client.get("/api/v2/public/open-data")
        assert resp.status_code == 200


# ============================================================================
# MONITORING — full coverage
# ============================================================================

class TestMonitoringFull:

    @pytest.mark.anyio
    async def test_data_quality(self, client):
        resp = await client.get("/api/v2/monitoring/data-quality")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_cleansing_report(self, client):
        resp = await client.get("/api/v2/monitoring/cleansing-report")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_api_performance(self, client):
        resp = await client.get("/api/v2/monitoring/api-performance")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_audit_log(self, client):
        resp = await client.get("/api/v2/monitoring/audit-log")
        assert resp.status_code == 200


# ============================================================================
# GIS — remaining endpoints
# ============================================================================

class TestGISFull:

    @pytest.mark.anyio
    async def test_gis_single_facility(self, client):
        resp = await client.get("/api/v2/gis/facilities", params={"limit": 1})
        assert resp.status_code == 200
        data = resp.json()
        # Response may be a list or a dict with "facilities" key
        if isinstance(data, list):
            facilities = data
        elif isinstance(data, dict) and "facilities" in data:
            facilities = data["facilities"]
        else:
            facilities = list(data.values()) if isinstance(data, dict) else []
        if not facilities:
            pytest.skip("No facilities")
        first = facilities[0] if isinstance(facilities, list) else facilities
        code = first.get("code") or first.get("facility_code", "") if isinstance(first, dict) else ""
        if code:
            resp2 = await client.get(f"/api/v2/gis/facilities/{code}")
            assert resp2.status_code in (200, 404)

    @pytest.mark.anyio
    async def test_gis_boundaries(self, client):
        resp = await client.get("/api/v2/gis/boundaries/districts")
        assert resp.status_code == 200
