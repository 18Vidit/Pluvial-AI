"""One-off migration: copy the SQLite memory store into Neon Postgres.

Design spec §"Migration / backfill":
https://github.com — see docs/superpowers/specs/2026-08-24-neon-postgres-port-design.md

Run once, against the real data/pluvial.db, after DATABASE_URL is set and
the Postgres schema (schema_postgres.sql) has been applied. Streams each
table in batches, converts SQLite TEXT-JSON -> Python dict/list and
INTEGER 0/1 -> bool in flight, and verifies row counts match at the end.
The source SQLite file is never modified — this script only reads it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from pluvial.memory import dal

load_dotenv()

app = typer.Typer(help="Migrate the SQLite memory store into Neon Postgres")

DEFAULT_SQLITE = Path(__file__).resolve().parents[2] / "data" / "pluvial.db"

# (table, columns-in-insert-order, json-columns, bool-columns)
TABLES: list[tuple[str, list[str], list[str], list[str]]] = [
    (
        "segments",
        ["segment_id", "name", "highway_class", "centroid_lat", "centroid_lon",
         "profile_json", "soil_usable", "profiled_at", "mireye_account"],
        ["profile_json"],
        ["soil_usable"],
    ),
    (
        "moisture_history",
        ["date", "station_id", "precip_mm", "tmax_c", "antecedent_30d_mm",
         "antecedent_60d_mm", "antecedent_90d_mm", "trigger_state", "usdm_class"],
        [],
        [],
    ),
    (
        "complaints",
        ["case_number", "segment_id", "incident_case_type", "title", "status",
         "latitude", "longitude", "created_at", "closed_at"],
        [],
        [],
    ),
    (
        "verdicts",
        ["verdict_id", "segment_id", "case_numbers", "disposition", "priority",
         "reasoning_json", "cited_evidence_json", "rejected_counter_argument",
         "invalidation_condition_json", "agent_version", "decided_at",
         "reawakened_from", "frozen_at"],
        ["case_numbers", "reasoning_json", "cited_evidence_json", "invalidation_condition_json"],
        [],
    ),
    (
        "outcomes",
        ["outcome_id", "verdict_id", "observed_outcome", "label", "observed_at"],
        [],
        [],
    ),
    (
        "calibration",
        ["version", "run_at", "metrics_json", "reporting_bias_json", "guidance_diff"],
        ["metrics_json", "reporting_bias_json"],
        [],
    ),
    (
        "precedents",
        ["verdict_id", "shrink_swell_class", "trigger_state", "symptom_class", "disposition", "label"],
        [],
        [],
    ),
]


def _convert_row(row: sqlite3.Row, columns: list[str], json_cols: list[str], bool_cols: list[str]) -> tuple:
    out = []
    for col in columns:
        v = row[col]
        if col in json_cols and v is not None:
            v = Jsonb(json.loads(v))
        elif col in bool_cols and v is not None:
            v = bool(v)
        out.append(v)
    return tuple(out)


@app.command()
def migrate(sqlite_path: Path = DEFAULT_SQLITE, batch_size: int = 2000, apply_schema: bool = True) -> None:
    if apply_schema:
        dal.init_db()
        typer.echo("applied schema_postgres.sql")

    sconn = sqlite3.connect(str(sqlite_path))
    sconn.row_factory = sqlite3.Row

    with dal.connect() as pcon:
        for table, columns, json_cols, bool_cols in TABLES:
            rows = sconn.execute(f"SELECT * FROM {table}").fetchall()
            placeholders = ", ".join(["%s"] * len(columns))
            col_list = ", ".join(columns)
            with pcon.cursor() as cur:
                for i in range(0, len(rows), batch_size):
                    batch = [
                        _convert_row(r, columns, json_cols, bool_cols)
                        for r in rows[i : i + batch_size]
                    ]
                    if not batch:
                        continue
                    cur.executemany(
                        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                        batch,
                    )
            typer.echo(f"{table}: inserted {len(rows)} rows")

        # sequences advance from explicit-id inserts only on some drivers;
        # bump them explicitly so future INSERTs without an id don't collide.
        for table, id_col in (("verdicts", "verdict_id"), ("outcomes", "outcome_id"), ("calibration", "version")):
            pcon.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{id_col}'), "
                f"COALESCE((SELECT MAX({id_col}) FROM {table}), 1))"
            )

    typer.echo("verifying row counts...")
    mismatches = []
    with dal.connect() as pcon:
        for table, *_ in TABLES:
            sqlite_n = sconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            pg_n = pcon.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            status = "OK" if sqlite_n == pg_n else "MISMATCH"
            typer.echo(f"  {table}: sqlite={sqlite_n} postgres={pg_n} [{status}]")
            if sqlite_n != pg_n:
                mismatches.append(table)

    sconn.close()
    if mismatches:
        typer.echo(f"row count mismatch in: {mismatches}", err=True)
        raise typer.Exit(1)
    typer.echo("migration complete, all row counts match")


if __name__ == "__main__":
    app()
