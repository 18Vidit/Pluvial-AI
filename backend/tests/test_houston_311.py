from pathlib import Path

import duckdb
import pytest

from bellwether.ingest.houston_311 import build_clean_table, load_extract

HEADER = (
    "365 Case Number|Case Number|Incident Address|Latitude|Longitude|Status|"
    "Created Date Local|Closed Date|Title|Incident Case Type|SLA Time|Department"
)


def _write_synthetic_extract(path: Path) -> None:
    lines = [
        "--------------------------------------------",
        "----- 311 Public Data D365 - YTD - 2025 ----",
        "--------------------------------------------",
        "Extract date-time: [2026-08-22 06:00:03.26]",
        "--------------------------------------------",
        HEADER,
        # a real water leak case, geocoded
        "1|C-001|123 MAIN ST HOUSTON TX 77002|29.7604|-95.3698|Service Completed|"
        "2025-01-10 08:00:00.0000000|2025-01-11 09:00:00.000|Leak at curb|Water Leak|14 Days|Public Works",
        # a bulky-item case (should be filtered out — not water-related)
        "2|C-002|456 OAK ST HOUSTON TX 77003|29.7000|-95.4000|Service Completed|"
        "2025-01-10 08:00:00.0000000|2025-01-12 09:00:00.000|Old couch|Bulky Items|14 Days|Solid Waste",
        # a water case with no coordinates (should be filtered out)
        "3|C-003|789 ELM ST HOUSTON TX 77004|||Open|"
        "2025-01-11 08:00:00.0000000||No water|Water Service|14 Days|Public Works",
        # duplicate case_number with a later row (dedupe should keep the row, not double-count)
        "1|C-001|123 MAIN ST HOUSTON TX 77002|29.7604|-95.3698|Service Completed|"
        "2025-01-10 08:00:00.0000000|2025-01-11 09:00:00.000|Leak at curb (dup)|Water Leak|14 Days|Public Works",
    ]
    path.write_text("\n".join(lines), encoding="latin-1")


def test_load_and_clean_synthetic_extract(tmp_path: Path):
    extract = tmp_path / "311_2025.txt"
    _write_synthetic_extract(extract)

    con = duckdb.connect(":memory:")
    n_loaded = load_extract(con, extract, "311_2025")
    assert n_loaded == 4  # all raw rows loaded, including the non-water and no-coord ones

    n_clean = build_clean_table(con)
    # only the Water Leak case with valid coords survives, deduped to 1
    assert n_clean == 1

    rows = con.execute("SELECT case_number, incident_case_type, latitude, longitude FROM complaints").fetchall()
    assert rows == [("C-001", "Water Leak", 29.7604, -95.3698)]


def test_reinserted_header_rows_are_skipped(tmp_path: Path):
    """The real Houston export is paginated and re-inserts the full header
    line every ~30k rows (confirmed on the live 2022 extract: 12
    occurrences). A naive parser treats the repeated header as a data row
    and fails converting 'Latitude' to DOUBLE downstream."""
    extract = tmp_path / "311_2025.txt"
    lines = [
        "--------------------------------------------",
        HEADER,
        "1|C-001|123 MAIN ST HOUSTON TX 77002|29.7604|-95.3698|Service Completed|"
        "2025-01-10 08:00:00.0000000|2025-01-11 09:00:00.000|Leak at curb|Water Leak|14 Days|Public Works",
        HEADER,  # re-inserted mid-file, as the real extracts do
        "2|C-002|456 OAK ST HOUSTON TX 77003|29.7500|-95.3600|Service Completed|"
        "2025-01-12 08:00:00.0000000|2025-01-13 09:00:00.000|Another leak|Water Leak|14 Days|Public Works",
    ]
    extract.write_text("\n".join(lines), encoding="latin-1")

    con = duckdb.connect(":memory:")
    n_loaded = load_extract(con, extract, "311_2025")
    assert n_loaded == 2  # both real rows loaded, the re-inserted header is not

    build_clean_table(con)
    lats = con.execute("SELECT latitude FROM complaints ORDER BY case_number").fetchall()
    assert lats == [(29.7604,), (29.75,)]  # no 'Latitude' string leaked through as a row


def test_missing_header_raises(tmp_path: Path):
    bad_file = tmp_path / "bad.txt"
    bad_file.write_text("no header here\njust noise", encoding="latin-1")
    con = duckdb.connect(":memory:")
    with pytest.raises(ValueError):
        load_extract(con, bad_file, "bad")
