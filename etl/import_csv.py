#!/usr/bin/env python3
"""
ETL pipeline: import CSV files from BMA portal_top into PostgreSQL raw tables.

Usage:
    python etl/import_csv.py [--data-dir path] [--db-url postgresql://...]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values as _pg_execute_values


def execute_values(cur, sql, rows, page_size=500):
    """Wrapper around psycopg2 execute_values that skips bad rows instead of failing.

    On batch error: retries rows one-by-one, skipping those that fail.
    """
    try:
        cur.execute("SAVEPOINT batch_sp")
        _pg_execute_values(cur, sql, rows, page_size=page_size)
        cur.execute("RELEASE SAVEPOINT batch_sp")
    except (psycopg2.errors.NumericValueOutOfRange,
            psycopg2.errors.StringDataRightTruncation,
            psycopg2.errors.DataException) as batch_err:
        # Batch failed — rollback to before the batch, then insert one-by-one
        cur.execute("ROLLBACK TO SAVEPOINT batch_sp")
        skipped = 0
        for row in rows:
            try:
                cur.execute("SAVEPOINT row_sp")
                _pg_execute_values(cur, sql, [row])
                cur.execute("RELEASE SAVEPOINT row_sp")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                skipped += 1
        if skipped:
            print(f"    Skipped {skipped} bad rows (of {len(rows)})")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Secret salt for HMAC hashing — MUST be set in production
# Store separately from database (env var or key management service)
_HASH_SECRET = os.getenv("IDCARD_HASH_SECRET", "dev-hash-secret-change-in-production").encode()


def hash_id(raw_value: str) -> Optional[str]:
    """Base64-decode the value then return its HMAC-SHA-256 hex digest.

    Uses a secret key to prevent brute-force reversal of Thai national ID numbers.
    The secret MUST be stored separately from the database.
    """
    if pd.isna(raw_value) or str(raw_value).strip() == "":
        return None
    try:
        decoded = base64.b64decode(str(raw_value).strip())
        return hmac.new(_HASH_SECRET, decoded, hashlib.sha256).hexdigest()
    except Exception:
        return None


def parse_date(val, fmt: str = "%d/%m/%Y %H:%M:%S"):
    """Parse a DD/MM/YYYY HH:MM:SS string into a Python datetime, or None."""
    if pd.isna(val) or str(val).strip() == "":
        return None
    s = str(val).strip()
    # Try full format first, then date-only
    for f in (fmt, "%d/%m/%Y"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def parse_date_only(val):
    """Parse to a date object (no time component)."""
    dt = parse_date(val)
    return dt.date() if dt else None


_INT4_MIN, _INT4_MAX = -2147483648, 2147483647


def safe_int(val, lo=_INT4_MIN, hi=_INT4_MAX):
    """Parse int; return None if outside [lo, hi]. Default = INT4 range."""
    if pd.isna(val) or str(val).strip() == "":
        return None
    try:
        v = int(float(val))
        if v < lo or v > hi:
            return None
        return v
    except (ValueError, TypeError, OverflowError):
        return None


def safe_float(val, lo=None, hi=None):
    """Parse float; return None if empty, unparseable, inf, nan, or outside [lo, hi]."""
    if pd.isna(val) or str(val).strip() == "":
        return None
    try:
        v = float(val)
        if v != v or v == float('inf') or v == float('-inf'):  # nan/inf
            return None
        if lo is not None and v < lo:
            return None
        if hi is not None and v > hi:
            return None
        return v
    except (ValueError, TypeError, OverflowError):
        return None


def safe_str(val):
    if pd.isna(val) or str(val).strip() == "":
        return None
    return str(val).strip()


def to_bool(val) -> Optional[bool]:
    """Truthy if not empty / 0 / NaN."""
    if pd.isna(val) or str(val).strip() == "":
        return None
    try:
        return float(val) != 0
    except (ValueError, TypeError, OverflowError):
        return None


def collect_array(row, prefixes: List[str]) -> Optional[list]:
    """Collect non-null integer values from columns into a list."""
    vals = []
    for col in prefixes:
        v = safe_int(row.get(col))
        if v is not None:
            vals.append(v)
    return vals if vals else None


def age_group(birth_year: Optional[int], current_year: int) -> Optional[str]:
    if birth_year is None:
        return None
    age = current_year - birth_year
    if age < 15:
        return None
    if age <= 21:
        return "วัยเรียน"
    if age <= 35:
        return "วัยเริ่มทำงาน"
    if age <= 45:
        return "วัยทำงาน"
    if age <= 55:
        return "วัยกลางคน"
    if age <= 64:
        return "วัยก่อนสูงอายุ"
    return "สูงวัย"


# ---------------------------------------------------------------------------
# Per-table import functions
# ---------------------------------------------------------------------------

def import_patients(cur, df, current_year: int) -> Dict[str, int]:
    """Import pt.csv -> raw_patients. Returns mapping idcard_hash -> patient id."""
    print("[1/7] Importing pt.csv -> raw_patients ...")
    rows = []
    for r in df.to_dict(orient='records'):
        id_hash = hash_id(r.get("IDCARD"))
        if id_hash is None:
            continue
        by = parse_date(r.get("BIRTHDATE"))
        by_year = by.year if by else None
        # Buddhist calendar: if year > 2400, assume Buddhist era
        if by_year and by_year > 2400:
            by_year -= 543
        # Sanity check: year must be 1900-2030
        if by_year is not None and (by_year < 1900 or by_year > 2030):
            by_year = None
        computed_age = (current_year - by_year) if by_year else None
        if computed_age is not None and (computed_age < 0 or computed_age > 150):
            computed_age = None
        rows.append((
            id_hash,
            safe_int(r.get("NOTYPE")),
            safe_int(r.get("PNAME")),
            safe_int(r.get("MALE")),
            by_year,
            age_group(by_year, current_year),
            computed_age,
            parse_date(r.get("FIRSTDATE")),
            parse_date(r.get("LASTDATE")),
        ))

    if not rows:
        print("  No valid rows.")
        return {}

    # Dedup by idcard_hash (first column) — keep last occurrence
    seen = {}
    for row in rows:
        seen[row[0]] = row
    deduped = list(seen.values())
    if len(deduped) < len(rows):
        print(f"  Deduped {len(rows)} -> {len(deduped)} (removed {len(rows)-len(deduped)} duplicates)")
    rows = deduped

    sql = """
        INSERT INTO raw_patients (idcard_hash, notype, pname, sex, birth_year, age_group, age, created_at, updated_at)
        VALUES %s
        ON CONFLICT (idcard_hash) DO UPDATE SET
            notype     = EXCLUDED.notype,
            pname      = EXCLUDED.pname,
            sex        = EXCLUDED.sex,
            birth_year = EXCLUDED.birth_year,
            age_group  = EXCLUDED.age_group,
            age        = EXCLUDED.age,
            updated_at = EXCLUDED.updated_at
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"  Upserted {len(rows)} patients.")

    # Build lookup: idcard_hash -> id
    cur.execute("SELECT idcard_hash, id FROM raw_patients")
    mapping = {row[0]: row[1] for row in cur.fetchall()}
    return mapping


def _batch_ensure_patients(df, patient_map: Dict[str, int], cur):
    """Pre-register all PIDs in a DataFrame that aren't in patient_map yet."""
    pid_col = None
    for col in ("PID", "IDCARD"):
        if col in df.columns:
            pid_col = col
            break
    if pid_col is None:
        return

    new_hashes = []
    for raw_pid in df[pid_col].dropna().unique():
        h = hash_id(raw_pid)
        if h and h not in patient_map:
            new_hashes.append((h,))

    if new_hashes:
        execute_values(
            cur,
            """INSERT INTO raw_patients (idcard_hash)
               VALUES %s ON CONFLICT (idcard_hash) DO NOTHING""",
            new_hashes, page_size=500,
        )
        # Refresh the patient map
        cur.execute("SELECT idcard_hash, id FROM raw_patients")
        patient_map.clear()
        patient_map.update({row[0]: row[1] for row in cur.fetchall()})
        print(f"  Auto-registered {len(new_hashes)} new patients. Map size: {len(patient_map)}")


def _ensure_patient(pid_raw, patient_map: Dict[str, int], cur) -> Optional[int]:
    """Hash a PID and look up the patient id."""
    h = hash_id(pid_raw)
    if h is None:
        return None
    return patient_map.get(h)


def import_visits(cur, df, patient_map: Dict[str, int]):
    """Import pthistory.csv -> raw_visits."""
    print("[2/7] Importing pthistory.csv -> raw_visits ...")
    rows = []
    skipped = 0
    for r in df.to_dict(orient='records'):
        pid = _ensure_patient(r.get("PID"), patient_map, cur)
        if pid is None:
            skipped += 1
            continue
        rows.append((
            pid,
            parse_date_only(r.get("VSTDATE")),
            safe_str(r.get("HPTCODE")),
            safe_int(r.get("RLGN")),
            safe_int(r.get("LGBTQ")),
            safe_int(r.get("CANCELST")),
            safe_str(r.get("FIRSTSTF")),
            parse_date(r.get("FIRSTDATE")),
        ))

    if not rows:
        print(f"  No valid rows (skipped {skipped}).")
        return

    sql = """
        INSERT INTO raw_visits (patient_id, visit_date, facility_code, religion, lgbtq, cancel_status, staff_code, created_at)
        VALUES %s
        ON CONFLICT DO NOTHING
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"  Inserted {len(rows)} visits (skipped {skipped} unmatched PIDs).")


def import_vitalsigns(cur, df, patient_map: Dict[str, int]):
    """Import vitalsignslf.csv -> raw_vitalsigns."""
    print("[3/7] Importing vitalsignslf.csv -> raw_vitalsigns ...")
    rows = []
    skipped = 0
    for r in df.to_dict(orient='records'):
        pid = _ensure_patient(r.get("PID"), patient_map, cur)
        if pid is None:
            skipped += 1
            continue

        stress = collect_array(r, ["STMNG1", "STMNG2", "STMNG3", "STMNG4"])

        height = safe_float(r.get("HEIGHT"), 50, 250)
        weight = safe_float(r.get("WEIGHT"), 10, 300)
        computed_bmi = None
        if height and weight:
            computed_bmi = round(weight / (height / 100.0) ** 2, 2)
            if computed_bmi > 80:
                computed_bmi = None

        rows.append((
            pid,
            parse_date(r.get("VSTDATE")),
            safe_str(r.get("HPTCODE")),
            safe_int(r.get("HBPN")),
            safe_int(r.get("LBPN")),
            safe_float(r.get("PREFPG"), 0, 999),
            safe_float(r.get("POSTFPG"), 0, 999),
            height,
            weight,
            safe_float(r.get("WSTL"), 30, 200),
            safe_int(r.get("PR")),
            safe_int(r.get("SMOKE")),
            safe_int(r.get("ALCOHAL")),
            safe_int(r.get("CHEST")),
            safe_int(r.get("EKG")),
            safe_int(r.get("VSACT")),
            safe_int(r.get("DRSCN")),
            safe_int(r.get("SCR2Q1")),
            safe_int(r.get("SCR2Q2")),
            safe_int(r.get("SCN9Q1")),
            safe_int(r.get("SCN9Q2")),
            safe_int(r.get("SCN9Q3")),
            safe_int(r.get("SCN9Q4")),
            safe_int(r.get("SCN9Q5")),
            safe_int(r.get("SCN9Q6")),
            safe_int(r.get("SCN9Q7")),
            safe_int(r.get("SCN9Q8")),
            safe_int(r.get("SCN9Q9")),
            safe_int(r.get("ST501")),
            safe_int(r.get("ST502")),
            safe_int(r.get("ST503")),
            safe_int(r.get("ST504")),
            safe_int(r.get("ST505")),
            safe_int(r.get("SCRRS")),
            to_bool(r.get("RISKDM")),
            to_bool(r.get("RISKHPT")),
            to_bool(r.get("RISKCDVCL")),
            to_bool(r.get("RISKBMI")),
            to_bool(r.get("DM")),
            to_bool(r.get("HPT")),
            to_bool(r.get("CDVCL")),
            to_bool(r.get("STROKE")),
            to_bool(r.get("FAT")),
            to_bool(r.get("CHLTR")),
            to_bool(r.get("OTH")),
            safe_int(r.get("DMFM")),
            safe_str(r.get("DISTRICTBKK")),
            safe_str(r.get("LOCATION")),
            stress,
            safe_int(r.get("CANCELST")),
            computed_bmi,
        ))

    if not rows:
        print(f"  No valid rows (skipped {skipped}).")
        return

    sql = """
        INSERT INTO raw_vitalsigns (
            patient_id, visit_date, facility_code,
            sbp, dbp, fasting_glucose, post_glucose,
            height_cm, weight_kg, waist_cm, pulse_rate,
            smoking, alcohol, chest_xray, ekg, vision, dr_screening,
            depression_2q_1, depression_2q_2,
            phq9_q1, phq9_q2, phq9_q3, phq9_q4, phq9_q5,
            phq9_q6, phq9_q7, phq9_q8, phq9_q9,
            st5_q1, st5_q2, st5_q3, st5_q4, st5_q5,
            screening_result,
            risk_dm, risk_hpt, risk_cvd, risk_bmi,
            found_dm, found_hpt, found_cvd, found_stroke,
            found_obesity, found_dyslipidemia, found_other,
            family_dm, district_code, location_code,
            stress_management, cancel_status,
            bmi
        ) VALUES %s
        ON CONFLICT DO NOTHING
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"  Inserted {len(rows)} vitalsign records (skipped {skipped}).")


def import_homevisit(cur, df, patient_map: Dict[str, int]):
    """Import homevisit.csv -> raw_homevisit."""
    print("[4/7] Importing homevisit.csv -> raw_homevisit ...")
    rows = []
    skipped = 0
    for r in df.to_dict(orient='records'):
        pid = _ensure_patient(r.get("PID"), patient_map, cur)
        if pid is None:
            skipped += 1
            continue

        disability = collect_array(r, [f"DISTYPE{i}" for i in range(1, 9)])
        requests = collect_array(r, [f"REQUEST{i}" for i in range(1, 8)])

        rows.append((
            pid,
            parse_date(r.get("VSTDATE")),
            safe_str(r.get("HPTCODE")),
            safe_int(r.get("SELFOUR")),
            disability,
            safe_int(r.get("EDU")),
            safe_int(r.get("OCCPTN")),
            safe_int(r.get("PROVINCE"), 0, 99999),
            safe_int(r.get("DISTRICT"), 0, 99999),
            safe_int(r.get("SUBDISTRICT"), 0, 999999),
            safe_int(r.get("HOMETYPE")),
            safe_int(r.get("PRVLG")),
            safe_int(r.get("CRPROVINCE"), 0, 99999),
            safe_int(r.get("CRDISTRICT"), 0, 99999),
            safe_int(r.get("WRKDISTRICT"), 0, 99999),
            safe_int(r.get("WRKTYPE")),
            safe_int(r.get("WRKJOURNEY")),
            safe_int(r.get("HEALTHUSE")),
            requests,
            safe_int(r.get("WORKSHOP")),
            safe_int(r.get("CANCELST")),
        ))

    if not rows:
        print(f"  No valid rows (skipped {skipped}).")
        return

    sql = """
        INSERT INTO raw_homevisit (
            patient_id, visit_date, facility_code, self_care,
            disability_types, education, occupation,
            home_province, home_district, home_subdistrict,
            home_type, health_privilege,
            current_province, current_district,
            work_district, work_type, work_journey,
            health_facility_used, service_requests,
            workshop_willing, cancel_status
        ) VALUES %s
        ON CONFLICT DO NOTHING
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"  Inserted {len(rows)} homevisit records (skipped {skipped}).")


def import_homehealth(cur, df, patient_map: Dict[str, int]):
    """Import homehealth.csv -> raw_homehealth."""
    print("[5/7] Importing homehealth.csv -> raw_homehealth ...")
    rows = []
    skipped = 0
    for r in df.to_dict(orient='records'):
        pid = _ensure_patient(r.get("PID"), patient_map, cur)
        if pid is None:
            skipped += 1
            continue

        rows.append((
            pid,
            parse_date(r.get("VSTDATE")),
            safe_str(r.get("HPTCODE")),
            safe_int(r.get("CGTDS")),
            safe_int(r.get("DM")),
            safe_int(r.get("HPT")),
            safe_int(r.get("STROKE")),
            safe_int(r.get("CHLTR")),
            safe_int(r.get("HRT")),
            safe_int(r.get("KIDNEY")),
            safe_int(r.get("DMRS")),
            safe_int(r.get("HPTRS")),
            safe_int(r.get("CHLTRRS")),
            safe_int(r.get("HRTRS")),
            safe_int(r.get("KIDNEYRS")),
            safe_int(r.get("STROKERS")),
            safe_int(r.get("PARENT")),
            to_bool(r.get("PDM")),
            to_bool(r.get("PKIDNEY")),
            to_bool(r.get("PSTROKE")),
            to_bool(r.get("PHPT")),
            to_bool(r.get("PHRTM")),
            to_bool(r.get("PGOUT")),
            to_bool(r.get("PEPM")),
            safe_int(r.get("EXCERCISE")),
            to_bool(r.get("FDSW")),
            to_bool(r.get("FDSLT")),
            to_bool(r.get("FDFAT")),
            safe_int(r.get("FOOD")),
            safe_int(r.get("WATER")),
            safe_int(r.get("NOODLE")),
            safe_int(r.get("ALGYFOOD")),
            safe_int(r.get("ALGYMED")),
            safe_int(r.get("COVID")),
            safe_int(r.get("VCCCOVID")),
            safe_int(r.get("VCCINFLUZA")),
            safe_int(r.get("CHKHIV")),
            safe_int(r.get("CANCELST")),
        ))

    if not rows:
        print(f"  No valid rows (skipped {skipped}).")
        return

    sql = """
        INSERT INTO raw_homehealth (
            patient_id, visit_date, facility_code,
            has_chronic,
            history_dm, history_hpt, history_stroke,
            history_dyslipidemia, history_heart, history_kidney,
            dm_treatment, hpt_treatment, dyslipidemia_treatment,
            heart_treatment, kidney_treatment, stroke_treatment,
            parent_history,
            parent_dm, parent_kidney, parent_stroke, parent_hpt,
            parent_heart_attack, parent_gout, parent_emphysema,
            exercise,
            food_preference_sweet, food_preference_salty, food_preference_fatty,
            food_fried_freq, drink_sugar_freq, instant_noodle_freq,
            allergy_food, allergy_medicine,
            covid_history, vaccine_covid, vaccine_influenza,
            want_hiv_test,
            cancel_status
        ) VALUES %s
        ON CONFLICT DO NOTHING
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"  Inserted {len(rows)} homehealth records (skipped {skipped}).")


def import_lab_results(cur, df, patient_map: Dict[str, int]):
    """Import labhealth.csv -> raw_lab_results."""
    print("[6/7] Importing labhealth.csv -> raw_lab_results ...")
    rows = []
    skipped = 0
    for r in df.to_dict(orient='records'):
        pid = _ensure_patient(r.get("PID"), patient_map, cur)
        if pid is None:
            skipped += 1
            continue

        rows.append((
            pid,
            parse_date_only(r.get("VSTDATE")),
            safe_str(r.get("HPTCODE")),
            safe_int(r.get("PRVLG")),
            safe_int(r.get("CBCRS")),
            safe_int(r.get("WBC"), 0, 999999),
            safe_int(r.get("RBC"), 0, 999999),
            safe_float(r.get("HMGB"), 0, 30),         # hemoglobin g/dL
            safe_float(r.get("HMTC"), 0, 80),          # hematocrit %
            safe_float(r.get("MCV"), 0, 200),           # MCV fL
            safe_int(r.get("PITCNT"), 0, 9999999),
            safe_int(r.get("BLDSGTYPE")),
            safe_int(r.get("BLDSGRS")),
            safe_float(r.get("DTX"), 0, 999),
            safe_float(r.get("BLDSUGAR"), 0, 999),
            safe_float(r.get("FBS"), 0, 999),           # fasting blood sugar mg/dL
            safe_int(r.get("UARS")),
            safe_str(r.get("UAWBC")),
            safe_str(r.get("UARBC")),
            safe_str(r.get("PROTEIN")),
            safe_int(r.get("CHLTRTYPE")),
            safe_int(r.get("CHLTRRS")),
            safe_float(r.get("CHOLEST"), 0, 999),       # cholesterol mg/dL
            safe_float(r.get("TRIGLY"), 0, 999),
            safe_float(r.get("HDL"), 0, 500),
            safe_float(r.get("LDL"), 0, 500),
            safe_int(r.get("LIVERRS")),
            safe_float(r.get("SGOT"), 0, 999),
            safe_float(r.get("SGPT"), 0, 999),
            safe_float(r.get("ALKPPT"), 0, 999),        # alk phosphatase
            safe_int(r.get("URICRS")),
            safe_float(r.get("URICACID"), 0, 50),       # uric acid mg/dL
            safe_int(r.get("CVCRS")),
            safe_str(r.get("HPV")),
            safe_int(r.get("CLCRS")),
            safe_str(r.get("FITTEST")),
            safe_float(r.get("CRTININE"), 0, 50),       # creatinine mg/dL
            safe_float(r.get("EGFRRS"), 0, 200),        # eGFR mL/min
            safe_float(r.get("BUNRS"), 0, 200),          # BUN mg/dL
            safe_int(r.get("CANCELST")),
        ))

    if not rows:
        print(f"  No valid rows (skipped {skipped}).")
        return

    sql = """
        INSERT INTO raw_lab_results (
            patient_id, visit_date, facility_code, privilege,
            cbc_result, wbc, rbc, hemoglobin, hematocrit, mcv, platelet,
            blood_sugar_type, blood_sugar_result, dtx, blood_sugar, fbs,
            urine_result, urine_wbc, urine_rbc, urine_protein,
            cholesterol_type, cholesterol_result, cholesterol, triglyceride, hdl, ldl,
            liver_result, sgot, sgpt, alk_phosphatase,
            uric_acid_result, uric_acid,
            cervical_cancer_result, hpv,
            colorectal_result, fit_test,
            creatinine, egfr, bun,
            cancel_status
        ) VALUES %s
        ON CONFLICT DO NOTHING
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"  Inserted {len(rows)} lab result records (skipped {skipped}).")


def import_lab_extended(cur, df, patient_map: Dict[str, int]):
    """Import labhealthext.csv -> raw_lab_extended."""
    print("[7/7] Importing labhealthext.csv -> raw_lab_extended ...")
    rows = []
    skipped = 0
    for r in df.to_dict(orient='records'):
        pid = _ensure_patient(r.get("PID"), patient_map, cur)
        if pid is None:
            skipped += 1
            continue

        rows.append((
            pid,
            parse_date_only(r.get("VSTDATE")),
            safe_str(r.get("HPTCODE")),
            safe_int(r.get("SCRRES01")),
            safe_int(r.get("SCRRES02")),
            safe_int(r.get("SCRRES03")),
            safe_int(r.get("SCRRES04")),
            safe_int(r.get("FGRUB01")),
            safe_int(r.get("PTGRIGHT")),
            safe_int(r.get("PTGLEFT")),
            to_bool(r.get("HEAD")),
            to_bool(r.get("NECK")),
            to_bool(r.get("SHLDR")),
            to_bool(r.get("UPBH")),
            to_bool(r.get("ELBOW")),
            to_bool(r.get("LWBH")),
            to_bool(r.get("WRIST")),
            to_bool(r.get("HIP")),
            to_bool(r.get("KNEE")),
            to_bool(r.get("ANKLE")),
            to_bool(r.get("SYMP01")),
            to_bool(r.get("SYMP02")),
            to_bool(r.get("SYMP03")),
            to_bool(r.get("SYMP04")),
            safe_int(r.get("CANCELST")),
        ))

    if not rows:
        print(f"  No valid rows (skipped {skipped}).")
        return

    sql = """
        INSERT INTO raw_lab_extended (
            patient_id, visit_date, facility_code,
            respiratory_cough, dyspnea, chest_tight, breathing,
            hearing_test, pterygium_right, pterygium_left,
            pain_head, pain_neck, pain_shoulder, pain_upper_back,
            pain_elbow, pain_lower_back, pain_wrist, pain_hip,
            pain_knee, pain_ankle,
            symptom_neck_radiating, symptom_hand_numbness,
            symptom_back_radiating, symptom_heel_pain,
            cancel_status
        ) VALUES %s
        ON CONFLICT DO NOTHING
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"  Inserted {len(rows)} lab extended records (skipped {skipped}).")


# ---------------------------------------------------------------------------
# Materialized view refresh
# ---------------------------------------------------------------------------

def backfill_district_codes(cur):
    """Fill missing district_code in raw_vitalsigns from multiple sources."""
    total = 0

    # 1. From facility_code → district mapping (ref_facility_districts)
    cur.execute("""
        SELECT EXISTS(SELECT 1 FROM information_schema.tables
                      WHERE table_name = 'ref_facility_districts')
    """)
    if cur.fetchone()[0]:
        cur.execute("""
            UPDATE raw_vitalsigns v
            SET district_code = fd.district_code
            FROM ref_facility_districts fd
            WHERE v.facility_code = fd.facility_code
              AND v.district_code IS NULL
        """)
        filled_fc = cur.rowcount
        if filled_fc:
            print(f"  Backfilled {filled_fc} district_code from facility mapping")
            total += filled_fc

    # 2. From homevisit: patient's home_district (4-digit BKK district code)
    cur.execute("""
        UPDATE raw_vitalsigns v
        SET district_code = hv.home_district::text
        FROM (
            SELECT DISTINCT ON (patient_id) patient_id, home_district
            FROM raw_homevisit
            WHERE home_district IS NOT NULL AND home_district >= 1001 AND home_district <= 1050
            ORDER BY patient_id, visit_date DESC
        ) hv
        WHERE v.patient_id = hv.patient_id
          AND v.district_code IS NULL
    """)
    filled_hv = cur.rowcount
    if filled_hv:
        print(f"  Backfilled {filled_hv} district_code from homevisit")
        total += filled_hv

    # Report remaining
    cur.execute("SELECT count(*) FROM raw_vitalsigns WHERE district_code IS NULL")
    still_null = cur.fetchone()[0]
    if still_null:
        print(f"  Warning: {still_null} vitalsign records still have no district_code")
    else:
        print(f"  All vitalsign records have district_code")
    return total


def refresh_all_summaries(cur):
    """Refresh all materialized views if they exist."""
    backfill_district_codes(cur)
    print("\nRefreshing materialized views ...")
    cur.execute("""
        SELECT matviewname FROM pg_matviews
        WHERE schemaname = 'public'
        ORDER BY matviewname
    """)
    views = [row[0] for row in cur.fetchall()]
    if not views:
        print("  No materialized views found. Skipping.")
        return
    for v in views:
        print(f"  Refreshing {v} ...")
        cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {v}")
    print(f"  Refreshed {len(views)} materialized views.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def read_csv(data_dir: str, filename: str) -> pd.DataFrame:
    path = os.path.join(data_dir, filename)
    if not os.path.isfile(path):
        print(f"  WARNING: {path} not found, skipping.")
        return pd.DataFrame()
    df = pd.read_csv(path, dtype=str, keep_default_na=True)
    print(f"  Read {len(df)} rows from {filename}")
    return df


def main():
    parser = argparse.ArgumentParser(description="BMA Health ETL: import CSV -> PostgreSQL")
    parser.add_argument("--data-dir", default=None, help="Path to CSV directory")
    parser.add_argument("--db-url", default=None, help="PostgreSQL connection URL")
    args = parser.parse_args()

    # Resolve config (CLI args override env/defaults)
    from config import DATABASE_URL, DATA_DIR, CURRENT_YEAR

    db_url = args.db_url or DATABASE_URL
    data_dir = args.data_dir or DATA_DIR

    print(f"Data directory : {os.path.abspath(data_dir)}")
    print(f"Database URL   : {db_url.split('@')[0].rsplit(':', 1)[0]}:***@{db_url.split('@')[-1]}")
    print(f"Current year   : {CURRENT_YEAR}")
    print()

    if _HASH_SECRET == b"dev-hash-secret-change-in-production":
        print("\u26a0\ufe0f  WARNING: IDCARD_HASH_SECRET is using the default value.")
        print("   Set IDCARD_HASH_SECRET environment variable in production.")
        print()

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    try:
        cur = conn.cursor()

        # 1. Patients
        df_pt = read_csv(data_dir, "pt.csv")
        patient_map = {}
        if not df_pt.empty:
            patient_map = import_patients(cur, df_pt, CURRENT_YEAR)
            conn.commit()
        print(f"  Patient map size: {len(patient_map)}\n")

        # 2. Visits
        df_visit = read_csv(data_dir, "pthistory.csv")
        if not df_visit.empty:
            _batch_ensure_patients(df_visit, patient_map, cur)
            import_visits(cur, df_visit, patient_map)
            conn.commit()
        print()

        # 3. Vitalsigns
        df_vs = read_csv(data_dir, "vitalsignslf.csv")
        if not df_vs.empty:
            _batch_ensure_patients(df_vs, patient_map, cur)
            import_vitalsigns(cur, df_vs, patient_map)
            conn.commit()
        print()

        # 4. Home visit
        df_hv = read_csv(data_dir, "homevisit.csv")
        if not df_hv.empty:
            _batch_ensure_patients(df_hv, patient_map, cur)
            import_homevisit(cur, df_hv, patient_map)
            conn.commit()
        print()

        # 5. Home health
        df_hh = read_csv(data_dir, "homehealth.csv")
        if not df_hh.empty:
            _batch_ensure_patients(df_hh, patient_map, cur)
            import_homehealth(cur, df_hh, patient_map)
            conn.commit()
        print()

        # 6. Lab results
        df_lab = read_csv(data_dir, "labhealth.csv")
        if not df_lab.empty:
            _batch_ensure_patients(df_lab, patient_map, cur)
            import_lab_results(cur, df_lab, patient_map)
            conn.commit()
        print()

        # 7. Lab extended
        df_ext = read_csv(data_dir, "labhealthext.csv")
        if not df_ext.empty:
            _batch_ensure_patients(df_ext, patient_map, cur)
            import_lab_extended(cur, df_ext, patient_map)
            conn.commit()
        print()

        # Refresh materialized views
        refresh_all_summaries(cur)
        conn.commit()

        print("\nETL complete.")

    except Exception as e:
        conn.rollback()
        print(f"\nERROR: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
