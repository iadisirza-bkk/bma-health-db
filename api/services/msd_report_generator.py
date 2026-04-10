"""MSD Comprehensive Report Generator (100+ pages).

Pure Jinja2 template-based -- no LLM/swarm dependency.
All content is in LaTeX templates under templates/latex/msd/.
Data comes from ReportData (collect_report_data).
Charts come from ChartGenerator.

Ported from bma-health -- async removed, imports adjusted.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2

import config
from services.report_data_collector import (
    collect_report_data,
    ReportData,
    DISEASES,
    DISEASE_NAMES_TH,
    DISEASE_NAMES_EN,
)
from services.chart_generator import ChartGenerator
from services.latex_utils import TEMPLATE_DIR

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(config.REPORTS_DIR)
TECTONIC_PATH = config.TECTONIC_PATH
TECTONIC_TIMEOUT = int(config.TECTONIC_TIMEOUT)


class MSDReportGenerator:
    """Generates the comprehensive 100+ page MSD report from Jinja2 templates."""

    def __init__(self) -> None:
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
            block_start_string="<%",
            block_end_string="%>",
            variable_start_string="<<",
            variable_end_string=">>",
            comment_start_string="<#",
            comment_end_string="#>",
            autoescape=False,
        )
        self.env.filters["number_format"] = lambda n: f"{n:,}" if isinstance(n, (int, float)) else str(n)
        self.env.filters["truncate"] = lambda s, length=255, end='...': s[:length] + (end if len(s) > length else '')

    def generate(self, lang: str = "th") -> Path | None:
        """Generate the MSD comprehensive report PDF."""
        logger.info("MSD report generation starting (lang=%s)...", lang)

        # 1. Collect data
        report_data = collect_report_data()
        ctx = self._build_context(report_data)

        # 2. Generate charts
        charts_dir = Path(tempfile.mkdtemp(prefix="msd_charts_"))
        chart_gen = ChartGenerator(output_dir=charts_dir)
        chart_gen.generate_all(report_data)
        self._generate_zone_charts(report_data, ctx, charts_dir)

        # 3. Render master template (which \input's all parts)
        template = self.env.get_template("report_msd_master.tex.j2")
        rendered_master = template.render(
            data=ctx,
            generated_date=datetime.now().strftime("%d/%m/%Y %H:%M"),
        )

        # 4. Render each part template separately (for \input)
        parts = {}
        part_files = [
            "part1_executive_summary", "part2_introduction", "part3_methodology",
            "part2b_system_overview",
            "part4_descriptive", "part4b_descriptive_screening",
            "part4c_descriptive_mental", "part4d_descriptive_socio",
            "part5_factor_analysis",
            "part6_inferential", "part6b_logistic", "part6c_comorbidity",
            "part7_trend", "part7b_seasonal",
            "part8_zones", "part8b_zone_comparison", "part9_policy",
            "appendix_a_schema", "appendix_b_factors", "appendix_c_zones",
            "appendix_d_api_layers", "appendix_d_references", "appendix_f_glossary",
        ]
        for name in part_files:
            try:
                tmpl = self.env.get_template(f"msd/{name}.tex.j2")
                parts[name] = tmpl.render(data=ctx, generated_date=datetime.now().strftime("%d/%m/%Y"))
            except Exception as e:
                logger.error("Failed to render msd/%s: %s", name, e)
                parts[name] = f"% Render error: {e}"

        # 5. Compile
        pdf_path = self._compile(rendered_master, parts, charts_dir)
        if pdf_path:
            logger.info("MSD report ready: %s (%d bytes = %d pages est.)",
                        pdf_path, pdf_path.stat().st_size,
                        max(1, pdf_path.stat().st_size // 8000))
        return pdf_path

    def _build_context(self, rd: ReportData) -> dict[str, Any]:
        """Build template context from ReportData."""
        from data.facts import HEALTH_ZONES

        ctx: dict[str, Any] = {
            "total_screened": rd.total_screened,
            "district_count": rd.district_count,
            "zone_count": rd.zone_count,
            "disease_count": rd.disease_count,
            "city_disease_summary": rd.city_disease_summary,
            "top_diseases": rd.top_diseases,
            "factors": rd.factors,
            "inferential_tests": rd.inferential_tests,
            "trends": rd.trends,
            "zone_summaries": rd.zone_summaries,
            "risk_rankings": rd.risk_rankings,
            "district_data": rd.district_data,
            "key_findings": [],
            "anova_results": rd.anova_results,
            "bonferroni_alpha": rd.bonferroni_alpha,
            "bonferroni_significant_count": rd.bonferroni_significant_count,
            "comorbidity_disease_labels": getattr(rd, 'comorbidity_disease_labels', []),
            "screening_tests_detail": rd.screening_tests_detail,
            "lab_panel": rd.lab_panel,
            "mental_health": rd.mental_health,
            "occupation_distribution": rd.occupation_distribution,
            "education_distribution": rd.education_distribution,
            "housing_distribution": rd.housing_distribution,
            "insurance_distribution": rd.insurance_distribution,
            "diet_distribution": rd.diet_distribution,
            "vaccination": rd.vaccination,
            "family_history": rd.family_history,
            "lgbtq": rd.lgbtq,
            "service_requests": rd.service_requests,
            "pet_data": rd.pet_data,
            "comorbidity_matrix": rd.comorbidity_matrix,
            "multi_morbidity": rd.multi_morbidity,
            "logistic_models": rd.logistic_models,
            "seasonal_decomposition": rd.seasonal_decomposition,
            "weekly_heatmap": rd.weekly_heatmap,
            "pm25_lag_analysis": rd.pm25_lag_analysis,
            "gis_layers": rd.gis_layers,
            "zone_comparison": {
                "zones": rd.zone_summaries,
                "avg_smoking": rd.behavior_summary.get("smoking_pct", 0) if hasattr(rd, "behavior_summary") and rd.behavior_summary else 0,
                "avg_alcohol": rd.behavior_summary.get("alcohol_pct", 0) if hasattr(rd, "behavior_summary") and rd.behavior_summary else 0,
                "avg_no_exercise": rd.behavior_summary.get("no_exercise_pct", 0) if hasattr(rd, "behavior_summary") and rd.behavior_summary else 0,
                "avg_obese": next((d["avg_pct"] for d in rd.city_disease_summary if d["key"] == "obesity"), 0) if rd.city_disease_summary else 0,
                "total_hospitals": 11, "total_health_centers": 69,
                "total_districts": 50,
                "anova_results": [],
                "posthoc_results": [],
            },
        }

        # Add facilitator names to zone_summaries
        for zs in ctx["zone_summaries"]:
            zone_code = zs.get("zone", "")
            if zone_code in HEALTH_ZONES:
                zs["facilitator"] = HEALTH_ZONES[zone_code]["facilitator"]
                zs["area_managers"] = HEALTH_ZONES[zone_code]["area_manager_count"]
            else:
                zs.setdefault("facilitator", "---")
                zs.setdefault("area_managers", 0)

        return ctx

    def _generate_zone_charts(self, rd: ReportData, ctx: dict, charts_dir: Path):
        """Generate per-zone bar charts."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from services.latex_utils import register_thai_font
            register_thai_font()

            for zone in ctx.get("zone_summaries", []):
                zone_code = zone.get("zone", "")
                disease_avgs = zone.get("disease_avgs", {})
                if not disease_avgs:
                    continue

                sorted_items = sorted(disease_avgs.items(), key=lambda x: x[1], reverse=True)
                names = [DISEASE_NAMES_TH.get(k, k)[:10] for k, _ in sorted_items]
                vals = [v for _, v in sorted_items]
                colors = ["#ef4444" if v > 30 else "#f59e0b" if v > 15 else "#22c55e" for v in vals]

                fig, ax = plt.subplots(figsize=(8, 4))
                bars = ax.barh(names, vals, color=colors, height=0.6)
                ax.set_xlabel("% ความเสี่ยง")
                ax.invert_yaxis()
                for bar, v in zip(bars, vals):
                    ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
                            f"{v:.1f}%", va="center", fontsize=8)
                fig.tight_layout()
                fig.savefig(charts_dir / f"zone_{zone_code}.png", dpi=150)
                plt.close(fig)
        except Exception as e:
            logger.warning("Zone charts failed: %s", e)

    def _compile(self, master_tex: str, parts: dict[str, str], charts_dir: Path) -> Path | None:
        """Compile LaTeX to PDF via Tectonic."""
        tectonic = shutil.which("tectonic") or TECTONIC_PATH
        if not Path(tectonic).exists():
            logger.error("Tectonic not found at %s", tectonic)
            return None

        tmp = Path(tempfile.mkdtemp(prefix="msd_build_"))
        try:
            # Copy assets + preamble
            assets_src = TEMPLATE_DIR / "assets"
            if assets_src.exists():
                shutil.copytree(assets_src, tmp / "assets")
            for p in TEMPLATE_DIR.glob("bma_*_preamble.tex"):
                shutil.copy2(p, tmp / p.name)

            # Copy charts
            if charts_dir.exists():
                shutil.copytree(charts_dir, tmp / "charts")

            # Write master .tex
            (tmp / "report.tex").write_text(master_tex, encoding="utf-8")

            # Write part files into msd/ subdirectory
            msd_dir = tmp / "msd"
            msd_dir.mkdir(exist_ok=True)
            for name, content in parts.items():
                (msd_dir / f"{name}.tex").write_text(content, encoding="utf-8")

            # Compile
            result = subprocess.run(
                [tectonic, "-X", "compile", "report.tex"],
                capture_output=True, text=True,
                timeout=TECTONIC_TIMEOUT, cwd=str(tmp),
            )

            if result.returncode != 0:
                stderr = result.stderr.strip().split("\n")
                errors = [l for l in stderr if "error:" in l.lower()][-5:]
                logger.error("Tectonic failed:\n%s", "\n".join(errors))
                debug_dir = REPORTS_DIR / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tmp / "report.tex", debug_dir / "msd_debug.tex")
                for f in (tmp / "msd").glob("*.tex"):
                    shutil.copy2(f, debug_dir / f.name)
                logger.error("Debug files saved to %s", debug_dir)
                return None

            pdf = tmp / "report.pdf"
            if pdf.exists():
                out_dir = REPORTS_DIR / "th"
                out_dir.mkdir(parents=True, exist_ok=True)
                final = out_dir / "msd_comprehensive.pdf"
                shutil.copy2(pdf, final)
                logger.info("PDF saved: %s (%d bytes)", final, final.stat().st_size)
                return final

            logger.error("PDF not found after compilation")
            return None

        except subprocess.TimeoutExpired:
            logger.error("Tectonic timed out after %ds", TECTONIC_TIMEOUT)
            return None
        except Exception as e:
            logger.error("Compile error: %s", e)
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
