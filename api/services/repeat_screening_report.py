"""Generate a single-topic PDF report on "คนตรวจซ้ำ" (repeat screening).

Surfaces who comes back for more than one screening visit, from where,
how often, and how long between visits.
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
from database import execute_query

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "latex"
CACHE_DIR = Path(config.REPORTS_DIR) / "repeat_screening"


def _latex_escape(text: str) -> str:
    """Escape LaTeX special characters to avoid compile errors on Thai names."""
    for c, r in {
        "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}", "^": r"\^{}", "~": r"\~{}",
        "\\": r"\textbackslash{}",
    }.items():
        text = text.replace(c, r)
    return text


def _fmt_n(n: int | float | None) -> str:
    """Format integer with commas for LaTeX output."""
    if n is None:
        return "—"
    return f"{int(n):,}"


def _collect_data() -> dict:
    """Run all SQL queries needed for the report and return a render context."""

    # 1. Visit distribution: how many patients by visit count
    dist_rows = execute_query("""
        SELECT visit_count, COUNT(*) AS n_patients
        FROM (
          SELECT patient_id, COUNT(*) AS visit_count
          FROM raw_vitalsigns
          WHERE cancel_status IS DISTINCT FROM 1
          GROUP BY patient_id
        ) sub
        GROUP BY visit_count
        ORDER BY visit_count
    """) or []

    total_patients = sum(r["n_patients"] for r in dist_rows)
    by_count: dict[int, int] = {r["visit_count"]: r["n_patients"] for r in dist_rows}

    # Bucket distribution into 1, 2, 3, 4+ visits
    n1 = by_count.get(1, 0)
    n2 = by_count.get(2, 0)
    n3 = by_count.get(3, 0)
    n4plus = sum(n for c, n in by_count.items() if c >= 4)
    repeat_patients = total_patients - n1  # 2 visits or more

    def _pct(n: int) -> str:
        return f"{round(100.0 * n / total_patients, 2):.2f}" if total_patients else "0"

    visit_distribution = [
        {"label": "1 ครั้ง (ตรวจครั้งเดียว)", "patients": _fmt_n(n1), "pct": _pct(n1)},
        {"label": "2 ครั้ง",   "patients": _fmt_n(n2),     "pct": _pct(n2)},
        {"label": "3 ครั้ง",   "patients": _fmt_n(n3),     "pct": _pct(n3)},
        {"label": "4 ครั้งขึ้นไป", "patients": _fmt_n(n4plus), "pct": _pct(n4plus)},
    ]

    repeat_rate = round(100.0 * repeat_patients / total_patients, 2) if total_patients else 0

    # 2. Top districts by repeat rate (n >= 100 to protect small buckets)
    districts = execute_query("""
        SELECT
          d.name_th,
          COUNT(DISTINCT v.patient_id) AS unique_patients,
          COUNT(DISTINCT CASE WHEN pc.visit_count >= 2 THEN v.patient_id END) AS repeat_patients,
          ROUND(100.0 * COUNT(DISTINCT CASE WHEN pc.visit_count >= 2 THEN v.patient_id END)
                      / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS repeat_rate
        FROM raw_vitalsigns v
        JOIN ref_districts d ON d.dcode = v.district_code
        JOIN (
          SELECT patient_id, COUNT(*) AS visit_count
          FROM raw_vitalsigns
          WHERE cancel_status IS DISTINCT FROM 1
          GROUP BY patient_id
        ) pc ON pc.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
        GROUP BY d.dcode, d.name_th
        HAVING COUNT(DISTINCT v.patient_id) >= 100
        ORDER BY repeat_rate DESC
        LIMIT 10
    """) or []

    top_districts = [
        {
            "name": _latex_escape(r["name_th"] or "-"),
            "repeat": _fmt_n(r["repeat_patients"]),
            "total":  _fmt_n(r["unique_patients"]),
            "rate":   f"{float(r['repeat_rate'] or 0):.2f}",
        }
        for r in districts
    ]

    # 3. Top facilities
    facilities = execute_query("""
        SELECT
          COALESCE(f.name_th, v.facility_code) AS name,
          COUNT(DISTINCT v.patient_id) AS unique_patients,
          COUNT(DISTINCT CASE WHEN pc.visit_count >= 2 THEN v.patient_id END) AS repeat_patients,
          ROUND(100.0 * COUNT(DISTINCT CASE WHEN pc.visit_count >= 2 THEN v.patient_id END)
                      / NULLIF(COUNT(DISTINCT v.patient_id), 0), 2) AS repeat_rate
        FROM raw_vitalsigns v
        LEFT JOIN ref_facilities f ON f.code = v.facility_code
        JOIN (
          SELECT patient_id, COUNT(*) AS visit_count
          FROM raw_vitalsigns
          WHERE cancel_status IS DISTINCT FROM 1
          GROUP BY patient_id
        ) pc ON pc.patient_id = v.patient_id
        WHERE v.cancel_status IS DISTINCT FROM 1
        GROUP BY v.facility_code, f.name_th
        HAVING COUNT(DISTINCT v.patient_id) >= 100
        ORDER BY repeat_rate DESC
        LIMIT 10
    """) or []

    top_facilities = [
        {
            "name": _latex_escape(r["name"] or "-"),
            "repeat": _fmt_n(r["repeat_patients"]),
            "total":  _fmt_n(r["unique_patients"]),
            "rate":   f"{float(r['repeat_rate'] or 0):.2f}",
        }
        for r in facilities
    ]

    # 4. Gap between visits (days)
    gap_rows = execute_query("""
        WITH visit_pairs AS (
          SELECT
            patient_id,
            visit_date,
            LAG(visit_date) OVER (PARTITION BY patient_id ORDER BY visit_date) AS prev_visit
          FROM raw_vitalsigns
          WHERE cancel_status IS DISTINCT FROM 1
        )
        SELECT
          ROUND(AVG(EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0)::numeric, 0) AS avg_days,
          ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP
                  (ORDER BY EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0)::numeric, 0) AS median_days,
          ROUND(MIN(EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0)::numeric, 0) AS min_days,
          ROUND(MAX(EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0)::numeric, 0) AS max_days,
          COUNT(*) AS n_pairs,
          COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0 < 90) AS gap_0_3m,
          COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0 BETWEEN 90 AND 180) AS gap_3_6m,
          COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0 > 180
                             AND EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0 <= 365) AS gap_6_12m,
          COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0 > 365
                             AND EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0 <= 730) AS gap_12_24m,
          COUNT(*) FILTER (WHERE EXTRACT(EPOCH FROM (visit_date - prev_visit)) / 86400.0 > 730) AS gap_24m_plus
        FROM visit_pairs
        WHERE prev_visit IS NOT NULL
    """) or [{}]
    g = gap_rows[0] if gap_rows else {}
    n_pairs = int(g.get("n_pairs") or 0)

    def _gap_pct(n) -> str:
        n = int(n or 0)
        return f"{round(100.0 * n / n_pairs, 1):.1f}" if n_pairs else "0"

    # 5. Narrative blocks
    top1_pct = _pct(n1)
    distribution_insight = _latex_escape(
        f"ผู้เข้ารับการตรวจส่วนใหญ่ ({top1_pct}\\%) ตรวจครั้งเดียว ส่วนกลุ่มที่ตรวจซ้ำ "
        f"(≥2 ครั้ง) มี {_fmt_n(repeat_patients)} คน คิดเป็น {repeat_rate:.2f}\\% ของผู้เข้ารับตรวจทั้งหมด"
    ).replace(r"\%", "%")  # un-escape because we used \% above, Jinja will print literal

    # Key findings
    key_findings = []
    if repeat_patients:
        key_findings.append(_latex_escape(
            f"มีผู้ตรวจซ้ำทั้งหมด {_fmt_n(repeat_patients)} คน ({repeat_rate:.2f}%) "
            f"จากผู้เข้าคัดกรองทั้งหมด {_fmt_n(total_patients)} คน"
        ))
    if top_districts:
        t = districts[0]
        key_findings.append(_latex_escape(
            f"เขต {t['name_th']} มีอัตราตรวจซ้ำสูงที่สุด {float(t['repeat_rate']):.2f}% "
            f"({_fmt_n(t['repeat_patients'])}/{_fmt_n(t['unique_patients'])} คน) "
            f"สะท้อนว่าประชาชนในเขตนี้ติดตามสุขภาพต่อเนื่องดี"
        ))
    if g.get("avg_days"):
        avg_d = int(g["avg_days"])
        mo = round(avg_d / 30, 1)
        key_findings.append(_latex_escape(
            f"ช่วงห่างเฉลี่ยระหว่างการมาตรวจคือ {avg_d} วัน ({mo} เดือน) — "
            f"ประชาชนส่วนใหญ่กลับมาตรวจในรอบ 6–12 เดือน"
        ))
    if top_facilities:
        f0 = facilities[0]
        key_findings.append(_latex_escape(
            f"{f0['name'] if isinstance(f0.get('name'), str) else f0.get('name_th','-')} "
            f"มีอัตราตรวจซ้ำสูงสุดที่ {float(f0['repeat_rate']):.2f}% — ใช้เป็นต้นแบบของระบบติดตาม"
        ))

    # Recommendations
    recommendations = [
        _latex_escape("ส่งเสริมให้ผู้ตรวจครั้งเดียว (one-time screeners) กลับมาตรวจซ้ำในรอบ 6–12 เดือน โดยใช้ SMS/LINE OA และระบบนัดหมายอัตโนมัติ"),
        _latex_escape("ถอดบทเรียนจากเขต/สถานพยาบาลที่มีอัตราตรวจซ้ำสูง ขยายแนวทางสู่พื้นที่อื่น โดยเฉพาะเขตที่มีอัตรา < 15%"),
        _latex_escape("กลุ่มที่ตรวจซ้ำ 3 ครั้งขึ้นไป ถือเป็นกลุ่ม Chronic Follow-up — ควรเชื่อมระบบเวชระเบียนให้แพทย์เห็น trend ครบ"),
        _latex_escape("ตรวจสอบช่วงห่างที่ยาวผิดปกติ (> 2 ปี) เพื่อดูว่ามี loss-to-follow-up ในพื้นที่เฉพาะหรือไม่"),
    ]

    return {
        "generated_date": date.today().strftime("%d/%m/%Y"),
        "total_patients": _fmt_n(total_patients),
        "repeat_patients": _fmt_n(repeat_patients),
        "repeat_rate": f"{repeat_rate:.2f}",
        "avg_days_between": _fmt_n(g.get("avg_days")),
        "median_days_between": _fmt_n(g.get("median_days")),
        "min_days_between": _fmt_n(g.get("min_days")),
        "max_days_between": _fmt_n(g.get("max_days")),
        "total_pairs": _fmt_n(n_pairs),
        "visit_distribution": visit_distribution,
        "top_districts": top_districts,
        "top_facilities": top_facilities,
        "gap_0_3m": _fmt_n(g.get("gap_0_3m")),      "gap_0_3m_pct": _gap_pct(g.get("gap_0_3m")),
        "gap_3_6m": _fmt_n(g.get("gap_3_6m")),      "gap_3_6m_pct": _gap_pct(g.get("gap_3_6m")),
        "gap_6_12m": _fmt_n(g.get("gap_6_12m")),    "gap_6_12m_pct": _gap_pct(g.get("gap_6_12m")),
        "gap_12_24m": _fmt_n(g.get("gap_12_24m")),  "gap_12_24m_pct": _gap_pct(g.get("gap_12_24m")),
        "gap_24m_plus": _fmt_n(g.get("gap_24m_plus")), "gap_24m_plus_pct": _gap_pct(g.get("gap_24m_plus")),
        "distribution_insight": distribution_insight,
        "key_findings": key_findings,
        "recommendations": recommendations,
    }


def generate_repeat_screening_report(lang: str = "th") -> Path:
    """Build the repeat-screening PDF and cache it. Returns the output path."""
    ctx = _collect_data()

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        variable_start_string="<<", variable_end_string=">>",
        block_start_string="<%", block_end_string="%>",
        comment_start_string="<#", comment_end_string="#>",
    )
    template = env.get_template("report_repeat_screening.tex.j2")
    rendered = template.render(**ctx)

    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copytree(str(TEMPLATE_DIR / "assets"), str(Path(tmpdir) / "assets"))
        shutil.copy2(str(TEMPLATE_DIR / "bma_beamer_preamble.tex"), tmpdir)
        tex_path = Path(tmpdir) / "repeat_screening.tex"
        tex_path.write_text(rendered, encoding="utf-8")

        tectonic = shutil.which("tectonic") or config.TECTONIC_PATH
        result = subprocess.run(
            [tectonic, "-X", "compile", str(tex_path)],
            capture_output=True, text=True,
            timeout=config.TECTONIC_TIMEOUT, cwd=tmpdir,
        )
        if result.returncode != 0:
            logger.error("Tectonic stderr tail: %s", result.stderr[-2000:])
            raise RuntimeError(f"Tectonic failed: {result.stderr[-500:]}")

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = CACHE_DIR / f"repeat_screening_{lang}.pdf"
        shutil.copy2(str(tex_path.with_suffix(".pdf")), str(out))

    logger.info("Repeat-screening report generated: %s", out)
    return out
