"""TS registry generator — emits frontend/src/data/pipelineDiseases.ts.

This is the single source of truth on the frontend for which diseases
have the 4-axis pipeline. UI components (DiseaseControls, StatisticsBoard,
ZoneTooltip, BangkokMap, NonBkkLayer) iterate over this registry instead
of hard-coding `if isDiabetes ... else if isHpt`.

Auto-generated from scaffold/diseases.py. The same scaffolder run that
emits a new disease's SQL/router/hooks also re-emits this file, so the
frontend gets the new entry automatically.
"""
from __future__ import annotations

from ..diseases import DISEASES, SCREENING


def _capitalize(s: str) -> str:
    return s[:1].upper() + s[1:]


def gen_ts_registry() -> str:
    """Emit the full TS file with one entry per disease in DISEASES."""
    entries = []
    for spec in DISEASES.values():
        entries.append(f"""  {{
    key: '{spec.key}',
    heatmapKey: '{spec.heatmap_key}',
    shortUpper: '{spec.short_upper}',
    nameTh: '{spec.name_th}',
    nameEn: '{spec.name_en}',
    emoji: '{spec.emoji}',
    chipWordTh: '{spec.chip_disease_word_th}',
    chipWordEn: '{spec.chip_disease_word_en}',
    labChipId: '{spec.lab.chip_id}',
    labChipLabelTh: '{spec.lab.chip_label_th}',
    labChipLabelEn: '{spec.lab.chip_label_en}',
    labShort: '{spec.lab.name.replace('_high', '')}',
    headlineSubtitleLabTh: '{spec.lab.headline_subtitle_lab_th}',
    headlineSubtitleLabEn: '{spec.lab.headline_subtitle_lab_en}',
    chipIds: ['all', 'risk', 'diag', 'family', '{spec.lab.chip_id}', 'undiagnosed'],
    newlyFoundCriteriaTh: '{_escape(spec.newly_found.criteria_th)}',
    newlyFoundCriteriaEn: '{_escape(spec.newly_found.criteria_en)}',
  }}""")

    body = ",\n".join(entries)
    # Per-disease hook imports + wrapper hooks. React rules-of-hooks require
    # static (unconditional) hook calls — so we list every disease here, but
    # gate fetches via the `enabled` flag based on activeKey.
    hook_imports = "\n".join(
        f"import {{ use{_capitalize(s.key)}Classification, type {_capitalize(s.key)}Classification, type {_capitalize(s.key)}DistrictBreakdown }} from '@/hooks/use{_capitalize(s.key)}Classification'"
        for s in DISEASES.values()
    ) + "\n" + "\n".join(
        f"import {{ use{_capitalize(s.key)}FactorsBulk, type {_capitalize(s.key)}FactorsBulk }} from '@/hooks/use{_capitalize(s.key)}FactorsBulk'"
        for s in DISEASES.values()
    ) + "\n" + "\n".join(
        f"import {{ use{_capitalize(s.key)}Factors, type {_capitalize(s.key)}FactorsResponse }} from '@/hooks/use{_capitalize(s.key)}Factors'"
        for s in DISEASES.values()
    )

    classification_calls = "\n".join(
        f"  const {s.key}Cls = use{_capitalize(s.key)}Classification('city', undefined, {{ enabled: activeKey === '{s.key}' }})"
        for s in DISEASES.values()
    )
    classification_returns = ",\n".join(
        f"    {s.key}: {s.key}Cls.data ?? null"
        for s in DISEASES.values()
    )

    bulk_calls = "\n".join(
        f"  const {s.key}Bulk = use{_capitalize(s.key)}FactorsBulk({{ enabled: activeKey === '{s.key}' }})"
        for s in DISEASES.values()
    )
    bulk_returns = ",\n".join(
        f"    {s.key}: {s.key}Bulk.data ?? null"
        for s in DISEASES.values()
    )

    nonbkk_classification_calls = "\n".join(
        f"  const {s.key}NonBkkCls = use{_capitalize(s.key)}Classification('non_bkk', undefined, {{ enabled: activeKey === '{s.key}' }})"
        for s in DISEASES.values()
    )
    nonbkk_classification_returns = ",\n".join(
        f"    {s.key}: {s.key}NonBkkCls.data ?? null"
        for s in DISEASES.values()
    )

    zone_classification_calls = "\n".join(
        f"  const {s.key}ZoneCls = use{_capitalize(s.key)}Classification('zone', zoneId, {{ enabled: !!zoneId && activeKey === '{s.key}' }})"
        for s in DISEASES.values()
    )
    zone_classification_returns = ",\n".join(
        f"    {s.key}: {s.key}ZoneCls.data ?? null"
        for s in DISEASES.values()
    )

    cls_type_union = " | ".join(f"{_capitalize(s.key)}Classification" for s in DISEASES.values())
    bulk_type_union = " | ".join(f"{_capitalize(s.key)}FactorsBulk" for s in DISEASES.values())

    # Screening pipeline
    screening_entries = []
    for s in SCREENING.values():
        screening_entries.append(f"""  {{
    key: '{s.key}',
    heatmapKey: '{s.heatmap_key}',
    shortUpper: '{s.short_upper}',
    nameTh: '{s.name_th}',
    nameEn: '{s.name_en}',
    emoji: '{s.emoji}',
    chipLabelTh: '{_escape(s.chip_label_th)}',
    chipLabelEn: '{_escape(s.chip_label_en)}',
    thresholdLabelTh: '{_escape(s.threshold_label_th)}',
    thresholdLabelEn: '{_escape(s.threshold_label_en)}',
  }}""")
    screening_body = ",\n".join(screening_entries)

    screening_imports = "\n".join(
        f"import {{ use{_capitalize(s.key)}Screening, type {_capitalize(s.key)}Screening }} from '@/hooks/use{_capitalize(s.key)}Screening'"
        for s in SCREENING.values()
    ) + "\n" + "\n".join(
        f"import {{ use{_capitalize(s.key)}ScreeningFactorsBulk, type {_capitalize(s.key)}ScreeningFactorsBulk }} from '@/hooks/use{_capitalize(s.key)}ScreeningFactorsBulk'"
        for s in SCREENING.values()
    )
    screening_calls = "\n".join(
        f"  const {s.key}Scr = use{_capitalize(s.key)}Screening(scope, id, {{ enabled: activeKey === '{s.key}' }})"
        for s in SCREENING.values()
    )
    screening_returns = ",\n".join(
        f"    {s.key}: {s.key}Scr.data ?? null"
        for s in SCREENING.values()
    )
    screening_type_union = " | ".join(f"{_capitalize(s.key)}Screening" for s in SCREENING.values()) or "never"

    screening_factors_calls = "\n".join(
        f"  const {s.key}ScrFB = use{_capitalize(s.key)}ScreeningFactorsBulk({{ enabled: activeKey === '{s.key}' }})"
        for s in SCREENING.values()
    )
    screening_factors_returns = ",\n".join(
        f"    {s.key}: {s.key}ScrFB.data ?? null"
        for s in SCREENING.values()
    )
    screening_factors_type_union = " | ".join(
        f"{_capitalize(s.key)}ScreeningFactorsBulk" for s in SCREENING.values()
    ) or "never"

    return f"""/**
 * Pipeline disease registry — single source of truth for the 4-axis
 * disease pipeline (DM, HPT, ...). UI components iterate over this list
 * instead of hard-coding individual diseases.
 *
 * AUTO-GENERATED from /Users/dev/bma-health-db/scaffold/diseases.py.
 * Re-emitted on every `python3 -m scaffold.scaffold <key>` run.
 * Do NOT hand-edit this file — your edits will be lost.
 */
import type {{ HeatmapType }} from '@/stores/mapStore'
{hook_imports}
{screening_imports}

export interface PipelineDisease {{
  /** Short identifier matching the SQL/API/hooks (e.g. 'dm', 'hpt'). */
  key: string
  /** Maps to the activeHeatmap value in mapStore (e.g. 'diabetes'). */
  heatmapKey: HeatmapType
  /** UPPERCASE form for TS type names + comments (e.g. 'DM', 'HPT'). */
  shortUpper: string
  nameTh: string
  nameEn: string
  emoji: string
  /** Disease word for chip labels — "เสี่ย{{chipWordTh}}" → "เสี่ยงเบาหวาน". */
  chipWordTh: string
  chipWordEn: string
  /** c4 lab axis chip identifier ('fpg', 'bp', 'chol'). */
  labChipId: string
  labChipLabelTh: string
  labChipLabelEn: string
  /** Short form used in field names: c4_<labShort>, any_<key>_signal. */
  labShort: string
  /** Subtitle line under disease name in tooltip header. */
  headlineSubtitleLabTh: string
  headlineSubtitleLabEn: string
  /** The 6 chip ids in order: ['all', 'risk', 'diag', 'family', <labChipId>, 'undiagnosed']. */
  chipIds: readonly string[]
  /** Active Follow-up criteria text (shown at bottom of callout). */
  newlyFoundCriteriaTh: string
  newlyFoundCriteriaEn: string
}}

export const PIPELINE_DISEASES: readonly PipelineDisease[] = [
{body},
]

export const PIPELINE_BY_HEATMAP: Readonly<Record<string, PipelineDisease>> =
  Object.fromEntries(PIPELINE_DISEASES.map(d => [d.heatmapKey, d]))

export const PIPELINE_BY_KEY: Readonly<Record<string, PipelineDisease>> =
  Object.fromEntries(PIPELINE_DISEASES.map(d => [d.key, d]))

/** Resolve the spec for a heatmap key, or null if not a pipeline disease. */
export function pipelineSpec(heatmap: string | null | undefined): PipelineDisease | null {{
  return heatmap ? (PIPELINE_BY_HEATMAP[heatmap] ?? null) : null
}}

// ─────────────────────────────────────────────────────────────────────────
// Wrapper hooks — the bridge between the registry and React components.
//
// React rules-of-hooks require static, unconditional calls. So we list
// every disease here and gate the actual network fetch via `enabled:
// activeKey === '<key>'`. Adding a new disease re-runs the scaffolder,
// which appends one more line to each hook below — components don't
// change.
//
// The result is a record keyed by disease key (e.g. 'dm', 'hpt') so a
// component can read e.g. `result[spec.key]` to get the right payload.
// ─────────────────────────────────────────────────────────────────────────

/** All BKK-city classifications. Only the active disease actually fetches. */
export function usePipelineClassifications(activeKey: string | null): Record<string, ({cls_type_union}) | null> {{
{classification_calls}
  return {{
{classification_returns},
  }}
}}

/** All non-BKK classifications (used by NonBkkLayer). */
export function usePipelineNonBkkClassifications(activeKey: string | null): Record<string, ({cls_type_union}) | null> {{
{nonbkk_classification_calls}
  return {{
{nonbkk_classification_returns},
  }}
}}

/** Per-zone classifications keyed by hovered zone code. Used by the
 *  hover tooltip's 4-axis card. Only the active disease + non-empty
 *  zoneId triggers a network fetch; everything else is no-op. */
export function usePipelineZoneClassifications(
  activeKey: string | null,
  zoneId: string | undefined,
): Record<string, ({cls_type_union}) | null> {{
{zone_classification_calls}
  return {{
{zone_classification_returns},
  }}
}}

/** All factor-bulk payloads. */
export function usePipelineFactorsBulk(activeKey: string | null): Record<string, ({bulk_type_union}) | null> {{
{bulk_calls}
  return {{
{bulk_returns},
  }}
}}

// ─────────────────────────────────────────────────────────────────────────
// Screening pipeline — single-axis lab/test result. Diseases here lack
// the c2/c3 self-report + family columns needed for the 4-axis NCD
// pattern; they're pure "abnormal screening test" prevalence cards.
// ─────────────────────────────────────────────────────────────────────────

export interface ScreeningDisease {{
  key: string
  heatmapKey: HeatmapType
  shortUpper: string
  nameTh: string
  nameEn: string
  emoji: string
  chipLabelTh: string
  chipLabelEn: string
  thresholdLabelTh: string
  thresholdLabelEn: string
}}

export const SCREENING_DISEASES: readonly ScreeningDisease[] = [
{screening_body},
]

export const SCREENING_BY_HEATMAP: Readonly<Record<string, ScreeningDisease>> =
  Object.fromEntries(SCREENING_DISEASES.map(d => [d.heatmapKey, d]))

export function screeningSpec(heatmap: string | null | undefined): ScreeningDisease | null {{
  return heatmap ? (SCREENING_BY_HEATMAP[heatmap] ?? null) : null
}}

/** Wrapper hook for screening data. Same fan-out pattern as the NCD
 *  classification wrapper — only the active disease actually fetches. */
export function usePipelineScreening(
  activeKey: string | null,
  scope: 'city' | 'zone' | 'district' | 'non_bkk',
  id?: string,
): Record<string, ({screening_type_union}) | null> {{
{screening_calls}
  return {{
{screening_returns},
  }}
}}

/** Bulk screening-factors hook — one fetch per active disease (city +
 *  every zone + every district in a single payload). Same caching idiom
 *  as usePipelineFactorsBulk for NCDs. */
export function usePipelineScreeningFactorsBulk(
  activeKey: string | null,
): Record<string, ({screening_factors_type_union}) | null> {{
{screening_factors_calls}
  return {{
{screening_factors_returns},
  }}
}}
"""


def _escape(s: str) -> str:
    """Escape single quotes for embedding in a TS string literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'")
