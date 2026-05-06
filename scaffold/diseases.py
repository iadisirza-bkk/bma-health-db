"""Disease pipeline registry — single source of truth for the scaffolder.

Each entry produces:
  • DB migration (4 MVs: classification, factors, factors_district, factors_region)
  • FastAPI router (/api/v2/<key>/{classification,factors,factors/bulk})
  • TS hooks (useXClassification, useXFactors, useXFactorsBulk)
  • mapStore patches (XPattern type, selectedXPattern state, setter action)
  • i18n patches (6 chip labels Th/En)

To add a disease:
  1. Add an entry below
  2. Run `python scaffold.py <key>`
  3. Apply the migration; the frontend auto-renders chips/cards via the
     registry on next reload.

The four-axis pattern is fixed:
  c1 = risk    — pre-computed risk_<key> column on mv_visit_resolved
  c2 = diag    — patient self-reported having the disease (column in
                 app1_*/portal_* tables)
  c3 = family  — parent had the disease (column in homehealth tables)
  c4 = lab     — disease-specific lab/measurement signal; provide the
                 CTE body verbatim in `lab.sql_app1` / `lab.sql_portal`.
                 The CTE must select (patient_id, <name> AS bool).
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LabAxis:
    """c4 axis: bespoke per disease. The scaffolder injects these CTE
    bodies verbatim into the migration. The combined CTE is called
    `labs` and exposes (patient_id, <name> bool).

    `name` is the bool column name (e.g. 'fpg_high', 'bp_high').
    `chip_label_th/en` is used in the chip 'bp' / 'fpg' (the 4th chip
    after risk/diag/family). e.g. "BP ≥ 140/90" or "FPG ≥ 126".
    """
    name: str                  # SQL alias, e.g. 'fpg_high'
    sql_app1: str              # CTE body — must select patient_id and <name>
    sql_portal: str            # CTE body — same shape
    chip_id: str               # 'fpg' / 'bp' — pattern enum value
    chip_label_th: str         # display in chip + tooltip subtitle
    chip_label_en: str
    headline_subtitle_lab_th: str  # short form for tooltip subtitle
    headline_subtitle_lab_en: str


@dataclass
class NewlyFoundCohort:
    """Definition of the 'newly found in screening' Active Follow-up cohort.

    Strict cohort = self-reported NOT having disease (c2 explicitly false)
    AND lab+ (c4=1).
    """
    cohort_label_th: str       # 'เจอใหม่ในโครงการ — เบาหวานจากการคัดกรอง'
    cohort_label_en: str       # 'Newly found via screening — Diabetes'
    criteria_th: str           # 'เกณฑ์: ติ้กว่า "ไม่เป็นเบาหวาน" + FPG ≥ 126 mg/dL'
    criteria_en: str


@dataclass
class DiseaseSpec:
    """Pipeline spec for one disease.

    The DB column conventions (used in MV CTEs):
      - risk:    mv_visit_resolved.<c1_risk_col>      (already pre-derived)
      - diag:    bma_med.app1_vitalsignslf.<c2_diag_col>  (numeric: 1=yes)
                 + bma_med.app1_homehealth.<c2_diag_col>
                 + bma_med.portal_*.<c2_diag_col>          (text: '1','true','TRUE')
      - family:  bma_med.app1_homehealth.<c3_family_col>     (parent col)
                 + bma_med.portal_homehealth.<c3_family_col>
      - lab:     spec.lab.sql_app1 / sql_portal (bespoke)
    """
    key: str                   # 'dm', 'hpt', 'cvd', etc. — short identifier
    short_upper: str           # 'DM', 'HPT' — used in Python names + comments
    name_th: str
    name_en: str
    emoji: str
    heatmap_key: str           # 'diabetes' / 'hypertension' — matches DISEASE_REGISTRY key

    c1_risk_col: str           # 'risk_dm'
    c2_diag_col: str           # 'dm' / 'hpt'
    c3_family_col: str         # 'pdm' / 'phpt'
    lab: LabAxis
    newly_found: NewlyFoundCohort

    # Migration sequence number (assign manually to keep history clean)
    migration_number: int

    # Disease-name fragments used inside the chip labels for axes 1-3.
    # e.g. for DM: 'เบาหวาน' → "เสี่ยง<word>", "ป่วย<word>", "ครอบครัวเป็น<word>"
    chip_disease_word_th: str
    chip_disease_word_en: str  # 'DM' / 'HPT'

    # Optional: which raw tables actually carry the c2 diag column. Defaults
    # to the full set used by DM/HPT (vitalsignslf + homehealth, both
    # sources). Override for diseases like CVD where `hrt` only lives on
    # homehealth + portal_vitalsignslf, not app1_vitalsignslf.
    diag_sources: list[str] = field(default_factory=lambda: [
        "app1_vitalsignslf", "app1_homehealth",
        "portal_vitalsignslf", "portal_homehealth",
    ])

    # Threshold info already lives in DISEASE_THRESHOLDS in constants.ts
    # (frontend handles display); scaffolder doesn't need to emit it.


# ── Lab CTE templates for common shapes ─────────────────────────────────

def lab_single_threshold(
    *,
    name: str,
    app1_table: str,
    app1_col: str,
    portal_table: str,
    portal_col: str,
    op: str,
    value: float,
) -> tuple[str, str]:
    """Generate (sql_app1, sql_portal) for a simple single-column threshold
    test (e.g. FPG >= 126). Portal needs ::numeric cast since values are text.
    """
    sql_app1 = f"""SELECT patient_id,
       ({app1_col} {op} {value}) AS {name}
FROM bma_med.{app1_table}
WHERE patient_id IS NOT NULL"""
    sql_portal = f"""SELECT patient_id,
       (CASE WHEN {portal_col} ~ '^[0-9]+(\\.[0-9]+)?$'
             THEN {portal_col}::numeric {op} {value} END) AS {name}
FROM bma_med.{portal_table}
WHERE patient_id IS NOT NULL"""
    return sql_app1, sql_portal


def lab_dual_or_visits(
    *,
    name: str,
    rules: list[tuple[str, tuple[float, float], str, float]],
) -> tuple[str, str]:
    """Generate (sql_app1, sql_portal) reading from mv_visit_resolved.
    Rules: list of (col, (min, max), op, value).

    Same SQL works for both sources because mv_visit_resolved already
    unions across app1 + portal. We return (sql, '') and the template
    skips the portal CTE.
    """
    or_clauses = " OR ".join(
        f"({col} BETWEEN {lo} AND {hi} AND {col} {op} {val})"
        for col, (lo, hi), op, val in rules
    )
    sql = f"""SELECT patient_id,
       ({or_clauses}) AS {name}
FROM public.mv_visit_resolved
WHERE is_dedup_kept = TRUE AND patient_id IS NOT NULL"""
    return sql, ""


# ── Existing diseases (keep parity with current MVs/router/hooks) ───────

DISEASES: dict[str, DiseaseSpec] = {
    "dm": DiseaseSpec(
        key="dm",
        short_upper="DM",
        name_th="เบาหวาน",
        name_en="Diabetes",
        emoji="🩸",
        heatmap_key="diabetes",
        c1_risk_col="risk_dm",
        c2_diag_col="dm",
        c3_family_col="pdm",
        chip_disease_word_th="เบาหวาน",
        chip_disease_word_en="DM",
        lab=LabAxis(
            name="fpg_high",
            sql_app1="""SELECT patient_id,
       (COALESCE(fbs, bldsugar) >= 126) AS fpg_high
FROM bma_med.app1_labhealth
WHERE patient_id IS NOT NULL""",
            sql_portal="""SELECT patient_id,
       (COALESCE(
          CASE WHEN fbs      ~ '^[0-9]+(\\.[0-9]+)?$' THEN fbs::numeric      END,
          CASE WHEN bldsugar ~ '^[0-9]+(\\.[0-9]+)?$' THEN bldsugar::numeric END
        ) >= 126) AS fpg_high
FROM bma_med.portal_labhealth
WHERE patient_id IS NOT NULL""",
            chip_id="fpg",
            chip_label_th="ผลแลป FPG ≥ 126",
            chip_label_en="FPG ≥ 126",
            headline_subtitle_lab_th="FPG ≥ 126",
            headline_subtitle_lab_en="FPG ≥ 126",
        ),
        newly_found=NewlyFoundCohort(
            cohort_label_th="เจอใหม่ในโครงการ — เบาหวานจากการคัดกรอง",
            cohort_label_en="Newly found via screening — Diabetes",
            criteria_th='เกณฑ์: ติ้กว่า "ไม่เป็นเบาหวาน" + FPG ≥ 126 mg/dL',
            criteria_en="Criteria: self-reported NOT-DM + FPG ≥ 126 mg/dL",
        ),
        migration_number=210,
    ),
    "hpt": DiseaseSpec(
        key="hpt",
        short_upper="HPT",
        name_th="ความดันโลหิตสูง",
        name_en="Hypertension",
        emoji="❤️",
        heatmap_key="hypertension",
        c1_risk_col="risk_hpt",
        c2_diag_col="hpt",
        c3_family_col="phpt",
        chip_disease_word_th="ความดันสูง",
        chip_disease_word_en="HPT",
        lab=LabAxis(
            name="bp_high",
            sql_app1="""SELECT patient_id,
       ((sbp BETWEEN 50 AND 250 AND sbp >= 140)
        OR (dbp BETWEEN 30 AND 200 AND dbp >= 90)) AS bp_high
FROM public.mv_visit_resolved
WHERE is_dedup_kept = TRUE AND patient_id IS NOT NULL""",
            # BP comes from mv_visit_resolved (single source) — empty portal
            # CTE so the template skips the UNION.
            sql_portal="",
            chip_id="bp",
            chip_label_th="ผลวัด BP ≥ 140/90",
            chip_label_en="BP ≥ 140/90",
            headline_subtitle_lab_th="BP ≥ 140/90",
            headline_subtitle_lab_en="BP ≥ 140/90",
        ),
        newly_found=NewlyFoundCohort(
            cohort_label_th="เจอใหม่ในโครงการ — ความดันสูงจากการคัดกรอง",
            cohort_label_en="Newly found via screening — Hypertension",
            criteria_th='เกณฑ์: ติ้กว่า "ไม่เป็นความดันสูง" + SBP ≥ 140 หรือ DBP ≥ 90 mmHg',
            criteria_en="Criteria: self-reported NOT-HPT + SBP ≥ 140 or DBP ≥ 90 mmHg",
        ),
        migration_number=220,
    ),
}


def get_spec(key: str) -> DiseaseSpec:
    if key not in DISEASES:
        raise KeyError(
            f"Unknown disease key '{key}'. Known: {sorted(DISEASES.keys())}. "
            f"Add an entry to DISEASES in scaffold/diseases.py first."
        )
    return DISEASES[key]


# ─────────────────────────────────────────────────────────────────────────
# Screening-only pipeline (lab/test result, no risk/diag/family axes)
# ─────────────────────────────────────────────────────────────────────────
#
# Diseases that DON'T fit the 4-axis NCD pattern — they're pure screening
# tests where the only meaningful signal is "abnormal lab/test result".
# Examples: CKD (eGFR<60), Liver (SGOT/SGPT≥120), Anemia (Hb<13/12),
# X-ray (chest abnormal), Cancer screens (Pap, FOBT), Obesity (BMI≥23).
#
# The architecture is a strict subset of DiseaseSpec:
#   • One MV per disease: mv_<key>_screening (district, n_total, n_abnormal)
#   • One API endpoint: /api/v2/<key>/screening
#   • Simpler tooltip card: just "X% ผิดปกติ (n=Y)" — no 4-axis breakdown
#
# `msd_<disease>` precomputed smallint flags (1=abnormal, 0=normal, NULL=
# not done) are the cleanest source — they bake in sex-aware thresholds
# and handle source type-mismatches across app1/portal. See research
# report in scaffold/diseases.py history for column-by-column gotchas.


@dataclass
class ScreeningSpec:
    """Pipeline spec for one screening-only disease."""
    key: str                   # 'ckd', 'liver', 'anemia', etc.
    short_upper: str           # 'CKD', 'LIVER', 'ANEMIA' for type names
    name_th: str
    name_en: str
    emoji: str
    heatmap_key: str           # matches DISEASE_REGISTRY key

    # Lab CTE bodies — must SELECT (patient_id, <bool> AS abnormal).
    # Convention: prefer `msd_*` precomputed flags (already type-clean).
    sql_app1: str
    sql_portal: str

    chip_label_th: str         # 'eGFR < 60', 'SGOT/SGPT ≥ 120', ...
    chip_label_en: str

    migration_number: int

    # Threshold info for the prevalence card. Used by the tooltip
    # "X% ผิดปกติ — เกณฑ์ <threshold_label_th>" line.
    threshold_label_th: str = ""
    threshold_label_en: str = ""


SCREENING: dict[str, ScreeningSpec] = {
    "ckd": ScreeningSpec(
        key="ckd", short_upper="CKD",
        name_th="โรคไต", name_en="CKD",
        emoji="🧬",
        heatmap_key="ckd",
        sql_app1="""SELECT patient_id, (msd_kidney = 1) AS abnormal
FROM bma_med.app1_labhealth
WHERE patient_id IS NOT NULL AND msd_kidney IS NOT NULL""",
        sql_portal="""SELECT patient_id, (msd_kidney = 1) AS abnormal
FROM bma_med.portal_labhealth
WHERE patient_id IS NOT NULL AND msd_kidney IS NOT NULL""",
        chip_label_th="eGFR < 60",
        chip_label_en="eGFR < 60",
        threshold_label_th="ผลเลือดผิดปกติ (eGFR < 60)",
        threshold_label_en="Abnormal (eGFR < 60)",
        migration_number=250,
    ),
    "liver": ScreeningSpec(
        key="liver", short_upper="LIVER",
        name_th="โรคตับ", name_en="Liver",
        emoji="🟤",
        heatmap_key="liver",
        sql_app1="""SELECT patient_id, (msd_liver = 1) AS abnormal
FROM bma_med.app1_labhealth
WHERE patient_id IS NOT NULL AND msd_liver IS NOT NULL""",
        sql_portal="""SELECT patient_id, (msd_liver = 1) AS abnormal
FROM bma_med.portal_labhealth
WHERE patient_id IS NOT NULL AND msd_liver IS NOT NULL""",
        chip_label_th="SGOT/SGPT ≥ 120",
        chip_label_en="SGOT/SGPT ≥ 120",
        threshold_label_th="ผลเลือดผิดปกติ (SGOT/SGPT ≥ 120)",
        threshold_label_en="Abnormal (SGOT/SGPT ≥ 120)",
        migration_number=251,
    ),
    "anemia": ScreeningSpec(
        key="anemia", short_upper="ANEMIA",
        name_th="ภาวะโลหิตจาง", name_en="Anemia",
        emoji="🩹",
        heatmap_key="anemia",
        sql_app1="""SELECT patient_id, (msd_anemia = 1) AS abnormal
FROM bma_med.app1_labhealth
WHERE patient_id IS NOT NULL AND msd_anemia IS NOT NULL""",
        sql_portal="""SELECT patient_id, (msd_anemia = 1) AS abnormal
FROM bma_med.portal_labhealth
WHERE patient_id IS NOT NULL AND msd_anemia IS NOT NULL""",
        chip_label_th="Hb < 13 (ช.) / < 12 (ญ.)",
        chip_label_en="Hb < 13 (M) / < 12 (F)",
        threshold_label_th="ผลเลือดผิดปกติ — Hb < 13 ชาย / < 12 หญิง",
        threshold_label_en="Abnormal Hb (sex-aware)",
        migration_number=252,
    ),
    "xray": ScreeningSpec(
        key="xray", short_upper="XRAY",
        name_th="X-ray", name_en="Chest X-ray",
        emoji="📷",
        heatmap_key="xray",
        sql_app1="""SELECT patient_id, (msd_chest = 1) AS abnormal
FROM bma_med.app1_vitalsignslf
WHERE patient_id IS NOT NULL AND msd_chest IS NOT NULL""",
        sql_portal="""SELECT patient_id, (msd_chest = 1) AS abnormal
FROM bma_med.portal_vitalsignslf
WHERE patient_id IS NOT NULL AND msd_chest IS NOT NULL""",
        chip_label_th="Chest X-ray ผิดปกติ",
        chip_label_en="Chest X-ray abnormal",
        threshold_label_th="ผล Chest X-ray ผิดปกติ",
        threshold_label_en="Chest X-ray abnormal",
        migration_number=253,
    ),
    "cervical": ScreeningSpec(
        key="cervical", short_upper="CERVICAL",
        name_th="มะเร็งปากมดลูก", name_en="Cervical cancer",
        emoji="🎀",
        heatmap_key="cervical",
        sql_app1="""SELECT patient_id, (msd_cervical = 1) AS abnormal
FROM bma_med.app1_labhealth
WHERE patient_id IS NOT NULL AND msd_cervical IS NOT NULL""",
        sql_portal="""SELECT patient_id, (msd_cervical = 1) AS abnormal
FROM bma_med.portal_labhealth
WHERE patient_id IS NOT NULL AND msd_cervical IS NOT NULL""",
        chip_label_th="ผลตรวจมะเร็งปากมดลูก ผิดปกติ",
        chip_label_en="Cervical cancer screen abnormal",
        threshold_label_th="ผลตรวจมะเร็งปากมดลูก ผิดปกติ",
        threshold_label_en="Cervical cancer screen abnormal",
        migration_number=254,
    ),
    "colon": ScreeningSpec(
        key="colon", short_upper="COLON",
        name_th="มะเร็งลำไส้", name_en="Colon cancer",
        emoji="🎗️",
        heatmap_key="colon",
        sql_app1="""SELECT patient_id, (msd_colon = 1) AS abnormal
FROM bma_med.app1_labhealth
WHERE patient_id IS NOT NULL AND msd_colon IS NOT NULL""",
        sql_portal="""SELECT patient_id, (msd_colon = 1) AS abnormal
FROM bma_med.portal_labhealth
WHERE patient_id IS NOT NULL AND msd_colon IS NOT NULL""",
        chip_label_th="ผลตรวจมะเร็งลำไส้ ผิดปกติ",
        chip_label_en="Colon cancer screen abnormal",
        threshold_label_th="ผลตรวจมะเร็งลำไส้ ผิดปกติ",
        threshold_label_en="Colon cancer screen abnormal",
        migration_number=255,
    ),
    "obesity": ScreeningSpec(
        key="obesity", short_upper="OBESITY",
        name_th="โรคอ้วน", name_en="Obesity",
        emoji="⚖️",
        heatmap_key="obesity",
        sql_app1="""SELECT patient_id, (bmi_calc >= 23 AND bmi_calc < 80) AS abnormal
FROM bma_med.app1_vitalsignslf
WHERE patient_id IS NOT NULL AND bmi_calc IS NOT NULL AND bmi_calc > 0 AND bmi_calc < 80""",
        sql_portal="""SELECT patient_id, (bmi_calc >= 23 AND bmi_calc < 80) AS abnormal
FROM bma_med.portal_vitalsignslf
WHERE patient_id IS NOT NULL AND bmi_calc IS NOT NULL AND bmi_calc > 0 AND bmi_calc < 80""",
        chip_label_th="BMI ≥ 23",
        chip_label_en="BMI ≥ 23",
        threshold_label_th="BMI ≥ 23 kg/m² (เกณฑ์ Asia-Pacific)",
        threshold_label_en="BMI ≥ 23 (Asia-Pacific)",
        migration_number=256,
    ),
    "cvd": ScreeningSpec(
        key="cvd", short_upper="CVD",
        name_th="โรคหัวใจและหลอดเลือด", name_en="Cardiovascular",
        emoji="🫀",
        heatmap_key="cardiovascular",
        # Per the official guideline (medical-knowledge/disease-criteria-guideline.jpg):
        # CVD criterion is "ผล EKG ผิดปกติ" — single-axis screening, NOT 4-axis NCD.
        # msd_cvd_ekg is the precomputed smallint flag on *_vitalsignslf
        # (1=abnormal, 0=normal, NULL=not done).
        sql_app1="""SELECT patient_id, (msd_cvd_ekg = 1) AS abnormal
FROM bma_med.app1_vitalsignslf
WHERE patient_id IS NOT NULL AND msd_cvd_ekg IS NOT NULL""",
        sql_portal="""SELECT patient_id, (msd_cvd_ekg = 1) AS abnormal
FROM bma_med.portal_vitalsignslf
WHERE patient_id IS NOT NULL AND msd_cvd_ekg IS NOT NULL""",
        chip_label_th="ผล EKG ผิดปกติ",
        chip_label_en="EKG abnormal",
        threshold_label_th="ผล EKG ผิดปกติ",
        threshold_label_en="Abnormal EKG",
        migration_number=258,
    ),
    "dyslipidemia": ScreeningSpec(
        key="dyslipidemia", short_upper="LIPID",
        name_th="โรคไขมันในเลือดสูง", name_en="Dyslipidemia",
        emoji="🧪",
        heatmap_key="dyslipidemia",
        sql_app1="""SELECT patient_id,
       (cholest >= 200 AND cholest < 1000) AS abnormal
FROM bma_med.app1_labhealth
WHERE patient_id IS NOT NULL AND cholest IS NOT NULL AND cholest > 0 AND cholest < 1000""",
        sql_portal="""SELECT patient_id,
       (CASE WHEN cholest ~ '^[0-9.]+$' THEN cholest::numeric BETWEEN 0 AND 1000 AND cholest::numeric >= 200 END) AS abnormal
FROM bma_med.portal_labhealth
WHERE patient_id IS NOT NULL""",
        chip_label_th="Cholesterol ≥ 200",
        chip_label_en="Cholesterol ≥ 200",
        threshold_label_th="Cholesterol ≥ 200 mg/dL",
        threshold_label_en="Cholesterol ≥ 200 mg/dL",
        migration_number=257,
    ),
}


def get_screening_spec(key: str) -> ScreeningSpec:
    if key not in SCREENING:
        raise KeyError(
            f"Unknown screening key '{key}'. Known: {sorted(SCREENING.keys())}."
        )
    return SCREENING[key]
