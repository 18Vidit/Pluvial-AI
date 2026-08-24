"""Street geometry from a local Geofabrik OSM PBF extract, in place of the
live Overpass API.

design spec's original snap-streets path (osm_segments.py) queried Overpass
directly, but the shared instance proved unreliable in practice: it cycled
between working and refusing connections within the same session (10 clean
tile requests, then sustained "connection refused" for the rest of a run;
recovered briefly later, then refused again). A one-time download of the
Texas extract removes that live dependency entirely — the weekly Calibrator
never needs fresh street geometry (roads don't move), so a local extract is
strictly better here, not just a workaround.

Download once (not automated — a 715MB state-wide file shouldn't be an
unattended side effect of running a CLI command):
    curl -sL -o data/raw/osm/texas-latest.osm.pbf \\
      https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import osmium

from pluvial.ingest.osm_segments import HIGHWAY_CLASSES, HOUSTON_BBOX, SEGMENTS_TABLE

# Small margin beyond the exact bbox so a way that starts just inside Houston
# and ends just outside isn't dropped for the endpoint alone.
BBOX_MARGIN_DEG = 0.02


class _HoustonHighwayHandler(osmium.SimpleHandler):
    def __init__(self, bbox: tuple[float, float, float, float]):
        super().__init__()
        s, w, n, e = bbox
        self.bbox = (s - BBOX_MARGIN_DEG, w - BBOX_MARGIN_DEG, n + BBOX_MARGIN_DEG, e + BBOX_MARGIN_DEG)
        self.rows: list[tuple] = []

    def _in_bbox(self, lat: float, lon: float) -> bool:
        s, w, n, e = self.bbox
        return s <= lat <= n and w <= lon <= e

    def way(self, w) -> None:
        highway = w.tags.get("highway")
        if highway not in HIGHWAY_CLASSES:
            return
        try:
            points = [(n.location.lat, n.location.lon) for n in w.nodes if n.location.valid()]
        except osmium.InvalidLocationError:
            return
        if len(points) < 2:
            return
        # Keep the way if ANY node falls inside the (margined) Houston bbox —
        # matches the spirit of the original Overpass tile query, which
        # returned any way intersecting a tile.
        if not any(self._in_bbox(lat, lon) for lat, lon in points):
            return
        wkt_points = ", ".join(f"{lon} {lat}" for lat, lon in points)
        self.rows.append((
            w.id,
            w.tags.get("name"),
            highway,
            f"LINESTRING({wkt_points})",
        ))


def extract_houston_streets(pbf_path: Path, bbox: tuple[float, float, float, float] = HOUSTON_BBOX) -> list[tuple]:
    """Stream the PBF once, filtering to highway ways with at least one node
    in the Houston bbox. Node locations are resolved via NodeLocationsForWays
    (sparse in-memory index — a state extract has tens of millions of nodes,
    a dense array would be wasteful)."""
    handler = _HoustonHighwayHandler(bbox)
    idx = osmium.index.create_map("sparse_mem_array")
    location_handler = osmium.NodeLocationsForWays(idx)
    location_handler.ignore_errors()
    osmium.apply(str(pbf_path), location_handler, handler)
    return handler.rows


def load_streets_from_pbf(con: duckdb.DuckDBPyConnection, pbf_path: Path) -> int:
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    con.execute(f"""
        CREATE OR REPLACE TABLE {SEGMENTS_TABLE} (
            segment_id BIGINT PRIMARY KEY,
            name TEXT,
            highway_class TEXT,
            wkt TEXT,
            geom GEOMETRY
        )
    """)
    rows = extract_houston_streets(pbf_path)
    # executemany + bulk cast, not executemany(..., ST_GeomFromText(?), ...) —
    # see the identical note in osm_segments.load_streets_into_db, same
    # DuckDB limitation applies here.
    con.executemany(f"INSERT INTO {SEGMENTS_TABLE} (segment_id, name, highway_class, wkt) VALUES (?, ?, ?, ?)", rows)
    con.execute(f"UPDATE {SEGMENTS_TABLE} SET geom = ST_GeomFromText(wkt)")
    con.execute(f"ALTER TABLE {SEGMENTS_TABLE} DROP COLUMN wkt")
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_segments_geom ON {SEGMENTS_TABLE} USING RTREE (geom)")
    return len(rows)


def run(db_path: Path, pbf_path: Path) -> None:
    from pluvial.ingest.osm_segments import snap_complaints_to_segments

    if not pbf_path.exists():
        raise FileNotFoundError(
            f"{pbf_path} not found. Download it first:\n"
            f"  curl -sL -o {pbf_path} "
            f"https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf"
        )
    con = duckdb.connect(str(db_path))
    print(f"extracting Houston-area highways from {pbf_path.name}...")
    n = load_streets_from_pbf(con, pbf_path)
    print(f"  loaded {n:,} street segments")
    print("snapping complaints to nearest segment (<=150m)...")
    n_snapped = snap_complaints_to_segments(con)
    total = con.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]
    print(f"  snapped {n_snapped:,}/{total:,} complaints")
    con.close()


if __name__ == "__main__":
    import sys
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/pluvial.duckdb")
    pbf = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/raw/osm/texas-latest.osm.pbf")
    run(db, pbf)
