"""Generate zone-specific slide decks for each of 8 Bangkok health zones.

Ported from bma-health -- async removed, imports adjusted.
Uses load_district_data() instead of reading JSON file directly.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import jinja2

import config
from services.data_adapter import load_district_data
from data.facts import HEALTH_ZONES, ZONE_FACILITATORS

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "latex"
CACHE_DIR = Path(config.REPORTS_DIR) / "zone"


def _latex_escape(text: str) -> str:
    for c, r in {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}"}.items():
        text = text.replace(c, r)
    return text


def _get_ai_insight_fallback(zone_name: str, facilitator: str, stats: dict) -> str:
    """Deterministic fallback insight (no LLM call)."""
    return _latex_escape(
        f"{zone_name}มีผู้คัดกรอง {stats['total']:,} คน "
        f"โรคที่เสี่ยงสูงสุดคือ{stats['top_disease']} ({stats['top_pct']}%)"
    )


def generate_zone_report(zone_code: str, lang: str = "th") -> Path:
    """Generate a zone-specific slide deck PDF. Returns path to PDF."""
    zone = HEALTH_ZONES.get(zone_code)
    if not zone:
        raise ValueError(f"Zone {zone_code} not found")

    data = load_district_data()

    # Aggregate zone data
    districts = []
    zone_disease_sums: dict[str, float] = {}
    zone_total = 0
    for dcode in zone["dcodes"]:
        d = data.get(dcode)
        if not d:
            continue
        districts.append({"name": d["name_th"], "screened": f"{d['total_screened']:,}", "dcode": dcode})
        zone_total += d["total_screened"]
        for dk, dv in d["diseases"].items():
            zone_disease_sums[dk] = zone_disease_sums.get(dk, 0) + dv["pct_at_risk"] * d["total_screened"]

    disease_risks = []
    first_district = next((data[dc] for dc in zone["dcodes"] if dc in data), None)
    for dk, total_weighted in sorted(zone_disease_sums.items(), key=lambda x: x[1], reverse=True):
        pct = round(total_weighted / zone_total, 1) if zone_total > 0 else 0
        name = first_district["diseases"].get(dk, {}).get("name", dk) if first_district else dk
        disease_risks.append({"name": name, "pct": pct})

    top = disease_risks[0] if disease_risks else {"name": "N/A", "pct": 0}
    low = disease_risks[-1] if disease_risks else {"name": "N/A", "pct": 0}

    stats = {
        "total": zone_total, "n_districts": len(districts),
        "top_disease": top["name"], "top_pct": top["pct"],
        "low_disease": low["name"], "low_pct": low["pct"],
    }
    ai_insight = _get_ai_insight_fallback(zone["name_th"], zone["facilitator"], stats)

    facilitator_short = zone["facilitator"].replace("โรงพยาบาล", "รพ.")
    top_risk_text = _latex_escape(f"{top['name']} มีความเสี่ยงสูงที่สุด ({top['pct']}%) ควรเฝ้าระวังเป็นพิเศษ")
    recommendation = _latex_escape(
        f"แนะนำให้ {facilitator_short} ร่วมกับศูนย์บริการสาธารณสุข {zone['area_manager_count']} หน่วย "
        f"จัดกิจกรรมคัดกรองเชิงรุกในเขตที่มีความเสี่ยงสูง"
    )

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        variable_start_string="<<", variable_end_string=">>",
        block_start_string="<%", block_end_string="%>",
        comment_start_string="<#", comment_end_string="#>",
    )
    template = env.get_template("report_zone.tex.j2")
    rendered = template.render(
        zone_name=zone["name_th"], facilitator=zone["facilitator"],
        facilitator_short=facilitator_short,
        generated_date=date.today().strftime("%d/%m/%Y"),
        total_screened=f"{zone_total:,}", district_count=len(districts),
        area_manager_count=zone["area_manager_count"],
        districts=districts, disease_risks=disease_risks,
        top_risk_text=top_risk_text, recommendation=recommendation,
        ai_insight=ai_insight, chart_path=None,
        sub_facilitators=", ".join(zone.get("sub_facilitators", [])),
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copytree(str(TEMPLATE_DIR / "assets"), str(Path(tmpdir) / "assets"))
        shutil.copy2(str(TEMPLATE_DIR / "bma_beamer_preamble.tex"), tmpdir)
        tex_path = Path(tmpdir) / "zone.tex"
        tex_path.write_text(rendered, encoding="utf-8")

        tectonic = shutil.which("tectonic") or config.TECTONIC_PATH
        result = subprocess.run(
            [tectonic, "-X", "compile", str(tex_path)],
            capture_output=True, text=True,
            timeout=config.TECTONIC_TIMEOUT, cwd=tmpdir,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Tectonic failed: {result.stderr[-500:]}")

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = CACHE_DIR / f"zone{zone_code}_{lang}.pdf"
        shutil.copy2(str(tex_path.with_suffix(".pdf")), str(out))

    return out
