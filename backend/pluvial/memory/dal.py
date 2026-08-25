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

from pluvial.memory import migrations

SCHEMA_PATH = Path(__file__).parent / "schema_postgres.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(database_url: str | None = None) -> None:
    url = database_url or os.environ["DATABASE_URL"]
    con = psycopg.connect(url, row_factory=dict_row)
    with con.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text())
    con.commit()
    # Schema DDL is all CREATE ... IF NOT EXISTS, so it cannot change a
    # table that already exists. migrations.apply_all handles those.
    migrations.apply_all(con)
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
    con: psycopg.Connection, shrink_swell_class: str, trigger_state: str, symptom_class: str,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """as_of filters to verdicts decided on or before that moment. Only the
    eval harness passes it; without it a backtest frozen at T can retrieve
    precedents recorded after T, which is the future leaking into a
    prediction."""
    clause = "AND v.decided_at <= %s" if as_of else ""
    params: tuple = (shrink_swell_class, trigger_state, symptom_class)
    if as_of:
        params = params + (as_of,)
    rows = con.execute(
        f"""
        SELECT p.*, v.disposition AS verdict_disposition, v.decided_at
        FROM precedents p JOIN verdicts v ON v.verdict_id = p.verdict_id
        WHERE p.shrink_swell_class = %s AND p.trigger_state = %s AND p.symptom_class = %s
          {clause}
        ORDER BY v.decided_at DESC LIMIT 10
        """,
        params,
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
    trigger_state: str | None, usdm_class: str | None, region_key: str | None = None,
) -> None:
    """region_key defaults to station_id: the series IS the station's, and
    keeping them separate columns only matters if a region ever resolves to
    a station under a different label."""
    con.execute(
        """
        INSERT INTO moisture_history (region_key, date, station_id, precip_mm, tmax_c,
            antecedent_30d_mm, antecedent_60d_mm, antecedent_90d_mm, trigger_state, usdm_class)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(region_key, date) DO UPDATE SET
            station_id=excluded.station_id,
            precip_mm=excluded.precip_mm, tmax_c=excluded.tmax_c,
            antecedent_30d_mm=excluded.antecedent_30d_mm,
            antecedent_60d_mm=excluded.antecedent_60d_mm,
            antecedent_90d_mm=excluded.antecedent_90d_mm,
            trigger_state=excluded.trigger_state, usdm_class=excluded.usdm_class
        """,
        (region_key or station_id, date, station_id, precip_mm, tmax_c, a30, a60, a90, trigger_state, usdm_class),
    )


def current_trigger_state(
    con: psycopg.Connection, as_of: str | None = None, region_key: str | None = None
) -> Optional[dict[str, Any]]:
    """Latest moisture row for a region. region_key=None means "whatever
    region is in the store", which is what the Houston-only triage path and
    the backtest want; address mode always passes one."""
    clauses, params = [], []
    if region_key:
        clauses.append("region_key = %s")
        params.append(region_key)
    if as_of:
        clauses.append("date <= %s")
        params.append(as_of)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = con.execute(
        f"SELECT * FROM moisture_history {where} ORDER BY date DESC LIMIT 1", tuple(params)
    ).fetchone()
    return dict(row) if row else None


def moisture_region_days(con: psycopg.Connection, region_key: str) -> int:
    row = con.execute(
        "SELECT COUNT(*) AS n FROM moisture_history WHERE region_key = %s", (region_key,)
    ).fetchone()
    return int(row["n"])


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


# --- address mode: locations, samples, rulings -------------------------------

def create_location(
    con: psycopg.Connection, query_text: str, label: str | None,
    lat: float, lon: float, region_key: str | None,
) -> int:
    cur = con.execute(
        """
        INSERT INTO locations (query_text, label, lat, lon, region_key, geocoded_at)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING location_id
        """,
        (query_text, label, lat, lon, region_key, datetime.now(timezone.utc)),
    )
    return cur.fetchone()["location_id"]


def get_location(con: psycopg.Connection, location_id: int) -> Optional[dict[str, Any]]:
    row = con.execute("SELECT * FROM locations WHERE location_id = %s", (location_id,)).fetchone()
    return dict(row) if row else None


def create_samples(
    con: psycopg.Connection, location_id: int, points: list[tuple[str, float, float]]
) -> list[int]:
    """points: (role, lat, lon), in plan order. Rows are created unfetched —
    the map draws the plan before any credit is spent, and `profile_json`
    stays null until the user confirms."""
    ids = []
    for role, lat, lon in points:
        cur = con.execute(
            """
            INSERT INTO location_samples (location_id, role, lat, lon)
            VALUES (%s, %s, %s, %s) RETURNING sample_id
            """,
            (location_id, role, lat, lon),
        )
        ids.append(cur.fetchone()["sample_id"])
    return ids


def record_sample_profile(
    con: psycopg.Connection, sample_id: int, profile: dict[str, Any],
    soil_usable: bool, mireye_account: str | None,
) -> None:
    con.execute(
        """
        UPDATE location_samples
        SET profile_json = %s, soil_usable = %s, mireye_account = %s, fetched_at = %s
        WHERE sample_id = %s
        """,
        (Jsonb(profile), soil_usable, mireye_account, datetime.now(timezone.utc), sample_id),
    )


def location_samples(con: psycopg.Connection, location_id: int) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM location_samples WHERE location_id = %s ORDER BY sample_id",
        (location_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["profile"] = d.get("profile_json")
        out.append(d)
    return out


def get_sample(con: psycopg.Connection, sample_id: int) -> Optional[dict[str, Any]]:
    row = con.execute("SELECT * FROM location_samples WHERE sample_id = %s", (sample_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["profile"] = d.get("profile_json")
    return d


@dataclass
class ThreatRulingRecord:
    location_id: int
    threat: str
    severity: str
    reasoning: dict[str, Any]
    cited_evidence: list[dict[str, Any]]
    rejected_counter_argument: str | None
    invalidation_condition: dict[str, Any] | None
    agent_version: str
    reawakened_from: int | None = None


def record_threat_ruling(con: psycopg.Connection, r: ThreatRulingRecord) -> int:
    cur = con.execute(
        """
        INSERT INTO threat_rulings (
            location_id, threat, severity, reasoning_json, cited_evidence_json,
            rejected_counter_argument, invalidation_condition_json, agent_version,
            decided_at, reawakened_from
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING ruling_id
        """,
        (
            r.location_id, r.threat, r.severity, Jsonb(r.reasoning), Jsonb(r.cited_evidence),
            r.rejected_counter_argument,
            Jsonb(r.invalidation_condition) if r.invalidation_condition is not None else None,
            r.agent_version, datetime.now(timezone.utc), r.reawakened_from,
        ),
    )
    return cur.fetchone()["ruling_id"]


def location_rulings(con: psycopg.Connection, location_id: int) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT DISTINCT ON (threat) * FROM threat_rulings
        WHERE location_id = %s ORDER BY threat, decided_at DESC
        """,
        (location_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reasoning"] = d.pop("reasoning_json")
        d["cited_evidence"] = d.pop("cited_evidence_json")
        d["invalidation_condition"] = d.pop("invalidation_condition_json")
        out.append(d)
    return out


def open_rulings_with_invalidation(con: psycopg.Connection) -> list[dict[str, Any]]:
    """Address-mode analogue of open_verdicts_with_invalidation: rulings that
    came back below `high` and carry a condition worth re-checking. This is
    what turns an invalidation condition into "watch this address"."""
    rows = con.execute(
        """
        SELECT r.*, l.lat, l.lon, l.label, l.region_key
        FROM threat_rulings r JOIN locations l ON l.location_id = r.location_id
        WHERE r.severity IN ('low', 'elevated', 'unresolved')
          AND r.invalidation_condition_json IS NOT NULL
          AND r.ruling_id NOT IN (
              SELECT reawakened_from FROM threat_rulings WHERE reawakened_from IS NOT NULL
          )
        ORDER BY r.decided_at DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["invalidation_condition"] = d["invalidation_condition_json"]
        out.append(d)
    return out


# --- address mode: region search ---------------------------------------------

def create_region_search(
    con: psycopg.Connection, query_text: str, objective: dict[str, Any],
    bbox: dict[str, float], credit_budget: int,
) -> int:
    cur = con.execute(
        """
        INSERT INTO region_searches (query_text, objective_json, bbox, credit_budget, started_at)
        VALUES (%s, %s, %s, %s, %s) RETURNING search_id
        """,
        (query_text, Jsonb(objective), Jsonb(bbox), credit_budget, datetime.now(timezone.utc)),
    )
    return cur.fetchone()["search_id"]


def finish_region_search(
    con: psycopg.Connection, search_id: int, credits_spent: int, exhausted_budget: bool
) -> None:
    con.execute(
        """
        UPDATE region_searches SET credits_spent = %s, exhausted_budget = %s, finished_at = %s
        WHERE search_id = %s
        """,
        (credits_spent, exhausted_budget, datetime.now(timezone.utc), search_id),
    )


def create_region_cell(
    con: psycopg.Connection, search_id: int, level: int, lat: float, lon: float,
    bbox: dict[str, float],
) -> int:
    cur = con.execute(
        """
        INSERT INTO region_cells (search_id, level, lat, lon, bbox)
        VALUES (%s, %s, %s, %s, %s) RETURNING cell_id
        """,
        (search_id, level, lat, lon, Jsonb(bbox)),
    )
    return cur.fetchone()["cell_id"]


def record_cell_profile(
    con: psycopg.Connection, cell_id: int, profile: dict[str, Any],
    soil_usable: bool, objective_score: float | None,
) -> None:
    con.execute(
        """
        UPDATE region_cells SET profile_json = %s, soil_usable = %s, objective_score = %s
        WHERE cell_id = %s
        """,
        (Jsonb(profile), soil_usable, objective_score, cell_id),
    )


def mark_cell_subdivided(con: psycopg.Connection, cell_id: int) -> None:
    con.execute("UPDATE region_cells SET subdivided = TRUE WHERE cell_id = %s", (cell_id,))


def search_cells(con: psycopg.Connection, search_id: int) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT * FROM region_cells WHERE search_id = %s ORDER BY cell_id", (search_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["profile"] = d.get("profile_json")
        out.append(d)
    return out
