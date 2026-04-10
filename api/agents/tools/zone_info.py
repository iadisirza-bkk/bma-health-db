"""QueryZoneInfoTool — look up zone/facilitator/district from FACT data.

SYNC — uses data.facts directly.
"""
from __future__ import annotations

from agents.tools.base import BaseTool, ToolResult
from data.facts import HEALTH_ZONES, DCODE_TO_ZONE


class QueryZoneInfoTool(BaseTool):
    name = "query_zone_info"
    description = "Look up Bangkok health zone info: districts, facilitator hospital, zone for a district."
    parameters_schema = {
        "type": "object",
        "properties": {
            "zone": {"type": "string", "description": "Zone number 1-8"},
            "district": {"type": "string", "description": "District name (Thai)"},
            "query_type": {"type": "string", "enum": ["zone_details", "district_zone", "all_zones"]},
        },
    }

    def execute(self, args: dict) -> ToolResult:
        qt = args.get("query_type", "all_zones")

        if qt == "zone_details":
            zc = str(args.get("zone", "1"))
            zone = HEALTH_ZONES.get(zc)
            if not zone:
                return ToolResult(text=f"ไม่พบโซน {zc}")
            text = (
                f"## {zone['name_th']}\n"
                f"- **Facilitator**: {zone['facilitator']}\n"
                f"- **Sub-facilitator**: {', '.join(zone.get('sub_facilitators', []))}\n"
                f"- **เขต ({len(zone['districts'])})**: {', '.join(zone['districts'])}\n"
                f"- **Area Managers**: {zone['area_manager_count']} หน่วย"
            )
            return ToolResult(text=text)

        if qt == "district_zone":
            name = args.get("district", "").replace("เขต", "").strip()
            for zc, zone in HEALTH_ZONES.items():
                for d in zone["districts"]:
                    if name in d or d in name:
                        return ToolResult(text=f"เขต{d} อยู่ใน **{zone['name_th']}** (Facilitator: {zone['facilitator']})")
            return ToolResult(text=f"ไม่พบเขต '{name}'")

        # all_zones
        lines = ["## โซนสุขภาพ 8 โซน กทม.\n"]
        for zc, zone in HEALTH_ZONES.items():
            fac = zone["facilitator"].replace("โรงพยาบาล", "รพ.")
            lines.append(f"**{zone['name_th']}** ({fac}) -- {len(zone['districts'])} เขต: {', '.join(zone['districts'])}")
        return ToolResult(text="\n".join(lines))
