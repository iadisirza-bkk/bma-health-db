"""
ETL script to load clinic_latlong.xls into ref_facilities.
Loads 14,092 facility records from the BMA facility registry.

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
    """Insert/update facilities into ref_facilities using batch insert."""
    stats = {"inserted": 0, "skipped": 0, "total": len(df)}

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

        rows_to_insert.append((code, name_th, ct_name, lat, lng, address, telephone, ct_id, ct_name))

    cur = conn.cursor()
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
    conn.commit()
    stats["inserted"] = len(rows_to_insert)
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
