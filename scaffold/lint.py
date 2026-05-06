"""Validate scaffold/diseases.py against the criteria YAML.

The YAML at `/Users/dev/bma-med/medical-knowledge/disease-criteria.yaml` is the
authoritative spec for the 11 BMA-MED diseases. This linter checks that
diseases.py agrees with it on:

  1. Pipeline classification (ncd vs screening) — wrong dict = error
  2. name_th / name_en — verbatim
  3. chip / criterion label_th / label_en — verbatim
  4. Set membership (no diseases in code missing from YAML, vice versa)

Run standalone:
    python -m scaffold.lint

Exits 0 if clean, 1 if any drift detected. Wired into scaffold.scaffold so
`python -m scaffold.scaffold <key> --apply` refuses to run on a dirty tree.

When the official guideline updates a threshold:
  1. Update disease-criteria.yaml first.
  2. Update diseases.py to match.
  3. Re-run this linter — it must pass before applying.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

import yaml

from .diseases import DISEASES, SCREENING


# YAML location resolution (in priority order):
#   1. $BMA_CRITERIA_YAML env var — used by CI / non-default environments
#   2. /Users/dev/bma-med/medical-knowledge/disease-criteria.yaml — local default
#   3. <this-repo>/medical-knowledge/disease-criteria.yaml — when shipped in-tree
def _resolve_yaml_path() -> Path:
    if (env := os.environ.get("BMA_CRITERIA_YAML")):
        return Path(env)
    bma_med_default = Path("/Users/dev/bma-med/medical-knowledge/disease-criteria.yaml")
    if bma_med_default.exists():
        return bma_med_default
    in_repo = Path(__file__).resolve().parent.parent / "medical-knowledge" / "disease-criteria.yaml"
    return in_repo


YAML_PATH = _resolve_yaml_path()


def _check_one(yaml_entry: dict, errors: list[str]) -> None:
    """Compare a single YAML entry against its diseases.py counterpart."""
    key = yaml_entry["key"]
    pipeline = yaml_entry["pipeline"]
    yaml_th = yaml_entry["name_th"]
    yaml_en = yaml_entry["name_en"]
    yaml_lab_th = yaml_entry["criterion"]["label_th"]
    yaml_lab_en = yaml_entry["criterion"]["label_en"]

    if pipeline == "ncd":
        spec = DISEASES.get(key)
        if spec is None:
            errors.append(
                f"  [{key}] YAML expects pipeline='ncd' but '{key}' is not in DISEASES dict"
            )
            return
        actual_lab_th = spec.lab.chip_label_th
        actual_lab_en = spec.lab.chip_label_en
    elif pipeline == "screening":
        spec = SCREENING.get(key)
        if spec is None:
            errors.append(
                f"  [{key}] YAML expects pipeline='screening' but '{key}' is not in SCREENING dict"
            )
            return
        actual_lab_th = spec.chip_label_th
        actual_lab_en = spec.chip_label_en
    else:
        errors.append(f"  [{key}] YAML pipeline must be 'ncd' or 'screening', got '{pipeline}'")
        return

    # Field-by-field comparison
    if spec.name_th != yaml_th:
        errors.append(f"  [{key}] name_th: YAML='{yaml_th}' diseases.py='{spec.name_th}'")
    if spec.name_en != yaml_en:
        errors.append(f"  [{key}] name_en: YAML='{yaml_en}' diseases.py='{spec.name_en}'")
    if actual_lab_th != yaml_lab_th:
        errors.append(
            f"  [{key}] criterion.label_th: YAML='{yaml_lab_th}' diseases.py='{actual_lab_th}'"
        )
    if actual_lab_en != yaml_lab_en:
        errors.append(
            f"  [{key}] criterion.label_en: YAML='{yaml_lab_en}' diseases.py='{actual_lab_en}'"
        )


def _check_set_membership(yaml_keys_ncd: set[str], yaml_keys_scr: set[str], errors: list[str]) -> None:
    """Verify no extra entries exist in either source."""
    extra_in_ncd = set(DISEASES.keys()) - yaml_keys_ncd
    extra_in_scr = set(SCREENING.keys()) - yaml_keys_scr
    misclassified_ncd = set(DISEASES.keys()) & yaml_keys_scr  # in NCD but YAML says screening
    misclassified_scr = set(SCREENING.keys()) & yaml_keys_ncd  # in SCREENING but YAML says NCD

    for k in extra_in_ncd - misclassified_ncd:
        errors.append(f"  [{k}] in DISEASES but not in YAML — add it or remove from diseases.py")
    for k in extra_in_scr - misclassified_scr:
        errors.append(f"  [{k}] in SCREENING but not in YAML — add it or remove from diseases.py")
    for k in misclassified_ncd:
        errors.append(
            f"  [{k}] in DISEASES dict but YAML classifies it as 'screening' — move to SCREENING dict"
        )
    for k in misclassified_scr:
        errors.append(
            f"  [{k}] in SCREENING dict but YAML classifies it as 'ncd' — move to DISEASES dict"
        )


def lint() -> tuple[bool, list[str]]:
    """Run all checks. Returns (ok, errors)."""
    errors: list[str] = []

    if not YAML_PATH.exists():
        return False, [f"Criteria YAML not found at {YAML_PATH}"]

    spec = yaml.safe_load(YAML_PATH.read_text())
    yaml_diseases = spec.get("diseases", [])
    if not yaml_diseases:
        return False, ["YAML has no `diseases:` list"]

    # Check each YAML entry against diseases.py
    for entry in yaml_diseases:
        _check_one(entry, errors)

    # Set-membership symmetry
    yaml_keys_ncd = {d["key"] for d in yaml_diseases if d.get("pipeline") == "ncd"}
    yaml_keys_scr = {d["key"] for d in yaml_diseases if d.get("pipeline") == "screening"}
    _check_set_membership(yaml_keys_ncd, yaml_keys_scr, errors)

    return not errors, errors


def main() -> int:
    ok, errors = lint()
    if ok:
        n_total = len(DISEASES) + len(SCREENING)
        print(
            f"✓ Disease criteria match — {len(DISEASES)} NCD + {len(SCREENING)} screening "
            f"({n_total} total) consistent with disease-criteria.yaml"
        )
        return 0
    print(
        "✗ DISEASE CRITERIA DRIFT — diseases.py disagrees with disease-criteria.yaml.\n"
        "  Update one to match the other, then re-run.\n",
        file=sys.stderr,
    )
    for e in errors:
        print(e, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
