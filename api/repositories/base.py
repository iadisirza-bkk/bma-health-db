"""
Base repository providing database query methods.
All repositories inherit from this class.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional


class BaseRepository:
    """Base class for all data repositories.

    Accepts query callables via constructor — this allows both the API
    (using database.py pool) and MCP server (using its own pool) to
    share the same repository logic.
    """

    def __init__(
        self,
        execute_query: Callable[..., List[Dict]],
        execute_scalar: Callable[..., any],
    ):
        self._q = execute_query
        self._s = execute_scalar

    def _build_where(
        self,
        conditions: list[str],
        params: list,
        *,
        district: Optional[str] = None,
        zone_code: Optional[str] = None,
        sex: Optional[int] = None,
        age_group: Optional[str] = None,
        district_col: str = "district_code",
        zone_col: str = "zone_code",
    ) -> tuple[str, list]:
        """Build WHERE clause from common filters. Returns (clause, params)."""
        if district:
            conditions.append(f"{district_col} = %s")
            params.append(district)
        if zone_code:
            conditions.append(f"{zone_col} = %s")
            params.append(zone_code)
        if sex is not None:
            conditions.append("sex = %s")
            params.append(sex)
        if age_group:
            conditions.append("age_group = %s")
            params.append(age_group)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params
