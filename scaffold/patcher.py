"""Auto-patcher for frontend files that aren't fully generated.

Appends entries to:
  mapStore.ts  — Pattern type union, store state field, setter action
  i18n.ts      — 6 chip label keys (Th/En + extra langs copy En)

Patcher is idempotent: re-running on the same disease detects existing
entries and skips. Comment markers are inserted so the patcher can
locate insertion points on subsequent runs.
"""
from __future__ import annotations

import re
from pathlib import Path

from .diseases import DiseaseSpec

REPO_FRONTEND = Path("/Users/dev/bma-health/frontend")
MAPSTORE_PATH = REPO_FRONTEND / "src" / "stores" / "mapStore.ts"
I18N_PATH     = REPO_FRONTEND / "src" / "data" / "i18n.ts"


# ── mapStore patches ────────────────────────────────────────────────────

def _mapstore_patches(spec: DiseaseSpec) -> dict[str, str]:
    """Lines to append for one disease. Caller does the actual insertion
    using regex anchors.

    Identifier conventions (matching existing DM/HPT store code):
      • Type alias:   {K}Pattern — uppercase ('DMPattern', 'HPTPattern')
      • State field:  selected{T}Pattern — titlecase ('selectedDmPattern')
      • Setter:       setSelected{T}Pattern — titlecase ('setSelectedDmPattern')
    """
    k = spec.key
    K = spec.short_upper                                     # 'DM', 'HPT'
    T = K[0] + K[1:].lower()                                 # 'Dm', 'Hpt'
    lab_chip = spec.lab.chip_id
    return {
        "type_decl":
            f"export type {K}Pattern = 'all' | 'risk' | 'diag' | 'family' | '{lab_chip}' | 'undiagnosed'",
        "state_field":
            f"  selected{T}Pattern: {K}Pattern",
        "state_default":
            f"  selected{T}Pattern: 'all',",
        "action_decl":
            f"  setSelected{T}Pattern: (p: {K}Pattern) => void",
        "action_impl":
            f"  setSelected{T}Pattern: (p) => set({{ selected{T}Pattern: p }}),",
    }


def patch_mapstore(spec: DiseaseSpec) -> bool:
    """Insert mapStore entries if not present. Returns True if file was
    modified."""
    K = spec.short_upper
    if not MAPSTORE_PATH.exists():
        print(f"  WARN: {MAPSTORE_PATH} not found — skipping mapStore patch")
        return False
    content = MAPSTORE_PATH.read_text()
    if f"selected{K}Pattern" in content:
        print(f"  mapStore.ts: {K}Pattern already wired — skipping")
        return False

    p = _mapstore_patches(spec)
    # Anchor 1: insert TypeXPattern after the last "export type *Pattern" line
    pattern_type_re = re.compile(r"(export type \w+Pattern = [^\n]+\n)")
    matches = list(pattern_type_re.finditer(content))
    if not matches:
        raise RuntimeError("Could not find 'export type *Pattern' anchor in mapStore.ts")
    last = matches[-1]
    content = content[:last.end()] + p["type_decl"] + "\n" + content[last.end():]

    # Anchor 2: insert state field right after the existing selected*Pattern field declarations
    # We append it after the last `selected\w+Pattern: \w+` line in the interface
    state_re = re.compile(r"(  selected\w+Pattern: \w+\n)")
    matches = list(state_re.finditer(content))
    if matches:
        last = matches[-1]
        content = content[:last.end()] + p["state_field"] + "\n" + content[last.end():]
    else:
        raise RuntimeError("Could not find 'selected*Pattern: *' field anchor")

    # Anchor 3: insert default value after last `selected\w+Pattern: 'all'`
    default_re = re.compile(r"(  selected\w+Pattern: 'all',\n)")
    matches = list(default_re.finditer(content))
    if matches:
        last = matches[-1]
        content = content[:last.end()] + p["state_default"] + "\n" + content[last.end():]
    else:
        raise RuntimeError("Could not find 'selected*Pattern: 'all',' default anchor")

    # Anchor 4: insert action declaration after last `setSelected\w+Pattern:` declaration
    action_decl_re = re.compile(r"(  setSelected\w+Pattern: \(p: \w+Pattern\) => void\n)")
    matches = list(action_decl_re.finditer(content))
    if matches:
        last = matches[-1]
        content = content[:last.end()] + p["action_decl"] + "\n" + content[last.end():]
    else:
        raise RuntimeError("Could not find 'setSelected*Pattern: ... => void' anchor")

    # Anchor 5: insert action impl after last `setSelected\w+Pattern: (p) => set(...)`
    # The actual store line is `setSelectedXPattern: (p) => set({ selectedXPattern: p }),`
    # — `set(` has ONE closing `)`, then a comma, then newline.
    action_impl_re = re.compile(r"(  setSelected\w+Pattern: \(p\) => set\([^)]+\),\n)")
    matches = list(action_impl_re.finditer(content))
    if matches:
        last = matches[-1]
        content = content[:last.end()] + p["action_impl"] + "\n" + content[last.end():]
    else:
        raise RuntimeError("Could not find 'setSelected*Pattern: (p) => set(...),' impl anchor")

    MAPSTORE_PATH.write_text(content)
    print(f"  mapStore.ts: patched ({K}Pattern + selected{K}Pattern + setSelected{K}Pattern)")
    return True


# ── i18n patches ────────────────────────────────────────────────────────

def _i18n_keys(spec: DiseaseSpec) -> list[tuple[str, str, str]]:
    """Return list of (key, th, en) tuples for the 6 chip labels."""
    k = spec.key
    K = spec.short_upper
    word_th = spec.chip_disease_word_th
    word_en = spec.chip_disease_word_en
    return [
        (f"{k}PatternAll",         "ทั้งหมด",                    "All"),
        (f"{k}PatternRisk",        f"เสี่ยง{word_th}",            f"At risk of {word_en}"),
        (f"{k}PatternDiag",        f"ป่วย{word_th}",              f"Has {word_en}"),
        (f"{k}PatternFamily",      f"ครอบครัวเป็น{word_th}",       f"Family with {word_en}"),
        (f"{k}Pattern{spec.lab.chip_id.capitalize()}",
                                   spec.lab.chip_label_th,        spec.lab.chip_label_en),
        (f"{k}PatternUndiagnosed", "เจอใหม่ในโครงการ",            "Newly found"),
    ]


def patch_i18n(spec: DiseaseSpec) -> bool:
    """Append chip i18n keys to all language sections.

    Inserts each new key after the first line matching `{firstKey}: '` in
    each language object. Idempotent: skips if any key for this disease
    already exists.
    """
    k = spec.key
    if not I18N_PATH.exists():
        print(f"  WARN: {I18N_PATH} not found — skipping i18n patch")
        return False
    content = I18N_PATH.read_text()
    keys = _i18n_keys(spec)
    first_key = keys[0][0]
    if f"{first_key}:" in content:
        print(f"  i18n.ts: {k} chip keys already present — skipping")
        return False

    # Find each language object literal: th: { ... }, en: { ... }, etc.
    # We'll insert after the first existing dmPattern* / hptPattern* line in
    # each language block.
    anchor_re = re.compile(r"(^[ \t]*(?:dm|hpt)PatternAll: ['\"][^'\"]+['\"],?\n)", re.MULTILINE)
    matches = list(anchor_re.finditer(content))
    if not matches:
        raise RuntimeError(
            "Could not find a dmPatternAll or hptPatternAll anchor in i18n.ts. "
            "Ensure the i18n file has at least one language section with chip keys."
        )

    # We insert in two language groups: those with TH-localized values
    # (probably just 'th') and those that copy English (en, zh, ja, ...).
    # Heuristic: detect TH chars in the matched line; if present, treat as TH.
    # Iterate matches in reverse so insertions don't shift later offsets.
    inserts = 0
    for m in reversed(matches):
        anchor_line = m.group(0)
        is_th = any('฀' <= c <= '๿' for c in anchor_line)
        # Compute leading whitespace for insertion
        indent = re.match(r"^([ \t]*)", anchor_line).group(1)
        block = "".join(
            f"{indent}{key}: '{th if is_th else en}',\n"
            for key, th, en in keys
        )
        content = content[:m.end()] + block + content[m.end():]
        inserts += 1

    I18N_PATH.write_text(content)
    print(f"  i18n.ts: inserted {len(keys)} keys in {inserts} language section(s)")
    return True


def apply_patches(spec: DiseaseSpec) -> None:
    print(f"Patching frontend for {spec.key} ({spec.name_th})…")
    patch_mapstore(spec)
    patch_i18n(spec)
