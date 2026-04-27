#!/usr/bin/env python3
"""
ETL v3 — writes to private.* (EAV-based) schema.

Entry points (called from api/admin.py):
  import_patients(cur, df, source_code)
    → upserts private.patient + patient_alias
  import_visit_data(cur, df, source_code, file_type)
    → for vital/hv/hh/lab/labext: upserts visit_event + visit_measurement
  import_app2(cur, df)
    → splits the combined App2 row into vital + hv + hh + lab measurements

The variable_definition catalog drives column→variable_id resolution. Every
non-null CSV column → 1 row in visit_measurement (or lab_measurement).

Key design:
  - We don't ALTER tables when a new variable arrives; just INSERT
    private.variable_definition. The ETL automatically picks it up.
  - Patient identity is deduplicated across sources via SHA-256(IDCARD).
  - Address fields (HDISTRICT/DISTRICT/WRKDISTRICT) → SCD Type 2
    private.patient_address (NOT visit_measurement).
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, date
from typing import Dict, Optional, Tuple

import pandas as pd
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)

# Address columns extracted to patient_address instead of visit_measurement
ADDRESS_COLUMNS = {
    'HDISTRICT', 'DISTRICT', 'WRKDISTRICT', 'CRDISTRICT',
    'HPROVINCE', 'WRKPROVINCE', 'CRPROVINCE',
    'HSUBDISTRICT', 'WRKSUBDISTRICT', 'CRSUBDISTRICT',
}

# These columns identify the visit, not measurements
VISIT_META_COLUMNS = {'PID', 'IDCARD', 'VSTDATE', 'VSTTIME', 'HPTCODE', 'CANCELST'}


# =============================================================================
# Helpers
# =============================================================================

def _hash_idcard(value: str) -> str:
    """SHA-256 of citizen ID for privacy."""
    if value is None:
        return ''
    s = str(value).strip()
    if not s or s.lower() in ('nan', 'none', ''):
        return ''
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def _parse_date(value) -> Optional[date]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.lower() in ('nan', 'none'):
        return None
    for fmt in (
        '%Y-%m-%d',
        '%Y-%m-%d %H:%M:%S',
        '%d/%m/%Y',
        '%d/%m/%Y %H:%M:%S',     # 19/01/2024 13:43:47 — Portal/App1 CSV format
        '%d/%m/%Y %H:%M',
        '%d-%b-%Y',
        '%Y/%m/%d',
        '%Y/%m/%d %H:%M:%S',
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_int(value) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def _parse_float(value) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _is_blank(value) -> bool:
    if value is None or pd.isna(value):
        return True
    s = str(value).strip()
    return not s or s.lower() in ('nan', 'none', '')


def _load_variable_map(cur, source_code: str) -> Dict[str, dict]:
    """Return {csv_column_name: {id, data_type, valid_min, valid_max}} for source."""
    cur.execute("""
        SELECT csv_column_name, id, data_type, valid_min, valid_max
        FROM private.variable_definition
        WHERE source_code = %s AND deprecated_at IS NULL
    """, (source_code,))
    return {
        row[0]: {'id': row[1], 'data_type': row[2],
                 'valid_min': row[3], 'valid_max': row[4]}
        for row in cur.fetchall()
    }


# =============================================================================
# 1. Import patients (pt.csv)
# =============================================================================

def import_patients(cur, df: pd.DataFrame, source_code: str,
                    import_batch_id: Optional[int] = None) -> Dict[str, int]:
    """
    Upsert into private.patient + private.patient_alias.

    Returns: {idcard_hash: patient_id} mapping for downstream use.
    """
    # Detect IDCARD column (Portal uses IDCARD, App1/App2 use PID)
    idcard_col = 'IDCARD' if 'IDCARD' in df.columns else 'PID'
    if idcard_col not in df.columns:
        raise ValueError(f"No IDCARD/PID column in {source_code} pt.csv")

    rows = []
    for _, r in df.iterrows():
        if _is_blank(r.get(idcard_col)):
            continue
        h = _hash_idcard(r.get(idcard_col))
        if not h:
            continue
        sex = str(r.get('MALE', '')).strip() or None
        if sex and sex not in ('1', '2'):
            sex = None

        # Birth year — Portal: BIRTHDATE, App1: BRTHDATE/AGE
        bdate = _parse_date(r.get('BIRTHDATE') or r.get('BRTHDATE'))
        byear = bdate.year if bdate else None
        bmonth = bdate.month if bdate else None
        if byear is None and 'AGE' in df.columns:
            age = _parse_int(r.get('AGE'))
            if age and 0 < age < 120:
                byear = datetime.now().year - age

        rows.append((h, sex, byear, bmonth, source_code))

    if not rows:
        logger.warning(f"No valid patient rows in {source_code} pt")
        return {}

    # Upsert patient (UNIQUE on idcard_hash)
    execute_values(cur, """
        INSERT INTO private.patient
          (idcard_hash, sex_code, birth_year, birth_month, primary_source)
        VALUES %s
        ON CONFLICT (idcard_hash) DO UPDATE
          SET sex_code     = COALESCE(EXCLUDED.sex_code, private.patient.sex_code),
              birth_year   = COALESCE(EXCLUDED.birth_year, private.patient.birth_year),
              birth_month  = COALESCE(EXCLUDED.birth_month, private.patient.birth_month),
              last_seen_at = NOW(),
              updated_at   = NOW()
    """, rows, page_size=1000)

    # Get patient_id for each idcard_hash
    cur.execute("SELECT idcard_hash, id FROM private.patient WHERE idcard_hash = ANY(%s)",
                ([r[0] for r in rows],))
    pid_map = {h: pid for h, pid in cur.fetchall()}

    # Insert patient_alias rows for source_pid mapping
    alias_rows = []
    for _, r in df.iterrows():
        h = _hash_idcard(r.get(idcard_col))
        if not h or h not in pid_map:
            continue
        source_pid = str(r.get(idcard_col)).strip()
        alias_rows.append((pid_map[h], source_code, source_pid))

    if alias_rows:
        execute_values(cur, """
            INSERT INTO private.patient_alias (patient_id, source_code, source_pid)
            VALUES %s
            ON CONFLICT (patient_id, source_code) DO NOTHING
        """, alias_rows, page_size=1000)

    logger.info(f"  patients: {len(pid_map)} unique, {len(alias_rows)} aliases")
    return pid_map


# =============================================================================
# 2. Import visit + measurements
# =============================================================================

def import_visits_and_measurements(cur, df: pd.DataFrame, source_code: str,
                                     file_type: str, patient_map: Dict[str, int],
                                     import_batch_id: Optional[int] = None) -> int:
    """
    For vital/hv/hh/labext (or any file with PID + VSTDATE):
    1. Create visit_event (one per pid + visit_date + source)
    2. Create visit_measurement (one per non-null measurement column)
    3. For address columns → patient_address (SCD Type 2)

    Returns: count of visits created.
    """
    var_map = _load_variable_map(cur, source_code)
    if not var_map:
        logger.warning(f"No variables defined for {source_code}; skipping")
        return 0

    # Cache for facility-code FK validation (avoid N queries)
    _facility_cache: Dict[str, bool] = {}

    # Identify PID + date columns
    pid_col = 'PID' if 'PID' in df.columns else 'IDCARD'
    if pid_col not in df.columns:
        raise ValueError(f"No PID/IDCARD column in {source_code} {file_type}")
    if 'VSTDATE' not in df.columns:
        # Some files don't have VSTDATE (e.g. raw pt.csv) — skip
        logger.warning(f"No VSTDATE in {source_code} {file_type}; skipping visit creation")
        return 0

    # PHASE 1: Auto-create missing patients
    # Some children files reference PIDs that aren't in pt.csv (data quality
    # issue or test slices). Insert minimal placeholder patient rows so we
    # don't lose visits. They get sex/birth_year=NULL until backfilled.
    missing_hashes: list[tuple[str, str]] = []  # [(idcard_hash, source_pid)]
    seen = set()
    for _, r in df.iterrows():
        raw = r.get(pid_col)
        if _is_blank(raw):
            continue
        h = _hash_idcard(raw)
        if h and h not in patient_map and h not in seen:
            missing_hashes.append((h, str(raw).strip()))
            seen.add(h)
    if missing_hashes:
        execute_values(cur, """
            INSERT INTO private.patient (idcard_hash, primary_source)
            VALUES %s
            ON CONFLICT (idcard_hash) DO UPDATE SET last_seen_at = NOW()
        """, [(h, source_code) for h, _ in missing_hashes], page_size=1000)
        cur.execute("SELECT idcard_hash, id FROM private.patient WHERE idcard_hash = ANY(%s)",
                    ([h for h, _ in missing_hashes],))
        for h, pid in cur.fetchall():
            patient_map[h] = pid
        # Add aliases for new patients
        execute_values(cur, """
            INSERT INTO private.patient_alias (patient_id, source_code, source_pid)
            VALUES %s
            ON CONFLICT (patient_id, source_code) DO NOTHING
        """, [(patient_map[h], source_code, pid_str)
              for h, pid_str in missing_hashes if h in patient_map],
            page_size=1000)
        logger.info(f"  auto-created {len(missing_hashes)} placeholder patients for {source_code}/{file_type}")

    # 2a. Build visit_event rows
    visit_rows = []
    visit_meta_lookup = []  # (pid_hash, visit_date, df_index)
    skipped_no_pid = 0
    skipped_no_date = 0
    for idx, r in df.iterrows():
        h = _hash_idcard(r.get(pid_col))
        if not h or h not in patient_map:
            skipped_no_pid += 1
            continue
        v_date = _parse_date(r.get('VSTDATE'))
        if not v_date:
            skipped_no_date += 1
            continue
        cancel = _parse_int(r.get('CANCELST')) or 0
        facility = str(r.get('HPTCODE', '')).strip() or None
        if facility and facility not in _facility_cache:
            cur.execute("SELECT 1 FROM private.facility WHERE code = %s LIMIT 1", (facility,))
            _facility_cache[facility] = cur.fetchone() is not None
        if facility and not _facility_cache.get(facility):
            facility = None  # unknown facility code → NULL (FK can be null)
        source_visit_id = str(r.get('VST_ID', '')).strip() or str(r.get(pid_col, '')).strip()

        visit_rows.append((
            patient_map[h], source_code, v_date,
            None,  # visit_time (TODO: parse VSTTIME)
            facility, cancel, source_visit_id, import_batch_id
        ))
        visit_meta_lookup.append((h, v_date, idx))

    if not visit_rows:
        return 0

    # Dedupe by (patient_id, visit_date) — same patient may appear multiple
    # times on same day in CSV (multiple measurements at one visit window).
    # Keep the LAST occurrence (overwrites earlier with same key).
    deduped = {}
    for row, lookup in zip(visit_rows, visit_meta_lookup):
        key = (row[0], row[2])  # (patient_id, visit_date)
        deduped[key] = (row, lookup)
    visit_rows = [v[0] for v in deduped.values()]
    visit_meta_lookup = [v[1] for v in deduped.values()]

    # Insert visits + capture IDs
    execute_values(cur, """
        INSERT INTO private.visit_event
          (patient_id, source_code, visit_date, visit_time,
           facility_code, cancel_status, source_visit_id, import_batch_id)
        VALUES %s
        ON CONFLICT (patient_id, source_code, visit_date) WHERE cancel_status = 0
        DO UPDATE SET facility_code = EXCLUDED.facility_code,
                      import_batch_id = EXCLUDED.import_batch_id
    """, visit_rows, page_size=1000)

    # Re-fetch all visit_ids for this batch
    pids = list({patient_map[h] for h, _, _ in visit_meta_lookup})
    cur.execute("""
        SELECT patient_id, visit_date, id
        FROM private.visit_event
        WHERE source_code = %s AND patient_id = ANY(%s)
    """, (source_code, pids))
    visit_id_map = {(pid, vd): vid for pid, vd, vid in cur.fetchall()}

    # 2b. Build measurement rows + address rows
    measurement_rows = []
    address_rows = []
    for h, v_date, idx in visit_meta_lookup:
        pid = patient_map[h]
        vid = visit_id_map.get((pid, v_date))
        if not vid:
            continue
        r = df.iloc[idx]

        # Address columns → patient_address (SCD Type 2)
        home_dc = None
        home_prov = None
        work_dc = None
        for col in ('HDISTRICT', 'DISTRICT'):
            if col in df.columns and not _is_blank(r.get(col)):
                v = _parse_int(r.get(col))
                if v and v != 9999:
                    home_dc = home_dc or str(v).zfill(4)
        if 'HPROVINCE' in df.columns and not _is_blank(r.get('HPROVINCE')):
            home_prov = str(_parse_int(r.get('HPROVINCE'))).zfill(2)
        if 'WRKDISTRICT' in df.columns and not _is_blank(r.get('WRKDISTRICT')):
            v = _parse_int(r.get('WRKDISTRICT'))
            if v and v != 9999:
                work_dc = str(v).zfill(4)

        if home_dc:
            address_rows.append((pid, 'home', home_prov, home_dc, None, v_date, vid, source_code))
        if work_dc:
            address_rows.append((pid, 'work', None, work_dc, None, v_date, vid, source_code))

        # Measurement columns
        for col, val in r.items():
            col_upper = str(col).upper()
            if col_upper in VISIT_META_COLUMNS or col_upper in ADDRESS_COLUMNS:
                continue
            if _is_blank(val):
                continue
            var = var_map.get(col_upper)
            if not var:
                continue

            value_number = None
            value_text = None
            value_boolean = None
            value_date = None
            dtype = var['data_type']
            if dtype == 'number':
                value_number = _parse_float(val)
                if value_number is None:
                    value_text = str(val)[:500]
            elif dtype == 'boolean':
                v = str(val).strip().lower()
                if v in ('1', 'true', 'yes', 'y', 't'):
                    value_boolean = True
                elif v in ('0', 'false', 'no', 'n', 'f'):
                    value_boolean = False
                else:
                    value_text = str(val)[:500]
            elif dtype == 'date':
                value_date = _parse_date(val)
            elif dtype == 'code':
                value_text = str(val).strip()[:500]
            else:
                value_text = str(val)[:500]

            if (value_number is None and value_text is None
                and value_boolean is None and value_date is None):
                continue

            measurement_rows.append((
                vid, var['id'],
                value_number, value_text, value_boolean, value_date,
                False,  # is_computed
                str(val)[:500],
            ))

    # Bulk insert measurements
    if measurement_rows:
        # Dedupe by (visit_id, variable_id) — same visit can have multiple
        # rows mapping to same variable (CSV duplicate / multi-day same date).
        deduped = {}
        for row in measurement_rows:
            deduped[(row[0], row[1])] = row
        measurement_rows = list(deduped.values())

        execute_values(cur, """
            INSERT INTO private.visit_measurement
              (visit_id, variable_id,
               value_number, value_text, value_boolean, value_date,
               is_computed, source_value)
            VALUES %s
            ON CONFLICT (visit_id, variable_id) DO UPDATE
              SET value_number  = EXCLUDED.value_number,
                  value_text    = EXCLUDED.value_text,
                  value_boolean = EXCLUDED.value_boolean,
                  value_date    = EXCLUDED.value_date,
                  source_value  = EXCLUDED.source_value
        """, measurement_rows, page_size=2000)

    # Address: insert SCD Type 2 (close existing, insert new)
    if address_rows:
        for arow in address_rows:
            pid, atype = arow[0], arow[1]
            cur.execute("""
                UPDATE private.patient_address
                SET effective_to = %s
                WHERE patient_id = %s AND address_type = %s
                  AND effective_to IS NULL
                  AND district_code IS DISTINCT FROM %s
            """, (arow[5], pid, atype, arow[3]))
        execute_values(cur, """
            INSERT INTO private.patient_address
              (patient_id, address_type, province_code, district_code,
               subdistrict_code, effective_from, reported_by_visit_id, source_code)
            VALUES %s
            ON CONFLICT DO NOTHING
        """, address_rows, page_size=1000)

    logger.info(f"  visits: {len(visit_rows)} created, "
                f"measurements: {len(measurement_rows)}, "
                f"addresses: {len(address_rows)}, "
                f"skipped: pid={skipped_no_pid} date={skipped_no_date}")
    return len(visit_rows)


# =============================================================================
# 3. Import lab (lab.csv / labhealthext.csv)
# =============================================================================

def import_lab(cur, df: pd.DataFrame, source_code: str,
               patient_map: Dict[str, int],
               import_batch_id: Optional[int] = None) -> int:
    """Upsert lab_event + lab_measurement (parallel to visit_measurement)."""
    var_map = _load_variable_map(cur, source_code)
    pid_col = 'PID' if 'PID' in df.columns else 'IDCARD'
    _lab_facility_cache: Dict[str, bool] = {}

    lab_rows = []
    df_idx = []
    for idx, r in df.iterrows():
        h = _hash_idcard(r.get(pid_col))
        if not h or h not in patient_map:
            continue
        lab_date = _parse_date(r.get('VSTDATE') or r.get('LABDATE'))
        if not lab_date:
            continue
        facility = str(r.get('HPTCODE', '')).strip() or None
        if facility and facility not in _lab_facility_cache:
            cur.execute("SELECT 1 FROM private.facility WHERE code = %s LIMIT 1", (facility,))
            _lab_facility_cache[facility] = cur.fetchone() is not None
        if facility and not _lab_facility_cache.get(facility):
            facility = None
        cancel = _parse_int(r.get('CANCELST')) or 0
        privilege = str(r.get('PRVLG', '')).strip() or None
        source_lab_id = str(r.get('LAB_ID', '')).strip() or None

        lab_rows.append((
            patient_map[h], source_code, lab_date, facility,
            cancel, source_lab_id, privilege, import_batch_id
        ))
        df_idx.append((h, lab_date, idx))

    if not lab_rows:
        return 0

    cur.executemany("""
        INSERT INTO private.lab_event
          (patient_id, source_code, lab_date, facility_code,
           cancel_status, source_lab_id, privilege_code, import_batch_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, lab_rows)

    # Re-fetch lab_ids
    pids = list({patient_map[h] for h, _, _ in df_idx})
    cur.execute("""
        SELECT patient_id, lab_date, id
        FROM private.lab_event
        WHERE source_code = %s AND patient_id = ANY(%s)
    """, (source_code, pids))
    lab_id_map = {}
    for pid, ld, lid in cur.fetchall():
        lab_id_map.setdefault((pid, ld), lid)

    # Build measurement rows
    measurement_rows = []
    for h, lab_date, idx in df_idx:
        pid = patient_map[h]
        lid = lab_id_map.get((pid, lab_date))
        if not lid:
            continue
        r = df.iloc[idx]
        for col, val in r.items():
            col_upper = str(col).upper()
            if col_upper in VISIT_META_COLUMNS or col_upper == 'PRVLG':
                continue
            if _is_blank(val):
                continue
            var = var_map.get(col_upper)
            if not var:
                continue

            value_number = None
            value_text = None
            value_boolean = None
            dtype = var['data_type']
            if dtype == 'number':
                value_number = _parse_float(val)
            elif dtype == 'boolean':
                v = str(val).strip().lower()
                if v in ('1', 'true', 'yes'):
                    value_boolean = True
                elif v in ('0', 'false', 'no'):
                    value_boolean = False
            else:
                value_text = str(val)[:500]

            if value_number is None and value_text is None and value_boolean is None:
                continue

            out_of_range = None
            if (value_number is not None
                and var['valid_min'] is not None
                and var['valid_max'] is not None):
                out_of_range = (value_number < float(var['valid_min'])
                                or value_number > float(var['valid_max']))

            measurement_rows.append((
                lid, var['id'],
                value_number, value_text, value_boolean,
                out_of_range, False, str(val)[:500],
            ))

    if measurement_rows:
        # Dedupe by (lab_id, variable_id) — same lab can have multiple
        # source rows mapping to same variable (e.g., duplicate columns).
        deduped = {}
        for row in measurement_rows:
            deduped[(row[0], row[1])] = row  # last wins
        measurement_rows = list(deduped.values())

        execute_values(cur, """
            INSERT INTO private.lab_measurement
              (lab_id, variable_id,
               value_number, value_text, value_boolean,
               out_of_range, is_computed, source_value)
            VALUES %s
            ON CONFLICT (lab_id, variable_id) DO UPDATE
              SET value_number = EXCLUDED.value_number,
                  value_text   = EXCLUDED.value_text,
                  value_boolean = EXCLUDED.value_boolean,
                  out_of_range = EXCLUDED.out_of_range
        """, measurement_rows, page_size=2000)

    logger.info(f"  lab: {len(lab_rows)} events, {len(measurement_rows)} measurements")
    return len(lab_rows)


# =============================================================================
# 4. App2 — single combined CSV; needs splitting
# =============================================================================

def import_app2(cur, df: pd.DataFrame,
                import_batch_id: Optional[int] = None) -> Tuple[int, int]:
    """
    App2 has 1 combined CSV. We:
    1. Create patient (PID + sex + age inferred)
    2. Create visit_event (PID + HD as date)
    3. All other columns → visit_measurement
    """
    if 'PID' not in df.columns:
        raise ValueError("App2 missing PID")

    # Synthesize 'IDCARD' for hashing — App2 uses PID
    df = df.copy()
    df['IDCARD'] = df['PID']

    pid_map = import_patients(cur, df, 'app2', import_batch_id)
    if 'HD' in df.columns:
        df['VSTDATE'] = df['HD']
    return (
        len(pid_map),
        import_visits_and_measurements(cur, df, 'app2', 'app2', pid_map, import_batch_id),
    )


# =============================================================================
# 5. Refresh public MVs (called after each import)
# =============================================================================

def refresh_public_mvs(cur) -> None:
    """Refresh all public.mv_* — non-fatal if a single MV fails."""
    cur.execute("SELECT public.refresh_all_mvs()")
    # Log to mv_refresh_log
    cur.execute("""
        INSERT INTO public.mv_refresh_log (view_name, status, duration_ms)
        SELECT view_name, status, duration_ms FROM public.refresh_all_mvs()
    """)
    logger.info("  Refreshed public.mv_*")
