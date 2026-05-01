#!/usr/bin/env python3
"""
Geocode private.facility rows: lat/lng → district_code + zone_code.

Reads the canonical Bangkok district shapes from the frontend's vectors
folder (single source of truth for the choropleth) and uses point-in-polygon
to find which BKK district each facility falls in. Then looks up zone_code
from private.geo_district.

Facilities outside BKK 50 districts (e.g. in BMR provinces) are left NULL —
that's correct; this script only resolves BKK 50.

Usage:
  python3 etl/geocode_facilities.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
from shapely.geometry import Point, shape
from shapely.prepared import prep

GEOJSON_PATH = Path('/Users/dev/bma-health/frontend/public/vectors/bangkok-districts.geojson')
DB_URL = os.getenv('DATABASE_URL_WRITER',
                   'postgresql://postgres:bma_health_dev@localhost:5433/bma_health')


def load_district_polygons():
    """Load 50 BKK district polygons keyed by dcode."""
    with GEOJSON_PATH.open() as f:
        geo = json.load(f)
    polys = []
    for feat in geo['features']:
        dcode = feat['properties']['dcode']
        geom = shape(feat['geometry'])
        polys.append((dcode, prep(geom), geom))
    print(f'Loaded {len(polys)} district polygons')
    return polys


def lookup_zone_codes(conn):
    """dcode → zone_code lookup from private.geo_district."""
    with conn.cursor() as cur:
        cur.execute('SELECT dcode, zone_code FROM private.geo_district')
        return dict(cur.fetchall())


def geocode(dry_run: bool = False) -> int:
    polys = load_district_polygons()
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False

    zone_map = lookup_zone_codes(conn)

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT code, latitude::float, longitude::float
        FROM private.facility
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
          AND district_code IS NULL
    """)
    facilities = cur.fetchall()
    print(f'Geocoding {len(facilities):,} facilities...')

    matched = 0
    no_match = 0
    by_zone: dict[str, int] = {}
    updates: list[tuple[str, str | None, str]] = []

    t0 = time.time()
    for f in facilities:
        pt = Point(f['longitude'], f['latitude'])
        hit_dcode = None
        for dcode, prepared, _geom in polys:
            if prepared.contains(pt):
                hit_dcode = dcode
                break
        if hit_dcode:
            zone_code = zone_map.get(hit_dcode)
            updates.append((hit_dcode, zone_code, f['code']))
            matched += 1
            by_zone[zone_code or '?'] = by_zone.get(zone_code or '?', 0) + 1
        else:
            no_match += 1

    elapsed = time.time() - t0
    print(f'  Matched : {matched:,} ({matched / len(facilities) * 100:.1f}%)')
    print(f'  No match: {no_match:,} (outside BKK 50 districts)')
    print(f'  Per zone: {sorted(by_zone.items())}')
    print(f'  Time    : {elapsed:.1f}s')

    if dry_run:
        print('Dry run — no UPDATE applied')
        conn.close()
        return 0

    print(f'Applying {len(updates):,} UPDATEs...')
    psycopg2.extras.execute_values(
        cur,
        """
        UPDATE private.facility AS f
           SET district_code = u.dcode,
               zone_code     = u.zcode
          FROM (VALUES %s) AS u(dcode, zcode, code)
         WHERE f.code = u.code
        """,
        updates,
        template='(%s, %s, %s)',
    )
    conn.commit()
    print(f'Committed.')

    cur.execute("""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE district_code IS NOT NULL) AS with_district,
          COUNT(*) FILTER (WHERE zone_code IS NOT NULL)     AS with_zone
        FROM private.facility
    """)
    row = cur.fetchone()
    print(f'Final: {row["total"]:,} total, '
          f'{row["with_district"]:,} with_district, '
          f'{row["with_zone"]:,} with_zone')

    conn.close()
    return matched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='Preview, no UPDATE')
    args = ap.parse_args()
    geocode(dry_run=args.dry_run)


if __name__ == '__main__':
    sys.exit(main() or 0)
