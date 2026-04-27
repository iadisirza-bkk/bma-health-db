#!/usr/bin/env python3
"""
Bootstrap private.variable_definition from /Users/dev/bma-med/all_var.xlsx.

Loads the 675-row Excel sheet (Source / File / ตัวแปร / คำอธิบาย / ค่าที่เป็นไปได้
/ Sub-domain / Domain) and INSERTs into private.variable_definition.

Variable type inference:
- 'POINTS', 'SCORE', '0-X' patterns           → number
- 'TRUE/FALSE', 'Y/N', 'YES/NO'                → boolean
- 'DATE', 'TIMESTAMP'                          → date
- '1=…, 2=…' pattern                           → code (and parses values)
- numeric ranges 'XX–YY mg/dL'                  → number
- Default                                       → text

Domain inference:
- maps Excel "Domain (file group)" → canonical domain code
  - 'Patient Registration'        → 'identity'
  - 'Vital Signs & Screening'      → 'vital'
  - 'Laboratory Results'           → 'lab'
  - 'Home Visit / Social'          → 'address'
  - 'Home Health Survey'           → 'lifestyle'
  - 'Physical Exam Extended'       → 'symptom'
  - 'Patient Profile'              → 'identity'
  - 'Calculated/Dashboard (App2)'  → 'derived'

variable_key (canonical) is derived from csv_column_name with these rules:
- lowercased, replace '_OTH' with '_other'
- strip 'OBJ_' prefixes if present
- standardize common renames (HBPN→sbp, LBPN→dbp, ...)
- domain prefix when ambiguous

Usage:
  python -m etl.bootstrap_variable_definitions [--db-url URL] \\
                                                [--xlsx /path/to/all_var.xlsx]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional, Tuple

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values


# ----- Domain mapping (Excel sheet → canonical) -----------------------------

DOMAIN_MAP = {
    'ข้อมูลทะเบียนผู้ป่วย (Patient Registration)':       'identity',
    'ตรวจร่างกายภายนอก/อาการ (Physical Exam Extended)':   'symptom',
    'ตัวแปรคำนวณ/Dashboard (App2)':                       'derived',
    'บริบทที่อยู่/สังคม (Home Visit / Social Determinants)': 'address',
    'ประวัติติดต่อ/อัตลักษณ์ (Patient Profile)':           'identity',
    'ผลตรวจห้องปฏิบัติการ (Laboratory Results)':           'lab',
    'พฤติกรรมและโรคประจำตัว (Home Health Survey)':         'lifestyle',
    'สัญญาณชีพและการคัดกรอง (Vital Signs & Screening)':    'vital',
}


# ----- File mapping (Excel sheet → canonical csv_file value) -----------------

FILE_MAP = {
    'pt':            'pt',
    'pt.csv':        'pt',
    'pthistory':     'pthistory',
    'vital':         'vital',
    'vitalsignslf':  'vital',
    'hv':            'hv',
    'homevisit':     'hv',
    'hh':            'hh',
    'homehealth':    'hh',
    'lab':           'lab',
    'labhealth':     'lab',
    'labext':        'labext',
    'labhealthext':  'labext',
    'app2':          'app2',
    'app2.csv':      'app2',
}


# ----- Source code mapping ---------------------------------------------------

SOURCE_MAP = {'Portal': 'portal', 'App1': 'app1', 'App2': 'app2'}


# ----- Canonical variable_key rewrites ---------------------------------------
# (csv_column_name → canonical variable_key)
# These are domain-knowledge mappings to keep keys human-readable & consistent.

CANONICAL_RENAMES = {
    # Identity
    'IDCARD':    'idcard',
    'PID':       'source_pid',
    'MALE':      'sex',
    'BIRTHDATE': 'birthdate',
    'BRTHDATE':  'birthdate',
    'AGE':       'age',
    'PNAME':     'name_prefix',

    # Vital signs
    'HBPN':      'sbp',
    'LBPN':      'dbp',
    'HEIGHT':    'height_cm',
    'WEIGHT':    'weight_kg',
    'WAIST':     'waist_cm',
    'PULSE':     'pulse_rate',
    'BMI':       'bmi',
    'FAT':       'body_fat_pct',

    # Glucose
    'PREFPG':    'fasting_glucose',
    'POSTFPG':   'post_glucose',
    'DTX':       'dtx',
    'BLDSGR':    'blood_sugar',
    'HBA1C':     'hba1c',

    # Disease flags (CAPS in CSV → snake_case)
    'RISKDM':    'risk_dm',
    'RISKHPT':   'risk_hpt',
    'RISKCDVCL': 'risk_cvd',
    'RISKBMI':   'risk_bmi',
    'DM':        'found_dm',
    'HPT':       'found_hpt',
    'CDVCL':     'found_cvd',
    'STROKE':    'found_stroke',
    'CHLTR':     'found_dyslipidemia',

    # Mental health
    'SCR2Q1':    'depression_2q_1',
    'SCR2Q2':    'depression_2q_2',
    'SCN9Q1':    'phq9_q1',
    'SCN9Q2':    'phq9_q2',
    'SCN9Q3':    'phq9_q3',
    'SCN9Q4':    'phq9_q4',
    'SCN9Q5':    'phq9_q5',
    'SCN9Q6':    'phq9_q6',
    'SCN9Q7':    'phq9_q7',
    'SCN9Q8':    'phq9_q8',
    'SCN9Q9':    'phq9_q9',
    'ST501':     'st5_q1',
    'ST502':     'st5_q2',
    'ST503':     'st5_q3',
    'ST504':     'st5_q4',
    'ST505':     'st5_q5',

    # Address
    'HDISTRICT': 'home_district',
    'HPROVINCE': 'home_province',
    'HSUBDISTRICT': 'home_subdistrict',
    'DISTRICT':  'home_district',     # App1/App2 use plain DISTRICT
    'WRKDISTRICT': 'work_district',
    'WRKPROVINCE': 'work_province',
    'CRDISTRICT':  'current_district',
    'CRPROVINCE':  'current_province',

    # Lifestyle
    'SMOKE':     'smoking',
    'ALCOHAL':   'alcohol',
    'EXCERCISE': 'exercise',

    # Lab
    'CHOLEST':   'total_cholesterol',
    'TRIGLY':    'triglyceride',
    'HDL':       'hdl',
    'LDL':       'ldl',
    'CRTN':      'creatinine',
    'EGFR':      'egfr',
    'HGB':       'hemoglobin',
    'HCT':       'hematocrit',
    'WBC':       'wbc',
    'RBC':       'rbc',
    'PLT':       'platelet',
    'SGOT':      'sgot',
    'SGPT':      'sgpt',
    'URICACID':  'uric_acid',
    'FBS':       'fbs',
}


# ----- Type inference --------------------------------------------------------

NUMBER_HINTS = re.compile(r'\b(score|points?|mg/dL|mmHg|cm|kg|%|ปี|ครั้ง)\b', re.I)
BOOLEAN_HINTS = re.compile(r'\b(0|1)\s*[/=,]\s*(0|1)\b|TRUE/FALSE|Y/N|YES/NO', re.I)
CODE_HINTS = re.compile(r'\d\s*=\s*\S')
DATE_HINTS = re.compile(r'\bDATE|TIMESTAMP|วันที่\b', re.I)


def infer_data_type(possible_values: Optional[str], description: str) -> str:
    pv = (possible_values or '').strip()
    desc = (description or '').strip()

    if not pv and not desc:
        return 'text'

    haystack = (pv + ' ' + desc).strip()

    if DATE_HINTS.search(haystack):
        return 'date'
    if BOOLEAN_HINTS.search(pv):
        return 'boolean'
    # Code (categorical) — must come before number to catch "1=ชาย, 2=หญิง"
    if CODE_HINTS.search(pv) and len(re.findall(r'\d\s*=', pv)) >= 2:
        return 'code'
    if NUMBER_HINTS.search(haystack):
        return 'number'
    # Numeric ranges like "0-100" or "10–999"
    if re.search(r'\d+\s*[-–]\s*\d+', pv):
        return 'number'
    return 'text'


def infer_unit(possible_values: Optional[str], desc: str) -> Optional[str]:
    haystack = (possible_values or '') + ' ' + (desc or '')
    for pat in (r'mg/dL', r'mmHg', r'cm', r'kg', r'%', r'mEq/L', r'IU/L', r'g/dL', r'mL'):
        m = re.search(rf'\b({pat})\b', haystack)
        if m:
            return m.group(1)
    return None


def parse_code_values(possible_values: str) -> list[Tuple[str, str]]:
    """Parse '1=ชาย, 2=หญิง' style → [('1','ชาย'), ('2','หญิง')]."""
    if not possible_values:
        return []
    pairs = re.findall(r'(\d+)\s*=\s*([^,;0-9][^,;]*)', possible_values)
    return [(c.strip(), label.strip().rstrip('.')) for c, label in pairs]


def parse_numeric_range(possible_values: str) -> Tuple[Optional[float], Optional[float]]:
    if not possible_values:
        return None, None
    m = re.search(r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)', possible_values)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def to_variable_key(csv_col: str, source: str, sub_domain: Optional[str]) -> str:
    """Generate canonical variable_key from CSV column name."""
    upper = csv_col.upper().strip()
    if upper in CANONICAL_RENAMES:
        return CANONICAL_RENAMES[upper]
    # Default: lowercase + standardize OTH→other, replace special chars
    key = upper.lower()
    key = key.replace('_oth', '_other')
    key = re.sub(r'[^a-z0-9_]+', '_', key).strip('_')
    return key


def infer_tier(domain: str, csv_col: str, sub_domain: Optional[str]) -> int:
    """Tier 1 = hero (always shown), 2 = std, 3 = drilldown, 4 = audit."""
    key = csv_col.upper()
    sd = (sub_domain or '').lower()
    # Audit columns
    if 'audit' in sd or 'ระบบ' in sd or key in ('CREATED_AT', 'UPDATED_AT'):
        return 4
    # Tier 1: identity + 6 NCD core
    if key in ('IDCARD', 'PID', 'VSTDATE', 'HPTCODE', 'DISTRICT', 'HDISTRICT'):
        return 1
    if key.startswith(('RISK', 'FOUND', 'DM', 'HPT', 'CDVCL', 'STROKE', 'CHLTR', 'BMI')):
        return 2
    # Tier 2: lab core, mental health core
    if domain == 'lab' and key in ('FBS', 'HBA1C', 'CHOLEST', 'TRIGLY', 'HDL', 'LDL',
                                    'CRTN', 'EGFR', 'HGB'):
        return 2
    if domain == 'mental' or 'จิต' in sd:
        return 2
    return 3


# ----- Main loader -----------------------------------------------------------

def load(db_url: str, xlsx_path: str, dry_run: bool = False) -> int:
    print(f'Reading {xlsx_path} ...')
    df = pd.read_excel(xlsx_path, sheet_name='All_Variables')
    print(f'  {len(df)} rows')

    rows_dict = {}  # key = (source_code, csv_col) → row tuple (dedupe)
    code_values_to_insert = []  # [(csv_col, source, code, label)]

    for _, r in df.iterrows():
        source_excel = str(r.get('Source', '') or '').strip()
        source_code  = SOURCE_MAP.get(source_excel)
        if not source_code:
            continue

        csv_col   = str(r.get('ตัวแปร', '') or '').strip()
        if not csv_col:
            continue

        csv_file_excel = str(r.get('File', '') or '').strip().lower()
        csv_file       = FILE_MAP.get(csv_file_excel, csv_file_excel or 'unknown')

        domain_excel = str(r.get('Domain (file group)', '') or '').strip()
        domain       = DOMAIN_MAP.get(domain_excel, 'other')

        sub_domain   = str(r.get('Sub-domain', '') or '').strip() or None
        description  = str(r.get('คำอธิบาย', '') or '').strip()
        possible_v   = str(r.get('ค่าที่เป็นไปได้ (Possible Values)', '') or '').strip() or None

        data_type = infer_data_type(possible_v, description)
        unit      = infer_unit(possible_v, description)
        valid_min, valid_max = parse_numeric_range(possible_v) if data_type == 'number' else (None, None)
        tier      = infer_tier(domain, csv_col, sub_domain)
        var_key   = to_variable_key(csv_col, source_code, sub_domain)

        key = (source_code, csv_col)
        # Dedupe: same csv_col can appear in multiple files (e.g. PID in pt+vital+hv).
        # Keep first occurrence (preserves the primary file association).
        if key in rows_dict:
            continue

        rows_dict[key] = (
            var_key,
            csv_col,
            source_code,
            csv_file,
            domain,
            sub_domain,
            data_type,
            unit,
            description or None,
            None,                       # description_en (not in source)
            possible_v,
            tier,
            valid_min,
            valid_max,
            False,                      # is_pii
            False,                      # is_required
        )

        # If type=code, parse and stage code values
        if data_type == 'code':
            for code, label in parse_code_values(possible_v or ''):
                code_values_to_insert.append((csv_col, source_code, code, label))

    rows = list(rows_dict.values())
    print(f'  Prepared {len(rows)} variable definitions (deduped from {len(df)} rows)')
    print(f'  Prepared {len(code_values_to_insert)} code values')

    if dry_run:
        print('Dry-run: showing first 5 rows')
        for r in rows[:5]:
            print(' ', r)
        return 0

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute('TRUNCATE private.variable_code_value, private.variable_definition CASCADE')

    execute_values(cur, """
        INSERT INTO private.variable_definition (
          variable_key, csv_column_name, source_code, csv_file,
          domain, sub_domain, data_type, unit,
          description_th, description_en, possible_values,
          tier, valid_min, valid_max, is_pii, is_required
        ) VALUES %s
        ON CONFLICT (source_code, csv_column_name) DO UPDATE
          SET variable_key = EXCLUDED.variable_key,
              data_type = EXCLUDED.data_type,
              unit = EXCLUDED.unit,
              description_th = EXCLUDED.description_th,
              tier = EXCLUDED.tier
    """, rows, page_size=200)

    inserted_vars = cur.rowcount
    print(f'  Inserted/updated variable_definition rows: {inserted_vars}')

    # Now insert code values — need to look up variable_id
    if code_values_to_insert:
        cur.execute("""
            CREATE TEMP TABLE _stage_codes (
              csv_col TEXT, source_code TEXT, code TEXT, label_th TEXT
            ) ON COMMIT DROP
        """)
        execute_values(cur,
            'INSERT INTO _stage_codes VALUES %s', code_values_to_insert)
        cur.execute("""
            INSERT INTO private.variable_code_value (variable_id, code, label_th)
            SELECT vd.id, s.code, s.label_th
            FROM _stage_codes s
            JOIN private.variable_definition vd
              ON vd.csv_column_name = s.csv_col
             AND vd.source_code     = s.source_code
            ON CONFLICT (variable_id, code) DO UPDATE
              SET label_th = EXCLUDED.label_th
        """)
        print(f'  Inserted code values: {cur.rowcount}')

    conn.commit()
    cur.close()
    conn.close()
    print('Done.')
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--db-url',
                   default=os.environ.get('DATABASE_URL',
                                           'postgresql://postgres:bma_health_dev@localhost:5433/bma_health'))
    p.add_argument('--xlsx', default='/Users/dev/bma-med/all_var.xlsx')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    return load(args.db_url, args.xlsx, args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
