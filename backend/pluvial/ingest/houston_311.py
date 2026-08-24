"""Parse the Houston 311 CRIS public data extracts into DuckDB.

The extracts are pipe-delimited text with a handful of banner lines before
the header, use bare ``\\n`` line endings (not CSV-quoted, not CRLF), and the
schema changed in 2022 when the city moved to a Dynamics 365 (D365) backend.
We only ingest 2022-onward, where the schema is uniform. Pre-2022 files use a
different column set and are out of scope (documented in the design spec).
"""
from __future__ import annotations

from pathlib import Path

import duckdb

# Case types that plausibly indicate a water/sewer/drainage condition,
# confirmed against the live 2025 extract during design research.
WATER_CASE_TYPES = [
    "Water Leak",
    "Major Water Leak",
    "Minor Water Leak",
    "Water Service",
    "Water Main Valve",
    "Water Meter",
    "Water Quality",
    "Water or Ground Pollution",
    "Water/Sewer/Drainage Billing",
    "Sewer Wastewater",
    "Sewer Manhole",
    "Drainage",
    "Drainage System Violation",
    "Poor Drainage",
]

RAW_TABLE = "raw_complaints"
CLEAN_TABLE = "complaints"


REQUIRED_COLUMNS = [
    "Case Number", "Incident Address", "Latitude", "Longitude", "Status",
    "Created Date Local", "Closed Date", "Incident Case Type", "Department", "Title",
]


MAX_BANNER_LINES_SCANNED = 20  # header has appeared at line 6 in every extract seen; cap generously


def load_extract(con: duckdb.DuckDBPyConnection, path: Path, year_tag: str) -> int:
    """Load a single extract file into raw_complaints, streaming line-by-line
    in a SINGLE pass and projecting to only the required columns as it goes
    (the file is ~50 columns wide and only 10 are used — keeping the rest
    around for hundreds of thousands of rows is pure waste).

    Single-pass matters here, not just for speed: an earlier version read a
    fixed 10-line "banner" to locate the header, then iterated the rest of
    the file for data. On the real extracts the header sits at line 6, so
    that banner window silently swallowed the first 4 data rows into the
    discarded banner on every file. Scanning for the header and falling
    through to data processing in one loop avoids ever discarding a line
    that wasn't actually banner/header.
    """
    header: list[str] | None = None
    col_idx: dict[str, int] | None = None
    req_idx: list[int] = []
    lat_pos_in_required = REQUIRED_COLUMNS.index("Latitude")
    n_cols = 0
    n_banner_lines_seen = 0

    n_reinserted_headers = 0
    n_loaded = 0
    batch: list[tuple] = []
    BATCH_SIZE = 20_000

    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
            case_number TEXT, incident_address TEXT, latitude DOUBLE, longitude DOUBLE,
            status TEXT, created_date TEXT, closed_date TEXT, incident_case_type TEXT,
            department TEXT, title TEXT, source_file TEXT
        )
    """)

    def flush():
        nonlocal batch
        if batch:
            con.executemany(f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch = []

    with path.open(encoding="latin-1", errors="replace") as f:
        for line in f:
            if header is None:
                n_banner_lines_seen += 1
                parts = line.rstrip("\n").split("|")
                if "Latitude" in parts:
                    header = parts
                    col_idx = {name: i for i, name in enumerate(header)}
                    missing = [c for c in REQUIRED_COLUMNS if c not in col_idx]
                    if missing:
                        raise ValueError(f"{path}: missing expected columns {missing}")
                    req_idx = [col_idx[c] for c in REQUIRED_COLUMNS]
                    n_cols = len(header)
                    continue  # the header line itself is not data
                if n_banner_lines_seen > MAX_BANNER_LINES_SCANNED:
                    raise ValueError(f"no header row found in first {MAX_BANNER_LINES_SCANNED} lines of {path}")
                continue  # banner/dashes line, not data

            if not line.strip():
                continue
            parts = line.rstrip("\n").split("|")
            if len(parts) < n_cols:
                continue
            # Paginated export re-inserts the full header every ~30k rows
            # (12 occurrences confirmed on the real 2022 extract). Detect by
            # the projected Latitude slot literally reading "Latitude".
            if parts[req_idx[lat_pos_in_required]] == "Latitude":
                n_reinserted_headers += 1
                continue
            values = [parts[i].strip() or None for i in req_idx]
            batch.append((*values, year_tag))
            n_loaded += 1
            if len(batch) >= BATCH_SIZE:
                flush()
        flush()

    if header is None:
        raise ValueError(f"no header row found in {path}")
    if n_reinserted_headers:
        print(f"  {path.name}: skipped {n_reinserted_headers} re-inserted header rows")
    return n_loaded


def build_clean_table(con: duckdb.DuckDBPyConnection) -> int:
    """Filter raw_complaints to water/sewer/drainage cases with valid coordinates,
    dedupe by case_number, and materialize into the `complaints` table."""
    type_list = ", ".join(f"'{t}'" for t in WATER_CASE_TYPES)
    con.execute(f"""
        CREATE OR REPLACE TABLE {CLEAN_TABLE} AS
        SELECT DISTINCT ON (case_number)
            case_number,
            incident_address,
            latitude,
            longitude,
            status,
            TRY_CAST(created_date AS TIMESTAMP) AS created_at,
            TRY_CAST(closed_date AS TIMESTAMP) AS closed_at,
            incident_case_type,
            department,
            title,
            NULL AS segment_id
        FROM {RAW_TABLE}
        WHERE incident_case_type IN ({type_list})
          AND latitude IS NOT NULL AND longitude IS NOT NULL
          AND latitude BETWEEN 29.0 AND 30.3
          AND longitude BETWEEN -96.0 AND -94.8
        ORDER BY case_number, created_date DESC
    """)
    return con.execute(f"SELECT COUNT(*) FROM {CLEAN_TABLE}").fetchone()[0]


def ingest_all(db_path: Path, raw_dir: Path) -> None:
    con = duckdb.connect(str(db_path))
    total = 0
    for f in sorted(raw_dir.glob("311_*.txt")):
        n = load_extract(con, f, f.stem)
        print(f"  {f.name}: {n:,} rows")
        total += n
    print(f"raw_complaints total: {total:,}")
    n_clean = build_clean_table(con)
    print(f"complaints (water/sewer/drainage, geocoded, deduped): {n_clean:,}")
    con.close()


if __name__ == "__main__":
    import sys
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/pluvial.duckdb")
    raw = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/raw/houston_311")
    ingest_all(db, raw)
