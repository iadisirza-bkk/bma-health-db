"""TS hook generator for /api/v2/<key>/screening/factors/bulk."""
from __future__ import annotations

from ..diseases import ScreeningSpec


def _capitalize(s: str) -> str:
    return s[:1].upper() + s[1:]


def gen_screening_factors_hook(spec: ScreeningSpec) -> str:
    k = spec.key
    Cap = _capitalize(k)
    K = spec.short_upper
    return f'''\'use client\'

import {{ useQuery, type UseQueryResult }} from \'@tanstack/react-query\'

/** /api/v2/{k}/screening/factors/bulk — auto-generated. */
export interface {Cap}ScreeningFactorGroup {{
  group: string
  label_th: string
  label_en: string
  n_total: number
  n_abnormal: number
  abnormal_pct: number
  lift: number
}}

export interface {Cap}ScreeningFactor {{
  factor_key: string
  factor_label_th: string
  factor_label_en: string
  groups: {Cap}ScreeningFactorGroup[]
  top_group: string | null
}}

export interface {Cap}ScreeningFactors {{
  scope: \'city\' | \'zone\' | \'district\'
  scope_id: string | null
  area_abnormal_pct: number
  factors: {Cap}ScreeningFactor[]
}}

export interface {Cap}ScreeningFactorsBulk {{
  city: {Cap}ScreeningFactors
  zones: Record<string, {Cap}ScreeningFactors>
  districts: Record<string, {Cap}ScreeningFactors>
}}

export function use{Cap}ScreeningFactorsBulk(
  opts?: {{ enabled?: boolean }},
): UseQueryResult<{Cap}ScreeningFactorsBulk> {{
  return useQuery<{Cap}ScreeningFactorsBulk>({{
    queryKey: [\'v2\', \'{k}/screening/factors/bulk\'],
    queryFn: async () => {{
      const res = await fetch(\'/api/v2/{k}/screening/factors/bulk\')
      if (!res.ok) throw new Error(`HTTP ${{res.status}}`)
      const json = await res.json()
      return (json.data ?? json) as {Cap}ScreeningFactorsBulk
    }},
    staleTime: 10 * 60_000,
    enabled: opts?.enabled,
    retry: 1,
  }})
}}
'''
