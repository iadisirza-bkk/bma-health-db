"""Generate a focused mini-slide deck for a single disease.

Renders a 6-slide Beamer PDF via Tectonic.

Ported from bma-health -- async removed, imports adjusted.
Uses load_district_data() instead of reading JSON file directly.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import jinja2

import config
from services.data_adapter import load_district_data

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "latex"
CACHE_DIR = Path(config.REPORTS_DIR) / "disease"

DISEASE_NAMES_TH = {
    "diabetes": "เบาหวาน",
    "hypertension": "ความดันโลหิตสูง",
    "obesity": "โรคอ้วน",
    "dyslipidemia": "ไขมันในเลือดผิดปกติ",
    "kidney": "โรคไตเรื้อรัง",
    "cardiovascular": "โรคหลอดเลือดหัวใจ",
    "stroke": "โรคหลอดเลือดสมอง",
    "ckd": "โรคไตเรื้อรัง",
    "anemia": "โรคโลหิตจาง",
    "respiratory": "โรคระบบทางเดินหายใจ",
}


def _latex_escape(text: str) -> str:
    """Escape special LaTeX characters."""
    specials = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
                "^": r"\textasciicircum{}"}
    for char, replacement in specials.items():
        text = text.replace(char, replacement)
    return text


def _get_ai_insight_fallback(disease_name: str, stats: dict) -> str:
    """Generate a deterministic fallback insight (no LLM call)."""
    return _latex_escape(
        f"{disease_name}มีค่าเฉลี่ยกลุ่มเสี่ยง {stats['avg']}% "
        f"โดยเขตที่มีความเสี่ยงสูงสุดคือเขต{stats['max_district']} ({stats['max']}%) "
        f"และต่ำสุดคือเขต{stats['min_district']} ({stats['min']}%)"
    )


def generate_disease_slide(disease_key: str) -> Path:
    """Generate a 6-slide PDF for a single disease. Returns path to PDF."""
    disease_name = DISEASE_NAMES_TH.get(disease_key)
    if not disease_name:
        raise ValueError(f"Unknown disease key: {disease_key}")

    data = load_district_data()

    # Compute stats
    values = []
    district_data = []
    total_screened = 0
    total_at_risk = 0

    for d in data.values():
        dd = d["diseases"].get(disease_key)
        if dd:
            pct = dd["pct_at_risk"]
            values.append(pct)
            district_data.append({
                "name": d["name_th"].replace("เขต", ""),
                "pct": pct,
            })
            total_screened += d["total_screened"]
            total_at_risk += round(pct * d["total_screened"] / 100)

    if not values:
        raise ValueError(f"No data found for disease: {disease_key}")

    avg = round(sum(values) / len(values), 1)
    std = round((sum((x - avg) ** 2 for x in values) / len(values)) ** 0.5, 1)
    min_val = round(min(values), 1)
    max_val = round(max(values), 1)

    district_data.sort(key=lambda x: x["pct"], reverse=True)
    top_districts = district_data[:10]
    bottom_districts = list(reversed(district_data[-10:]))

    max_district = top_districts[0]["name"]
    min_district = bottom_districts[0]["name"]

    stats = {
        "avg": avg, "std": std, "min": min_val, "max": max_val,
        "max_district": max_district, "min_district": min_district,
        "total_screened": total_screened,
    }
    ai_insight = _get_ai_insight_fallback(disease_name, stats)

    # Generate chart
    chart_path = _generate_ranking_chart(disease_key, disease_name, top_districts)

    # Render template
    from datetime import date
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        variable_start_string="<<",
        variable_end_string=">>",
        block_start_string="<%",
        block_end_string="%>",
        comment_start_string="<#",
        comment_end_string="#>",
    )
    template = env.get_template("report_disease_slide.tex.j2")

    rendered = template.render(
        disease_name_th=disease_name,
        generated_date=date.today().strftime("%d/%m/%Y"),
        total_screened=f"{total_screened:,}",
        total_at_risk=f"{total_at_risk:,}",
        avg_pct=avg,
        std_pct=std,
        min_pct=min_val,
        max_pct=max_val,
        top_districts=top_districts,
        bottom_districts=bottom_districts,
        ai_insight=ai_insight,
        chart_path=chart_path,
    )

    # Compile with Tectonic
    with tempfile.TemporaryDirectory() as tmpdir:
        assets_src = TEMPLATE_DIR / "assets"
        assets_dst = Path(tmpdir) / "assets"
        shutil.copytree(str(assets_src), str(assets_dst))
        shutil.copy2(str(TEMPLATE_DIR / "bma_beamer_preamble.tex"), tmpdir)

        if chart_path:
            shutil.copy2(chart_path, tmpdir)

        tex_path = Path(tmpdir) / "slide.tex"
        tex_path.write_text(rendered, encoding="utf-8")

        tectonic_bin = shutil.which("tectonic") or config.TECTONIC_PATH
        result = subprocess.run(
            [tectonic_bin, "-X", "compile", str(tex_path)],
            capture_output=True, text=True,
            timeout=config.TECTONIC_TIMEOUT, cwd=tmpdir,
        )

        if result.returncode != 0:
            logger.error("Tectonic failed:\n%s", result.stderr[-2000:])
            raise RuntimeError(f"PDF compilation failed: {result.stderr[-500:]}")

        pdf_path = tex_path.with_suffix(".pdf")
        if not pdf_path.exists():
            raise RuntimeError("PDF not generated")

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out_path = CACHE_DIR / f"{disease_key}_th.pdf"
        shutil.copy2(str(pdf_path), str(out_path))

    return out_path


def _generate_ranking_chart(disease_key: str, disease_name: str, top10: list[dict]) -> str | None:
    """Generate a horizontal bar chart for top 10 districts. Returns path or None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        from services.latex_utils import register_thai_font
        register_thai_font()

        names = [d["name"] for d in reversed(top10)]
        values = [d["pct"] for d in reversed(top10)]

        fig, ax = plt.subplots(figsize=(8, 4))
        colors = ["#ef4444" if v > 40 else "#f59e0b" if v > 25 else "#00744B" for v in values]
        bars = ax.barh(names, values, color=colors, height=0.6)

        ax.set_xlabel("สัดส่วนกลุ่มเสี่ยง (%)", fontsize=10)
        ax.set_title(f"Top 10 เขตเสี่ยง{disease_name}", fontsize=12, fontweight="bold", color="#00744B")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        for bar, val in zip(bars, values):
            ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
                    f"{val}%", va="center", fontsize=9, fontweight="bold")

        plt.tight_layout()
        chart_path = f"/tmp/disease_chart_{disease_key}.png"
        fig.savefig(chart_path, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return chart_path
    except Exception as e:
        logger.warning("Chart generation failed: %s", e)
        return None
