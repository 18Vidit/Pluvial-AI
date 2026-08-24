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

import sqlite3

import httpx

from bellwether.mireye.fields import is_soil_usable

NYC_311_ENDPOINT = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"

NYC_WATER_COMPLAINT_TYPES = ["Water System", "Water Quality", "Sewer"]


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
