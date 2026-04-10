"""
Tests for Phase 3: Cache, error handling, and monitoring.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


# ---------------------------------------------------------------------------
# Cache module tests (works even without Redis running)
# ---------------------------------------------------------------------------

def test_cache_get_returns_none_when_redis_down():
    from cache import cache_get
    result = cache_get("nonexistent_key")
    assert result is None


def test_cache_set_does_not_crash_when_redis_down():
    from cache import cache_set
    # Should silently pass through
    cache_set("test_key", {"hello": "world"}, ttl=60)


def test_cache_stats_reports_unavailable():
    from cache import cache_stats
    stats = cache_stats()
    assert "available" in stats
    # Redis not running in test env
    assert stats["available"] is False


def test_cache_flush_returns_false_when_redis_down():
    from cache import cache_flush_all
    result = cache_flush_all()
    assert result is False


def test_cached_decorator_passthrough():
    """@cached decorator should call function normally when Redis is down."""
    from cache import cached, TTL_T2_AGGREGATE

    call_count = 0

    @cached("test_func", ttl=TTL_T2_AGGREGATE)
    def my_func(x):
        nonlocal call_count
        call_count += 1
        return {"value": x}

    result1 = my_func(42)
    result2 = my_func(42)
    assert result1 == {"value": 42}
    assert result2 == {"value": 42}
    # Without Redis, function is called every time (no caching)
    assert call_count == 2


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

def test_error_classes():
    from errors import (
        BMAException, NotFoundError, DataSuppressedError,
        DataNotAvailableError, InvalidParameterError, ExternalAPIError,
    )

    e = NotFoundError("District", "9999")
    assert e.status_code == 404
    assert "9999" in e.message

    e = DataSuppressedError()
    assert e.status_code == 200  # Not a real error
    assert "k-anonymity" in e.message

    e = DataNotAvailableError("food_preference_sweet")
    assert e.error_code == "DATA_NOT_AVAILABLE"

    e = InvalidParameterError("disease_key", "invalid", ["diabetes", "hypertension"])
    assert e.status_code == 400
    assert "invalid" in e.message

    e = ExternalAPIError("ArcGIS", "timeout")
    assert e.status_code == 502


# ---------------------------------------------------------------------------
# Integration tests for new endpoints
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_health_check_includes_cache(public_client):
    resp = await public_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "cache" in body
    assert "available" in body["cache"]


@pytest.mark.anyio
async def test_cache_stats_endpoint(client):
    resp = await client.get("/api/v2/monitoring/cache-stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "available" in body


@pytest.mark.anyio
async def test_global_error_handler_invalid_route(client):
    """Non-existent route should return 404, not 500."""
    resp = await client.get("/api/v2/nonexistent")
    assert resp.status_code in (404, 405)


@pytest.mark.anyio
async def test_overview_caching_passthrough(client):
    """Overview should work even with Redis down (passthrough)."""
    resp1 = await client.get("/api/v2/summary/overview")
    resp2 = await client.get("/api/v2/summary/overview")
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Both should return same data
    assert resp1.json()["total_screened"] == resp2.json()["total_screened"]
