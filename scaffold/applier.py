"""End-to-end apply: take generated files into a live, working dashboard.

Steps performed by `apply()`:
  1. Copy migration SQL into the postgres container; run it with psql.
     (k-anon checks + REFRESH happen inside the migration's transaction.)
  2. Patch api/main.py to import + include_router the new disease.
     (Idempotent — checks for an existing line first.)
  3. Flush the Redis cache so the API's TTL'd entries don't mask new MV state.

The API container auto-reloads on file change (uvicorn --reload), and the
frontend dev server picks up the new TS hooks via HMR. So after apply()
the dashboard is live without any further intervention.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .diseases import DiseaseSpec

REPO_DB = Path("/Users/dev/bma-health-db")
MAIN_PY = REPO_DB / "api" / "main.py"

POSTGRES_CONTAINER = "bma-health-db"
REDIS_CONTAINER    = "bma-health-redis"
PG_USER, PG_DB     = "postgres", "bma_health"


def _run(cmd: list[str], *, input_bytes: bytes | None = None) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr)."""
    r = subprocess.run(cmd, input=input_bytes, capture_output=True)
    return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")


# ── Step 1: apply SQL migration ─────────────────────────────────────────

def apply_migration(spec: DiseaseSpec) -> bool:
    """Copy + run the migration SQL inside the postgres container.

    The migration's own BEGIN/COMMIT manages transactionality. Returns
    True on success.
    """
    sql_path = REPO_DB / "db" / "migrations" / f"{spec.migration_number}_mv_{spec.key}.sql"
    if not sql_path.exists():
        print(f"  ERROR: migration file not found at {sql_path}", file=sys.stderr)
        return False

    print(f"  Applying migration {sql_path.name}…")
    container_path = f"/tmp/{sql_path.name}"
    rc, _, err = _run(["docker", "cp", str(sql_path), f"{POSTGRES_CONTAINER}:{container_path}"])
    if rc != 0:
        print(f"  ERROR: docker cp failed: {err}", file=sys.stderr)
        return False

    rc, out, err = _run([
        "docker", "exec", POSTGRES_CONTAINER,
        "psql", "-U", PG_USER, "-d", PG_DB,
        "-v", "ON_ERROR_STOP=1",
        "-f", container_path,
    ])
    if rc != 0:
        print(f"  ERROR: psql apply failed:\n{err}", file=sys.stderr)
        return False

    # Surface a brief summary (DROP / CREATE / REFRESH counts).
    counts = {kw: out.count(kw) for kw in ("CREATE MATERIALIZED VIEW", "REFRESH MATERIALIZED VIEW", "ERROR")}
    print(f"  ✓ {counts['CREATE MATERIALIZED VIEW']} MV(s) created, "
          f"{counts['REFRESH MATERIALIZED VIEW']} refreshed, "
          f"{counts['ERROR']} error(s)")
    return counts["ERROR"] == 0


# ── Step 2: patch main.py ───────────────────────────────────────────────

def patch_main_py(spec: DiseaseSpec) -> bool:
    """Append the import + include_router line to api/main.py.

    Idempotent: detects an existing entry for this key and skips.
    Returns True if the file was modified.
    """
    if not MAIN_PY.exists():
        print(f"  WARN: {MAIN_PY} not found — skipping main.py patch")
        return False

    content = MAIN_PY.read_text()
    import_line   = f"from routers.{spec.key} import router as {spec.key}_router"
    include_line  = f"app.include_router({spec.key}_router)"

    if import_line in content and include_line in content:
        print(f"  main.py: {spec.key} already wired — skipping")
        return False

    # Insert import after the last `from routers.* import router as *_router` line.
    import_anchor = re.compile(r"(from routers\.\w+ import router as \w+_router\n)")
    matches = list(import_anchor.finditer(content))
    if matches and import_line not in content:
        last = matches[-1]
        content = content[:last.end()] + import_line + "\n" + content[last.end():]
    elif import_line not in content:
        print(f"  ERROR: could not find router import anchor in main.py", file=sys.stderr)
        return False

    # Insert include_router after the last `app.include_router(*_router)` line.
    include_anchor = re.compile(r"(app\.include_router\(\w+_router\)\n)")
    matches = list(include_anchor.finditer(content))
    if matches and include_line not in content:
        last = matches[-1]
        content = content[:last.end()] + include_line + "\n" + content[last.end():]
    elif include_line not in content:
        print(f"  ERROR: could not find include_router anchor in main.py", file=sys.stderr)
        return False

    MAIN_PY.write_text(content)
    print(f"  main.py: registered {spec.key}_router")
    return True


# ── Step 3: flush redis (scoped to bma:* keys only) ─────────────────────

def flush_redis() -> bool:
    """Delete only keys matching the ``bma:*`` prefix.

    The previous implementation ran ``FLUSHDB`` which nukes the entire Redis
    DB — unsafe in any environment where Redis is shared with other apps or
    where another team holds keys under different prefixes. The cache module
    (``api/cache.py``) prefixes every BMA key with ``bma:``, so scanning that
    prefix and ``UNLINK``-ing the matches is correct and surgical.
    """
    print("  Flushing BMA-scoped Redis keys (bma:*)…")
    # SCAN + UNLINK pipeline. `-r` makes xargs a no-op when SCAN returns 0
    # keys (BusyBox-portable equivalent of GNU `--no-run-if-empty`).
    cmd = "redis-cli --scan --pattern 'bma:*' | xargs -r redis-cli unlink"
    rc, out, err = _run(["docker", "exec", REDIS_CONTAINER, "sh", "-c", cmd])
    if rc != 0:
        print(f"  WARN: redis flush failed: {err}", file=sys.stderr)
        return False
    # Count from stdout: each `unlink` line emits the integer of keys removed.
    n = sum(int(line.strip()) for line in out.splitlines() if line.strip().isdigit())
    print(f"  ✓ unlinked {n} bma:* key(s)")
    return True


# ── Top-level ───────────────────────────────────────────────────────────

def apply(spec: DiseaseSpec) -> bool:
    """Run all three steps. Returns True on full success."""
    print(f"\n=== Applying {spec.key} ({spec.name_th}) end-to-end ===")
    if not apply_migration(spec):
        return False
    apply_screening_factors_migration(spec)  # no-op for NCD diseases
    patch_main_py(spec)  # idempotent — non-fatal if already wired
    apply_summary_global()  # rebuild OverviewBoard MV (always — it spans all diseases)
    flush_redis()
    print(f"\n✓ {spec.key} live. Backend will hot-reload on file change; "
          f"frontend HMR picks up the new hooks. Refresh the browser.")
    return True


def apply_screening_factors_migration(spec) -> bool:
    """Apply mv_<key>_screening_factors migration if the file exists.

    No-op for NCD diseases (file won't exist). For screening diseases,
    runs the factor MV migration emitted by gen_screening_factors_migration.
    """
    factors_n = getattr(spec, "migration_number", 0) + 100
    sql_path = REPO_DB / "db" / "migrations" / f"{factors_n}_mv_{spec.key}_screening_factors.sql"
    if not sql_path.exists():
        return True
    print(f"  Applying screening factors migration {sql_path.name}…")
    container_path = f"/tmp/{sql_path.name}"
    rc, _, err = _run(["docker", "cp", str(sql_path), f"{POSTGRES_CONTAINER}:{container_path}"])
    if rc != 0:
        print(f"  ERROR: docker cp failed: {err}", file=sys.stderr)
        return False
    rc, out, err = _run([
        "docker", "exec", POSTGRES_CONTAINER,
        "psql", "-U", PG_USER, "-d", PG_DB,
        "-v", "ON_ERROR_STOP=1",
        "-f", container_path,
    ])
    if rc != 0:
        print(f"  ERROR: factors psql apply failed:\n{err}", file=sys.stderr)
        return False
    counts = {kw: out.count(kw) for kw in ("CREATE MATERIALIZED VIEW", "REFRESH MATERIALIZED VIEW", "ERROR")}
    print(f"  ✓ factors: {counts['CREATE MATERIALIZED VIEW']} MV(s) created, "
          f"{counts['REFRESH MATERIALIZED VIEW']} refreshed, "
          f"{counts['ERROR']} error(s)")
    return counts["ERROR"] == 0


def apply_summary_global() -> bool:
    """Re-apply 208_mv_summary_global.sql so the OverviewBoard's left
    panel includes every disease in DISEASES + screening totals."""
    sql_path = REPO_DB / "db" / "migrations" / "208_mv_summary_global.sql"
    if not sql_path.exists():
        return False
    print("  Refreshing OverviewBoard MV (208_mv_summary_global.sql)…")
    container_path = "/tmp/208_mv_summary_global.sql"
    rc, _, err = _run(["docker", "cp", str(sql_path), f"{POSTGRES_CONTAINER}:{container_path}"])
    if rc != 0:
        print(f"  WARN: docker cp failed: {err}")
        return False
    rc, out, err = _run([
        "docker", "exec", POSTGRES_CONTAINER,
        "psql", "-U", PG_USER, "-d", PG_DB,
        "-v", "ON_ERROR_STOP=1",
        "-f", container_path,
    ])
    if rc != 0:
        print(f"  WARN: summary_global re-apply failed:\n{err}")
        return False
    print(f"  ✓ mv_summary_global refreshed")
    return True
