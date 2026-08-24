"""Data access layer for the Pluvial-AI memory store (Neon Postgres).

All reads/writes to memory go through here — no raw SQL anywhere else in
the codebase, per the implementation plan. Keeping it typed and centralised
matters because memory is itself a graded artefact: reviewers should be able
to inspect exactly how a verdict was recorded and how calibration changed it.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(database_url: str | None = None) -> None:
    url = database_url or os.environ["DATABASE_URL"]
    con = psycopg.connect(url)
    with con.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text())
    con.commit()
    con.close()


@contextmanager
def connect(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    # DATABASE_URL points at Neon's pooled (PgBouncer transaction-mode)
    # endpoint. Session-level state like search_path can leak across
    # logical connections there, so pin it explicitly on every connect
    # rather than trusting whatever a pooled backend was left with.
    url = database_url or os.environ["DATABASE_URL"]
    con = psycopg.connect(url, row_factory=dict_row)
    con.execute("SET search_path TO public")
    try:
        yield con
        con.commit()
    finally:
        con.close()


# --- segments --------------------------------------------------------------

def upsert_segment(
    con: psycopg.Connection,
    segment_id: int,
    name: str | None,
    highway_class: str | None,
    centroid_lat: float,
    centroid_lon: float,
    profile: dict[str, Any] | None = None,
    soil_usable: bool | None = None,
    mireye_account: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO segments (segment_id, name, highway_class, centroid_lat, centroid_lon,
                               profile_json, soil_usable, profiled_at, mireye_account)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(segment_id) DO UPDATE SET
            name=COALESCE(excluded.name, segments.name),
            highway_class=COALESCE(excluded.highway_class, segments.highway_class),
            profile_json=COALESCE(excluded.profile_json, segments.profile_json),
            soil_usable=COALESCE(excluded.soil_usable, segments.soil_usable),
            profiled_at=COALESCE(excluded.profiled_at, segments.profiled_at),
            mireye_account=COALESCE(excluded.mireye_account, segments.mireye_account)
        """,
        (
            segment_id, name, highway_class, centroid_lat, centroid_lon,
            Jsonb(profile) if profile is not None else None,
            soil_usable,
            datetime.now(timezone.utc) if profile is not None else None,
            mireye_account,
        ),
    )


def get_segment(con: psycopg.Connection, segment_id: int) -> Optional[dict[str, Any]]:
    row = con.execute("SELECT * FROM segments WHERE segment_id = %s", (segment_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("profile_json"):
        d["profile"] = d["profile_json"]
    return d


def unprofiled_segments(con: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT segment_id, centroid_lat, centroid_lon FROM segments WHERE profile_json IS NULL LIMIT %s",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- complaints --------------------------------------------------------------

def upsert_segment_stubs_bulk(con: psycopg.Connection, rows: list[tuple]) -> None:
    """rows: (segment_id, name, highway_class, centroid_lat, centroid_lon).
    Used to backfill the FK target for a bulk complaint sync — never
    overwrites a profile already fetched through Mireye."""
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO segments (segment_id, name, highway_class, centroid_lat, centroid_lon)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(segment_id) DO UPDATE SET
                name=excluded.name, highway_class=excluded.highway_class
            """,
            rows,
        )


def upsert_complaints_bulk(con: psycopg.Connection, rows: list[tuple]) -> None:
    """rows: (case_number, segment_id, incident_case_type, title, status,
    latitude, longitude, created_at, closed_at)."""
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO complaints (case_number, segment_id, incident_case_type, title, status,
                                     latitude, longitude, created_at, closed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(case_number) DO UPDATE SET
                segment_id=excluded.segment_id, incident_case_type=excluded.incident_case_type,
                title=excluded.title, status=excluded.status, latitude=excluded.latitude,
                longitude=excluded.longitude, created_at=excluded.created_at, closed_at=excluded.closed_at
            """,
            rows,
        )


def neighbourhood_complaints(
    con: psycopg.Connection, segment_id: int, days: int = 30, exclude_case: str | None = None
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = con.execute(
        """
        SELECT * FROM complaints
        WHERE segment_id = %s
          AND created_at >= %s
          AND case_number != COALESCE(%s, '')
        ORDER BY created_at DESC
        """,
        (segment_id, cutoff, exclude_case),
    ).fetchall()
    return [dict(r) for r in rows]


# --- verdicts ----------------------------------------------------------------

@dataclass
class VerdictRecord:
    segment_id: int
    case_numbers: list[str]
    disposition: str  # dispatch | inspect | monitor | close
    priority: str | None
    reasoning: dict[str, Any]
    cited_evidence: list[dict[str, Any]]
    rejected_counter_argument: str | None
    invalidation_condition: dict[str, Any] | None
    agent_version: str
    reawakened_from: int | None = None
    frozen_at: str | None = None  # set only by the backtest harness


def record_verdict(con: psycopg.Connection, v: VerdictRecord) -> int:
    cur = con.execute(
        """
        INSERT INTO verdicts (
            segment_id, case_numbers, disposition, priority, reasoning_json,
            cited_evidence_json, rejected_counter_argument, invalidation_condition_json,
            agent_version, decided_at, reawakened_from, frozen_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING verdict_id
        """,
        (
            v.segment_id, Jsonb(v.case_numbers), v.disposition, v.priority,
            Jsonb(v.reasoning), Jsonb(v.cited_evidence),
            v.rejected_counter_argument,
            Jsonb(v.invalidation_condition) if v.invalidation_condition is not None else None,
            v.agent_version, datetime.now(timezone.utc), v.reawakened_from, v.frozen_at,
        ),
    )
    return cur.fetchone()["verdict_id"]


def open_verdicts_with_invalidation(con: psycopg.Connection) -> list[dict[str, Any]]:
    """Verdicts that closed/monitored a case and carry a condition to re-check."""
    rows = con.execute(
        """
        SELECT * FROM verdicts
        WHERE disposition IN ('close', 'monitor')
          AND invalidation_condition_json IS NOT NULL
          AND verdict_id NOT IN (SELECT reawakened_from FROM verdicts WHERE reawakened_from IS NOT NULL)
        ORDER BY decided_at DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["invalidation_condition"] = d["invalidation_condition_json"]
        out.append(d)
    return out


def recent_verdicts(con: psycopg.Connection, segment_id: int, limit: int = 10) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT verdict_id, disposition, priority, decided_at, reasoning_json FROM verdicts
        WHERE segment_id = %s ORDER BY decided_at DESC LIMIT %s
        """,
        (segment_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def precedent_search(
    con: psycopg.Connection, shrink_swell_class: str, trigger_state: str, symptom_class: str
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT p.*, v.disposition AS verdict_disposition, v.decided_at
        FROM precedents p JOIN verdicts v ON v.verdict_id = p.verdict_id
        WHERE p.shrink_swell_class = %s AND p.trigger_state = %s AND p.symptom_class = %s
        ORDER BY v.decided_at DESC LIMIT 10
        """,
        (shrink_swell_class, trigger_state, symptom_class),
    ).fetchall()
    return [dict(r) for r in rows]


def complaints_by_case_numbers(con: psycopg.Connection, case_numbers: list[str]) -> list[dict[str, Any]]:
    if not case_numbers:
        return []
    rows = con.execute(
        "SELECT * FROM complaints WHERE case_number = ANY(%s)",
        (case_numbers,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- moisture ------------------------------------------------------------

def upsert_moisture_day(
    con: psycopg.Connection, date: str, station_id: str, precip_mm: float | None,
    tmax_c: float | None, a30: float | None, a60: float | None, a90: float | None,
    trigger_state: str | None, usdm_class: str | None,
) -> None:
    con.execute(
        """
        INSERT INTO moisture_history (date, station_id, precip_mm, tmax_c,
            antecedent_30d_mm, antecedent_60d_mm, antecedent_90d_mm, trigger_state, usdm_class)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(date) DO UPDATE SET
            precip_mm=excluded.precip_mm, tmax_c=excluded.tmax_c,
            antecedent_30d_mm=excluded.antecedent_30d_mm,
            antecedent_60d_mm=excluded.antecedent_60d_mm,
            antecedent_90d_mm=excluded.antecedent_90d_mm,
            trigger_state=excluded.trigger_state, usdm_class=excluded.usdm_class
        """,
        (date, station_id, precip_mm, tmax_c, a30, a60, a90, trigger_state, usdm_class),
    )


def current_trigger_state(con: psycopg.Connection, as_of: str | None = None) -> Optional[dict[str, Any]]:
    if as_of:
        row = con.execute(
            "SELECT * FROM moisture_history WHERE date <= %s ORDER BY date DESC LIMIT 1", (as_of,)
        ).fetchone()
    else:
        row = con.execute("SELECT * FROM moisture_history ORDER BY date DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# --- calibration ------------------------------------------------------------

def record_calibration(
    con: psycopg.Connection, metrics: dict[str, Any], reporting_bias: dict[str, Any] | None,
    guidance_diff: str,
) -> int:
    cur = con.execute(
        """
        INSERT INTO calibration (run_at, metrics_json, reporting_bias_json, guidance_diff)
        VALUES (%s, %s, %s, %s) RETURNING version
        """,
        (datetime.now(timezone.utc), Jsonb(metrics), Jsonb(reporting_bias) if reporting_bias is not None else None, guidance_diff),
    )
    return cur.fetchone()["version"]


def latest_guidance_version(con: psycopg.Connection) -> int:
    row = con.execute("SELECT MAX(version) AS v FROM calibration").fetchone()
    return row["v"] or 0


def calibration_history(con: psycopg.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT version, run_at, guidance_diff FROM calibration ORDER BY version ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def frozen_guidance_version(con: psycopg.Connection, frozen_at: str) -> int:
    row = con.execute("SELECT MAX(version) AS v FROM calibration WHERE run_at <= %s", (frozen_at,)).fetchone()
    return row["v"] or 0


# --- outcomes ------------------------------------------------------------

def unlabelled_verdicts(con: psycopg.Connection, recurrence_days: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=recurrence_days)
    rows = con.execute(
        """
        SELECT v.verdict_id, v.segment_id, v.decided_at FROM verdicts v
        LEFT JOIN outcomes o ON o.verdict_id = v.verdict_id
        WHERE o.outcome_id IS NULL AND v.decided_at <= %s
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def escalating_complaint(
    con: psycopg.Connection, segment_id: int, after: str, before: str, case_types: list[str]
) -> Optional[dict[str, Any]]:
    row = con.execute(
        """
        SELECT case_number, incident_case_type, created_at FROM complaints
        WHERE segment_id = %s AND created_at > %s AND created_at <= %s
          AND incident_case_type = ANY(%s)
        LIMIT 1
        """,
        (segment_id, after, before, case_types),
    ).fetchone()
    return dict(row) if row else None


def repeat_complaint(
    con: psycopg.Connection, segment_id: int, after: str, before: str, exclude_case: str | None = None
) -> Optional[dict[str, Any]]:
    row = con.execute(
        """
        SELECT case_number FROM complaints
        WHERE segment_id = %s AND created_at > %s AND created_at <= %s
          AND case_number != COALESCE(%s, '')
        LIMIT 1
        """,
        (segment_id, after, before, exclude_case),
    ).fetchone()
    return dict(row) if row else None


def record_outcome(con: psycopg.Connection, verdict_id: int, observed_outcome: str, label: str) -> None:
    con.execute(
        "INSERT INTO outcomes (verdict_id, observed_outcome, label, observed_at) VALUES (%s, %s, %s, %s)",
        (verdict_id, observed_outcome, label, datetime.now(timezone.utc)),
    )


def verdicts_with_outcomes(con: psycopg.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT v.verdict_id, v.disposition, v.reasoning_json, v.segment_id, v.decided_at, o.label
        FROM verdicts v JOIN outcomes o ON o.verdict_id = v.verdict_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def segments_with_complaint_counts(con: psycopg.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT s.segment_id, COUNT(c.case_number) AS n_complaints, s.profile_json
        FROM segments s LEFT JOIN complaints c ON c.segment_id = s.segment_id
        WHERE s.profile_json IS NOT NULL
        GROUP BY s.segment_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


# --- backtest --------------------------------------------------------------

def complaints_up_to(con: psycopg.Connection, frozen_at: str) -> list[dict[str, Any]]:
    """Backtest candidates, oldest first.

    case_number is a tiebreaker, not decoration: thousands of complaints
    share an identical created_at, so ordering on created_at alone leaves
    the row order to the engine. That made "take the first N" select a
    different N under SQLite than under Postgres, and non-reproducible
    across runs on either. The tiebreaker makes a backtest re-runnable."""
    rows = con.execute(
        "SELECT * FROM complaints WHERE created_at <= %s ORDER BY created_at, case_number",
        (frozen_at,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- live loop ---------------------------------------------------------------

def covered_case_numbers(con: psycopg.Connection) -> set[str]:
    covered: set[str] = set()
    for row in con.execute("SELECT case_numbers FROM verdicts"):
        covered.update(row["case_numbers"])
    return covered


def uncovered_complaints_on_profiled_segments(
    con: psycopg.Connection, since: str | None, until: str | None
) -> list[dict[str, Any]]:
    query = """
        SELECT c.* FROM complaints c
        JOIN segments s ON s.segment_id = c.segment_id
        WHERE s.profile_json IS NOT NULL
    """
    params: list = []
    if since:
        query += " AND c.created_at >= %s"
        params.append(since)
    if until:
        query += " AND c.created_at <= %s"
        params.append(until)
    query += " ORDER BY c.created_at DESC"
    rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# --- negative control --------------------------------------------------------

def insert_synthetic_segment(
    con: psycopg.Connection, segment_id: int, lat: float, lon: float,
    profile: dict[str, Any], soil_usable: bool, mireye_account: str,
) -> None:
    con.execute(
        """
        INSERT INTO segments (segment_id, name, highway_class, centroid_lat, centroid_lon,
                               profile_json, soil_usable, profiled_at, mireye_account)
        VALUES (%s, NULL, NULL, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(segment_id) DO NOTHING
        """,
        (segment_id, lat, lon, Jsonb(profile), soil_usable, datetime.now(timezone.utc), mireye_account),
    )


# --- API surface -------------------------------------------------------------

def queue_cards(con: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT v.verdict_id, v.segment_id, v.disposition, v.priority, v.decided_at,
               v.reasoning_json, v.cited_evidence_json, v.rejected_counter_argument,
               v.invalidation_condition_json, v.reawakened_from,
               s.name AS segment_name, s.centroid_lat, s.centroid_lon
        FROM verdicts v
        JOIN segments s ON s.segment_id = v.segment_id
        WHERE v.verdict_id IN (
            SELECT MAX(verdict_id) FROM verdicts GROUP BY segment_id
        )
        ORDER BY
            CASE v.disposition WHEN 'dispatch' THEN 0 WHEN 'inspect' THEN 1 WHEN 'monitor' THEN 2 ELSE 3 END,
            v.decided_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def segment_complaints(con: psycopg.Connection, segment_id: int) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM complaints WHERE segment_id = %s ORDER BY created_at DESC", (segment_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def segment_verdicts(con: psycopg.Connection, segment_id: int, limit: int | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM verdicts WHERE segment_id = %s ORDER BY decided_at DESC"
    params: list = [segment_id]
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def segments_near_bbox(
    con: psycopg.Connection, lat_min: float, lat_max: float, lon_min: float, lon_max: float
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT segment_id, name, centroid_lat, centroid_lon
        FROM segments
        WHERE centroid_lat BETWEEN %s AND %s AND centroid_lon BETWEEN %s AND %s
        """,
        (lat_min, lat_max, lon_min, lon_max),
    ).fetchall()
    return [dict(r) for r in rows]


def stats_counts(con: psycopg.Connection) -> dict[str, Any]:
    segments_profiled = con.execute(
        "SELECT COUNT(*) AS n FROM segments WHERE profile_json IS NOT NULL AND segment_id > 0"
    ).fetchone()["n"]
    soil_usable = con.execute(
        "SELECT COUNT(*) AS n FROM segments WHERE soil_usable = TRUE AND segment_id > 0"
    ).fetchone()["n"]
    complaints = con.execute("SELECT COUNT(*) AS n FROM complaints").fetchone()["n"]
    verdicts_total = con.execute("SELECT COUNT(*) AS n FROM verdicts").fetchone()["n"]
    reawakened = con.execute(
        "SELECT COUNT(*) AS n FROM verdicts WHERE reawakened_from IS NOT NULL"
    ).fetchone()["n"]
    dispositions = {
        r["disposition"]: r["n"]
        for r in con.execute("SELECT disposition, COUNT(*) AS n FROM verdicts GROUP BY disposition")
    }
    outcomes = {
        r["label"]: r["n"]
        for r in con.execute("SELECT label, COUNT(*) AS n FROM outcomes GROUP BY label")
    }
    return {
        "segments_profiled": segments_profiled,
        "soil_usable": soil_usable,
        "complaints": complaints,
        "verdicts": verdicts_total,
        "reawakened": reawakened,
        "dispositions": dispositions,
        "outcomes": outcomes,
    }


def list_verdicts_brief(con: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT v.verdict_id, v.segment_id, v.disposition, v.priority, v.decided_at,
               v.reawakened_from, s.name AS segment_name
        FROM verdicts v JOIN segments s ON s.segment_id = v.segment_id
        ORDER BY v.decided_at DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_verdict(con: psycopg.Connection, verdict_id: int) -> Optional[dict[str, Any]]:
    row = con.execute("SELECT * FROM verdicts WHERE verdict_id = %s", (verdict_id,)).fetchone()
    return dict(row) if row else None


def verdict_brief(con: psycopg.Connection, verdict_id: int) -> Optional[dict[str, Any]]:
    row = con.execute(
        "SELECT verdict_id, disposition, priority, decided_at FROM verdicts WHERE verdict_id = %s",
        (verdict_id,),
    ).fetchone()
    return dict(row) if row else None


def get_complaint(con: psycopg.Connection, case_number: str) -> Optional[dict[str, Any]]:
    row = con.execute("SELECT * FROM complaints WHERE case_number = %s", (case_number,)).fetchone()
    return dict(row) if row else None
