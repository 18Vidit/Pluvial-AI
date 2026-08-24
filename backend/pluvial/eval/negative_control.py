"""Negative control (design spec §8): run the identical cascade against NYC
311 complaints, where SSURGO's dominant component is 'Urban land' at
78-92% of points (confirmed during design research). If the cascade still
flags confidently there, the physical signal is theatre and we report that
honestly rather than hiding it.

This module ingests a small NYC complaint sample and a handful of NYC
segment profiles (same field set, same wrapper) and runs them through the
unmodified cascade — no NYC-specific code path exists anywhere else in the
system, which is the point: the agents' own honesty gate (soil_usable)
should suppress soil claims on its own.
"""
from __future__ import annotations

import json
import sqlite3

import httpx

from pluvial.agents.cascade import run_cascade
from pluvial.agents.context import CascadeContext
from pluvial.mireye.client import MireyeAccount, MireyeClient, QuoteExceedsCeilingError, chunk_locations
from pluvial.mireye.fields import ALL_FIELDS, is_soil_usable
from pluvial.mireye.profile_job import extract_batch_result
from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget

NYC_311_ENDPOINT = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"

NYC_WATER_COMPLAINT_TYPES = ["Water System", "Water Quality", "Sewer"]

# NYC segment_ids are synthetic (design spec §8 note: no NYC OSM ingest
# exists — the negative control only needs a physical profile per complaint
# point, not a street-graph). Kept clearly out of Houston's real id space.
NYC_SYNTHETIC_ID_BASE = -1_000_000


def pull_nyc_sample(limit: int = 200) -> list[dict]:
    """Pull a small geocoded sample of recent NYC water/sewer complaints via
    the public Socrata API (no key required), for the negative-control run."""
    type_filter = " OR ".join(f"complaint_type='{t}'" for t in NYC_WATER_COMPLAINT_TYPES)
    with httpx.Client(timeout=30) as client:
        r = client.get(
            NYC_311_ENDPOINT,
            params={
                "$select": "unique_key,complaint_type,descriptor,latitude,longitude,created_date,resolution_description",
                "$where": f"latitude IS NOT NULL AND ({type_filter})",
                "$limit": limit,
                "$order": "created_date DESC",
            },
        )
        r.raise_for_status()
        return r.json()


def expected_soil_usable_rate(profiles: list[dict]) -> float:
    """Sanity check before running the cascade at all: what fraction of the
    NYC sample even HAS usable soil data. Design research found 3/12 —
    expect this to land in a similar low range, not near Houston's 11/12."""
    if not profiles:
        return 0.0
    usable = sum(1 for p in profiles if is_soil_usable(p))
    return usable / len(profiles)


def profile_nyc_sample(
    account: MireyeAccount, sample: list[dict], credit_ceiling: int
) -> list[dict]:
    """Fetch a live Mireye profile for each geocoded point in the sample.
    Quotes the whole batch up front and refuses to spend past
    credit_ceiling — same guard the Houston bulk job uses, since this
    shares the account's monthly allowance."""
    points = [
        (c["unique_key"], float(c["latitude"]), float(c["longitude"]))
        for c in sample
        if c.get("latitude") and c.get("longitude")
    ]
    if not points:
        return []

    profiles: list[dict] = []
    with MireyeClient(account) as client:
        chunks = chunk_locations(points, size=25)
        quoted = 0
        for chunk in chunks:
            q = client.quote(ALL_FIELDS, locations=len(chunk))
            quoted += int(q.get("credits") or q.get("total_credits") or len(ALL_FIELDS) * len(chunk))
        if quoted > credit_ceiling:
            raise QuoteExceedsCeilingError(quoted, credit_ceiling)

        for chunk in chunks:
            locs = [(lat, lon) for _, lat, lon in chunk]
            key_ids = "-".join(str(k) for k, _, _ in sorted(chunk))
            resp = client.fetch_batch(
                ALL_FIELDS, locs, idempotency_key=f"nyc-negative-control-{key_ids}"
            )
            results = resp.get("results") or resp.get("locations") or []
            for (unique_key, lat, lon), result in zip(chunk, results):
                values = extract_batch_result(result)
                profiles.append({"unique_key": unique_key, "lat": lat, "lon": lon, "profile": values})
    return profiles


async def run_negative_control(
    con: sqlite3.Connection, account: MireyeAccount, profiled_sample: list[dict], guidance_version: int
) -> dict:
    """Run the unmodified cascade over each profiled NYC point. No
    NYC-specific code path exists here or anywhere else — the point is that
    the agents' own honesty gate (soil_usable) should suppress soil claims
    on its own, not that we told them to. Reports, per case, whether a soil
    claim was made despite soil_usable=False — that would mean the gate
    failed and the physical signal is theatre, which we report honestly."""
    results = []
    with MireyeClient(account) as client:
        for i, point in enumerate(profiled_sample):
            segment_id = NYC_SYNTHETIC_ID_BASE - i
            soil_usable = is_soil_usable(point["profile"])
            con.execute(
                """
                INSERT INTO segments (segment_id, name, highway_class, centroid_lat, centroid_lon,
                                       profile_json, soil_usable, profiled_at, mireye_account)
                VALUES (?, NULL, NULL, ?, ?, ?, ?, datetime('now'), 'nyc-negative-control')
                ON CONFLICT(segment_id) DO NOTHING
                """,
                (segment_id, point["lat"], point["lon"], _to_json(point["profile"]), int(soil_usable)),
            )
            con.commit()

            ctx = CascadeContext(
                con=con,
                # profile already stored above so this hits cache; ceiling=0
                # means any *other* segment the agent tries (e.g. usgs_gage's
                # synthetic point) fails cleanly via CreditCeilingExceeded
                # rather than spending against this account mid-eval.
                mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=0)),
                run_budget=RunBudget(ceiling=0),
                guidance_version=guidance_version,
                frozen_at=None,
            )
            complaint_summary = f"NYC 311 complaint {point['unique_key']} near ({point['lat']}, {point['lon']})"
            dossier_summary = f"segment_id={segment_id}, soil_usable={soil_usable}"

            try:
                _, verdict, investigator_out, _ = await run_cascade(con, ctx, complaint_summary, dossier_summary)
            except Exception as e:
                results.append({"segment_id": segment_id, "error": repr(e)})
                continue

            soil_claim_made = bool(investigator_out) and any(
                "soil" in c.field.lower() and c.field != "soil_usable" for c in investigator_out.claims
            )
            results.append({
                "segment_id": segment_id,
                "soil_usable": soil_usable,
                "disposition": verdict.disposition if verdict else "discarded_at_triage",
                "false_soil_claim": soil_claim_made and not soil_usable,
            })

    n_leaked = sum(1 for r in results if r.get("false_soil_claim"))
    return {
        "n": len(results),
        "n_soil_usable": sum(1 for r in results if r.get("soil_usable")),
        "n_false_soil_claims": n_leaked,
        "results": results,
    }


def _to_json(profile: dict) -> str:
    return json.dumps(profile)
