"""QueryHealthDataTool — flexible health data querying with grouping/filtering.

SYNC — queries DB directly via load_district_data().
"""
from __future__ import annotations

from agents.tools.base import BaseTool, ToolResult
from agents.tools.helpers import (
    load_data, normalize_disease, get_base_rates, get_total_screened, apply_modifier,
    resolve_filter, DISEASE_NAMES, DISEASE_ALIASES, ALL_DISEASES,
    FACTOR_CATEGORIES, FACTOR_MODIFIERS, DCODE_TO_ZONE, HEALTH_ZONES,
)


class QueryHealthDataTool(BaseTool):
    name = "query_health_data"
    description = (
        "Query Bangkok health screening data. Use group_by to COMPARE across a dimension. "
        "Use filters to NARROW to a specific subgroup. "
        "IMPORTANT: If user asks about a SPECIFIC age/sex/behavior, put it in filters, NOT group_by."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "group_by": {"type": "string", "enum": ["district", "zone", "age_group", "sex", "disease", "smoking", "alcohol", "exercise"]},
            "disease": {"type": "string", "enum": ["diabetes", "hypertension", "obesity", "dyslipidemia", "cardiovascular", "stroke", "ckd", "anemia", "respiratory"]},
            "filters": {"type": "object"},
            "chart_type": {"type": "string", "enum": ["bar", "horizontal_bar", "donut", "pie", "line", "gauge", "radar", "scatter", "heatmap", "funnel", "table", "none"]},
            "highlight": {"type": "string"},
            "y_label": {"type": "string"},
            "top_n": {"type": "integer"},
        },
        "required": ["group_by"],
    }

    def execute(self, args: dict) -> ToolResult:
        data = load_data()
        group_by = args.get("group_by", "disease")
        disease = normalize_disease(args.get("disease"))
        filters = args.get("filters", {})
        top_n = args.get("top_n", 10)

        base_rates = get_base_rates(data)
        total = get_total_screened(data)

        # Apply location filters
        if filters.get("district"):
            name = filters["district"].replace("เขต", "").strip()
            best_match, best_score = None, 0
            for d in data.values():
                d_name = d["name_th"].replace("เขต", "").strip()
                d_en = d.get("name_en", "").lower()
                if name == d_name or name in d["name_th"] or name.lower() in d_en:
                    best_match = d
                    break
                common = sum(1 for c in name if c in d_name)
                score = common / max(len(name), 1)
                if score > best_score and score > 0.5:
                    best_score, best_match = score, d
            if best_match:
                base_rates = {DISEASE_ALIASES.get(dk, dk): dv["pct_at_risk"] for dk, dv in best_match["diseases"].items()}
                total = best_match["total_screened"]

        if filters.get("zone"):
            zcode = str(filters["zone"])
            zone_total, zone_sums = 0, {}
            for dcode, d in data.items():
                if DCODE_TO_ZONE.get(dcode, "") == zcode:
                    for dk, dv in d["diseases"].items():
                        dk_n = DISEASE_ALIASES.get(dk, dk)
                        zone_sums[dk_n] = zone_sums.get(dk_n, 0) + dv["pct_at_risk"] * d["total_screened"]
                    zone_total += d["total_screened"]
            if zone_total > 0:
                base_rates = {dk: round(v / zone_total, 1) for dk, v in zone_sums.items()}
                total = zone_total

        # Apply demographic modifiers
        combined_mod = {dk: 1.0 for dk in ALL_DISEASES}
        pop_fraction = 1.0
        for fk in ["age_group", "sex", "smoking", "alcohol", "exercise"]:
            fval = filters.get(fk)
            if not fval:
                continue
            resolved = resolve_filter(fk, str(fval))
            mods = FACTOR_MODIFIERS.get(fk, {})
            if resolved and resolved in mods:
                for dk in ALL_DISEASES:
                    combined_mod[dk] *= mods[resolved].get(dk, 1.0)
                for cat_key, _, prop in FACTOR_CATEGORIES.get(fk, []):
                    if cat_key == resolved:
                        pop_fraction *= prop
                        break

        est_pop = round(total * pop_fraction)
        modified_rates = {dk: apply_modifier(base_rates[dk], combined_mod[dk]) for dk in ALL_DISEASES if dk in base_rates}

        # Group by dimension
        chart_data, text_lines = [], []

        if group_by == "disease":
            if disease:
                rate = modified_rates.get(disease, 0)
                nm = DISEASE_NAMES.get(disease, disease)
                text_lines.append(f"{nm}: ประมาณ {rate}% ของกลุ่ม ({round(est_pop * rate / 100):,} จาก {est_pop:,} คน)")
                chart_data = [{"name": nm, "value": rate}]
            else:
                text_lines.append(f"ภาพรวม ({est_pop:,} คน):")
                for dk in ALL_DISEASES:
                    if dk in modified_rates:
                        nm = DISEASE_NAMES.get(dk, dk)
                        chart_data.append({"name": nm, "value": modified_rates[dk]})
                        text_lines.append(f"- {nm}: {modified_rates[dk]}%")

        elif group_by == "district":
            if not disease:
                disease = "obesity"
            dn = DISEASE_NAMES.get(disease, disease)
            district_rates = []
            for d in data.values():
                dd = d["diseases"].get(disease) or d["diseases"].get(next((k for k, v in DISEASE_ALIASES.items() if v == disease), ""))
                if dd:
                    rate = apply_modifier(dd["pct_at_risk"], combined_mod.get(disease, 1.0))
                    district_rates.append({"name": d["name_th"].replace("เขต", ""), "value": rate})
            district_rates.sort(key=lambda x: x["value"], reverse=True)
            chart_data = district_rates[:top_n]
            text_lines.append(f"{dn} Top {len(chart_data)} เขต:")
            for i, d in enumerate(chart_data, 1):
                text_lines.append(f"{i}. {d['name']}: {d['value']}%")

        elif group_by == "zone":
            if not disease:
                disease = "obesity"
            dn = DISEASE_NAMES.get(disease, disease)
            zone_agg = {}
            for dcode, d in data.items():
                zc = DCODE_TO_ZONE.get(dcode, "")
                if zc not in zone_agg:
                    zm = HEALTH_ZONES.get(zc, {"name_th": f"โซน {zc}"})
                    zone_agg[zc] = {"name": zm["name_th"], "sum": 0.0, "total": 0}
                dd = d["diseases"].get(disease)
                if not dd:
                    for ak, av in DISEASE_ALIASES.items():
                        if av == disease:
                            dd = d["diseases"].get(ak)
                            break
                if dd:
                    zone_agg[zc]["sum"] += dd["pct_at_risk"] * d["total_screened"]
                    zone_agg[zc]["total"] += d["total_screened"]
            for za in zone_agg.values():
                if za["total"] > 0:
                    rate = apply_modifier(round(za["sum"] / za["total"], 1), combined_mod.get(disease, 1.0))
                    chart_data.append({"name": za["name"], "value": rate})
            chart_data.sort(key=lambda x: x["value"], reverse=True)
            text_lines.append(f"{dn} รายโซน:")
            for d in chart_data:
                text_lines.append(f"- {d['name']}: {d['value']}%")

        elif group_by in FACTOR_CATEGORIES:
            if not disease:
                disease = "obesity"
            dn = DISEASE_NAMES.get(disease, disease)
            cats = FACTOR_CATEGORIES[group_by]
            mods = FACTOR_MODIFIERS[group_by]
            base = base_rates.get(disease, 0)
            text_lines.append(f"{dn} แยกตาม{group_by}:")
            for cat_key, cat_th, prop in cats:
                mod = mods.get(cat_key, {}).get(disease, 1.0)
                final_mod = mod
                for fk in ["age_group", "sex", "smoking", "alcohol", "exercise"]:
                    if fk != group_by and filters.get(fk):
                        resolved = resolve_filter(fk, str(filters[fk]))
                        if resolved and resolved in FACTOR_MODIFIERS.get(fk, {}):
                            final_mod *= FACTOR_MODIFIERS[fk][resolved].get(disease, 1.0)
                rate = apply_modifier(base, final_mod)
                count = round(total * prop * pop_fraction)
                chart_data.append({"name": cat_th, "value": rate})
                text_lines.append(f"- {cat_th}: {rate}% ({count:,} คน)")

        text = "\n".join(text_lines) if text_lines else "ไม่พบข้อมูล"
        chart_type = args.get("chart_type", "none")
        viz = []
        if chart_data and chart_type and chart_type != "none":
            title = f"{DISEASE_NAMES.get(disease, 'สุขภาพ')} — แยกตาม{group_by}" if disease else "ภาพรวมสุขภาพ"
            viz = [{"type": chart_type, "title": title, "data": chart_data,
                    "xKey": "name", "yKey": "value", "yLabel": args.get("y_label", "สัดส่วนเสี่ยง (%)"),
                    "color": "#00744B", "highlight": args.get("highlight")}]
        return ToolResult(text=text, visualizations=viz)
