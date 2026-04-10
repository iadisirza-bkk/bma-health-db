"""
FastAPI dependency injection — creates repository and service instances.
"""
from __future__ import annotations

from functools import lru_cache

from database import execute_query, execute_scalar


@lru_cache(maxsize=1)
def get_query_funcs():
    """Return the database query functions (singleton)."""
    return execute_query, execute_scalar
