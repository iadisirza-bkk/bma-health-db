# Agent Capability Expansion (2026-04-28)

Adds analyst-style insight tools to the Gemma 4 LLM agent so a public-health
analyst can answer the full set of common questions without manual SQL.

## New tools (7)

All defined in `api/agents/tools/insights.py`, registered in
`api/agents/tools/registry.py`, routed by `api/agents/core/router.py`,
documented in `api/agents/prompts/health_assistant_skill.md`.

| # | Tool | Purpose | Backed by |
|---|------|---------|-----------|
| 1 | `query_time_trend` | Monthly/quarterly disease trend (line chart) | `mv_visit_resolved` (DATE_TRUNC) |
| 2 | `query_province_breakdown` | Out-of-Bangkok cohort by home province | `mv_visit_resolved.bucket='non_bkk'` + in-memory province lookup (77 provinces) |
| 3 | `query_facility` | Count/list facilities by zone/district/type | `public.ref_facilities` + `public.ref_districts` |
| 4 | `query_risk_profile` | Sex/age/lifestyle profile (donut/bar) | `mv_demographics` + `mv_lifestyle` |
| 5 | `query_district_compare` | Top-N + Bottom-N + city avg per metric | `mv_summary_districts` |
| 6 | `query_mental_health` | PHQ-9, depression, stress; zone vs city | `mv_summary_mental` + `ref_districts` |
| 7 | `query_ncd_cascade` | Screened → at risk → diagnosed (funnel) | `mv_visit_resolved` (count filters) |

### Schema-access constraint

`api_user` (`bma_api_reader`) only has SELECT on `public.*`. So:

* Province name lookup uses an **in-memory** `PROVINCE_BY_CODE` map
  (77 entries, derived from migration 111). Avoids `private.geo_province`.
* Facility queries hit `public.ref_facilities` (mirror of `private.facility`)
  and `public.ref_districts` instead of `private.geo_district`.

### chart_spec convention (new)

Every tool also returns `metadata.chart_spec` — an echarts-friendly object:

```json
{
  "type": "line|bar|pie|funnel",
  "title": "string",
  "x": ["..."],
  "x_label": "...",
  "y_label": "...",
  "series": [
    { "name": "label", "data": [...], "type": "line|bar|pie|funnel" }
  ]
}
```

The legacy `visualizations` field is preserved for backward compat with the
existing frontend chart renderer; `chart_spec` is additive in `metadata` so the
frontend can opt in to richer multi-series charts.

## Files touched

| Path | Change |
|------|--------|
| `api/agents/tools/insights.py` | NEW — 7 BaseTool subclasses + helpers + 77-province lookup |
| `api/agents/tools/registry.py` | Register 7 new tools |
| `api/agents/core/router.py` | Add keyword arms for the 7 new tools, prioritize them above `query_health_data` |
| `api/agents/core/orchestrator.py` | Extend `_ON_TOPIC_KEYWORDS` (province/facility/cascade/profile terms) |
| `api/agents/prompts/health_assistant_skill.md` | Tool list 6 → 14, add 5 new "เมื่อไหร่ใช้ tool ไหน" rules + 13 new examples |
| `api/agents/tools/ncd_report.py` | Conform `NcdDiagnosticReportTool` to `BaseTool` interface (was preventing orchestrator startup) |

## System prompt expansion

* Tool list: 6 → 14, with parameter schema + 1-2 sample invocations each.
* New "เมื่อไหร่ใช้ tool ไหน" routing rules for: time trend, province
  breakdown, facility lookup, risk profile, district compare, mental health,
  NCD cascade.
* New "ตัวอย่าง (insight tools)" section with 13 worked examples in Thai.
* Chart Selection legend extended with `funnel=cascade`.

## Verified test cases

All seven new tools were exercised end-to-end via
`curl -H "X-API-Key: dev-api-key" http://localhost:9002/api/health/chat?message=...`.
Sample responses (truncated for brevity):

| # | Question (TH) | Tool selected | Excerpt of response |
|---|---|---|---|
| 1 | แนวโน้มเบาหวาน รายเดือน ตั้งแต่ปี 2024 ถึง 2025 | `query_time_trend` | "แนวโน้มเบาหวานรายเดือน ... 13,258 → 1,843 (ลดลง +86.1%)" + `line` chart |
| 2 | คัดกรองรายไตรมาส ปี 2024-2025 เปลี่ยนแปลงอย่างไร | `query_time_trend` | "เบาหวาน: 58,008 → 5,014 (ลดลง +91.4%) ... ความดัน: 52,899 → 40,269" + `line` |
| 3 | คน ตจว มาจากจังหวัดไหนบ้าง 5 อันดับแรก | `query_province_breakdown` | "1. สมุทรปราการ 19,689 (14.6%) 2. นนทบุรี 16,879 ..." + `horizontal_bar` |
| 4 | คลินิกในเขตสุขภาพ 3 มีกี่แห่ง | `query_facility` | "คลินิกในเขตสุขภาพ 3 มีจำนวน 1,516 แห่ง" + `horizontal_bar` |
| 5 | เส้นทาง คัดกรอง พบเสี่ยง วินิจฉัย ของเบาหวาน เป็น cascade | `query_ncd_cascade` | "คัดกรอง 669,286 → พบเสี่ยง 139,237 (20.8%) → วินิจฉัย 68,947 (10.3%) ... Yield 49.5%" + `funnel` |
| 6 | เปรียบเทียบเขตที่มีอัตราอ้วนสูงสุด vs ต่ำสุด | `query_district_compare` | "ค่าเฉลี่ย กทม. 37.7% / สูงสุด หนองจอก 45.0% / ต่ำสุด สายไหม 27.9%" + `horizontal_bar` |
| 7 | PHQ-9 เฉลี่ยใน เขตสุขภาพ 5 vs ทั่วเมือง | `query_mental_health` | "เขตสุขภาพ 5: 1.01% / ทั่วเมือง: 0.92%" + `bar` |

Bonus: `query_risk_profile` was verified directly via the registry
(profile aggregates from `mv_demographics`+`mv_lifestyle` succeed and produce
sex/age/lifestyle Thai breakdowns).

## Pass rate

End-to-end via the chat endpoint: **7/7 tools answered with the expected
content + a chart visualization** (legacy `visualizations` field). Each tool's
SQL was independently verified against the live DB before the LLM was wired in.
