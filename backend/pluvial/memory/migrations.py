"""In-place migrations for tables that already exist with data.

`CREATE TABLE IF NOT EXISTS` in schema_postgres.sql covers new tables and
fresh databases, but it is a no-op against a table that is already there —
so a column added or a primary key repinned in that file never reaches the
live Neon database. These functions close that gap. Every one is idempotent
and safe to run on every startup; `dal.init_db()` calls `apply_all` right
after applying the schema.
"""
from __future__ import annotations

import psycopg

# The station that produced every moisture_history row written before
# address mode existed: Houston Bush Intercontinental (IAH). Backfilling
# with it is not a guess — pluvial/ingest/ncei.py hardcoded it.
LEGACY_REGION_KEY = "USW00012960"


def apply_all(con: psycopg.Connection) -> None:
    _moisture_history_region_key(con)


def _moisture_history_region_key(con: psycopg.Connection) -> None:
    """Repin moisture_history from (date) to (region_key, date).

    Address mode can be asked about any US address, so the moisture series
    has to be per-region. Existing rows are all Houston IAH and are
    backfilled as such before the key changes — no row is dropped and no
    value is invented.
    """
    exists = con.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'moisture_history'
          AND column_name = 'region_key'
        """
    ).fetchone()
    if exists:
        return

    con.execute("ALTER TABLE moisture_history ADD COLUMN region_key TEXT")
    con.execute(
        "UPDATE moisture_history SET region_key = COALESCE(station_id, %s)",
        (LEGACY_REGION_KEY,),
    )
    con.execute("ALTER TABLE moisture_history ALTER COLUMN region_key SET NOT NULL")
    # The old PK's constraint name is whatever Postgres generated; look it
    # up rather than assuming moisture_history_pkey.
    row = con.execute(
        """
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'moisture_history'::regclass AND contype = 'p'
        """
    ).fetchone()
    if row:
        con.execute(f'ALTER TABLE moisture_history DROP CONSTRAINT "{row["conname"]}"')
    con.execute("ALTER TABLE moisture_history ADD PRIMARY KEY (region_key, date)")
    con.commit()
