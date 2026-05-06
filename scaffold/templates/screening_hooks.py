"""TS hook generator for screening-only diseases."""
from __future__ import annotations

from ..diseases import ScreeningSpec


def _capitalize(s: str) -> str:
    return s[:1].upper() + s[1:]


def gen_screening_hook(spec: ScreeningSpec) -> str:
    k = spec.key
    Title = _capitalize(k)
    return f"""'use client'

import {{ useQuery, type UseQueryResult }} from '@tanstack/react-query'

/** /api/v2/{k}/screening — auto-generated. */
export interface {Title}ScreeningDistrict {{
  district_code: string
  district_name: string | null
  zone_code: string | null
  n_total: number
  n_abnormal: number
  abnormal_pct: number
}}

export interface {Title}Screening {{
  scope: 'city' | 'zone' | 'district' | 'non_bkk'
  scope_id: string | null
  n_total: number
  n_abnormal: number
  abnormal_pct: number
  districts: {Title}ScreeningDistrict[]
}}

export function use{Title}Screening(
  scope: 'city' | 'zone' | 'district' | 'non_bkk',
  id?: string,
  opts?: {{ enabled?: boolean }},
): UseQueryResult<{Title}Screening> {{
  return useQuery<{Title}Screening>({{
    queryKey: ['v2', '{k}/screening', scope, id ?? null],
    queryFn: async () => {{
      const qs = `{k}/screening?scope=${{scope}}${{id ? `&id=${{id}}` : ''}}`
      const res = await fetch(`/api/v2/${{qs}}`)
      if (!res.ok) throw new Error(`HTTP ${{res.status}}`)
      const json = await res.json()
      return (json.data ?? json) as {Title}Screening
    }},
    staleTime: 5 * 60_000,
    enabled: opts?.enabled,
    retry: 1,
  }})
}}
"""
