"""Content-hash cache keys for the report renderer.

S9 — "Bulk Pre-build with Content-Hash Cache"
=============================================

Core principle::

    ข้อมูลเปลี่ยน → บิลด์ใหม่ / ข้อมูลไม่เปลี่ยน → อ่านของเก่า

There is no time-based "rebuild every 24h" — the cache key is a SHA-256
of every input that affects the rendered output. Same hash → identical
artefact → return the on-disk copy. Drift is impossible by construction.

Public surface
--------------
* :func:`content_hash` — pure helper that hashes the request shape.
* :func:`data_version` — DB roundtrip that summarises "the data" into
  a short hex string. The hash CHANGES whenever upstream MVs are
  refreshed or a new ``ingestion_batch`` row lands.
* :func:`data_version_human` — turn the data_version (or a raw timestamp)
  into a human-readable date for the PDF cover stamp.

Importable from :mod:`api.services.reports.cache_keys` so other agents
(Agent 2's bulk pre-build orchestrator) can reuse the same primitives
without duplicating logic.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger("api.services.reports.cache_keys")


# ---------------------------------------------------------------------------
# 15 MVs to summarise — see DATABASE.md §"Cold Materialised Views". The
# list is the union of the public-schema MVs the report blocks query.
# Adding a new MV here automatically participates in cache invalidation.
# ---------------------------------------------------------------------------
_MV_NAMES: tuple[str, ...] = (
    # Hot zone-level MVs
    "mv_demographics",
    "mv_kpi_tier1",
    "mv_lab_distribution",
    "mv_lifestyle",
    "mv_mental_health",
    "mv_ncd_diagnostic_report",
    "mv_ncd_diagnostic_zone",
    "mv_summary_districts",
    "mv_summary_global",
    "mv_summary_mental",
    "mv_summary_zones",
    "mv_visit_resolved",
    # Cold summaries
    "summary_disease_age_sex",
    "summary_district_disease",
    "summary_facility",
)


# ---------------------------------------------------------------------------
# content_hash — pure helper (no I/O). Same inputs → same hex digest.
# ---------------------------------------------------------------------------


def _canonical_audience(audience: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Sort + de-dup an audience iterable for stable hashing.

    ``None`` and the empty set both render the full report → both produce
    the SAME hash. Order of values must NOT matter
    (``{executive, clinician}`` and ``{clinician, executive}`` collapse).
    """
    if audience is None:
        return None
    items = sorted({str(a).strip() for a in audience if str(a).strip()})
    if not items:
        return None
    return items


def _canonical_params(params: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Sorted-keys, JSON-coercible dict for stable hashing.

    Values pass through ``json.dumps`` later — anything not JSON-able
    becomes ``str(...)`` so we never blow up on stray Decimals etc.
    """
    if not params:
        return None
    out: dict[str, Any] = {}
    for k in sorted(str(k) for k in params.keys()):
        v = params[k]
        try:
            json.dumps(v)
            out[k] = v
        except TypeError:
            out[k] = str(v)
    return out or None


def content_hash(
    *,
    report_id: str,
    fmt: str,
    lang: str,
    audience: Optional[Iterable[str]],
    params: Optional[Mapping[str, Any]],
    data_version: str,
    descriptor_mtime: float,
    gemma_version: str = "v0",
    code_version: Optional[str] = None,
) -> str:
    """SHA-256 of every input that affects the rendered output.

    Truncated to 16 hex chars so cache filenames stay short
    (``a1b2c3d4e5f6a7b8.pdf``). Collision risk at 16 hex chars is
    negligible for the size of the report cache (low thousands of files).

    Parameters
    ----------
    report_id : str
        Descriptor id (e.g. ``"zone"``, ``"whitepaper"``).
    fmt : str
        Output format (``"pdf"``, ``"html"``, ``"pptx"``). Aliases (e.g.
        ``"latex"`` → ``"pdf"``) MUST be canonicalised before being
        passed in — caller's responsibility.
    lang : str
        ``"th"`` or ``"en"``.
    audience : list[str] | None
        S8 audience filter — sorted + de-duped before hashing.
    params : Mapping[str, Any] | None
        Free-form descriptor parameters (e.g. ``{"zone_code": "03"}``).
        Sorted by key before hashing.
    data_version : str
        Hash that changes when the underlying data changes
        (see :func:`data_version`).
    descriptor_mtime : float
        ``Path.stat().st_mtime`` of the descriptor YAML — bumps the cache
        whenever the YAML is edited (regardless of data movement).
    gemma_version : str
        Bumped when the Gemma model used for AI insights is swapped.
        Cheap forward-compat.
    code_version : str, optional
        Hash of the rendering code (block files + LaTeX template files).
        When omitted, defers to :func:`code_version` to compute. Cache busts
        when ANY block's ``render_*`` or any template file is edited so
        users never see stale output after a code-only fix.

    Returns
    -------
    str
        Lowercase hex digest, exactly 16 chars long.

    Examples
    --------
    >>> a = content_hash(
    ...     report_id="zone", fmt="pdf", lang="th",
    ...     audience=None, params={"zone_code": "03"},
    ...     data_version="abc123", descriptor_mtime=1700000000.0,
    ... )
    >>> b = content_hash(
    ...     report_id="zone", fmt="pdf", lang="th",
    ...     audience=None, params={"zone_code": "03"},
    ...     data_version="abc123", descriptor_mtime=1700000000.0,
    ... )
    >>> a == b
    True
    """
    payload = {
        "report_id": str(report_id),
        "fmt": str(fmt),
        "lang": str(lang),
        "audience": _canonical_audience(audience),
        "params": _canonical_params(params),
        "data_version": str(data_version),
        # Round descriptor_mtime to micro precision — st_mtime can return
        # an arbitrarily long float on some filesystems and the trailing
        # noise doesn't reflect a real edit.
        "descriptor_mtime": round(float(descriptor_mtime), 6),
        "gemma_version": str(gemma_version),
        "code_version": str(
            code_version if code_version is not None else compute_code_version()
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# code_version — hash of the files whose edits should bust the cache.
# ---------------------------------------------------------------------------

# These paths are relative to the api package root. Changing any of them —
# block render code or LaTeX template — should produce different output
# and therefore needs the cache busted. We deliberately leave NON-rendering
# files (data_collector.py, registry, repositories) OUT — those affect
# data_version instead, and including them would over-invalidate.
_CODE_VERSION_PATHS = (
    "services/reports/blocks/chart.py",
    "services/reports/blocks/heading.py",
    "services/reports/blocks/paragraph.py",
    "services/reports/blocks/callout.py",
    "services/reports/blocks/cover_page.py",
    "services/reports/blocks/closing_page.py",
    "services/reports/blocks/audience_summary_people.py",
    "services/reports/blocks/audience_summary_executive.py",
    "services/reports/blocks/audience_summary_clinician.py",
    "services/reports/blocks/audience_summary_researcher.py",
    "services/reports/blocks/_chart_matplotlib.py",
    "services/reports/blocks/_stats_helpers.py",
    "services/reports/renderers/latex.py",
    "services/reports/renderers/html.py",
    "templates/latex/descriptor_latex_root.tex.j2",
    "templates/latex/bma_article_preamble.tex",
)

_CODE_VERSION_CACHE: Optional[str] = None
_CODE_VERSION_CACHED_AT_MTIME: float = 0.0


def compute_code_version(api_root: Optional[Path] = None) -> str:
    """Hash of all rendering source files' mtimes — busts the cache when
    any block render code or template file is edited. Cached in-process
    keyed by max-mtime so the per-request cost is one ``Path.stat()`` per
    file (fast; ~16 stat() calls / request).
    """
    global _CODE_VERSION_CACHE, _CODE_VERSION_CACHED_AT_MTIME
    if api_root is None:
        # cache_keys.py lives at api/services/reports/cache_keys.py — walk up
        # 2 levels to reach api/.
        api_root = Path(__file__).resolve().parent.parent.parent
    mtimes: list[float] = []
    for rel in _CODE_VERSION_PATHS:
        p = api_root / rel
        try:
            mtimes.append(p.stat().st_mtime)
        except FileNotFoundError:
            # Missing file is fine — counts as "not present" in the hash.
            mtimes.append(0.0)
    max_mtime = max(mtimes) if mtimes else 0.0
    if _CODE_VERSION_CACHE is not None and max_mtime == _CODE_VERSION_CACHED_AT_MTIME:
        return _CODE_VERSION_CACHE
    blob = json.dumps(
        [round(m, 6) for m in mtimes], separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()[:12]
    _CODE_VERSION_CACHE = digest
    _CODE_VERSION_CACHED_AT_MTIME = max_mtime
    return digest


# ---------------------------------------------------------------------------
# data_version — DB roundtrip that summarises "the data". Cheap-ish (one
# round trip with a small SQL union) and called once per request.
# ---------------------------------------------------------------------------


def data_version(conn: Any) -> str:
    """Compute a hash that changes whenever upstream data shifts.

    Inputs to the hash:
        * ``max(batch_id)`` from ``bma_med.ingestion_batch``
        * ``max(finished_at)`` from ``bma_med.ingestion_batch``
        * (mv_name, last_refresh_timestamp, row_count) for each of the
          15 MVs we care about — pulled from ``pg_stat_user_tables``
          (``last_analyze`` is the closest proxy for "last refresh"
          since plain ``REFRESH MATERIALIZED VIEW`` doesn't update
          ``pg_stat_all_tables.last_vacuum``).

    The function is intentionally robust: if the DB is unreachable or
    the schema differs, we fall back to a sentinel so renders STILL
    happen (they just always go through the renderer — never serve a
    stale cache because we couldn't hash the world).

    Parameters
    ----------
    conn :
        A psycopg2 connection (or anything with a ``cursor()`` ctx
        manager that yields a DB-API cursor). Read-only.

    Returns
    -------
    str
        16-char hex digest.

    Notes
    -----
    Tests inject a fake ``conn`` whose cursor returns canned rows, so
    we don't need a live DB to exercise the hashing logic.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(MAX(batch_id), 0) AS max_batch,
                    COALESCE(MAX(finished_at)::text, '') AS max_finished
                FROM bma_med.ingestion_batch
                """
            )
            row = cur.fetchone()
            max_batch = row[0] if row else 0
            max_finished = row[1] if row else ""

            # MV names + row counts are mutable state we want to fold in.
            # NB: ``relname`` is the MV's local name; ``schemaname`` filters
            # to ``public``. The 15 names we care about are a subset.
            cur.execute(
                """
                SELECT
                    schemaname,
                    relname,
                    n_live_tup,
                    COALESCE(last_analyze, last_autoanalyze)::text AS last_seen
                FROM pg_stat_user_tables
                WHERE schemaname = 'public'
                  AND relname = ANY(%s)
                ORDER BY relname
                """,
                (list(_MV_NAMES),),
            )
            mv_rows = cur.fetchall() or []
    except Exception as exc:
        logger.warning(
            "data_version: DB roundtrip failed (%s); using fallback hash",
            exc,
        )
        # Fallback — encode the time so successive calls drift forward,
        # which forces the cache to MISS rather than serve stale.
        # Bucketed to whole-minute granularity so two near-simultaneous
        # requests still hash the same way (avoid hot-path drift).
        bucket = int(datetime.now(timezone.utc).timestamp() // 60)
        return hashlib.sha256(
            f"fallback:{bucket}".encode("utf-8")
        ).hexdigest()[:16]

    payload = {
        "max_batch": int(max_batch),
        "max_finished": str(max_finished or ""),
        "mvs": [
            {
                "schema": r[0],
                "name": r[1],
                "rows": int(r[2] or 0),
                "last_seen": str(r[3] or ""),
            }
            for r in mv_rows
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# data_version_human — turn a data_version into a human-readable date.
# ---------------------------------------------------------------------------


def data_version_human(conn: Any) -> str:
    """Read ``MAX(finished_at)`` from ``bma_med.ingestion_batch`` and
    format it for the PDF cover stamp ("ข้อมูล ณ ...").

    The result is a Thai-friendly ISO-style date string ``YYYY-MM-DD``
    (or ``"ไม่ทราบ"`` if the table is empty / unreachable).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(finished_at) FROM bma_med.ingestion_batch"
            )
            row = cur.fetchone()
            if row and row[0]:
                ts = row[0]
                # psycopg2 returns timezone-aware datetimes for tstz cols.
                if hasattr(ts, "strftime"):
                    return ts.strftime("%Y-%m-%d")
                return str(ts)[:10]
    except Exception as exc:
        logger.warning("data_version_human: DB read failed: %s", exc)
    return "ไม่ทราบ"


# ---------------------------------------------------------------------------
# descriptor_mtime helper — small wrapper around Path.stat().st_mtime
# that gracefully degrades to 0.0 for in-memory descriptors.
# ---------------------------------------------------------------------------


def descriptor_mtime(path: Optional[Path]) -> float:
    """Return ``path.stat().st_mtime`` or ``0.0`` if unreadable.

    Returning a stable ``0.0`` for missing files means in-memory test
    descriptors hash deterministically without a real YAML on disk.
    """
    if path is None:
        return 0.0
    try:
        return float(Path(path).stat().st_mtime)
    except OSError:
        return 0.0


__all__ = [
    "content_hash",
    "data_version",
    "data_version_human",
    "descriptor_mtime",
]
