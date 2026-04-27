"""Insight tools — answers analyst-style questions backed by pre-aggregated MVs.

Each tool returns ToolResult with:
  - text: human-readable Thai summary (LLM uses this for synthesis)
  - visualizations: legacy chart format (frontend already renders)
  - metadata.chart_spec: echarts-friendly chart spec for direct rendering

SYNC — calls database.execute_query() directly.

NOTE on schema access:
  api_user (bma_api_reader) has SELECT on public.* ONLY.
  We cannot reference private.* — use:
    - public.ref_facilities (mirror of private.facility)
    - public.ref_districts (mirror of private.geo_district + zone_code)
    - public.mv_visit_resolved, public.mv_summary_*
  Province name lookup is done in-memory (PROVINCE_BY_CODE).
"""
from __future__ import annotations

import logging
from typing import Any

from agents.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


def _query(sql: str, params: tuple = None) -> list[dict]:
    from database import execute_query
    return execute_query(sql, params)


def _scalar(sql: str, params: tuple = None):
    from database import execute_scalar
    return execute_scalar(sql, params)


# 77 provinces (from migration 111). Province code = first 2 digits of dcode.
# (name_th, region)
PROVINCE_BY_CODE: dict[str, tuple[str, str]] = {
    "10": ("กรุงเทพมหานคร", "Central"),
    "11": ("สมุทรปราการ", "Central"), "12": ("นนทบุรี", "Central"),
    "13": ("ปทุมธานี", "Central"), "14": ("พระนครศรีอยุธยา", "Central"),
    "15": ("อ่างทอง", "Central"), "16": ("ลพบุรี", "Central"),
    "17": ("สิงห์บุรี", "Central"), "18": ("ชัยนาท", "Central"),
    "19": ("สระบุรี", "Central"),
    "20": ("ชลบุรี", "East"), "21": ("ระยอง", "East"),
    "22": ("จันทบุรี", "East"), "23": ("ตราด", "East"),
    "24": ("ฉะเชิงเทรา", "East"), "25": ("ปราจีนบุรี", "East"),
    "26": ("นครนายก", "Central"), "27": ("สระแก้ว", "East"),
    "30": ("นครราชสีมา", "Northeast"), "31": ("บุรีรัมย์", "Northeast"),
    "32": ("สุรินทร์", "Northeast"), "33": ("ศรีสะเกษ", "Northeast"),
    "34": ("อุบลราชธานี", "Northeast"), "35": ("ยโสธร", "Northeast"),
    "36": ("ชัยภูมิ", "Northeast"), "37": ("อำนาจเจริญ", "Northeast"),
    "38": ("บึงกาฬ", "Northeast"), "39": ("หนองบัวลำภู", "Northeast"),
    "40": ("ขอนแก่น", "Northeast"), "41": ("อุดรธานี", "Northeast"),
    "42": ("เลย", "Northeast"), "43": ("หนองคาย", "Northeast"),
    "44": ("มหาสารคาม", "Northeast"), "45": ("ร้อยเอ็ด", "Northeast"),
    "46": ("กาฬสินธุ์", "Northeast"), "47": ("สกลนคร", "Northeast"),
    "48": ("นครพนม", "Northeast"), "49": ("มุกดาหาร", "Northeast"),
    "50": ("เชียงใหม่", "North"), "51": ("ลำพูน", "North"),
    "52": ("ลำปาง", "North"), "53": ("อุตรดิตถ์", "North"),
    "54": ("แพร่", "North"), "55": ("น่าน", "North"),
    "56": ("พะเยา", "North"), "57": ("เชียงราย", "North"),
    "58": ("แม่ฮ่องสอน", "North"),
    "60": ("นครสวรรค์", "Central"), "61": ("อุทัยธานี", "Central"),
    "62": ("กำแพงเพชร", "Central"), "63": ("ตาก", "West"),
    "64": ("สุโขทัย", "Central"), "65": ("พิษณุโลก", "Central"),
    "66": ("พิจิตร", "Central"), "67": ("เพชรบูรณ์", "Central"),
    "70": ("ราชบุรี", "West"), "71": ("กาญจนบุรี", "West"),
    "72": ("สุพรรณบุรี", "Central"), "73": ("นครปฐม", "Central"),
    "74": ("สมุทรสาคร", "Central"), "75": ("สมุทรสงคราม", "Central"),
    "76": ("เพชรบุรี", "West"), "77": ("ประจวบคีรีขันธ์", "West"),
    "80": ("นครศรีธรรมราช", "South"), "81": ("กระบี่", "South"),
    "82": ("พังงา", "South"), "83": ("ภูเก็ต", "South"),
    "84": ("สุราษฎร์ธานี", "South"), "85": ("ระนอง", "South"),
    "86": ("ชุมพร", "South"),
    "90": ("สงขลา", "South"), "91": ("สตูล", "South"),
    "92": ("ตรัง", "South"), "93": ("พัทลุง", "South"),
    "94": ("ปัตตานี", "South"), "95": ("ยะลา", "South"),
    "96": ("นราธิวาส", "South"),
}


def _viz(chart_type: str, title: str, data: list[dict], xKey: str = "name",
         yKey: str = "value", color: str = "#00744B", yLabel: str = "") -> dict:
    """Legacy viz format (used by existing frontend chart renderer)."""
    return {
        "type": chart_type,
        "title": title,
        "data": data,
        "xKey": xKey,
        "yKey": yKey,
        "color": color,
        "yLabel": yLabel,
    }


def _chart_spec(chart_type: str, title: str, x: list, series: list[dict],
                x_label: str = "", y_label: str = "") -> dict:
    """Echarts-friendly spec for the frontend to render directly.

    series: [{ name: "...", data: [...], type: "line"|"bar"|... }]
    """
    return {
        "type": chart_type,
        "title": title,
        "x": x,
        "x_label": x_label,
        "y_label": y_label,
        "series": series,
    }


# ---------------------------------------------------------------------------
# 1. Time trend tool — monthly/quarterly disease counts
# ---------------------------------------------------------------------------

_DISEASE_TREND_FIELDS = {
    "diabetes": ("risk_dm", "เบาหวาน"),
    "hypertension": ("risk_hpt", "ความดันโลหิตสูง"),
    "cardiovascular": ("risk_cvd", "หลอดเลือดหัวใจ"),
    "obesity": ("found_obesity", "อ้วน"),
    "dyslipidemia": ("found_dyslipidemia", "ไขมันในเลือดสูง"),
    "stroke": ("found_stroke", "หลอดเลือดสมอง"),
}


class TimeTrendTool(BaseTool):
    name = "query_time_trend"
    description = (
        "Show monthly or quarterly screening trend for one or more diseases. "
        "Use for 'แนวโน้ม', 'เปลี่ยนไปยังไง', 'รายเดือน/ไตรมาส', 'ปี 2024 vs 2025'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "disease": {
                "type": "string",
                "enum": list(_DISEASE_TREND_FIELDS.keys()),
                "description": "Disease key. Omit to show total screened only.",
            },
            "period": {
                "type": "string",
                "enum": ["month", "quarter"],
                "description": "Aggregation granularity. Default: month.",
            },
            "from_date": {
                "type": "string",
                "description": "Start date (YYYY-MM-DD). Default: 2024-01-01.",
            },
            "to_date": {
                "type": "string",
                "description": "End date (YYYY-MM-DD). Default: today.",
            },
        },
    }

    def execute(self, args: dict) -> ToolResult:
        from security import K_ANONYMITY_THRESHOLD

        disease = args.get("disease")
        period = args.get("period", "month")
        from_date = args.get("from_date", "2024-01-01")
        to_date = args.get("to_date")

        # Build per-disease COUNT FILTER columns
        filters_sql = []
        keys: list[tuple[str, str]] = []
        if disease and disease in _DISEASE_TREND_FIELDS:
            col, th = _DISEASE_TREND_FIELDS[disease]
            filters_sql.append(f"COUNT(DISTINCT patient_id) FILTER (WHERE {col}) AS {col}")
            keys.append((col, th))
        else:
            for col, th in _DISEASE_TREND_FIELDS.values():
                filters_sql.append(f"COUNT(DISTINCT patient_id) FILTER (WHERE {col}) AS {col}")
                keys.append((col, th))

        bucket = "month" if period == "month" else "quarter"
        date_filter = "AND visit_date >= %s"
        params: list[Any] = [from_date]
        if to_date:
            date_filter += " AND visit_date <= %s"
            params.append(to_date)
        params.append(K_ANONYMITY_THRESHOLD)

        sql = f"""
            SELECT DATE_TRUNC('{bucket}', visit_date)::date AS period,
                   COUNT(DISTINCT patient_id) AS total,
                   {', '.join(filters_sql)}
            FROM public.mv_visit_resolved
            WHERE cancel_status = 0 AND visit_date IS NOT NULL {date_filter}
            GROUP BY DATE_TRUNC('{bucket}', visit_date)
            HAVING COUNT(DISTINCT patient_id) >= %s
            ORDER BY period
        """
        rows = _query(sql, tuple(params))
        if not rows:
            return ToolResult(text="ไม่พบข้อมูลในช่วงเวลาที่ระบุ")

        x = [str(r["period"])[:10] for r in rows]
        series = []
        # Total line always first
        series.append({"name": "คัดกรองทั้งหมด", "data": [int(r["total"] or 0) for r in rows], "type": "line"})
        for col, th in keys:
            series.append({"name": th, "data": [int(r.get(col) or 0) for r in rows], "type": "line"})

        # Build summary text
        lines = [f"แนวโน้มราย{('เดือน' if bucket == 'month' else 'ไตรมาส')} ({x[0]} ถึง {x[-1]}):"]
        first = rows[0]
        last = rows[-1]
        for col, th in keys:
            v0 = int(first.get(col) or 0)
            v1 = int(last.get(col) or 0)
            delta_pct = round(100.0 * (v1 - v0) / max(v0, 1), 1)
            arrow = "เพิ่มขึ้น" if v1 > v0 else "ลดลง"
            lines.append(f"- {th}: {v0:,} → {v1:,} ({arrow} {abs(delta_pct):+.1f}%)")
        lines.append(f"- คัดกรองรวม: {int(first['total'] or 0):,} → {int(last['total'] or 0):,}")

        # Legacy viz (single-series line, total)
        viz = [_viz(
            "line",
            f"แนวโน้มการคัดกรองราย{'เดือน' if bucket=='month' else 'ไตรมาส'}",
            [{"name": x_, "value": s["data"][i]} for i, x_ in enumerate(x) for s in series[1:2]] if len(series) > 1 else [{"name": x_, "value": series[0]["data"][i]} for i, x_ in enumerate(x)],
            yLabel="จำนวนคน",
        )]

        chart_spec = _chart_spec(
            "line",
            f"แนวโน้มราย{'เดือน' if bucket=='month' else 'ไตรมาส'}",
            x=x,
            series=series,
            x_label=("เดือน" if bucket == "month" else "ไตรมาส"),
            y_label="จำนวนคน",
        )
        return ToolResult(
            text="\n".join(lines),
            visualizations=viz,
            metadata={"chart_spec": chart_spec, "rows": rows},
        )


# ---------------------------------------------------------------------------
# 2. Province breakdown — non-BKK origin
# ---------------------------------------------------------------------------

class ProvinceBreakdownTool(BaseTool):
    name = "query_province_breakdown"
    description = (
        "Break down out-of-Bangkok (ตจว.) screened persons by home province. "
        "Use for 'มาจากจังหวัดไหน', 'ตจว.', 'ต่างจังหวัด', 'นอก กทม.'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "top_n": {"type": "integer", "description": "Top N provinces to return. Default: 10."},
            "region": {"type": "string", "description": "Optional: filter by region (Central/North/Northeast/East/South/West)."},
        },
    }

    def execute(self, args: dict) -> ToolResult:
        from security import K_ANONYMITY_THRESHOLD
        top_n = int(args.get("top_n", 10))
        region = args.get("region")

        # No private.geo_province access — group by 2-digit prefix only,
        # then resolve province name in Python.
        sql = """
            SELECT LEFT(v.home_district_code, 2) AS pcode,
                   COUNT(DISTINCT v.patient_id) AS persons,
                   COUNT(*) AS visits
            FROM public.mv_visit_resolved v
            WHERE v.cancel_status = 0
              AND v.bucket = 'non_bkk'
              AND v.home_district_code IS NOT NULL
            GROUP BY LEFT(v.home_district_code, 2)
            HAVING COUNT(DISTINCT v.patient_id) >= %s
            ORDER BY persons DESC
        """
        rows = _query(sql, (K_ANONYMITY_THRESHOLD,))
        # Resolve names + filter region
        enriched = []
        for r in rows:
            pcode = r["pcode"]
            name_th, prov_region = PROVINCE_BY_CODE.get(pcode, (f"รหัส {pcode}", ""))
            if region and prov_region != region:
                continue
            enriched.append({
                "pcode": pcode,
                "province": name_th,
                "region": prov_region,
                "persons": int(r["persons"]),
                "visits": int(r["visits"]),
            })
        enriched = enriched[:top_n]
        if not enriched:
            return ToolResult(text="ไม่พบข้อมูลจังหวัดนอก กทม.")

        total_non_bkk = int(_scalar(
            "SELECT COUNT(DISTINCT patient_id) FROM public.mv_visit_resolved "
            "WHERE bucket='non_bkk' AND cancel_status=0"
        ) or 0)
        rows = enriched  # use enriched downstream

        lines = [f"คน ตจว. ที่มาคัดกรองในโครงการ ({total_non_bkk:,} คน) — Top {len(rows)}:"]
        for i, r in enumerate(rows, 1):
            pname = r.get("province") or f"รหัส {r.get('pcode')}"
            n = int(r["persons"])
            pct = round(100.0 * n / max(total_non_bkk, 1), 1)
            lines.append(f"{i}. {pname} ({r.get('region') or '-'}): {n:,} คน ({pct}%)")

        chart_data = [
            {"name": (r.get("province") or f"รหัส {r.get('pcode')}"), "value": int(r["persons"])}
            for r in rows
        ]
        viz = [_viz("horizontal_bar", f"จังหวัดต้นทางของคน ตจว. — Top {len(rows)}",
                    chart_data, yLabel="จำนวนคน", color="#00744B")]
        chart_spec = _chart_spec(
            "bar",
            f"จังหวัดต้นทาง — Top {len(rows)}",
            x=[d["name"] for d in chart_data],
            series=[{"name": "จำนวนคน", "data": [d["value"] for d in chart_data], "type": "bar"}],
            y_label="จำนวนคน",
        )
        return ToolResult(
            text="\n".join(lines),
            visualizations=viz,
            metadata={"chart_spec": chart_spec, "rows": rows, "total_non_bkk": total_non_bkk},
        )


# ---------------------------------------------------------------------------
# 3. Facility lookup — count/list facilities by zone/district/type
# ---------------------------------------------------------------------------

class FacilityLookupTool(BaseTool):
    name = "query_facility"
    description = (
        "ค้นหา/นับสถานพยาบาล (รพ./คลินิก/ร้านยา) แยกตามเขต/โซน/ประเภท. "
        "ใช้กับคำถามเกี่ยวกับ 'จำนวนสถานพยาบาล', 'รพ.ในเขต', 'คลินิกในโซน', 'ร้านยา', "
        "'มีกี่แห่ง', 'มีกี่ที่', facility count. "
        "ห้ามใช้กับคำถามเรื่องโรคหรือสถิติผู้ป่วย — ใช้ query_health_data แทน."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "zone_code": {"type": "string", "description": "Zone code 01-08."},
            "district_code": {"type": "string", "description": "District code (4-digit)."},
            "district_name": {"type": "string", "description": "District name in Thai."},
            "facility_type": {"type": "string", "description": "Optional facility type filter (Thai)."},
            "list_count": {"type": "integer", "description": "How many facilities to list (0 = count only). Default: 5."},
        },
    }

    def execute(self, args: dict) -> ToolResult:
        zone_code = args.get("zone_code")
        district_code = args.get("district_code")
        district_name = args.get("district_name")
        facility_type = args.get("facility_type")
        list_count = int(args.get("list_count", 5))

        # Resolve district_name -> code if needed (use public.ref_districts)
        if district_name and not district_code:
            row = _query(
                "SELECT dcode FROM public.ref_districts WHERE name_th LIKE %s LIMIT 1",
                (f"%{district_name.replace('เขต','').strip()}%",),
            )
            if row:
                district_code = row[0]["dcode"]

        # Normalize zone code: '3' -> '03'
        if zone_code:
            zone_code = str(zone_code).zfill(2)

        # public.ref_facilities lacks an `active` column; all rows are assumed active.
        where = ["1=1"]
        params: list[Any] = []
        if zone_code:
            where.append("f.zone_code = %s")
            params.append(zone_code)
        if district_code:
            where.append("f.district_code = %s")
            params.append(district_code)
        if facility_type:
            where.append("f.facility_type ILIKE %s")
            params.append(f"%{facility_type}%")

        where_sql = " AND ".join(where)
        # Total count + breakdown by type
        total = int(_scalar(
            f"SELECT COUNT(*) FROM public.ref_facilities f WHERE {where_sql}",
            tuple(params),
        ) or 0)
        by_type = _query(
            f"""SELECT f.facility_type, COUNT(*) AS n
                FROM public.ref_facilities f WHERE {where_sql}
                GROUP BY f.facility_type ORDER BY n DESC LIMIT 10""",
            tuple(params),
        )
        # Sample list (with district name)
        sample: list[dict] = []
        if list_count > 0:
            sample = _query(
                f"""SELECT f.code, f.name_th, f.facility_type,
                           f.zone_code, rd.name_th AS district_name
                    FROM public.ref_facilities f
                    LEFT JOIN public.ref_districts rd ON rd.dcode = f.district_code
                    WHERE {where_sql}
                    ORDER BY f.name_th LIMIT %s""",
                tuple(params + [list_count]),
            )

        # Build summary
        scope = []
        if zone_code:
            scope.append(f"เขตสุขภาพ {int(zone_code)}")
        if district_code:
            dn = _scalar(
                "SELECT name_th FROM public.ref_districts WHERE dcode = %s",
                (district_code,),
            )
            if dn:
                scope.append(f"เขต{dn}")
        if facility_type:
            scope.append(f"ประเภท: {facility_type}")
        scope_str = " ".join(scope) if scope else "ทั้ง กทม."

        lines = [f"สถานพยาบาล/คลินิก/ร้านยา ใน{scope_str}: **{total:,} แห่ง**"]
        if by_type and not facility_type:
            lines.append("\nแยกตามประเภท (Top 10):")
            for r in by_type:
                lines.append(f"- {r['facility_type'] or '(ไม่ระบุ)'}: {int(r['n']):,} แห่ง")
        if sample:
            lines.append(f"\nรายชื่อตัวอย่าง ({len(sample)} แห่ง):")
            for s in sample:
                d = f" — เขต{s['district_name']}" if s.get("district_name") else ""
                lines.append(f"- {s['name_th']} ({s['facility_type'] or '-'}){d}")

        chart_data = [{"name": r["facility_type"] or "ไม่ระบุ", "value": int(r["n"])} for r in by_type]
        viz: list[dict] = []
        if chart_data:
            viz = [_viz("horizontal_bar", f"สถานพยาบาลใน{scope_str} ({total:,} แห่ง)",
                        chart_data, yLabel="จำนวน", color="#00744B")]
        chart_spec = None
        if chart_data:
            chart_spec = _chart_spec(
                "bar",
                f"สถานพยาบาลใน{scope_str}",
                x=[d["name"] for d in chart_data],
                series=[{"name": "จำนวน", "data": [d["value"] for d in chart_data], "type": "bar"}],
                y_label="จำนวน",
            )
        return ToolResult(
            text="\n".join(lines),
            visualizations=viz,
            metadata={"chart_spec": chart_spec, "total": total, "by_type": by_type, "sample": sample},
        )


# ---------------------------------------------------------------------------
# 4. Risk-factor profile — multi-dimension demographics + lifestyle
# ---------------------------------------------------------------------------

_AGE_BAND_TH = {
    "lt20": "ต่ำกว่า 20",
    "20_34": "20-34",
    "35_49": "35-49",
    "50_64": "50-64",
    "65plus": "65+",
    "unknown": "ไม่ระบุ",
}

_LIFESTYLE_VAR_TH = {
    "smoking": "การสูบบุหรี่",
    "alcohol": "การดื่มแอลกอฮอล์",
    "exercise": "การออกกำลังกาย",
}


class RiskProfileTool(BaseTool):
    name = "query_risk_profile"
    description = (
        "Risk-factor profile of screened patients: sex/age/lifestyle breakdown. "
        "Use for 'ผู้ป่วย X เพศไหน อายุเท่าไหร่ สูบบุหรี่ไหม', 'โปรไฟล์'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "dimension": {
                "type": "string",
                "enum": ["sex", "age", "lifestyle", "all"],
                "description": "Which profile dimension to return. 'all' returns sex+age+lifestyle.",
            },
            "lifestyle_var": {
                "type": "string",
                "enum": ["smoking", "alcohol", "exercise"],
                "description": "If dimension=lifestyle, pick one. Default: exercise.",
            },
            "district_code": {"type": "string", "description": "Optional: scope to one district."},
            "zone_code": {"type": "string", "description": "Optional: scope to one zone."},
        },
    }

    def execute(self, args: dict) -> ToolResult:
        dim = args.get("dimension", "all")
        lifestyle_var = args.get("lifestyle_var", "exercise")
        district_code = args.get("district_code")
        zone_code = args.get("zone_code")

        # Build a CTE that resolves districts in scope
        if district_code:
            scope_sql = "AND district_code = %s"
            scope_params: tuple = (district_code,)
            scope_label = f"เขต รหัส {district_code}"
        elif zone_code:
            zone_code = str(zone_code).zfill(2)
            scope_sql = (
                "AND district_code IN (SELECT dcode FROM public.ref_districts WHERE zone_code = %s)"
            )
            scope_params = (zone_code,)
            scope_label = f"เขตสุขภาพ {int(zone_code)}"
        else:
            scope_sql = ""
            scope_params = tuple()
            scope_label = "ทั้ง กทม."

        out = {"sex": [], "age": [], "lifestyle": []}
        text_lines: list[str] = [f"## โปรไฟล์ผู้คัดกรอง ({scope_label})"]

        if dim in ("sex", "all"):
            rows = _query(
                f"""SELECT sex_code, SUM(persons)::bigint AS persons
                    FROM public.mv_demographics
                    WHERE district_code IS NOT NULL {scope_sql}
                    GROUP BY sex_code""",
                scope_params,
            )
            total = sum(int(r["persons"]) for r in rows) or 1
            text_lines.append("\n**เพศ**")
            mapping = {"M": "ชาย", "F": "หญิง", "unknown": "ไม่ระบุ"}
            for r in rows:
                sx = mapping.get(r["sex_code"], r["sex_code"])
                n = int(r["persons"])
                pct = round(100.0 * n / total, 1)
                text_lines.append(f"- {sx}: {n:,} คน ({pct}%)")
                out["sex"].append({"name": sx, "value": n})

        if dim in ("age", "all"):
            rows = _query(
                f"""SELECT age_band, SUM(persons)::bigint AS persons
                    FROM public.mv_demographics
                    WHERE district_code IS NOT NULL {scope_sql}
                    GROUP BY age_band ORDER BY age_band""",
                scope_params,
            )
            total = sum(int(r["persons"]) for r in rows) or 1
            text_lines.append("\n**ช่วงอายุ**")
            for r in rows:
                ab = _AGE_BAND_TH.get(r["age_band"], r["age_band"])
                n = int(r["persons"])
                pct = round(100.0 * n / total, 1)
                text_lines.append(f"- {ab}: {n:,} คน ({pct}%)")
                out["age"].append({"name": ab, "value": n})

        if dim in ("lifestyle", "all"):
            rows = _query(
                f"""SELECT value, SUM(persons)::bigint AS persons
                    FROM public.mv_lifestyle
                    WHERE variable_key = %s {scope_sql.replace('district_code','district_code')}
                    GROUP BY value ORDER BY persons DESC LIMIT 10""",
                (lifestyle_var,) + scope_params,
            )
            total = sum(int(r["persons"]) for r in rows) or 1
            label = _LIFESTYLE_VAR_TH.get(lifestyle_var, lifestyle_var)
            text_lines.append(f"\n**{label}**")
            for r in rows:
                v = (r["value"] or "")[:50]
                n = int(r["persons"])
                pct = round(100.0 * n / total, 1)
                text_lines.append(f"- {v}: {n:,} คน ({pct}%)")
                out["lifestyle"].append({"name": v, "value": n})

        # Pick best primary chart for visualization
        primary = None
        primary_label = ""
        if dim == "sex" or (dim == "all" and out["sex"]):
            primary = out["sex"]
            primary_label = "เพศ"
        elif dim == "age" or (dim == "all" and out["age"]):
            primary = out["age"]
            primary_label = "ช่วงอายุ"
        elif dim == "lifestyle":
            primary = out["lifestyle"]
            primary_label = _LIFESTYLE_VAR_TH.get(lifestyle_var, lifestyle_var)

        viz: list[dict] = []
        chart_spec = None
        if primary:
            viz = [_viz("donut" if len(primary) <= 6 else "bar",
                        f"โปรไฟล์ผู้คัดกรอง — {primary_label}",
                        primary, yLabel="จำนวนคน")]
            chart_spec = _chart_spec(
                "pie" if len(primary) <= 6 else "bar",
                f"โปรไฟล์ผู้คัดกรอง — {primary_label}",
                x=[d["name"] for d in primary],
                series=[{"name": primary_label, "data": [d["value"] for d in primary],
                         "type": "pie" if len(primary) <= 6 else "bar"}],
            )
        return ToolResult(
            text="\n".join(text_lines),
            visualizations=viz,
            metadata={"chart_spec": chart_spec, "profile": out, "scope": scope_label},
        )


# ---------------------------------------------------------------------------
# 5. District comparison — top-N / bottom-N / vs city average
# ---------------------------------------------------------------------------

_METRIC_MAP = {
    # (column, direction, th_label)
    "diabetes": ("pct_risk_dm", "เบาหวาน"),
    "hypertension": ("pct_risk_hpt", "ความดันโลหิตสูง"),
    "cardiovascular": ("pct_risk_cvd", "หลอดเลือดหัวใจ"),
    "obesity": ("pct_risk_bmi", "อ้วน/น้ำหนักเกิน"),  # use pct_risk_bmi (%) which is reliable
    "screened": ("total_screened", "จำนวนคัดกรอง"),
}


class DistrictCompareTool(BaseTool):
    name = "query_district_compare"
    description = (
        "Compare districts: top N, bottom N, and city average for a disease/metric. "
        "Use for 'เปรียบเทียบเขต', 'สูงสุด vs ต่ำสุด', 'อันดับ', 'percentile'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "enum": list(_METRIC_MAP.keys()),
                "description": "Disease/metric to compare. Default: diabetes.",
            },
            "top_n": {"type": "integer", "description": "Top N. Default: 5."},
            "bottom_n": {"type": "integer", "description": "Bottom N. Default: 5."},
        },
    }

    def execute(self, args: dict) -> ToolResult:
        metric = args.get("metric", "diabetes")
        if metric not in _METRIC_MAP:
            return ToolResult(text=f"ไม่รู้จัก metric '{metric}'")
        col, label = _METRIC_MAP[metric]
        top_n = int(args.get("top_n", 5))
        bottom_n = int(args.get("bottom_n", 5))

        # Top N
        top = _query(
            f"""SELECT district_name, zone_code, total_screened, {col} AS metric
                FROM public.mv_summary_districts
                WHERE total_screened >= 100
                ORDER BY metric DESC NULLS LAST LIMIT %s""",
            (top_n,),
        )
        # Bottom N (only districts with sufficient sample size)
        bottom = _query(
            f"""SELECT district_name, zone_code, total_screened, {col} AS metric
                FROM public.mv_summary_districts
                WHERE total_screened >= 100 AND {col} IS NOT NULL
                ORDER BY metric ASC LIMIT %s""",
            (bottom_n,),
        )
        # City average (weighted)
        if metric == "screened":
            city_avg = float(_scalar(
                "SELECT AVG(total_screened) FROM public.mv_summary_districts WHERE total_screened >= 100"
            ) or 0)
            unit = "คน"
        else:
            city_avg = float(_scalar(
                f"""SELECT 100.0 * SUM({col.replace('pct_','').replace('risk_','risk_')+'_count' if col.startswith('pct_') else col}) / NULLIF(SUM(total_screened),0)
                    FROM public.mv_summary_districts"""
            ) or 0)
            # Fallback: simple average
            if not city_avg:
                city_avg = float(_scalar(
                    f"SELECT AVG({col}) FROM public.mv_summary_districts WHERE total_screened >= 100"
                ) or 0)
            unit = "%"

        is_pct = unit == "%"
        fmt = (lambda v: f"{round(float(v), 1)}{unit}") if is_pct else (lambda v: f"{int(v):,} {unit}")

        lines = [f"## เปรียบเทียบเขต — {label}"]
        lines.append(f"**ค่าเฉลี่ยทั้ง กทม.: {fmt(city_avg)}**")
        lines.append(f"\n### Top {len(top)} (สูงสุด)")
        for i, r in enumerate(top, 1):
            lines.append(f"{i}. เขต{r['district_name']} (โซน {int(r['zone_code']) if r.get('zone_code') else '-'}): "
                         f"{fmt(r['metric'])} (คัดกรอง {int(r['total_screened']):,} คน)")
        lines.append(f"\n### Bottom {len(bottom)} (ต่ำสุด)")
        for i, r in enumerate(bottom, 1):
            lines.append(f"{i}. เขต{r['district_name']} (โซน {int(r['zone_code']) if r.get('zone_code') else '-'}): "
                         f"{fmt(r['metric'])} (คัดกรอง {int(r['total_screened']):,} คน)")

        # Combine into one chart (top + bottom, sorted desc)
        chart_rows = list(top) + list(bottom)
        chart_data = [{"name": r["district_name"], "value": float(r["metric"] or 0)} for r in chart_rows]
        chart_data.sort(key=lambda x: x["value"], reverse=True)
        viz = [_viz("horizontal_bar", f"{label}: Top {top_n} vs Bottom {bottom_n}",
                    chart_data, yLabel=unit, color="#00744B")]
        chart_spec = _chart_spec(
            "bar",
            f"{label}: Top vs Bottom",
            x=[d["name"] for d in chart_data],
            series=[{"name": label, "data": [d["value"] for d in chart_data], "type": "bar"}],
            y_label=unit,
        )
        return ToolResult(
            text="\n".join(lines),
            visualizations=viz,
            metadata={"chart_spec": chart_spec, "top": top, "bottom": bottom, "city_avg": city_avg, "unit": unit},
        )


# ---------------------------------------------------------------------------
# 6. Mental health drilldown — zone vs city
# ---------------------------------------------------------------------------

class MentalHealthCompareTool(BaseTool):
    name = "query_mental_health"
    description = (
        "PHQ-9, depression risk, stress comparison. Optionally compare a zone vs city. "
        "Use for 'PHQ-9', 'ซึมเศร้า', 'สุขภาพจิต', 'เครียด'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "zone_code": {"type": "string", "description": "Optional zone code 01-08 to drill down."},
            "metric": {
                "type": "string",
                "enum": ["phq9_moderate", "depression_risk", "high_stress", "all"],
                "description": "Which mental-health metric to highlight. Default: all.",
            },
        },
    }

    def execute(self, args: dict) -> ToolResult:
        zone_code = args.get("zone_code")
        metric = args.get("metric", "all")
        if zone_code:
            zone_code = str(zone_code).zfill(2)

        # City weighted averages
        city = _query("""
            SELECT SUM(total_screened) AS total,
                   ROUND((SUM(total_screened * pct_phq9_moderate) / NULLIF(SUM(total_screened),0))::numeric, 2) AS pct_phq9,
                   ROUND((SUM(total_screened * pct_depression_risk) / NULLIF(SUM(total_screened),0))::numeric, 2) AS pct_dep,
                   ROUND((SUM(total_screened * pct_high_stress) / NULLIF(SUM(total_screened),0))::numeric, 2) AS pct_stress
            FROM public.mv_summary_mental
        """)
        c = city[0] if city else {}

        lines = ["## สุขภาพจิต — สรุปทั้ง กทม."]
        lines.append(f"- คัดกรอง {int(c.get('total') or 0):,} คน")
        lines.append(f"- PHQ-9 moderate ขึ้นไป: {c.get('pct_phq9') or 0}%")
        lines.append(f"- เสี่ยงซึมเศร้า: {c.get('pct_dep') or 0}%")
        lines.append(f"- ความเครียดสูง: {c.get('pct_stress') or 0}%")

        zone_data: dict = {}
        chart_x: list[str] = []
        chart_series_phq = []
        chart_series_stress = []
        zones = _query("""
            SELECT rd.zone_code,
                   SUM(m.total_screened) AS total,
                   ROUND((SUM(m.total_screened * m.pct_phq9_moderate) / NULLIF(SUM(m.total_screened),0))::numeric,2) AS pct_phq9,
                   ROUND((SUM(m.total_screened * m.pct_depression_risk) / NULLIF(SUM(m.total_screened),0))::numeric,2) AS pct_dep,
                   ROUND((SUM(m.total_screened * m.pct_high_stress) / NULLIF(SUM(m.total_screened),0))::numeric,2) AS pct_stress
            FROM public.mv_summary_mental m
            JOIN public.ref_districts rd ON rd.dcode = m.district_code
            GROUP BY rd.zone_code ORDER BY rd.zone_code
        """)
        for z in zones:
            zc = z["zone_code"]
            chart_x.append(f"โซน {int(zc)}")
            chart_series_phq.append(float(z.get("pct_phq9") or 0))
            chart_series_stress.append(float(z.get("pct_stress") or 0))
            if zone_code and zc == zone_code:
                zone_data = z

        if zone_code and zone_data:
            lines.append(f"\n## เขตสุขภาพ {int(zone_code)}")
            lines.append(f"- คัดกรอง {int(zone_data.get('total') or 0):,} คน")
            lines.append(f"- PHQ-9 moderate: {zone_data.get('pct_phq9')}% (เทียบ กทม. {c.get('pct_phq9')}%)")
            lines.append(f"- เสี่ยงซึมเศร้า: {zone_data.get('pct_dep')}% (เทียบ กทม. {c.get('pct_dep')}%)")
            lines.append(f"- ความเครียดสูง: {zone_data.get('pct_stress')}% (เทียบ กทม. {c.get('pct_stress')}%)")
            zp = float(zone_data.get("pct_phq9") or 0)
            cp = float(c.get("pct_phq9") or 0)
            if zp > cp:
                lines.append(f"\n*PHQ-9 ในเขตนี้สูงกว่าค่าเฉลี่ยเมือง {round(zp - cp, 2)} จุด*")
            else:
                lines.append(f"\n*PHQ-9 ในเขตนี้ต่ำกว่าค่าเฉลี่ยเมือง {round(cp - zp, 2)} จุด*")

        # Chart: zone-by-zone PHQ-9 with city-average reference line
        chart_data = [{"name": chart_x[i], "value": chart_series_phq[i]} for i in range(len(chart_x))]
        viz = [_viz("bar", "PHQ-9 (% moderate ขึ้นไป) แยกรายโซน",
                    chart_data, yLabel="%", color="#00744B")]
        chart_spec = _chart_spec(
            "bar",
            "PHQ-9 และความเครียดสูง รายโซน",
            x=chart_x,
            series=[
                {"name": "PHQ-9 moderate", "data": chart_series_phq, "type": "bar"},
                {"name": "เครียดสูง", "data": chart_series_stress, "type": "bar"},
            ],
            y_label="%",
        )
        return ToolResult(
            text="\n".join(lines),
            visualizations=viz,
            metadata={
                "chart_spec": chart_spec,
                "city": c,
                "by_zone": zones,
                "zone_focus": zone_data or None,
            },
        )


# ---------------------------------------------------------------------------
# 7. NCD cascade — screened → at risk → diagnosed (→ treatment)
# ---------------------------------------------------------------------------

_NCD_CASCADE_FIELDS = {
    "diabetes": ("risk_dm", "found_dm", "เบาหวาน"),
    "hypertension": ("risk_hpt", "found_hpt", "ความดันโลหิตสูง"),
    "cardiovascular": ("risk_cvd", "found_cvd", "หลอดเลือดหัวใจ"),
    "obesity": ("risk_bmi", "found_obesity", "อ้วน"),
}


class NCDCascadeTool(BaseTool):
    name = "query_ncd_cascade"
    description = (
        "NCD care cascade: screened → at risk → diagnosed for one disease. "
        "Use for 'cascade', 'เส้นทางการตรวจ', 'พบเสี่ยง → วินิจฉัย → รักษา'."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "disease": {
                "type": "string",
                "enum": list(_NCD_CASCADE_FIELDS.keys()),
                "description": "Which NCD to trace.",
            },
            "zone_code": {"type": "string", "description": "Optional zone scope 01-08."},
        },
        "required": ["disease"],
    }

    def execute(self, args: dict) -> ToolResult:
        disease = args.get("disease", "diabetes")
        if disease not in _NCD_CASCADE_FIELDS:
            return ToolResult(text=f"ไม่รู้จักโรค '{disease}'")
        risk_col, found_col, label = _NCD_CASCADE_FIELDS[disease]

        zone_code = args.get("zone_code")
        if zone_code:
            zone_code = str(zone_code).zfill(2)
            scope_sql = """
                AND v.home_district_code IN (
                    SELECT dcode FROM public.ref_districts WHERE zone_code = %s
                )
            """
            params: tuple = (zone_code,)
            scope_label = f"เขตสุขภาพ {int(zone_code)}"
        else:
            scope_sql = ""
            params = tuple()
            scope_label = "ทั้ง กทม."

        rows = _query(
            f"""SELECT COUNT(DISTINCT v.patient_id) AS total,
                       COUNT(DISTINCT v.patient_id) FILTER (WHERE {risk_col}) AS at_risk,
                       COUNT(DISTINCT v.patient_id) FILTER (WHERE {found_col}) AS found
                FROM public.mv_visit_resolved v
                WHERE v.cancel_status = 0 {scope_sql}""",
            params,
        )
        if not rows:
            return ToolResult(text="ไม่พบข้อมูล")
        r = rows[0]
        total = int(r["total"] or 0)
        at_risk = int(r["at_risk"] or 0)
        found = int(r["found"] or 0)

        pct_risk = round(100.0 * at_risk / max(total, 1), 1)
        pct_found = round(100.0 * found / max(total, 1), 2)
        # found / at_risk = "yield" of risk → diagnosis confirmation
        pct_yield = round(100.0 * found / max(at_risk, 1), 1) if at_risk else 0

        lines = [
            f"## NCD Cascade — {label} ({scope_label})",
            f"1. **คัดกรอง (Screened)**: {total:,} คน",
            f"2. **พบเสี่ยง (At Risk)**: {at_risk:,} คน ({pct_risk}% ของผู้คัดกรอง)",
            f"3. **วินิจฉัย/ยืนยัน (Diagnosed)**: {found:,} คน ({pct_found}% ของผู้คัดกรอง)",
        ]
        if at_risk > 0:
            lines.append(f"\n*Yield: ในกลุ่มเสี่ยง {at_risk:,} คน → ได้รับการวินิจฉัย {found:,} คน ({pct_yield}%)*")

        chart_data = [
            {"name": "คัดกรอง", "value": total},
            {"name": "พบเสี่ยง", "value": at_risk},
            {"name": "วินิจฉัย", "value": found},
        ]
        viz = [_viz("funnel", f"NCD Cascade — {label} ({scope_label})",
                    chart_data, yLabel="จำนวนคน", color="#00744B")]
        chart_spec = _chart_spec(
            "funnel",
            f"NCD Cascade — {label}",
            x=[d["name"] for d in chart_data],
            series=[{"name": "จำนวนคน", "data": [d["value"] for d in chart_data], "type": "funnel"}],
            y_label="จำนวนคน",
        )
        return ToolResult(
            text="\n".join(lines),
            visualizations=viz,
            metadata={
                "chart_spec": chart_spec,
                "disease": disease,
                "scope": scope_label,
                "stages": chart_data,
                "yield_pct": pct_yield,
            },
        )
