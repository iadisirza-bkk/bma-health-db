"""File-backed PDF/HTML cache keyed by content hash (S9 Task 1.2).

Layout
------
::

    <root>/                        (default: ``<REPORTS_DIR>/.pdf_cache``)
        manifest.sqlite             (one row per cached artefact)
        a1b2c3d4e5f6a7b8.pdf        (artefact, named by content_hash)
        f1e2d3c4b5a69788.html

SQLite manifest schema
----------------------
::

    CREATE TABLE manifest (
        hash TEXT PRIMARY KEY,
        report_id TEXT NOT NULL,
        fmt TEXT NOT NULL,
        lang TEXT NOT NULL,
        audience TEXT,             -- comma-list or NULL
        params_json TEXT,
        data_version TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL,  -- ISO-8601 UTC
        last_served_at TEXT,
        descriptor_mtime REAL DEFAULT 0
    );
    CREATE INDEX idx_manifest_lookup ON manifest(report_id, fmt, lang);

The implementation is intentionally process-safe: SQLite handles
concurrent readers, and writes go through a per-process ``Lock`` so we
don't race the ``put`` codepath when two requests arrive at the same
hash before the first one finishes (rare but possible under burst load).

Importable
----------
``api.services.reports.pdf_cache.PdfCache`` — Agent 2's bulk pre-build
orchestrator uses the same class to populate the cache.

Backward compatibility
----------------------
This module replaces the JSON-lines stub Agent 2 dropped while the real
implementation was in flight. We keep the same public surface
(``PdfCache(root)``, ``get(hash)``, ``put(hash, source_path, **meta)``,
``list_for_report``, ``get_pdf_cache``, ``ManifestRow``) so any code
written against the stub keeps working — only the on-disk format
changes (manifest.json → manifest.sqlite).
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Mapping, Optional

logger = logging.getLogger("api.services.reports.pdf_cache")


# ---------------------------------------------------------------------------
# Format → on-disk extension. Aliased per ``services.reports.format_alias``
# so ``latex`` and ``pdf`` both end up as ``.pdf``.
# ---------------------------------------------------------------------------


_FMT_EXT: Mapping[str, str] = {
    "pdf": ".pdf",
    "latex": ".pdf",
    "html": ".html",
    "pptx": ".pptx",
}


def _ext_for(fmt: str) -> str:
    """Map a format to its on-disk extension. Unknown formats fall back to
    ``.<fmt>`` so we never crash on a never-seen-before format name."""
    return _FMT_EXT.get(fmt, f".{fmt}" if fmt else "")


def _now_iso() -> str:
    """Timezone-aware UTC ISO-8601 to the second — fits the manifest column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public dataclass — used by ``list_for_report`` callers (FE / Agent 2).
# Kept JSON-friendly so the existing Agent 2 stubs that round-trip through
# ``asdict`` keep working.
# ---------------------------------------------------------------------------


@dataclass
class ManifestRow:
    """One row in the cache manifest. Pure data — JSON round-trip safe."""

    hash: str
    report_id: str
    fmt: str
    lang: str
    audience: Optional[List[str]] = None
    params: Optional[dict] = None
    path: str = ""                 # absolute path to the cached artefact
    bytes: int = 0
    created_at: str = ""           # ISO-8601, UTC
    data_version: str = ""
    descriptor_mtime: float = 0.0
    last_served_at: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "ManifestRow":
        # Defensive: only pass known keys so future manifests with extra
        # columns don't break older readers.
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# PdfCache — file-backed cache + SQLite manifest.
# ---------------------------------------------------------------------------


class PdfCache:
    """File-backed cache keyed by content_hash + SQLite manifest.

    Parameters
    ----------
    root :
        Directory holding the cached files + ``manifest.sqlite``. Created
        if absent.

    Notes
    -----
    Per the S9 design, ``put`` COPIES the source file (via
    ``shutil.copy2``) — the renderer's tmp-dir cleanup might delete the
    original any moment. The copy preserves mtime so cache GC has a
    sensible age signal even when ``last_served_at`` is NULL.
    """

    DEFAULT_SUBDIR = ".pdf_cache"

    def __init__(self, root: Optional[Path] = None, *, cache_dir: Optional[Path] = None) -> None:
        # ``cache_dir`` is the spec name; ``root`` is the legacy stub name.
        # Accept both so any caller in either flavour keeps working.
        chosen = root if root is not None else cache_dir
        if chosen is None:
            raise TypeError(
                "PdfCache.__init__ requires `root` (or alias `cache_dir`)"
            )
        self.root = Path(chosen)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.sqlite"
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Create ``manifest`` + index if they don't already exist."""
        with closing(sqlite3.connect(self.manifest_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS manifest (
                    hash TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL,
                    fmt TEXT NOT NULL,
                    lang TEXT NOT NULL,
                    audience TEXT,
                    params_json TEXT,
                    data_version TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_served_at TEXT,
                    descriptor_mtime REAL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_manifest_lookup
                    ON manifest(report_id, fmt, lang);
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Connection helper — short-lived; SQLite handles concurrent readers.
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.manifest_path)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Path helper
    # ------------------------------------------------------------------

    def _path_for(self, hash: str, fmt: str) -> Path:
        return self.root / f"{hash}{_ext_for(fmt)}"

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, hash: str) -> Optional[Path]:
        """Return the on-disk path for a cached artefact, or ``None``.

        Drops the manifest row if the file is missing on disk so the
        next ``put`` starts clean.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT fmt FROM manifest WHERE hash = ?", (hash,)
            ).fetchone()
        if row is None:
            return None
        path = self._path_for(hash, row["fmt"])
        if not path.exists():
            logger.warning(
                "PdfCache: manifest hit but file missing (%s); evicting row",
                path,
            )
            with self._lock, self._conn() as conn:
                conn.execute("DELETE FROM manifest WHERE hash = ?", (hash,))
                conn.commit()
            return None
        return path

    def touch(self, hash: str) -> None:
        """Update ``last_served_at`` after a cache hit.

        Best-effort — failures are logged but never bubble to the caller.
        The router uses this to keep popularity stats fresh.
        """
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    "UPDATE manifest SET last_served_at = ? WHERE hash = ?",
                    (_now_iso(), hash),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.debug("PdfCache.touch failed: %s", exc)

    def _lookup(self, hash: str) -> Optional[ManifestRow]:
        """Return the full :class:`ManifestRow` for ``hash`` (or None)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT hash, report_id, fmt, lang, audience, params_json,
                       data_version, size_bytes, created_at, last_served_at,
                       descriptor_mtime
                FROM manifest WHERE hash = ?
                """,
                (hash,),
            ).fetchone()
        return self._row_to_manifest(row) if row is not None else None

    def _row_to_manifest(self, row: sqlite3.Row) -> ManifestRow:
        """Project a ``sqlite3.Row`` onto :class:`ManifestRow`."""
        audience_str = row["audience"]
        audience: Optional[List[str]]
        if audience_str:
            audience = [a for a in audience_str.split(",") if a]
        else:
            audience = None

        params_json = row["params_json"]
        params: Optional[dict]
        if params_json:
            try:
                params = json.loads(params_json)
            except json.JSONDecodeError:
                params = None
        else:
            params = None

        return ManifestRow(
            hash=row["hash"],
            report_id=row["report_id"],
            fmt=row["fmt"],
            lang=row["lang"],
            audience=audience,
            params=params,
            path=str(self._path_for(row["hash"], row["fmt"])),
            bytes=int(row["size_bytes"]),
            created_at=row["created_at"],
            data_version=row["data_version"],
            descriptor_mtime=float(row["descriptor_mtime"] or 0.0),
            last_served_at=row["last_served_at"],
        )

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def put(
        self,
        hash: str,
        source_path: Path,
        *,
        report_id: str = "",
        fmt: str = "",
        lang: str = "",
        audience: Optional[Iterable[str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        data_version: str = "",
        size_bytes: Optional[int] = None,
        descriptor_mtime: float = 0.0,
        **_extra: Any,
    ) -> None:
        """Copy ``source_path`` into the cache and record manifest row.

        Idempotent: a second ``put`` for the same hash overwrites the
        manifest row + file (handy when Agent 2's pre-build re-runs).

        Returns ``None`` (the on-disk path is recoverable via
        :meth:`get` or :meth:`_path_for`).
        """
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(
                f"PdfCache.put: source artefact does not exist: {source}"
            )

        # If caller didn't tell us the fmt, fall back to the source's
        # suffix — keeps backward compat with the Agent 2 stub which
        # used ``source_path.suffix`` directly.
        chosen_fmt = fmt or source.suffix.lstrip(".") or "pdf"
        target = self._path_for(hash, chosen_fmt)
        target.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            # Copy first; bail out before touching the manifest if the
            # source disappears mid-flight.
            shutil.copy2(source, target)
            actual_size = (
                int(size_bytes)
                if size_bytes is not None
                else target.stat().st_size
            )

            # Canonicalise audience / params for stable manifest entries.
            audience_str: Optional[str]
            if audience:
                audience_list = sorted(
                    {str(a) for a in audience if str(a).strip()}
                )
                audience_str = ",".join(audience_list) if audience_list else None
            else:
                audience_str = None

            params_json: Optional[str]
            if params:
                try:
                    params_json = json.dumps(
                        dict(params), sort_keys=True, separators=(",", ":")
                    )
                except TypeError:
                    params_json = json.dumps(
                        {k: str(v) for k, v in dict(params).items()},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
            else:
                params_json = None

            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO manifest (
                        hash, report_id, fmt, lang, audience, params_json,
                        data_version, size_bytes, created_at,
                        last_served_at, descriptor_mtime
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(hash) DO UPDATE SET
                        report_id=excluded.report_id,
                        fmt=excluded.fmt,
                        lang=excluded.lang,
                        audience=excluded.audience,
                        params_json=excluded.params_json,
                        data_version=excluded.data_version,
                        size_bytes=excluded.size_bytes,
                        created_at=excluded.created_at,
                        descriptor_mtime=excluded.descriptor_mtime
                    """,
                    (
                        hash,
                        str(report_id),
                        str(chosen_fmt),
                        str(lang),
                        audience_str,
                        params_json,
                        str(data_version),
                        actual_size,
                        _now_iso(),
                        float(descriptor_mtime),
                    ),
                )
                conn.commit()

        logger.info(
            "PdfCache.put hash=%s report_id=%s fmt=%s lang=%s size=%d -> %s",
            hash, report_id, chosen_fmt, lang, actual_size, target,
        )

    # ------------------------------------------------------------------
    # GC + introspection
    # ------------------------------------------------------------------

    def evict_older_than(self, days: int) -> int:
        """Drop manifest rows + files whose ``created_at`` is older
        than ``days`` days. Returns the number of rows dropped.

        We do the cutoff in Python (single pass) instead of fighting
        SQLite's date arithmetic — our timestamps include offsets.
        """
        if days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        dropped = 0
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT hash, fmt, created_at FROM manifest"
            ).fetchall()
            for row in rows:
                try:
                    ts = datetime.fromisoformat(row["created_at"]).timestamp()
                except ValueError:
                    # Garbled date — drop defensively.
                    ts = 0
                if ts < cutoff:
                    path = self._path_for(row["hash"], row["fmt"])
                    if path.exists():
                        try:
                            path.unlink()
                        except OSError:
                            logger.debug(
                                "PdfCache.evict: unlink failed: %s", path,
                            )
                    conn.execute(
                        "DELETE FROM manifest WHERE hash = ?",
                        (row["hash"],),
                    )
                    dropped += 1
            conn.commit()
        if dropped:
            logger.info(
                "PdfCache.evict_older_than(%d) dropped %d row(s)", days, dropped,
            )
        return dropped

    def delete(self, hash: str) -> bool:
        """Remove the artefact + manifest row for ``hash``. Returns True iff
        something was removed."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT fmt FROM manifest WHERE hash = ?", (hash,)
            ).fetchone()
            if row is None:
                return False
            path = self._path_for(hash, row["fmt"])
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            conn.execute("DELETE FROM manifest WHERE hash = ?", (hash,))
            conn.commit()
        return True

    def all_rows(self) -> List[ManifestRow]:
        """Every manifest row, newest first. Used by FE admin panels."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT hash, report_id, fmt, lang, audience, params_json,
                       data_version, size_bytes, created_at, last_served_at,
                       descriptor_mtime
                FROM manifest
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [self._row_to_manifest(r) for r in rows]

    def list_for_report(self, report_id: str) -> List[ManifestRow]:
        """Every manifest row for a given ``report_id`` (any fmt/lang)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT hash, report_id, fmt, lang, audience, params_json,
                       data_version, size_bytes, created_at, last_served_at,
                       descriptor_mtime
                FROM manifest
                WHERE report_id = ?
                ORDER BY created_at DESC
                """,
                (report_id,),
            ).fetchall()
        return [self._row_to_manifest(r) for r in rows]

    def stats(self) -> dict:
        """Aggregate stats for ops dashboards / FE admin panels.

        Returns
        -------
        dict
            Keys: ``total_files``, ``total_bytes``, ``oldest`` (ISO date
            string or None), ``newest``, ``by_report`` (dict from
            ``report_id`` to ``{files, bytes}``), ``root`` (cache root).
            ``oldest_entry`` / ``newest_entry`` are aliased so callers
            built against the legacy JSON-lines stub stay compatible.
        """
        with self._conn() as conn:
            agg = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_files,
                    COALESCE(SUM(size_bytes), 0) AS total_bytes,
                    MIN(created_at) AS oldest,
                    MAX(created_at) AS newest
                FROM manifest
                """
            ).fetchone()
            by_report_rows = conn.execute(
                """
                SELECT report_id, COUNT(*) AS files,
                       COALESCE(SUM(size_bytes), 0) AS bytes
                FROM manifest
                GROUP BY report_id
                """
            ).fetchall()
        by_report = {
            (r["report_id"] or "(unknown)"): {
                "files": int(r["files"]),
                "bytes": int(r["bytes"]),
            }
            for r in by_report_rows
        }
        return {
            "total_files": int(agg["total_files"]),
            "total_bytes": int(agg["total_bytes"]),
            "oldest": agg["oldest"],
            "newest": agg["newest"],
            # Legacy aliases — Agent 2's tests / admin endpoints used these.
            "oldest_entry": agg["oldest"],
            "newest_entry": agg["newest"],
            "by_report": by_report,
            "root": str(self.root),
        }


# ---------------------------------------------------------------------------
# Singleton helper — mirrors get_report_service().
# ---------------------------------------------------------------------------


_CACHE: Optional[PdfCache] = None


def get_pdf_cache(root: Optional[Path] = None) -> PdfCache:
    """Return the process-wide :class:`PdfCache` singleton.

    ``root`` overrides the default (``<REPORTS_DIR>/.pdf_cache``).
    Passing a non-None ``root`` re-initialises the singleton — handy for
    tests that want a tmp_path-rooted cache.
    """
    global _CACHE
    if _CACHE is None or root is not None:
        if root is None:
            try:
                import config
                root = Path(config.REPORTS_DIR) / PdfCache.DEFAULT_SUBDIR
            except Exception:
                root = Path("/tmp/bma-pdf-cache")
        _CACHE = PdfCache(Path(root))
    return _CACHE


def reset_pdf_cache_singleton() -> None:
    """Test helper — drop the cached singleton so the next call rebuilds."""
    global _CACHE
    _CACHE = None


__all__ = [
    "PdfCache",
    "ManifestRow",
    "get_pdf_cache",
    "reset_pdf_cache_singleton",
]
