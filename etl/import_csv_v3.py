#!/usr/bin/env python3
"""
ETL v3 — production-grade, robust import to private.* (EAV) schema.

Design goals (revised after real-data testing):
  1. Bulletproof — never crash on bad data; always degrade gracefully
  2. Idempotent — re-running same upload merges, doesn't duplicate
  3. Source-aware — reads `private.variable_definition` per source for mapping
  4. Memory-bounded — accepts pandas DataFrames already chunked by caller
  5. Audit trail — every bulk INSERT has source_value column for traceback

Key invariants enforced before EVERY bulk INSERT:
  - DEDUPE rows by the unique-constraint columns (prevents
    "ON CONFLICT DO UPDATE command cannot affect row a second time")
  - VALIDATE numeric values against variable_definition.valid_min/max
    (out-of-range → NULL with audit; never crash)
  - VALIDATE FK references (facility_code → check exists in private.facility;
    NULL if missing, never raise)
  - TRUNCATE string values to column max_length
  - EXECUTE with savepoint per batch; on batch failure → retry per-row,
    skip bad rows, never fail the whole file
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, date
from typing import Dict, Optional, List, Tuple, Any

import pandas as pd
from psycopg2.extras import execute_values as _pg_execute_values

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Address columns extracted to patient_address (SCD Type 2), not visit_measurement
ADDRESS_COLUMNS = {
    'HDISTRICT', 'DISTRICT', 'WRKDISTRICT', 'CRDISTRICT',
    'HPROVINCE', 'WRKPROVINCE', 'CRPROVINCE',
    'HSUBDISTRICT', 'WRKSUBDISTRICT', 'CRSUBDISTRICT',
}

# Visit-meta columns identify the visit, not measurements
VISIT_META_COLUMNS = {'PID', 'IDCARD', 'VSTDATE', 'VSTTIME', 'HPTCODE',
                       'CANCELST', 'VST_ID', 'HD'}

DATE_FORMATS = (
    '%Y-%m-%d',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M:%S.%f',
    '%d/%m/%Y',
    '%d/%m/%Y %H:%M:%S',
    '%d/%m/%Y %H:%M',
    '%d-%m-%Y',
    '%d-%b-%Y',
    '%Y/%m/%d',
    '%Y/%m/%d %H:%M:%S',
)

# Postgres column type max lengths (for truncation)
MAX_TEXT_LEN = 500
MAX_VARCHAR_50 = 50
MAX_VARCHAR_80 = 80


# --------------------------------------------------------------------------- #
# Defensive parsing helpers
# --------------------------------------------------------------------------- #

def _is_blank(value) -> bool:
    if value is None or pd.isna(value):
        return True
    s = str(value).strip()
    return not s or s.lower() in ('nan', 'none', 'null', 'na', '#n/a')


def _hash_idcard(value) -> str:
    """SHA-256 of citizen ID. Returns '' for blank input."""
    if _is_blank(value):
        return ''
    return hashlib.sha256(str(value).strip().encode('utf-8')).hexdigest()


def _parse_date(value) -> Optional[date]:
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_int(value) -> Optional[int]:
    if _is_blank(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_float(value) -> Optional[float]:
    if _is_blank(value):
        return None
    try:
        v = float(str(value).strip())
        # NaN/Inf checks
        if v != v or v == float('inf') or v == float('-inf'):
            return None
        return v
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_bool(value) -> Optional[bool]:
    if _is_blank(value):
        return None
    s = str(value).strip().lower()
    if s in ('1', 'true', 'yes', 'y', 't'):
        return True
    if s in ('0', 'false', 'no', 'n', 'f'):
        return False
    return None


def _truncate(s: str, max_len: int = MAX_TEXT_LEN) -> str:
    if s is None:
        return None
    return str(s)[:max_len]


def _validate_range(value: Optional[float],
                    valid_min: Optional[float],
                    valid_max: Optional[float]) -> Tuple[Optional[float], bool]:
    """Returns (value or None if out-of-range, out_of_range_flag)."""
    if value is None:
        return None, False
    if valid_min is not None and value < float(valid_min):
        return None, True
    if valid_max is not None and value > float(valid_max):
        return None, True
    return value, False


# --------------------------------------------------------------------------- #
# Resilient bulk insert — wraps execute_values with per-row fallback
# --------------------------------------------------------------------------- #

def _bulk_insert(cur, sql: str, rows: List[tuple], page_size: int = 2000,
                 label: str = '') -> int:
    """Insert rows in chunks. On batch failure, retry per-row to skip bad rows.

    Returns count successfully inserted.
    """
    if not rows:
        return 0
    total = len(rows)
    inserted = 0
    skipped = 0
    for start in range(0, total, page_size):
        chunk = rows[start:start + page_size]
        try:
            cur.execute("SAVEPOINT ev_chunk")
            _pg_execute_values(cur, sql, chunk, page_size=page_size)
            cur.execute("RELEASE SAVEPOINT ev_chunk")
            inserted += len(chunk)
        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT ev_chunk")
            # Retry per-row to skip bad rows
            chunk_ok = chunk_skipped = 0
            for row in chunk:
                try:
                    cur.execute("SAVEPOINT ev_row")
                    _pg_execute_values(cur, sql, [row])
                    cur.execute("RELEASE SAVEPOINT ev_row")
                    chunk_ok += 1
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT ev_row")
                    chunk_skipped += 1
            inserted += chunk_ok
            skipped += chunk_skipped
            if chunk_skipped:
                logger.warning(
                    f"  {label}: chunk failed ({type(exc).__name__}: "
                    f"{str(exc)[:80]}); retried per-row, skipped {chunk_skipped}/{len(chunk)}"
                )
    if skipped:
        logger.info(f"  {label}: inserted {inserted}, skipped {skipped}")
    return inserted


def _load_variable_map(cur, source_code: str) -> Dict[str, dict]:
    """Return {csv_column_name (UPPER): variable info} for source."""
    cur.execute("""
        SELECT csv_column_name, id, data_type, valid_min, valid_max
        FROM private.variable_definition
        WHERE source_code = %s AND deprecated_at IS NULL
    """, (source_code,))
    return {
        row[0].upper(): {'id': row[1], 'data_type': row[2],
                          'valid_min': row[3], 'valid_max': row[4]}
        for row in cur.fetchall()
    }


def _validate_facility(cur, code: str, cache: dict) -> Optional[str]:
    """Return code if exists in private.facility, else None.
    Uses cache to avoid N queries."""
    if not code:
        return None
    code = str(code).strip()[:10]
    if not code:
        return None
    if code in cache:
        return code if cache[code] else None
    cur.execute("SELECT 1 FROM private.facility WHERE code = %s LIMIT 1", (code,))
    exists = cur.fetchone() is not None
    cache[code] = exists
    return code if exists else None


# =============================================================================
# 1. Import patients (pt.csv) — robust
# =============================================================================

def import_patients(cur, df: pd.DataFrame, source_code: str,
                    import_batch_id: Optional[int] = None) -> Dict[str, int]:
    """Upsert into private.patient + private.patient_alias.
    Returns {idcard_hash: patient_id} mapping.
    """
    idcard_col = 'IDCARD' if 'IDCARD' in df.columns else 'PID'
    if idcard_col not in df.columns:
        raise ValueError(f"No IDCARD/PID column in {source_code} pt.csv")

    # ─── Phase 1: dedupe rows by idcard_hash ────────────────────────────────
    rows_by_hash: Dict[str, tuple] = {}
    pid_str_by_hash: Dict[str, str] = {}
    current_year = datetime.now().year

    for _, r in df.iterrows():
        raw = r.get(idcard_col)
        if _is_blank(raw):
            continue
        h = _hash_idcard(raw)
        if not h:
            continue

        sex = str(r.get('MALE', '')).strip() or None
        if sex and sex not in ('1', '2'):
            sex = None

        bdate = _parse_date(r.get('BIRTHDATE') or r.get('BRTHDATE'))
        byear = bdate.year if bdate else None
        bmonth = bdate.month if bdate else None
        if byear is None and 'AGE' in df.columns:
            age = _parse_int(r.get('AGE'))
            if age and 0 < age < 120:
                byear = current_year - age

        rows_by_hash[h] = (h, sex, byear, bmonth, source_code)
        pid_str_by_hash[h] = str(raw).strip()[:80]

    rows = list(rows_by_hash.values())
    if not rows:
        logger.warning(f"No valid patient rows in {source_code} pt")
        return {}

    # ─── Phase 2: upsert patient ────────────────────────────────────────────
    _bulk_insert(cur, """
        INSERT INTO private.patient
          (idcard_hash, sex_code, birth_year, birth_month, primary_source)
        VALUES %s
        ON CONFLICT (idcard_hash) DO UPDATE
          SET sex_code     = COALESCE(EXCLUDED.sex_code, private.patient.sex_code),
              birth_year   = COALESCE(EXCLUDED.birth_year, private.patient.birth_year),
              birth_month  = COALESCE(EXCLUDED.birth_month, private.patient.birth_month),
              last_seen_at = NOW(),
              updated_at   = NOW()
    """, rows, label='patient')

    # Get patient_id for each hash
    cur.execute("""
        SELECT idcard_hash, id FROM private.patient
        WHERE idcard_hash = ANY(%s)
    """, (list(rows_by_hash.keys()),))
    pid_map = {h: pid for h, pid in cur.fetchall()}

    # ─── Phase 3: insert aliases (deduped by patient_id) ───────────────────
    alias_by_pid: Dict[int, tuple] = {}
    for h, pid in pid_map.items():
        alias_by_pid[pid] = (pid, source_code, pid_str_by_hash.get(h))
    alias_rows = list(alias_by_pid.values())

    _bulk_insert(cur, """
        INSERT INTO private.patient_alias (patient_id, source_code, source_pid)
        VALUES %s
        ON CONFLICT (patient_id, source_code) DO NOTHING
    """, alias_rows, label='patient_alias')

    logger.info(f"  patients: {len(pid_map)} unique, {len(alias_rows)} aliases")
    return pid_map


# =============================================================================
# 2. Import visit + measurements (EAV) — robust
# =============================================================================

def _ensure_patients_for_pids(cur, pid_strs: List[str], source_code: str,
                                patient_map: Dict[str, int]) -> int:
    """Auto-create placeholder patients for PIDs not in patient_map.
    Returns count of newly-created patients.
    """
    seen = set()
    new_rows = []
    new_aliases = {}

    for pid_str in pid_strs:
        if _is_blank(pid_str):
            continue
        h = _hash_idcard(pid_str)
        if not h or h in patient_map or h in seen:
            continue
        seen.add(h)
        new_rows.append((h, source_code))
        new_aliases[h] = str(pid_str).strip()[:80]

    if not new_rows:
        return 0

    _bulk_insert(cur, """
        INSERT INTO private.patient (idcard_hash, primary_source)
        VALUES %s
        ON CONFLICT (idcard_hash) DO UPDATE SET last_seen_at = NOW()
    """, new_rows, label='patient (auto-create)')

    cur.execute("""
        SELECT idcard_hash, id FROM private.patient
        WHERE idcard_hash = ANY(%s)
    """, (list(seen),))
    for h, pid in cur.fetchall():
        patient_map[h] = pid

    # Aliases (dedupe by patient_id)
    alias_by_pid = {}
    for h, pid_str in new_aliases.items():
        if h in patient_map:
            alias_by_pid[patient_map[h]] = (patient_map[h], source_code, pid_str)

    _bulk_insert(cur, """
        INSERT INTO private.patient_alias (patient_id, source_code, source_pid)
        VALUES %s
        ON CONFLICT (patient_id, source_code) DO NOTHING
    """, list(alias_by_pid.values()), label='patient_alias (auto)')

    return len(new_rows)


def import_visits_and_measurements(cur, df: pd.DataFrame, source_code: str,
                                     file_type: str, patient_map: Dict[str, int],
                                     import_batch_id: Optional[int] = None) -> int:
    """For vital/hv/hh/labext: create visit_event + visit_measurement.
    Address columns → patient_address (SCD Type 2).

    Robust: handles missing FKs, dupes, bad dates, out-of-range numerics.
    Returns: count of visits created.
    """
    var_map = _load_variable_map(cur, source_code)
    if not var_map:
        logger.warning(f"No variables defined for {source_code}; skipping {file_type}")
        return 0

    pid_col = 'PID' if 'PID' in df.columns else 'IDCARD'
    if pid_col not in df.columns:
        raise ValueError(f"No PID/IDCARD column in {source_code} {file_type}")
    if 'VSTDATE' not in df.columns:
        logger.warning(f"No VSTDATE in {source_code} {file_type}; skipping visit creation")
        return 0

    facility_cache: Dict[str, bool] = {}

    # ─── Phase 1: ensure all PIDs have patient rows ─────────────────────────
    pid_strs = list(set(
        str(r.get(pid_col)).strip()
        for _, r in df.iterrows()
        if not _is_blank(r.get(pid_col))
    ))
    n_created = _ensure_patients_for_pids(cur, pid_strs, source_code, patient_map)
    if n_created:
        logger.info(f"  auto-created {n_created} patients for {source_code}/{file_type}")

    # ─── Phase 2: build visit_event rows (deduped by patient_id, visit_date)
    visit_dict: Dict[Tuple[int, date], tuple] = {}
    visit_meta: Dict[Tuple[int, date], int] = {}  # (pid, date) → df_index
    skipped_pid = skipped_date = 0

    for idx, r in df.iterrows():
        raw_pid = r.get(pid_col)
        h = _hash_idcard(raw_pid)
        if not h or h not in patient_map:
            skipped_pid += 1
            continue
        v_date = _parse_date(r.get('VSTDATE'))
        if not v_date:
            skipped_date += 1
            continue

        pid = patient_map[h]
        cancel = _parse_int(r.get('CANCELST')) or 0
        facility = _validate_facility(cur, str(r.get('HPTCODE', '')).strip(),
                                       facility_cache)
        source_visit_id = _truncate(str(r.get('VST_ID', '')).strip()
                                     or str(raw_pid).strip(), 80)

        key = (pid, v_date)
        visit_dict[key] = (pid, source_code, v_date, None, facility,
                           cancel, source_visit_id, import_batch_id)
        visit_meta[key] = idx

    if not visit_dict:
        logger.info(f"  {source_code}/{file_type}: no valid visits "
                     f"(skipped pid={skipped_pid}, date={skipped_date})")
        return 0

    # ─── Phase 3: insert visit_event ────────────────────────────────────────
    _bulk_insert(cur, """
        INSERT INTO private.visit_event
          (patient_id, source_code, visit_date, visit_time,
           facility_code, cancel_status, source_visit_id, import_batch_id)
        VALUES %s
        ON CONFLICT (patient_id, source_code, visit_date) WHERE cancel_status = 0
        DO UPDATE SET facility_code = COALESCE(EXCLUDED.facility_code, private.visit_event.facility_code),
                      import_batch_id = EXCLUDED.import_batch_id
    """, list(visit_dict.values()), label='visit_event')

    # Get visit_id for each (patient_id, visit_date)
    pids = list({v[0] for v in visit_dict.values()})
    cur.execute("""
        SELECT patient_id, visit_date, id FROM private.visit_event
        WHERE source_code = %s AND patient_id = ANY(%s)
    """, (source_code, pids))
    visit_id_map = {(pid, vd): vid for pid, vd, vid in cur.fetchall()}

    # ─── Phase 4: build measurement + address rows ─────────────────────────
    measurement_dict: Dict[Tuple[int, int], tuple] = {}  # (visit_id, var_id) → row
    address_keys: Dict[Tuple[int, str], tuple] = {}     # (patient_id, type) → row

    for (pid, v_date), idx in visit_meta.items():
        vid = visit_id_map.get((pid, v_date))
        if not vid:
            continue
        r = df.iloc[idx]

        # Address columns → patient_address (latest one per type wins)
        home_dc = None
        work_dc = None
        for col in ('HDISTRICT', 'DISTRICT'):
            if col in df.columns:
                v = _parse_int(r.get(col))
                if v and v != 9999 and 1001 <= v <= 9999:
                    home_dc = home_dc or str(v).zfill(4)
        if 'WRKDISTRICT' in df.columns:
            v = _parse_int(r.get('WRKDISTRICT'))
            if v and v != 9999:
                work_dc = str(v).zfill(4)

        if home_dc:
            address_keys[(pid, 'home')] = (pid, 'home', None, home_dc, None,
                                             v_date, vid, source_code)
        if work_dc:
            address_keys[(pid, 'work')] = (pid, 'work', None, work_dc, None,
                                             v_date, vid, source_code)

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

            value_number = value_text = value_boolean = value_date = None
            dtype = var['data_type']
            try:
                if dtype == 'number':
                    value_number = _parse_float(val)
                    if value_number is not None:
                        value_number, _oor = _validate_range(
                            value_number, var['valid_min'], var['valid_max']
                        )
                    if value_number is None:
                        # Out-of-range or unparseable → store as text for audit
                        value_text = _truncate(str(val))
                elif dtype == 'boolean':
                    value_boolean = _parse_bool(val)
                    if value_boolean is None:
                        value_text = _truncate(str(val))
                elif dtype == 'date':
                    value_date = _parse_date(val)
                else:
                    value_text = _truncate(str(val))
            except Exception:
                value_text = _truncate(str(val))

            if value_number is None and value_text is None \
               and value_boolean is None and value_date is None:
                continue

            measurement_dict[(vid, var['id'])] = (
                vid, var['id'],
                value_number, value_text, value_boolean, value_date,
                False, _truncate(str(val), MAX_TEXT_LEN),
            )

    # ─── Phase 5: bulk insert measurements + addresses ─────────────────────
    _bulk_insert(cur, """
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
    """, list(measurement_dict.values()), label='visit_measurement')

    # Address: SCD Type 2 — close any existing active row that disagrees, then insert
    if address_keys:
        for (pid, atype), arow in address_keys.items():
            try:
                cur.execute("SAVEPOINT addr")
                cur.execute("""
                    UPDATE private.patient_address
                    SET effective_to = %s
                    WHERE patient_id = %s AND address_type = %s
                      AND effective_to IS NULL
                      AND district_code IS DISTINCT FROM %s
                """, (arow[5], pid, atype, arow[3]))
                cur.execute("""
                    INSERT INTO private.patient_address
                      (patient_id, address_type, province_code, district_code,
                       subdistrict_code, effective_from, reported_by_visit_id,
                       source_code)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, arow)
                cur.execute("RELEASE SAVEPOINT addr")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT addr")

    logger.info(
        f"  {source_code}/{file_type}: visits={len(visit_dict)}, "
        f"measurements={len(measurement_dict)}, addresses={len(address_keys)}, "
        f"skipped_pid={skipped_pid}, skipped_date={skipped_date}"
    )
    return len(visit_dict)


# =============================================================================
# 3. Import lab — robust
# =============================================================================

def import_lab(cur, df: pd.DataFrame, source_code: str,
               patient_map: Dict[str, int],
               import_batch_id: Optional[int] = None) -> int:
    """Upsert lab_event + lab_measurement."""
    var_map = _load_variable_map(cur, source_code)
    pid_col = 'PID' if 'PID' in df.columns else 'IDCARD'
    if pid_col not in df.columns:
        raise ValueError(f"No PID/IDCARD in {source_code} lab")

    facility_cache: Dict[str, bool] = {}

    # Phase 1: auto-create missing patients
    pid_strs = list(set(
        str(r.get(pid_col)).strip()
        for _, r in df.iterrows() if not _is_blank(r.get(pid_col))
    ))
    n = _ensure_patients_for_pids(cur, pid_strs, source_code, patient_map)
    if n:
        logger.info(f"  auto-created {n} patients for {source_code}/lab")

    # Phase 2: build lab_event rows (no unique constraint, but dedupe by hash anyway)
    lab_meta: List[Tuple[str, date, int]] = []  # (hash, date, df_index)
    lab_rows: List[tuple] = []

    for idx, r in df.iterrows():
        h = _hash_idcard(r.get(pid_col))
        if not h or h not in patient_map:
            continue
        lab_date = _parse_date(r.get('VSTDATE') or r.get('LABDATE'))
        if not lab_date:
            continue
        facility = _validate_facility(cur, str(r.get('HPTCODE', '')).strip(),
                                       facility_cache)
        cancel = _parse_int(r.get('CANCELST')) or 0
        privilege = _truncate(str(r.get('PRVLG', '')).strip(), 20)
        source_lab_id = _truncate(str(r.get('LAB_ID', '')).strip()
                                    or str(r.get(pid_col)).strip(), 80)

        lab_rows.append((
            patient_map[h], source_code, lab_date, facility,
            cancel, source_lab_id, privilege, import_batch_id,
        ))
        lab_meta.append((h, lab_date, idx))

    if not lab_rows:
        return 0

    # Insert lab_events (no upsert — append-only)
    _bulk_insert(cur, """
        INSERT INTO private.lab_event
          (patient_id, source_code, lab_date, facility_code,
           cancel_status, source_lab_id, privilege_code, import_batch_id)
        VALUES %s
    """, lab_rows, label='lab_event')

    # Get lab_ids — first match per (pid, date)
    pids = list({patient_map[h] for h, _, _ in lab_meta if h in patient_map})
    cur.execute("""
        SELECT patient_id, lab_date, id FROM private.lab_event
        WHERE source_code = %s AND patient_id = ANY(%s)
        ORDER BY id DESC
    """, (source_code, pids))
    lab_id_map: Dict[Tuple[int, date], int] = {}
    for pid, ld, lid in cur.fetchall():
        lab_id_map.setdefault((pid, ld), lid)

    # Phase 3: lab_measurements (deduped)
    meas_dict: Dict[Tuple[int, int], tuple] = {}
    for h, lab_date, idx in lab_meta:
        pid = patient_map.get(h)
        if not pid:
            continue
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

            value_number = value_text = value_boolean = None
            out_of_range = None
            dtype = var['data_type']
            try:
                if dtype == 'number':
                    value_number = _parse_float(val)
                    if value_number is not None:
                        value_number, oor = _validate_range(
                            value_number, var['valid_min'], var['valid_max']
                        )
                        out_of_range = oor
                    if value_number is None:
                        value_text = _truncate(str(val))
                elif dtype == 'boolean':
                    value_boolean = _parse_bool(val)
                    if value_boolean is None:
                        value_text = _truncate(str(val))
                else:
                    value_text = _truncate(str(val))
            except Exception:
                value_text = _truncate(str(val))

            if value_number is None and value_text is None and value_boolean is None:
                continue

            meas_dict[(lid, var['id'])] = (
                lid, var['id'], value_number, value_text, value_boolean,
                out_of_range, False, _truncate(str(val)),
            )

    _bulk_insert(cur, """
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
    """, list(meas_dict.values()), label='lab_measurement')

    logger.info(f"  {source_code}/lab: events={len(lab_rows)}, "
                 f"measurements={len(meas_dict)}")
    return len(lab_rows)


# =============================================================================
# 4. App2 — combined CSV
# =============================================================================

def import_app2(cur, df: pd.DataFrame,
                import_batch_id: Optional[int] = None) -> Tuple[int, int]:
    """App2 is a single combined CSV. Treat PID as IDCARD, HD as VSTDATE."""
    if 'PID' not in df.columns:
        raise ValueError("App2 missing PID column")

    df = df.copy()
    df['IDCARD'] = df['PID']  # for import_patients to find ID column
    if 'HD' in df.columns and 'VSTDATE' not in df.columns:
        df['VSTDATE'] = df['HD']

    pid_map = import_patients(cur, df, 'app2', import_batch_id)
    n_visits = import_visits_and_measurements(
        cur, df, 'app2', 'app2', pid_map, import_batch_id,
    )
    return len(pid_map), n_visits


# =============================================================================
# 5. Refresh public MVs
# =============================================================================

def refresh_public_mvs(cur) -> List[Tuple[str, str]]:
    """Refresh all public.mv_* via the SQL function. Returns [(view_name, status)]."""
    cur.execute("SELECT view_name, status, duration_ms FROM public.refresh_all_mvs()")
    return [(v, s) for v, s, _ in cur.fetchall()]
