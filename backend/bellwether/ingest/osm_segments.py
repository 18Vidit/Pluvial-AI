"""Pull Houston street centrelines from OSM (Overpass API) and snap 311
complaints to the nearest street segment.

Mains run along streets, so a segment (not a grid cell) is the natural unit
for the dossier: two complaints on the same block should accumulate against
the same record even if they're 40m apart on the map.

Overpass times out on large bboxes, so the metro is queried in tiles and
results are cached to disk as GeoJSON-ish JSON to avoid re-fetching.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Houston city-limits-ish bounding box (south, west, north, east).
HOUSTON_BBOX = (29.52, -95.80, 30.11, -95.01)

HIGHWAY_CLASSES = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential",
)

SEGMENTS_TABLE = "street_segments"


def _tile_bbox(bbox: tuple[float, float, float, float], step_deg: float = 0.06):
    s, w, n, e = bbox
    lat = s
    while lat < n:
        lon = w
        lat_hi = min(lat + step_deg, n)
        while lon < e:
            lon_hi = min(lon + step_deg, e)
            yield (lat, lon, lat_hi, lon_hi)
            lon = lon_hi
        lat = lat_hi


def _fetch_tile(bbox: tuple[float, float, float, float], client: httpx.Client) -> list[dict]:
    s, w, n, e = bbox
    highway_re = "^(" + "|".join(HIGHWAY_CLASSES) + ")$"
    query = f"""
    [out:json][timeout:60];
    way["highway"~"{highway_re}"]({s},{w},{n},{e});
    out geom;
    """
    for attempt in range(3):
        try:
            r = client.post(OVERPASS_URL, data={"data": query}, timeout=90)
            r.raise_for_status()
            return r.json().get("elements", [])
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return []


def download_streets(cache_path: Path, bbox: tuple[float, float, float, float] = HOUSTON_BBOX) -> list[dict]:
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    ways: list[dict] = []
    tiles = list(_tile_bbox(bbox))
    with httpx.Client() as client:
        for i, tile in enumerate(tiles):
            print(f"  overpass tile {i + 1}/{len(tiles)} {tile}")
            ways.extend(_fetch_tile(tile, client))
            time.sleep(1.5)  # be polite to the shared Overpass instance

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(ways))
    return ways


def load_streets_into_db(con: duckdb.DuckDBPyConnection, ways: list[dict]) -> int:
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    con.execute(f"""
        CREATE OR REPLACE TABLE {SEGMENTS_TABLE} (
            segment_id BIGINT PRIMARY KEY,
            name TEXT,
            highway_class TEXT,
            geom GEOMETRY
        )
    """)
    rows = []
    for way in ways:
        geom = way.get("geometry")
        if not geom or len(geom) < 2:
            continue
        wkt_points = ", ".join(f"{p['lon']} {p['lat']}" for p in geom)
        rows.append((
            way["id"],
            way.get("tags", {}).get("name"),
            way.get("tags", {}).get("highway"),
            f"LINESTRING({wkt_points})",
        ))
    con.executemany(
        f"INSERT INTO {SEGMENTS_TABLE} VALUES (?, ?, ?, ST_GeomFromText(?))",
        rows,
    )
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_segments_geom ON {SEGMENTS_TABLE} USING RTREE (geom)")
    return len(rows)


def snap_complaints_to_segments(con: duckdb.DuckDBPyConnection, search_radius_m: float = 150.0) -> int:
    """For each complaint, find the nearest street segment within search_radius_m
    and write its id back onto complaints.segment_id.

    Distances are computed in degrees via ST_Distance on WGS84 geometry, which
    is a small approximation at Houston's latitude (~1 degree lat ~= 111km,
    ~1 degree lon ~= 96km) — acceptable for a ~150m snap radius, not for
    precise metric distance elsewhere.
    """
    deg_radius = search_radius_m / 96_000  # conservative (uses the smaller, longitude, scale)

    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    con.execute(f"""
        UPDATE complaints AS c
        SET segment_id = nearest.segment_id
        FROM (
            SELECT
                c2.case_number,
                (
                    SELECT s.segment_id
                    FROM {SEGMENTS_TABLE} s
                    WHERE ST_Distance(s.geom, ST_Point(c2.longitude, c2.latitude)) < {deg_radius}
                    ORDER BY ST_Distance(s.geom, ST_Point(c2.longitude, c2.latitude))
                    LIMIT 1
                ) AS segment_id
            FROM complaints c2
        ) AS nearest
        WHERE c.case_number = nearest.case_number
          AND nearest.segment_id IS NOT NULL
    """)
    return con.execute("SELECT COUNT(*) FROM complaints WHERE segment_id IS NOT NULL").fetchone()[0]


def run(db_path: Path, cache_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    print("downloading/loading OSM street geometry...")
    ways = download_streets(cache_path)
    print(f"  {len(ways):,} ways")
    n = load_streets_into_db(con, ways)
    print(f"  loaded {n:,} street segments")
    print("snapping complaints to nearest segment (<=150m)...")
    n_snapped = snap_complaints_to_segments(con)
    total = con.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]
    print(f"  snapped {n_snapped:,}/{total:,} complaints")
    con.close()


if __name__ == "__main__":
    import sys
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/bellwether.duckdb")
    cache = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/raw/osm_streets_houston.json")
    run(db, cache)
