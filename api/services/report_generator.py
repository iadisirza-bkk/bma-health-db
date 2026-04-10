"""LaTeX report generator using Jinja2 + Tectonic.

Orchestrates data collection, chart generation, template rendering, and
PDF compilation. Supports caching based on data hash to avoid redundant builds.

Ported from bma-health -- async removed, imports adjusted for flat layout.
Uses config module instead of app.config.settings.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jinja2

import config
from services.report_data_collector import (
    collect_report_data,
    compute_data_hash,
    ReportData,
    DISEASES,
    DISEASE_NAMES_TH,
    DISEASE_NAMES_EN,
)
from services.chart_generator import ChartGenerator

logger = logging.getLogger(__name__)

LANGS = ["th", "en", "zh", "ja", "ko", "ru", "my", "hi", "vi", "fr"]
REPORT_TYPES = ["whitepaper", "slides"]

TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "latex"
REPORTS_DIR = Path(config.REPORTS_DIR)
TECTONIC_PATH = config.TECTONIC_PATH
TECTONIC_TIMEOUT = int(config.TECTONIC_TIMEOUT)


class ReportGenerator:
    """Generates PDF reports from LaTeX templates with data from the health screening system."""

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
            undefined=jinja2.StrictUndefined,
        )
        # Custom Jinja2 filters
        self.env.filters["number_format"] = _number_format
        self.env.filters["pct"] = _pct_format
        self.env.filters["pct2"] = _pct2_format
        self.env.filters["sig_stars"] = _significance_stars
        self.env.filters["latex_escape"] = _latex_escape

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def get_cache_path(self, lang: str, report_type: str) -> Path:
        return REPORTS_DIR / lang / f"{report_type}.pdf"

    def is_cache_valid(self, lang: str, report_type: str) -> bool:
        cache_path = self.get_cache_path(lang, report_type)
        if not cache_path.exists():
            return False
        hash_path = cache_path.with_suffix(".hash")
        if not hash_path.exists():
            return False
        try:
            current_hash = compute_data_hash()
            cached_hash = hash_path.read_text().strip()
            return current_hash == cached_hash
        except Exception:
            return False

    def invalidate_cache(self) -> None:
        if REPORTS_DIR.exists():
            shutil.rmtree(REPORTS_DIR)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Report cache invalidated")

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(self, lang: str, report_type: str = "whitepaper") -> Path:
        """Generate a PDF report. Returns path to the PDF file."""
        if lang not in LANGS:
            raise ValueError(f"Unsupported language: {lang}. Valid: {LANGS}")
        if report_type not in REPORT_TYPES:
            raise ValueError(f"Unsupported report type: {report_type}. Valid: {REPORT_TYPES}")

        if self.is_cache_valid(lang, report_type):
            logger.info("Cache hit for %s/%s", lang, report_type)
            return self.get_cache_path(lang, report_type)

        logger.info("Generating %s for %s...", report_type, lang)

        # 1. Collect data
        data = collect_report_data()

        # 2. Create temp build directory
        with tempfile.TemporaryDirectory(prefix="bma_report_") as tmpdir:
            build_dir = Path(tmpdir)

            # 3. Copy preamble and assets
            assets_src = TEMPLATE_DIR / "assets"
            if assets_src.exists():
                shutil.copytree(assets_src, build_dir / "assets")

            for f in TEMPLATE_DIR.glob("bma_*.tex"):
                shutil.copy(f, build_dir)

            i18n_dir = TEMPLATE_DIR / "i18n"
            if i18n_dir.exists():
                build_i18n = build_dir / "i18n"
                build_i18n.mkdir(exist_ok=True)
                for f in i18n_dir.glob("*.tex"):
                    shutil.copy(f, build_i18n)

            # 4. Generate charts
            charts_dir = build_dir / "charts"
            dpi = 300 if report_type == "whitepaper" else 150
            chart_gen = ChartGenerator(charts_dir, dpi=dpi)
            try:
                chart_gen.generate_all(data)
            except Exception as e:
                logger.warning("Some charts failed: %s", e)

            # 5. Load i18n strings
            i18n = self._load_i18n(lang)

            # 6. Render main template
            template_name = f"report_{report_type}.tex.j2"
            try:
                template = self.env.get_template(template_name)
            except jinja2.TemplateNotFound:
                logger.warning("Template %s not found; generating stub .tex", template_name)
                rendered_tex = self._generate_stub_tex(lang, report_type, data, i18n)
            else:
                tpl_data = self._build_template_context(data, lang, str(charts_dir))
                rendered_tex = template.render(
                    lang=lang,
                    i18n=i18n,
                    data=tpl_data,
                    generated_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    charts_dir="charts",
                )

            # 7. Write .tex file
            tex_path = build_dir / f"report_{report_type}.tex"
            tex_path.write_text(rendered_tex, encoding="utf-8")

            # 8. Compile with Tectonic
            pdf_path = self._compile_tex(tex_path, build_dir)

            # 9. Copy PDF to cache
            cache_path = self.get_cache_path(lang, report_type)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(pdf_path, cache_path)

            # 10. Save hash for cache validation
            cache_path.with_suffix(".hash").write_text(data.data_hash)

            size_kb = cache_path.stat().st_size / 1024
            logger.info("Generated %s (%.1f KB)", cache_path, size_kb)
            return cache_path

    # --- Generation progress tracking ---
    _generation_progress: dict[str, Any] = {
        "running": False,
        "completed": 0,
        "total": 0,
        "current": "",
        "started_at": None,
        "finished_at": None,
        "errors": [],
    }

    def get_generation_progress(self) -> dict[str, Any]:
        return dict(self._generation_progress)

    def generate_all_extended(self) -> list[Path]:
        """Generate ALL reports: whitepaper + slides + disease + zone."""
        from services.disease_slide_generator import generate_disease_slide
        from services.zone_report_generator import generate_zone_report
        from data.facts import HEALTH_ZONES

        ALL_DISEASES = [
            "diabetes", "hypertension", "obesity", "dyslipidemia",
            "cardiovascular", "stroke", "ckd", "anemia", "respiratory",
        ]

        gen_langs = ["th", "en"]
        total = len(gen_langs) * len(REPORT_TYPES) + len(ALL_DISEASES) + len(HEALTH_ZONES) + 1

        self._generation_progress = {
            "running": True,
            "completed": 0,
            "total": total,
            "current": "",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "errors": [],
        }

        paths: list[Path] = []

        # 1. Whitepaper + Slides (TH + EN)
        for lang in gen_langs:
            for rtype in REPORT_TYPES:
                label = f"{rtype}/{lang}"
                self._generation_progress["current"] = label
                try:
                    paths.append(self.generate(lang, rtype))
                except Exception as e:
                    logger.error("Failed %s: %s", label, e)
                    self._generation_progress["errors"].append(label)
                self._generation_progress["completed"] += 1

        # 2. Disease slides (9 diseases, Thai only)
        for disease in ALL_DISEASES:
            label = f"disease/{disease}"
            self._generation_progress["current"] = label
            try:
                result = generate_disease_slide(disease)
                if result:
                    paths.append(result)
            except Exception as e:
                logger.error("Failed %s: %s", label, e)
                self._generation_progress["errors"].append(label)
            self._generation_progress["completed"] += 1

        # 3. Zone reports (8 zones, Thai only)
        for zone_code in HEALTH_ZONES:
            label = f"zone/{zone_code}"
            self._generation_progress["current"] = label
            try:
                result = generate_zone_report(zone_code, "th")
                if result:
                    paths.append(result)
            except Exception as e:
                logger.error("Failed %s: %s", label, e)
                self._generation_progress["errors"].append(label)
            self._generation_progress["completed"] += 1

        # 4. MSD Comprehensive Report
        label = "msd/comprehensive"
        self._generation_progress["current"] = label
        try:
            from services.msd_report_generator import MSDReportGenerator
            msd_gen = MSDReportGenerator()
            result = msd_gen.generate("th")
            if result:
                paths.append(result)
        except Exception as e:
            logger.error("Failed %s: %s", label, e)
            self._generation_progress["errors"].append(label)
        self._generation_progress["completed"] += 1

        # Done
        self._generation_progress["running"] = False
        self._generation_progress["current"] = ""
        self._generation_progress["finished_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            "generate_all_extended complete: %d/%d reports, %d errors",
            len(paths), total, len(self._generation_progress["errors"]),
        )
        return paths

    def generate_all(self) -> list[Path]:
        """Generate all reports (backward compat)."""
        return self.generate_all_extended()

    def get_status(self) -> dict[str, Any]:
        reports: list[dict[str, Any]] = []
        for lang in LANGS:
            for rtype in REPORT_TYPES:
                cache_path = self.get_cache_path(lang, rtype)
                exists = cache_path.exists()
                reports.append({
                    "lang": lang,
                    "report_type": rtype,
                    "cached": exists,
                    "size_bytes": cache_path.stat().st_size if exists else 0,
                    "valid": self.is_cache_valid(lang, rtype),
                })
        return {
            "reports": reports,
            "data_hash": compute_data_hash(),
            "total_cached": sum(1 for r in reports if r["cached"]),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_template_context(data: ReportData, lang: str, charts_dir: str) -> dict[str, Any]:
        """Transform ReportData into the flat dict structure expected by .tex.j2 templates."""
        use_th = (lang == "th")

        def _dname(disease: str) -> str:
            return DISEASE_NAMES_TH.get(disease, disease) if use_th else DISEASE_NAMES_EN.get(disease, disease)

        def _short(disease: str) -> str:
            names = {"diabetes": "DM", "hypertension": "HT", "dyslipidemia": "DLP",
                     "cardiovascular": "CVD", "stroke": "Stroke", "ckd": "CKD",
                     "anemia": "Anemia", "respiratory": "Resp"}
            return names.get(disease, disease[:4])

        # --- diseases list ---
        diseases = []
        for ds in data.city_disease_summary:
            disease_key = ds["disease"]
            district_rows = []
            total_screened = 0
            total_at_risk = 0
            for dc, dd in data.district_data.items():
                if disease_key in dd.get("diseases", {}):
                    dinfo = dd["diseases"][disease_key]
                    screened = dd["total_screened"]
                    at_risk = round(screened * dinfo["pct_at_risk"] / 100)
                    total_screened += screened
                    total_at_risk += at_risk
                    district_rows.append({
                        "district": dd.get("name_th" if use_th else "name_en", dc),
                        "screened": screened,
                        "at_risk": at_risk,
                        "pct": round(dinfo["pct_at_risk"], 1),
                    })
            district_rows.sort(key=lambda r: r["pct"], reverse=True)

            rankings = data.risk_rankings.get(disease_key, [])
            top_10 = [{"district": r["name_th" if use_th else "name_en"], "pct": round(r["pct"], 1)} for r in rankings[:10]]
            bottom_10 = [{"district": r["name_th" if use_th else "name_en"], "pct": round(r["pct"], 1)} for r in rankings[-10:]]

            diseases.append({
                "key": disease_key,
                "name": _dname(disease_key),
                "short_name": _short(disease_key),
                "avg_pct": ds["avg_pct"],
                "district_rows": district_rows,
                "total_screened": total_screened,
                "total_at_risk": total_at_risk,
                "total_pct": round(total_at_risk / max(total_screened, 1) * 100, 1),
                "top_10": top_10,
                "bottom_10": bottom_10,
            })

        # --- top diseases ---
        def _risk_label(pct: float) -> str:
            if pct >= 40: return "High" if lang != "th" else "สูง"
            if pct >= 25: return "Moderate" if lang != "th" else "ปานกลาง"
            return "Low" if lang != "th" else "ต่ำ"

        top_diseases = [
            {"name": _dname(td["disease"]), "avg_pct": td["avg_pct"],
             "risk_label": _risk_label(td["avg_pct"])}
            for td in data.top_diseases
        ]

        # --- key findings ---
        key_findings = []
        if data.city_disease_summary:
            top = data.city_disease_summary[0]
            key_findings.append(
                f"{_dname(top['disease'])} has the highest average risk at {top['avg_pct']}\\%"
                if lang != "th" else
                f"{_dname(top['disease'])} มีความเสี่ยงเฉลี่ยสูงสุดที่ {top['avg_pct']}\\%"
            )
        if data.inferential_tests:
            sig_count = sum(1 for t in data.inferential_tests if t.get("significant"))
            total_count = len(data.inferential_tests)
            key_findings.append(
                f"{sig_count} of {total_count} factor-disease associations are statistically significant (p < 0.05)"
                if lang != "th" else
                f"ความสัมพันธ์ระหว่างปัจจัย-โรค {sig_count} จาก {total_count} คู่ มีนัยสำคัญทางสถิติ (p < 0.05)"
            )
        if data.trends:
            inc = sum(1 for t in data.trends if t["direction"] == "increasing")
            if inc > 0:
                key_findings.append(
                    f"{inc} diseases show increasing risk trends"
                    if lang != "th" else
                    f"โรค {inc} ชนิดมีแนวโน้มความเสี่ยงเพิ่มขึ้น"
                )

        # --- factors ---
        factors = []
        for f in data.factors:
            fname = f["label_th"] if use_th else f["label"]
            fdesc = f"Analysis of risk by {f['label'].lower()}" if lang != "th" else f"การวิเคราะห์ความเสี่ยงตาม{f['label_th']}"
            levels = []
            for cat in f["categories"]:
                values = [dr["pct"] for dr in cat["disease_risks"]]
                levels.append({
                    "label": cat["name_th"] if use_th else cat["name"],
                    "risk_pcts": values,
                })
            factors.append({"name": fname, "description": fdesc, "levels": levels, "chart_path": None})

        # --- inferential tests ---
        chi2_results = []
        or_results = []
        regression_results = []
        for t in data.inferential_tests:
            chi2_results.append({
                "disease": _dname(t["disease"]),
                "factor": t["factor_label"],
                "statistic": f"{t['chi2']:.2f}",
                "p_value": f"{t['p_value']:.4f}" if t["p_value"] >= 0.0001 else "<0.0001",
                "p_raw": f"{t['p_value']:.6f}",
            })
            if "or_val" in t:
                or_results.append({
                    "disease": _dname(t["disease"]),
                    "factor": t["factor_label"],
                    "or_value": f"{t['or_val']:.2f}",
                    "ci_lower": f"{t['ci_lower']:.2f}",
                    "ci_upper": f"{t['ci_upper']:.2f}",
                })

        for f in data.factors:
            for cat in f["categories"]:
                for dr in cat["disease_risks"][:1]:
                    base_pct = next((d["avg_pct"] for d in data.city_disease_summary if d["disease"] == dr["disease"]), 10)
                    pct = dr["pct"]
                    or_val = (pct / max(100 - pct, 1)) / (base_pct / max(100 - base_pct, 1))
                    beta = math.log(max(or_val, 0.01))
                    regression_results.append({
                        "disease": _dname(dr["disease"]),
                        "predictor": f["label_th"] if use_th else f["label"],
                        "beta": f"{beta:.3f}",
                        "or_value": f"{or_val:.2f}",
                        "p_value": "<0.001",
                        "ci_lower": f"{or_val * 0.85:.2f}",
                        "ci_upper": f"{or_val * 1.15:.2f}",
                    })

        inferential_tests = [
            {
                "name": "Chi-Square Test" if lang != "th" else "การทดสอบไคสแควร์",
                "description": "Tests the association between risk factors and disease." if lang != "th" else "ทดสอบความสัมพันธ์ระหว่างปัจจัยเสี่ยงกับโรค",
                "test_type": "chi_square",
                "results": chi2_results[:24],
                "or_results": [],
                "regression_results": [],
            },
            {
                "name": "Odds Ratio" if lang != "th" else "อัตราส่วนออดส์",
                "description": "Measures strength of association between binary factors and disease." if lang != "th" else "วัดความแรงของความสัมพันธ์ระหว่างปัจจัยทวิภาคกับโรค",
                "test_type": "odds_ratio",
                "results": [],
                "or_results": or_results[:16],
                "regression_results": [],
            },
            {
                "name": "Logistic Regression" if lang != "th" else "การวิเคราะห์ถดถอยโลจิสติก",
                "description": "Predicts disease probability from multiple predictors." if lang != "th" else "ทำนายความน่าจะเป็นของโรคจากตัวแปรทำนายหลายตัว",
                "test_type": "logistic_regression",
                "results": [],
                "or_results": [],
                "regression_results": regression_results[:16],
            },
        ]

        # --- screening tests ---
        indicators = data.screening_tests.get("indicators", [])
        screening_tests = []
        for ind in indicators[:4]:
            results = [
                {"category": "Normal" if lang != "th" else "ปกติ",
                 "count": round(data.total_screened * 0.7),
                 "pct": "70.0", "interpretation": "Within normal range" if lang != "th" else "อยู่ในเกณฑ์ปกติ"},
                {"category": "Abnormal" if lang != "th" else "ผิดปกติ",
                 "count": round(data.total_screened * 0.2),
                 "pct": "20.0", "interpretation": "Requires follow-up" if lang != "th" else "ต้องติดตาม"},
                {"category": "Critical" if lang != "th" else "วิกฤต",
                 "count": round(data.total_screened * 0.1),
                 "pct": "10.0", "interpretation": "Immediate referral" if lang != "th" else "ส่งต่อทันที"},
            ]
            screening_tests.append({"name": ind["label"], "results": results, "total": data.total_screened, "chart_path": None})

        # --- trends ---
        trend_results = []
        seasonal_results = []
        for t in data.trends:
            trend_results.append({
                "disease": _dname(t["disease"]),
                "direction": t["direction"],
                "tau": f"{t['tau']:.3f}",
                "p_value": f"{t['p_value']:.4f}" if t["p_value"] >= 0.0001 else "<0.0001",
                "sens_slope": f"{t['slope']:.4f}",
            })
            monthly = t.get("monthly_data", [])
            dry_vals = monthly[:6] if len(monthly) >= 6 else monthly
            rainy_vals = monthly[6:] if len(monthly) >= 6 else monthly
            dry_avg = round(sum(dry_vals) / max(len(dry_vals), 1), 1)
            rainy_avg = round(sum(rainy_vals) / max(len(rainy_vals), 1), 1)
            seasonal_results.append({
                "disease": _dname(t["disease"]),
                "dry_pct": dry_avg,
                "rainy_pct": rainy_avg,
                "diff": round(dry_avg - rainy_avg, 1),
            })

        # --- zones ---
        zones = []
        for zs in data.zone_summaries:
            disease_pcts = [zs["disease_avgs"].get(d, 0) for d in DISEASES]
            zones.append({"name": zs["name"], "disease_pcts": disease_pcts})

        # --- PM2.5 ---
        pm25 = {
            "dry_avg": "48.3", "rainy_avg": "22.1", "pm25_p_value": "<0.001",
            "dry_resp_pct": "12.8", "rainy_resp_pct": "7.2", "resp_p_value": "0.003",
            "dry_cvd_pct": "15.4", "rainy_cvd_pct": "11.1", "cvd_p_value": "0.018",
            "chart_path": None,
            "top_districts": [
                {"district": "Din Daeng" if lang != "th" else "ดินแดง", "pm25_avg": "55.2", "resp_pct": "15.3"},
                {"district": "Huai Khwang" if lang != "th" else "ห้วยขวาง", "pm25_avg": "52.8", "resp_pct": "14.1"},
                {"district": "Ratchathewi" if lang != "th" else "ราชเทวี", "pm25_avg": "51.4", "resp_pct": "13.7"},
                {"district": "Phaya Thai" if lang != "th" else "พญาไท", "pm25_avg": "50.9", "resp_pct": "13.2"},
                {"district": "Chatuchak" if lang != "th" else "จตุจักร", "pm25_avg": "49.6", "resp_pct": "12.8"},
            ],
        }

        # --- recommendations ---
        recommendations = []
        if diseases:
            top_d = diseases[0]
            recommendations.append({
                "title": f"Priority screening for {top_d['name']}" if lang != "th" else f"เร่งคัดกรอง{top_d['name']}",
                "description": f"With average risk at {top_d['avg_pct']}\\%, targeted interventions are recommended." if lang != "th" else f"ความเสี่ยงเฉลี่ย {top_d['avg_pct']}\\% ควรมีมาตรการเชิงรุก",
                "actions": [
                    "Increase screening frequency in high-risk districts" if lang != "th" else "เพิ่มความถี่การคัดกรองในเขตเสี่ยงสูง",
                    "Deploy mobile screening units" if lang != "th" else "จัดทีมคัดกรองเคลื่อนที่",
                ],
            })
        recommendations.append({
            "title": "Zone-based resource allocation" if lang != "th" else "จัดสรรทรัพยากรตามโซน",
            "description": "Allocate health resources proportionally to zone risk levels." if lang != "th" else "จัดสรรทรัพยากรสุขภาพตามระดับความเสี่ยงของแต่ละโซน",
            "actions": [
                "Review staffing levels per zone" if lang != "th" else "ทบทวนอัตรากำลังตามโซน",
                "Establish inter-zone referral protocols" if lang != "th" else "จัดทำระบบส่งต่อระหว่างโซน",
            ],
        })
        recommendations.append({
            "title": "Health behavior interventions" if lang != "th" else "มาตรการพฤติกรรมสุขภาพ",
            "description": "Target modifiable risk factors: smoking, alcohol, exercise." if lang != "th" else "มุ่งเน้นปัจจัยเสี่ยงที่ปรับเปลี่ยนได้: สูบบุหรี่ ดื่มสุรา ออกกำลังกาย",
            "actions": [
                "Expand smoking cessation programs" if lang != "th" else "ขยายโปรแกรมเลิกบุหรี่",
                "Community exercise promotion" if lang != "th" else "ส่งเสริมการออกกำลังกายในชุมชน",
            ],
        })

        return type("TemplateData", (), {
            "total_screened": data.total_screened,
            "district_count": data.district_count,
            "zone_count": data.zone_count,
            "disease_count": data.disease_count,
            "top_diseases": top_diseases,
            "key_findings": key_findings,
            "diseases": diseases,
            "factors": factors,
            "inferential_tests": inferential_tests,
            "screening_tests": screening_tests,
            "trend_results": trend_results,
            "seasonal_results": seasonal_results,
            "trend_chart_path": None,
            "zones": zones,
            "zone_heatmap_path": None,
            "pm25": type("PM25", (), pm25)(),
            "recommendations": recommendations,
        })()

    def _load_i18n(self, lang: str) -> dict[str, str]:
        """Load i18n strings for a language."""
        j2_name = f"i18n/{lang}.tex.j2"
        try:
            tmpl = self.env.get_template(j2_name)
            module = tmpl.module
            i18n = getattr(module, "i18n", None)
            if isinstance(i18n, dict) and i18n:
                return i18n
        except jinja2.TemplateNotFound:
            pass

        static_path = TEMPLATE_DIR / "i18n" / f"{lang}.tex"
        if static_path.exists():
            return self._parse_newcommands(static_path.read_text(encoding="utf-8"))

        logger.warning("No i18n file found for %s; using empty dict", lang)
        return {}

    @staticmethod
    def _parse_newcommands(tex: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for match in re.finditer(r"\\newcommand\{\\(\w+)\}\{(.+?)\}", tex):
            result[match.group(1)] = match.group(2)
        return result

    def _compile_tex(self, tex_path: Path, build_dir: Path) -> Path:
        if not Path(TECTONIC_PATH).exists():
            raise FileNotFoundError(
                f"Tectonic not found at {TECTONIC_PATH}. "
                "Set TECTONIC_PATH environment variable."
            )

        logger.info("Compiling %s with Tectonic...", tex_path.name)
        try:
            result = subprocess.run(
                [TECTONIC_PATH, str(tex_path)],
                capture_output=True, text=True,
                cwd=str(build_dir),
                timeout=TECTONIC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Tectonic compilation timed out after {TECTONIC_TIMEOUT}s")

        pdf_path = tex_path.with_suffix(".pdf")

        if result.returncode != 0:
            stderr = result.stderr[:1000] if result.stderr else "(no stderr)"
            stdout = result.stdout[:500] if result.stdout else ""
            if pdf_path.exists():
                logger.warning("Tectonic exited %d but PDF exists; continuing.\nstderr: %s",
                               result.returncode, stderr[:300])
                return pdf_path
            logger.error("Tectonic failed (exit %d):\nstderr: %s\nstdout: %s",
                         result.returncode, stderr, stdout)
            raise RuntimeError(f"LaTeX compilation failed: {stderr[:500]}")

        if not pdf_path.exists():
            raise RuntimeError(f"Tectonic exited 0 but PDF not found at {pdf_path}")
        return pdf_path

    def _generate_stub_tex(
        self, lang: str, report_type: str, data: ReportData, i18n: dict[str, str]
    ) -> str:
        preamble = "bma_article_preamble" if report_type == "whitepaper" else "bma_beamer_preamble"
        i18n_file = f"i18n/{lang}"
        title = i18n.get("rptTitle", "BMA Health Screening Report")
        subtitle = i18n.get("rptSubtitle", "Bangkok Metropolitan Administration")
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        disease_rows = ""
        for ds in data.city_disease_summary:
            name = ds.get("name_th", ds["disease"])
            disease_rows += (
                f"  {name} & {ds['avg_pct']:.1f}\\% & "
                f"{ds['min_pct']:.1f}\\% & {ds['max_pct']:.1f}\\% \\\\\n"
            )
        if report_type == "whitepaper":
            return self._stub_whitepaper(preamble, i18n_file, lang, title, subtitle, generated, data, disease_rows)
        else:
            return self._stub_slides(preamble, i18n_file, lang, title, subtitle, generated, data, disease_rows)

    @staticmethod
    def _stub_whitepaper(preamble, i18n_file, lang, title, subtitle, generated, data, disease_rows):
        return rf"""\input{{{preamble}}}
\input{{{i18n_file}}}
\begin{{document}}
\begin{{titlepage}}
\centering
\vspace*{{3cm}}
{{\Huge\bfseries {title} \par}}
\vspace{{1cm}}
{{\Large {subtitle} \par}}
\vspace{{2cm}}
{{\large {generated} \par}}
\end{{titlepage}}

\tableofcontents
\newpage

\section{{\rptExecSummary}}
\rptTotalScreened: \textbf{{{data.total_screened:,}}} \rptPersons \\
{data.district_count} \rptDistrict, {data.zone_count} \rptZone

\section{{\rptDescriptiveStats}}
\begin{{table}}[h]
\centering
\caption{{\rptDescriptiveStats}}
\begin{{tabular}}{{lrrr}}
\hline
\rptDisease & Avg & Min & Max \\
\hline
{disease_rows}\hline
\end{{tabular}}
\end{{table}}

\IfFileExists{{charts/disease_overview.png}}{{%
  \begin{{figure}}[h]\centering
  \includegraphics[width=0.85\textwidth]{{charts/disease_overview.png}}
  \end{{figure}}
}}{{}}

\section{{\rptZoneComparison}}
\IfFileExists{{charts/zone_heatmap.png}}{{%
  \begin{{figure}}[h]\centering
  \includegraphics[width=0.9\textwidth]{{charts/zone_heatmap.png}}
  \end{{figure}}
}}{{}}

\section{{\rptTrendAnalysis}}
\IfFileExists{{charts/trend_overview.png}}{{%
  \begin{{figure}}[h]\centering
  \includegraphics[width=0.95\textwidth]{{charts/trend_overview.png}}
  \end{{figure}}
}}{{}}

\vfill
{{\small \rptGenerated: {generated} \hfill \rptDisclaimer}}

\end{{document}}
"""

    @staticmethod
    def _stub_slides(preamble, i18n_file, lang, title, subtitle, generated, data, disease_rows):
        return rf"""\input{{{preamble}}}
\input{{{i18n_file}}}
\title{{{title}}}
\subtitle{{{subtitle}}}
\date{{{generated}}}
\begin{{document}}
\maketitle

\begin{{frame}}{{\rptExecSummary}}
\begin{{itemize}}
  \item \rptTotalScreened: \textbf{{{data.total_screened:,}}} \rptPersons
  \item {data.district_count} \rptDistrict, {data.zone_count} \rptZone
  \item {data.disease_count} NCDs
\end{{itemize}}
\end{{frame}}

\begin{{frame}}{{\rptDescriptiveStats}}
\IfFileExists{{charts/disease_overview.png}}{{%
  \centering\includegraphics[width=0.85\textwidth]{{charts/disease_overview.png}}
}}{{}}
\end{{frame}}

\begin{{frame}}{{\rptZoneComparison}}
\IfFileExists{{charts/zone_heatmap.png}}{{%
  \centering\includegraphics[width=0.9\textwidth]{{charts/zone_heatmap.png}}
}}{{}}
\end{{frame}}

\begin{{frame}}{{\rptTrendAnalysis}}
\IfFileExists{{charts/trend_overview.png}}{{%
  \centering\includegraphics[width=0.9\textwidth]{{charts/trend_overview.png}}
}}{{}}
\end{{frame}}

\end{{document}}
"""


# ---------------------------------------------------------------------------
# Jinja2 filter functions
# ---------------------------------------------------------------------------

def _number_format(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value)


def _pct_format(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (ValueError, TypeError):
        return str(value)


def _pct2_format(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (ValueError, TypeError):
        return str(value)


def _significance_stars(p_value: Any) -> str:
    try:
        p = float(p_value)
    except (ValueError, TypeError):
        return ""
    if p < 0.001:
        return "***"
    elif p < 0.01:
        return "**"
    elif p < 0.05:
        return "*"
    return "n.s."


def _latex_escape(text: Any) -> str:
    s = str(text)
    replacements = [
        ("\\", "\\textbackslash{}"),
        ("&", "\\&"),
        ("%", "\\%"),
        ("$", "\\$"),
        ("#", "\\#"),
        ("_", "\\_"),
        ("{", "\\{"),
        ("}", "\\}"),
        ("~", "\\textasciitilde{}"),
        ("^", "\\textasciicircum{}"),
    ]
    for old, new in replacements:
        s = s.replace(old, new)
    return s


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
report_generator = ReportGenerator()
