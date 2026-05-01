"""``GET /metrics`` endpoint for Prometheus scrapers.

The route is intentionally **not** protected by the API-key middleware —
Prometheus scrapers don't carry custom headers. Public exposure is fine
because the metrics surface contains no PII (counts, latencies, status
labels — never raw query results or user IDs).

To make it public, ``/metrics`` is added to the ``_PUBLIC_PATHS`` set in
``api/security.py`` and to the audit-log skip list in ``api/main.py``
(scrapes happen every 15s and would drown the audit trail otherwise).
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from .metrics import CONTENT_TYPE_LATEST, generate_latest

prometheus_router = APIRouter(tags=["Monitoring"])


@prometheus_router.get(
    "/metrics",
    summary="Prometheus exposition endpoint",
    response_class=Response,
    include_in_schema=False,  # internal-only; not part of the public API surface
)
def metrics_endpoint() -> Response:
    """Return all registered metrics in the Prometheus text format.

    Cheap (microseconds) — safe to scrape on a 15-second interval. The
    response carries the official content-type so Prometheus parses it
    correctly; older scrapers that look for ``text/plain`` also accept it.
    """
    payload = generate_latest()
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


__all__ = ["prometheus_router"]
