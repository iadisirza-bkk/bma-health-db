#!/usr/bin/env python3
"""Validate chart spec YAML files against the ChartSpec contract.

Tries to import the real Pydantic model; falls back to duck-type validation
that mirrors ADR-01 §1 (mandatory fields + kind enum + axes + accepts).

Usage:
    python validate.py            # validate all *.yaml in this dir
    python validate.py file.yaml  # validate a single file
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent
ALLOWED_KINDS = {
    "bar", "line", "pyramid", "scatter",
    "boxplot", "heatmap", "donut", "stacked_bar",
}
MANDATORY_TOP = ("spec_id", "kind", "title_th", "query_id", "axes")
MANDATORY_AXES_BY_KIND = {
    "bar":          ("x", "y"),
    "line":         ("x", "y"),
    "stacked_bar":  ("x", "y"),
    "pyramid":      ("x", "y"),
    "scatter":      ("x", "y"),
    "boxplot":      ("x", "y"),
    "donut":        ("x", "y"),
    "heatmap":      ("x", "y", "value"),
}


def _try_import_pydantic_model() -> type | None:
    """Best-effort: locate the real ChartSpec Pydantic model."""
    candidates = (
        "bma_health_db.api.services.charts.spec",
        "api.services.charts.spec",
        "bma_health_db.services.charts.spec",
    )
    for mod in candidates:
        try:
            module = __import__(mod, fromlist=["ChartSpec"])
            cls = getattr(module, "ChartSpec", None)
            if cls is not None:
                return cls
        except Exception:
            continue
    return None


def _duck_validate(spec_id: str, raw: dict[str, Any]) -> list[str]:
    """Lightweight validator — returns a list of error messages (empty = OK)."""
    errs: list[str] = []
    for field in MANDATORY_TOP:
        if field not in raw:
            errs.append(f"missing mandatory field: {field}")

    kind = raw.get("kind")
    if kind not in ALLOWED_KINDS:
        errs.append(f"invalid kind '{kind}', must be one of {sorted(ALLOWED_KINDS)}")

    if raw.get("spec_id") and raw["spec_id"] != spec_id:
        errs.append(
            f"spec_id '{raw['spec_id']}' must equal filename stem '{spec_id}'"
        )

    axes = raw.get("axes")
    if not isinstance(axes, dict):
        errs.append("axes must be a mapping")
    elif kind in MANDATORY_AXES_BY_KIND:
        for ax in MANDATORY_AXES_BY_KIND[kind]:
            if ax not in axes:
                errs.append(f"axes.{ax} required for kind={kind}")

    accepts = raw.get("accepts", [])
    if not isinstance(accepts, list):
        errs.append("accepts must be a list")
    else:
        for i, p in enumerate(accepts):
            if not isinstance(p, dict):
                errs.append(f"accepts[{i}] must be a mapping")
                continue
            for f in ("name", "kind"):
                if f not in p:
                    errs.append(f"accepts[{i}].{f} missing")

    if "k_anon_threshold" in raw:
        v = raw["k_anon_threshold"]
        if not isinstance(v, int) or v < 1:
            errs.append("k_anon_threshold must be int >= 1")

    palette = raw.get("color_palette")
    if palette is not None:
        if not isinstance(palette, list) or not all(
            isinstance(c, str) and c.startswith("#") and len(c) in (4, 7)
            for c in palette
        ):
            errs.append("color_palette must be a list of #RRGGBB hex strings")

    return errs


def validate_one(path: Path, model: type | None) -> tuple[bool, list[str]]:
    try:
        with path.open() as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as e:
        line = getattr(e, "problem_mark", None)
        loc = f" (line {line.line + 1})" if line is not None else ""
        return False, [f"YAML parse error{loc}: {e}"]

    if not isinstance(raw, dict):
        return False, ["top-level must be a mapping"]

    spec_id = path.stem

    if model is not None:
        try:
            model(**raw)
            return True, []
        except Exception as e:
            return False, [f"Pydantic validation failed: {e}"]

    errs = _duck_validate(spec_id, raw)
    return (len(errs) == 0), errs


def main(argv: list[str]) -> int:
    files = (
        [Path(p).resolve() for p in argv[1:]]
        if len(argv) > 1
        else sorted(HERE.glob("*.yaml"))
    )
    if not files:
        print("no YAML files found", file=sys.stderr)
        return 2

    model = _try_import_pydantic_model()
    print(f"validator: {'pydantic ChartSpec' if model else 'duck-type fallback'}")
    print(f"files:     {len(files)}")
    print()

    results: list[dict[str, Any]] = []
    overall_ok = True
    for path in files:
        ok, errs = validate_one(path, model)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {path.name}")
        for e in errs:
            print(f"          - {e}")
        results.append({
            "file": path.name,
            "status": status,
            "errors": errs,
        })
        if not ok:
            overall_ok = False

    print()
    print(f"summary: {'ALL PASS' if overall_ok else 'FAILURES PRESENT'}")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
