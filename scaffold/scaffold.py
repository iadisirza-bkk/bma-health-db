#!/usr/bin/env python3
"""Disease pipeline scaffolder.

Usage:
    python -m scaffold.scaffold <key>            # write files to repo
    python -m scaffold.scaffold <key> --dry-run  # print to stdout, no writes
    python -m scaffold.scaffold <key> --diff     # diff against existing files

What gets generated for one disease (key=<key>):
    bma-health-db/db/migrations/<NNN>_mv_<key>.sql
    bma-health-db/api/routers/<key>.py
    bma-health/frontend/src/hooks/useXClassification.ts
    bma-health/frontend/src/hooks/useXFactors.ts
    bma-health/frontend/src/hooks/useXFactorsBulk.ts

After the files exist:
    1. Apply the migration:
         docker exec -i bma-health-db psql -U postgres -d bma_health \\
             -f db/migrations/<NNN>_mv_<key>.sql
    2. Restart the API (auto-reloads on file change).
    3. Wire the disease into the map: add a chip section in DiseaseControls
       and the i18n keys (see scaffold/README.md → "Frontend wiring").

The scaffolder will refuse to overwrite files that already exist unless
--force is passed. Use --dry-run to preview output.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from scaffold.diseases import DISEASES, SCREENING, get_spec, get_screening_spec
from scaffold.lint import lint as lint_criteria
from scaffold.templates.sql import gen_migration
from scaffold.templates.router import gen_router
from scaffold.templates.hooks import gen_all_hooks
from scaffold.templates.ts_registry import gen_ts_registry
from scaffold.templates.screening_sql import gen_screening_migration
from scaffold.templates.screening_router import gen_screening_router
from scaffold.templates.screening_hooks import gen_screening_hook
from scaffold.templates.screening_factors_sql import gen_screening_factors_migration
from scaffold.templates.screening_factors_hook import gen_screening_factors_hook
from scaffold.templates.summary_global import gen_summary_global_sql
from scaffold.patcher import apply_patches
from scaffold.applier import apply as apply_e2e

# Repo paths — absolute so the script works from any CWD.
REPO_DB        = Path("/Users/dev/bma-health-db")
REPO_FRONTEND  = Path("/Users/dev/bma-health/frontend")

MIGRATIONS_DIR = REPO_DB / "db" / "migrations"
ROUTERS_DIR    = REPO_DB / "api" / "routers"
HOOKS_DIR      = REPO_FRONTEND / "src" / "hooks"
REGISTRY_PATH  = REPO_FRONTEND / "src" / "data" / "pipelineDiseases.ts"


def _migration_filename(spec) -> str:
    return f"{spec.migration_number}_mv_{spec.key}.sql"


def _emit_files(spec, *, is_screening: bool = False) -> dict[Path, str]:
    """Build the {path: content} map for one disease."""
    out: dict[Path, str] = {}

    if is_screening:
        out[MIGRATIONS_DIR / _migration_filename(spec)] = gen_screening_migration(spec)
        out[ROUTERS_DIR    / f"{spec.key}.py"]          = gen_screening_router(spec)
        title = spec.key[:1].upper() + spec.key[1:]
        out[HOOKS_DIR / f"use{title}Screening.ts"]      = gen_screening_hook(spec)
        # Demographic / lifestyle factors MV + TS hook — feeds the
        # tooltip "ปัจจัยเด่น" panel for screening diseases. Migration
        # number = base + 100 to keep ordered after the screening MV.
        factors_n = spec.migration_number + 100
        out[MIGRATIONS_DIR / f"{factors_n}_mv_{spec.key}_screening_factors.sql"] = (
            gen_screening_factors_migration(spec)
        )
        out[HOOKS_DIR / f"use{title}ScreeningFactorsBulk.ts"] = gen_screening_factors_hook(spec)
    else:
        out[MIGRATIONS_DIR / _migration_filename(spec)] = gen_migration(spec)
        out[ROUTERS_DIR    / f"{spec.key}.py"]          = gen_router(spec)
        for fname, content in gen_all_hooks(spec).items():
            out[HOOKS_DIR / fname] = content
    # Always re-emit the TS registry — it spans every disease, so adding
    # or editing one entry keeps the frontend single-source-of-truth in sync.
    out[REGISTRY_PATH] = gen_ts_registry()
    # Always re-emit the OverviewBoard MV migration too — it spans every
    # NCD-pipeline disease's at-risk count and feeds /api/v2/summary/overview.
    out[MIGRATIONS_DIR / "208_mv_summary_global.sql"] = gen_summary_global_sql()
    return out


def _diff(path: Path, new_content: str) -> str:
    """Return unified diff between existing file and proposed new content."""
    if not path.exists():
        return f"=== NEW FILE: {path} ===\n{new_content[:500]}...\n"
    old = path.read_text()
    if old == new_content:
        return f"=== UNCHANGED: {path} ===\n"
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), new_content.splitlines(),
        fromfile=str(path) + " (current)", tofile=str(path) + " (proposed)",
        lineterm="",
    ))
    return f"=== DIFF: {path} ===\n{diff}\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Scaffold a disease pipeline (SQL + API + TS hooks).")
    p.add_argument(
        "key",
        help=f"Disease key. NCD: {sorted(DISEASES.keys())} | Screening: {sorted(SCREENING.keys())}",
    )
    p.add_argument("--dry-run", action="store_true", help="Print to stdout, do not write files")
    p.add_argument("--diff", action="store_true", help="Show diff vs existing files, do not write")
    p.add_argument("--force", action="store_true", help="Overwrite existing files (default: refuse)")
    p.add_argument("--apply", action="store_true",
                   help="After writing files: run migration in postgres, register router in main.py, flush redis")
    p.add_argument("--screening", action="store_true",
                   help="Treat key as a ScreeningSpec (single-axis lab test) rather than a 4-axis NCD")
    p.add_argument("--skip-lint", action="store_true",
                   help="Bypass the criteria-YAML drift check. Use only for emergency overrides.")
    args = p.parse_args()

    # Refuse to scaffold if scaffold/diseases.py drifts from
    # /Users/dev/bma-med/medical-knowledge/disease-criteria.yaml. The YAML is
    # the authoritative spec — keep it in sync with diseases.py and re-run.
    # `--skip-lint` exists for hand-of-god overrides only; CI should never use it.
    if not args.skip_lint:
        ok, errors = lint_criteria()
        if not ok:
            print(
                "ERROR: criteria drift detected. Refusing to scaffold.\n"
                "       Update disease-criteria.yaml or scaffold/diseases.py to match.\n"
                "       Run `python -m scaffold.lint` for details, or `--skip-lint` to override.\n",
                file=sys.stderr,
            )
            for e in errors:
                print(e, file=sys.stderr)
            return 3

    is_screening = args.screening or args.key in SCREENING

    try:
        spec = get_screening_spec(args.key) if is_screening else get_spec(args.key)
    except KeyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    files = _emit_files(spec, is_screening=is_screening)

    if args.diff:
        for path, content in sorted(files.items()):
            print(_diff(path, content))
        return 0

    if args.dry_run:
        for path, content in sorted(files.items()):
            print(f"=== {path} ({len(content):,} bytes) ===")
            print(content[:1500])
            if len(content) > 1500:
                print(f"... ({len(content) - 1500} more bytes)")
            print()
        return 0

    # Real write — the TS registry is always overwritten (it's a snapshot
    # of DISEASES, not one disease) so it stays in sync.
    ALWAYS_OVERWRITE = {REGISTRY_PATH, MIGRATIONS_DIR / "208_mv_summary_global.sql"}
    written, skipped = [], []
    for path, content in sorted(files.items()):
        if path.exists() and not args.force and path not in ALWAYS_OVERWRITE:
            skipped.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)

    print(f"Scaffolded disease: {spec.key} ({spec.name_th})")
    for p in written:
        print(f"  WROTE   {p}")
    for p in skipped:
        print(f"  SKIPPED {p}  (exists; pass --force to overwrite)")

    if skipped and not args.force:
        print(f"\n{len(skipped)} file(s) skipped. Re-run with --force, or delete those files first.")
        return 1

    # mapStore + i18n patches are NCD-only (chips, pattern types).
    # Screening diseases don't have chips or pattern state.
    if not is_screening:
        apply_patches(spec)

    if args.apply:
        ok = apply_e2e(spec)
        return 0 if ok else 1

    print(f"""
Next steps:
  1. Apply the migration + register router + flush redis (one command):
       python3 -m scaffold.scaffold {spec.key} --apply
  2. UI components currently still need wiring per disease — see
     scaffold/README.md → "What still needs manual wiring (UI)".
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
