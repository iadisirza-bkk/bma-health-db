"""Data-access layer (ADR-01).

Repositories are the only place SQL is run. Routers/services must go through
a Repository — never call psycopg2 directly.
"""
from __future__ import annotations

from .base import QueryNotFound, Repository  # noqa: F401
from .mv_repository import MVRepository  # noqa: F401

__all__ = ["MVRepository", "Repository", "QueryNotFound"]
