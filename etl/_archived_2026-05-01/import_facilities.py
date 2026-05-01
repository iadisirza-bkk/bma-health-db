"""
ETL script to load clinic_latlong.xls into ref_facilities AND private.facility.

Loads 14,092 facility records from the BMA facility registry. Migration 100
introduced `private.facility` as the v3 canonical table — this script writes
to BOTH so legacy (ref_facilities) and v3 (private.facility) consumers stay
in sync.

Usage:
    python etl/import_facilities.py [--xls PATH] [--db DATABASE_URL]
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DEFAULT_XLS = os.path.join(os.path.dirname(__file__), "..", "fact", "clinic_latlong.xls")
DEFAULT_DB = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:bma_health_dev@localhost:5433/bma_health",
)


def load_xls(path: str) -> pd.DataFrame:
    """Read the facility XLS and normalize columns."""
    df = pd.read_excel(path)
    # Rename for clarity
    df = df.rename(columns={
        "ct_id": "ct_id",
        "ct_name": "ct_name",
        "c_name": "name_th",
        "ct_name.1": "ct_name_dup",
        "address": "address",
        "tel": "telephone",
        "hcode": "hcode",
        "lat": "latitude",
        "lng": "longitude",
    })
    # Clean telephone — remove \r\n and extra spaces
    df["telephone"] = df["telephone"].astype(str).str.replace(r"\r\n.*", "", regex=True).str.strip()
    df.loc[df["telephone"] == "nan", "telephone"] = None
    # Ensure lat/lng are float
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    # Filter to Bangkok bounds (rough)
    valid = (
        df["latitude"].between(13.4, 14.1) &
        df["longitude"].between(100.2, 101.0)
    ) | df["latitude"].isna()
    df = df[valid].copy()
    return df


def import_facilities(conn, df: pd.DataFrame) -> dict:
    """Insert/update facilities into ref_facilities AND private.facility.

    The two tables overlap on (code, name_th, facility_type, latitude,
    longitude, address, telephone, ct_id, ct_name) — both are populated
    in the same transaction so v3 ETL (`etl/import_csv_v3.py:_validate_facility`)
    sees a non-empty FK target.
    """
    stats = {"inserted": 0, "skipped": 0, "total": len(df),
             "v3_inserted": 0, "v3_skipped": False}

    rows_to_insert = []
    seen_codes = set()
    auto_id = 0

    for _, row in df.iterrows():
        lat = row["latitude"] if pd.notna(row["latitude"]) else None
        lng = row["longitude"] if pd.notna(row["longitude"]) else None

        if lat is None or lng is None:
            stats["skipped"] += 1
            continue

        raw_hcode = row["hcode"]
        try:
            hcode = str(int(float(raw_hcode))) if pd.notna(raw_hcode) and str(raw_hcode).strip() not in ("", "-", "nan") else None
        except (ValueError, TypeError):
            hcode = None

        if hcode and hcode not in seen_codes:
            code = hcode
        else:
            auto_id += 1
            code = f"F{auto_id:06d}"

        if code in seen_codes:
            stats["skipped"] += 1
            continue
        seen_codes.add(code)

        name_th = str(row["name_th"])[:100] if pd.notna(row["name_th"]) else "Unknown"
        address = str(row["address"]) if pd.notna(row["address"]) else None
        telephone = str(row["telephone"])[:50] if row.get("telephone") else None
        ct_id = int(row["ct_id"]) if pd.notna(row["ct_id"]) else None
        ct_name = str(row["ct_name"])[:100] if pd.notna(row["ct_name"]) else None

        # facility_type column is VARCHAR(20) on ref_facilities and VARCHAR(40)
        # on private.facility — derive from ct_name truncated. Pass full ct_name
        # separately so v3 still keeps the long form.
        facility_type = ct_name[:20] if ct_name else None
        rows_to_insert.append((code, name_th, facility_type, lat, lng, address, telephone, ct_id, ct_name))

    cur = conn.cursor()
    # ─── Legacy: ref_facilities ──────────────────────────────────────────
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO ref_facilities (code, name_th, facility_type, latitude, longitude, address, telephone, ct_id, ct_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (code) DO UPDATE SET
            name_th = EXCLUDED.name_th,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            address = EXCLUDED.address,
            telephone = EXCLUDED.telephone,
            ct_id = EXCLUDED.ct_id,
            ct_name = EXCLUDED.ct_name
    """, rows_to_insert, page_size=500)
    stats["inserted"] = len(rows_to_insert)

    # ─── v3: private.facility (added migration 100) ──────────────────────
    # `private.facility` has only `code` (PK) and `name_th` (NOT NULL) as
    # required — district_code/zone_code are FKs but nullable, so we leave
    # them NULL until a separate geo-coding step backfills them. truncating
    # 10-char `code` matches the schema VARCHAR(10) constraint.
    try:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'private' AND table_name = 'facility')"
        )
        v3_exists = cur.fetchone()[0]
    except Exception:
        v3_exists = False
        conn.rollback()

    if v3_exists:
        # private.facility allows facility_type up to 40 chars, so we re-derive
        # from the original ct_name (r[8]) instead of the 20-char truncation
        # we passed to ref_facilities.
        v3_rows = [
            (
                r[0][:10],                                  # code (PK, VARCHAR(10))
                r[1],                                        # name_th
                (r[8][:40] if r[8] else None),               # facility_type from ct_name
                r[3],                                        # latitude
                r[4],                                        # longitude
                r[5],                                        # address
                r[6],                                        # telephone
                r[7],                                        # ct_id
                (r[8][:100] if r[8] else None),              # ct_name
            )
            for r in rows_to_insert
        ]
        psycopg2.extras.execute_batch(cur, """
            INSERT INTO private.facility
              (code, name_th, facility_type, latitude, longitude,
               address, telephone, ct_id, ct_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                name_th = EXCLUDED.name_th,
                facility_type = EXCLUDED.facility_type,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                address = EXCLUDED.address,
                telephone = EXCLUDED.telephone,
                ct_id = EXCLUDED.ct_id,
                ct_name = EXCLUDED.ct_name
        """, v3_rows, page_size=500)
        stats["v3_inserted"] = len(v3_rows)
    else:
        stats["v3_skipped"] = True

    conn.commit()
    cur.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Import clinic_latlong.xls into ref_facilities")
    parser.add_argument("--xls", default=DEFAULT_XLS, help="Path to clinic_latlong.xls")
    parser.add_argument("--db", default=DEFAULT_DB, help="PostgreSQL connection string")
    args = parser.parse_args()

    print(f"Loading XLS: {args.xls}")
    df = load_xls(args.xls)
    print(f"Loaded {len(df)} records ({df['latitude'].notna().sum()} with lat/lng)")

    print(f"Connecting to: {args.db.split('@')[1] if '@' in args.db else args.db}")
    conn = psycopg2.connect(args.db)

    print("Importing facilities...")
    stats = import_facilities(conn, df)
    print(f"Done: {stats['inserted']} inserted, {stats['skipped']} skipped")

    # Verify
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ref_facilities WHERE latitude IS NOT NULL")
    total = cur.fetchone()[0]
    print(f"Total facilities with coordinates in DB: {total}")
    conn.close()


if __name__ == "__main__":
    main()
