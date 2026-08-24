"""Phase 2 bulk pre-profiling: fetch the ~24-field Mireye profile for a
stratified sample of Houston street segments, once, cached forever.

Deliberately separate from the agent-facing wrapper: this is a batch ETL
job run by a human before any agent executes, not something an agent
triggers. It shards work across the team's Mireye accounts by geography
(design spec §9) and always quotes the full job before spending anything.
"""
from __future__ import annotations

import hashlib
import time
from typing import Sequence

from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount, MireyeClient, MireyeError, chunk_locations
from pluvial.mireye.fields import ALL_FIELDS, is_soil_usable


def select_stratified_segments(
    duck_con, n_target: int = 2000
) -> list[tuple[int, float, float, str | None, str | None]]:
    """Pick segments to profile: every segment that has at least one
    complaint (so we never pay for ground nobody has reported on) prioritised
    by complaint count, up to n_target. Returns (segment_id, lat, lng, name,
    highway_class)."""
    rows = duck_con.execute(
        """
        SELECT s.segment_id, s.name, s.highway_class,
               (ST_XMin(s.geom) + ST_XMax(s.geom)) / 2 AS lon,
               (ST_YMin(s.geom) + ST_YMax(s.geom)) / 2 AS lat,
               COUNT(c.case_number) AS n_complaints
        FROM street_segments s
        JOIN complaints c ON c.segment_id = s.segment_id
        GROUP BY s.segment_id, s.name, s.highway_class, lon, lat
        ORDER BY n_complaints DESC, s.segment_id ASC
        LIMIT ?
        """,
        [n_target],
    ).fetchall()
    return [(r[0], r[4], r[3], r[1], r[2]) for r in rows]


def shard_by_longitude(
    segments: Sequence[tuple[int, float, float, str | None, str | None]], n_shards: int
) -> list[list[tuple[int, float, float, str | None, str | None]]]:
    """Split the study area into n_shards geographic thirds by longitude,
    one per sharded Mireye account, so no single account's rate limit or
    monthly allowance gates the whole job."""
    sorted_segs = sorted(segments, key=lambda s: s[1])  # by lon
    shard_size = -(-len(sorted_segs) // n_shards)  # ceil div
    return [sorted_segs[i : i + shard_size] for i in range(0, len(sorted_segs), shard_size)]


def run_profiling_shard(
    account: MireyeAccount,
    segments: Sequence[tuple[int, float, float, str | None, str | None]],
    monthly_ceiling: int,
    idempotency_salt: str = "",
    batch_size: int = 25,
) -> None:
    dal.init_db()
    with dal.connect() as con, MireyeClient(account, timeout=60.0) as client:
        for seg_id, lat, lon, name, hwy in segments:
            dal.upsert_segment(con, seg_id, name, hwy, lat, lon)
        con.commit()

        already_profiled = {
            s[0] for s in segments if (cached := dal.get_segment(con, s[0])) and cached.get("profile")
        }
        if already_profiled:
            print(f"[{account.label}] skipping {len(already_profiled)} already-profiled segments", flush=True)

        to_fetch = [(s[0], s[1], s[2]) for s in segments if s[0] not in already_profiled]
        chunks = chunk_locations(to_fetch, size=batch_size)

        total_quoted = 0
        for chunk in chunks:
            q = client.quote(ALL_FIELDS, locations=len(chunk))
            total_quoted += int(q.get("credits") or q.get("total_credits") or len(ALL_FIELDS) * len(chunk))
        print(f"[{account.label}] quoted total for {len(to_fetch)} segments: {total_quoted} credits", flush=True)
        if total_quoted > monthly_ceiling:
            raise RuntimeError(
                f"[{account.label}] quoted {total_quoted} credits exceeds ceiling {monthly_ceiling}; "
                "reduce n_target or split further before spending anything"
            )

        for i, chunk in enumerate(chunks):
            locs = [(lat, lon) for _, lat, lon in chunk]
            content_key = ",".join(str(seg_id) for seg_id, _, _ in sorted(chunk))
            digest = hashlib.sha256(content_key.encode()).hexdigest()[:16]
            print(f"[{account.label}] fetching chunk {i + 1}/{len(chunks)} ({len(chunk)} locations)...", flush=True)
            try:
                resp = client.fetch_batch(
                    ALL_FIELDS, locs, idempotency_key=f"{account.label}-profile{idempotency_salt}-{digest}"
                )
            except MireyeError as e:
                # One pathological batch (e.g. a location Mireye can't
                # resolve) shouldn't block the rest of the study area —
                # skip it and keep going; it'll retry as a cache miss the
                # next time this shard is run.
                print(f"[{account.label}] chunk {i + 1}/{len(chunks)} failed, skipping: {e}", flush=True)
                continue
            results = resp.get("results") or resp.get("locations") or []
            for (seg_id, lat, lon), result in zip(chunk, results):
                values = extract_batch_result(result)
                soil_usable = is_soil_usable(values)
                dal.upsert_segment(
                    con, seg_id, None, None, lat, lon,
                    profile=values, soil_usable=soil_usable, mireye_account=account.label,
                )
            con.commit()
            print(f"[{account.label}] profiled chunk {i + 1}/{len(chunks)}", flush=True)
            time.sleep(1.1)  # 60 req/min ceiling, one batch call per chunk


def extract_batch_result(result: dict) -> dict:
    if not result.get("ok", True):
        return {}
    fields = result.get("fields") or result.get("data") or {}
    out = {}
    for name, entry in fields.items():
        if isinstance(entry, dict) and "value" in entry:
            out[name] = {"value": entry.get("value"), "source": entry.get("source")}
        else:
            out[name] = {"value": entry, "source": None}
    return out
