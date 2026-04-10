"""
Redis caching layer with 4-tier TTL strategy.
Fail-open: if Redis is unavailable, all operations silently pass through.

Tiers:
  T1:  5 min — External APIs (PM2.5, ArcGIS)
  T2: 15 min — Aggregate summaries (overview, zones, headline KPI)
  T3:  1 hour — Filtered queries (district detail, lab, trends)
  T4: 24 hours — Static/reference (district list, data dictionary, health tips)
"""
from __future__ import annotations

import json
import logging
from functools import wraps
from typing import Any, Callable, Optional

from config import REDIS_URL

logger = logging.getLogger("bma.cache")

# TTL tiers (seconds)
TTL_T1_EXTERNAL = 300       # 5 min
TTL_T2_AGGREGATE = 900      # 15 min
TTL_T3_FILTERED = 3600      # 1 hour
TTL_T4_STATIC = 86400       # 24 hours

_redis_client = None
_redis_available = False


def _get_redis():
    """Lazy-init Redis connection. Returns None if unavailable."""
    global _redis_client, _redis_available
    if _redis_client is not None:
        return _redis_client if _redis_available else None

    try:
        import redis
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        _redis_client.ping()
        _redis_available = True
        logger.info("Redis connected: %s", REDIS_URL.split("@")[-1] if "@" in REDIS_URL else REDIS_URL)
        return _redis_client
    except Exception as e:
        _redis_available = False
        logger.warning("Redis unavailable (%s) — cache disabled, all requests hit DB", e)
        return None


def cache_get(key: str) -> Optional[Any]:
    """Get a value from cache. Returns None if miss or Redis unavailable."""
    r = _get_redis()
    if r is None:
        return None
    try:
        val = r.get(f"bma:{key}")
        if val is not None:
            return json.loads(val)
    except Exception:
        pass
    return None


def cache_set(key: str, value: Any, ttl: int = TTL_T2_AGGREGATE) -> None:
    """Set a value in cache with TTL. Silently fails if Redis unavailable."""
    r = _get_redis()
    if r is None:
        return
    try:
        r.setex(f"bma:{key}", ttl, json.dumps(value, default=str))
    except Exception:
        pass


def cache_delete_pattern(pattern: str) -> int:
    """Delete all keys matching a pattern. Returns count deleted."""
    r = _get_redis()
    if r is None:
        return 0
    try:
        keys = list(r.scan_iter(f"bma:{pattern}"))
        if keys:
            return r.delete(*keys)
    except Exception:
        pass
    return 0


def cache_flush_all() -> bool:
    """Flush all bma:* keys. Called after materialized view refresh."""
    r = _get_redis()
    if r is None:
        return False
    try:
        keys = list(r.scan_iter("bma:*"))
        if keys:
            r.delete(*keys)
        logger.info("Cache flushed: %d keys deleted", len(keys))
        return True
    except Exception:
        return False


def cache_stats() -> dict:
    """Return cache statistics."""
    r = _get_redis()
    if r is None:
        return {"available": False, "message": "Redis not connected"}
    try:
        info = r.info("stats")
        key_count = r.dbsize()
        return {
            "available": True,
            "total_keys": key_count,
            "hits": info.get("keyspace_hits", 0),
            "misses": info.get("keyspace_misses", 0),
            "hit_rate_pct": round(
                100.0 * info.get("keyspace_hits", 0)
                / max(info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0), 1),
                1,
            ),
        }
    except Exception as e:
        return {"available": False, "message": str(e)}


# ---------------------------------------------------------------------------
# Decorator for easy cache-aside pattern
# ---------------------------------------------------------------------------


def cached(prefix: str, ttl: int = TTL_T2_AGGREGATE, key_func: Optional[Callable] = None):
    """Decorator that caches function results.

    Usage:
        @cached("overview", ttl=TTL_T2_AGGREGATE)
        def overview():
            return expensive_query()

        @cached("district", ttl=TTL_T3_FILTERED, key_func=lambda dcode: dcode)
        def district_detail(dcode):
            return expensive_query(dcode)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Build cache key
            if key_func:
                suffix = str(key_func(*args, **kwargs))
            else:
                # Auto-key from args
                parts = [str(a) for a in args] + [f"{k}={v}" for k, v in sorted(kwargs.items()) if v is not None]
                suffix = ":".join(parts) if parts else "default"

            cache_key = f"{prefix}:{suffix}"

            # Try cache
            hit = cache_get(cache_key)
            if hit is not None:
                return hit

            # Miss — call function
            result = func(*args, **kwargs)

            # Store in cache
            cache_set(cache_key, result, ttl)
            return result
        return wrapper
    return decorator
