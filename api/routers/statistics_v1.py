"""Statistics API router for Bangkok health screening data analysis.

Sync port — uses statistics_service functions backed by psycopg2 via data_adapter.
"""

from fastapi import APIRouter, HTTPException, Query

from services import statistics_service

router = APIRouter(prefix="/api/stats", tags=["statistics"])


@router.get("/district/{dcode}")
def get_district_stats(dcode: str):
    """Get descriptive statistics for all indicators in a district."""
    try:
        return statistics_service.descriptive_stats(dcode)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/compare")
def compare_districts(
    district1: str = Query(..., description="First district code"),
    district2: str = Query(..., description="Second district code"),
):
    """Compare two districts side-by-side with statistical significance testing."""
    if district1 == district2:
        raise HTTPException(status_code=400, detail="Cannot compare a district with itself")
    try:
        return statistics_service.compare_districts(district1, district2)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/zone/{zone_code}")
def get_zone_summary(zone_code: str):
    """Get aggregated statistics for a zone (weighted by screened population)."""
    try:
        return statistics_service.zone_summary(zone_code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/city")
def get_city_overview():
    """Get Bangkok-wide summary statistics."""
    return statistics_service.city_overview()


@router.get("/ranking/{disease}")
def get_risk_ranking(disease: str):
    """Rank all districts by pct_at_risk for a given disease."""
    try:
        return statistics_service.risk_ranking(disease)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/trends/{dcode}/{disease}")
def get_trends(dcode: str, disease: str):
    """Get trend data for a district and disease (placeholder for future time-series)."""
    try:
        return statistics_service.trend_analysis(dcode, disease)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
