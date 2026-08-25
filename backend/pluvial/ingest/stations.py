"""Resolve a coordinate to the nearest NOAA GHCN daily station.

The station id is the `region_key` the whole address-mode moisture series
is keyed on. Houston was hardcoded (`ncei.HOUSTON_STATION`) when the study
area was one city; an address can be anywhere, so the station has to be
looked up.

The list is bundled, not fetched: `reference/ghcn_stations.csv` holds the 1,307
US airport (USW) stations that reported both PRCP and TMAX into 2025 or
later, filtered from NOAA's ghcnd-inventory. Airport stations are the ones
with clean, continuous daily summaries — the co-op network is denser but
much patchier, and a station that stopped reporting in 2003 is worse than
one 40km away that reports today.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pluvial.geo.sample_plan import haversine_m

STATIONS_CSV = Path(__file__).parent / "reference" / "ghcn_stations.csv"


@dataclass(frozen=True)
class Station:
    station_id: str
    lat: float
    lon: float
    state: str
    name: str


@lru_cache(maxsize=1)
def load_stations() -> tuple[Station, ...]:
    with STATIONS_CSV.open() as f:
        return tuple(
            Station(r["station_id"], float(r["lat"]), float(r["lon"]), r["state"], r["name"])
            for r in csv.DictReader(f)
        )


def nearest_station(lat: float, lon: float) -> tuple[Station, float]:
    """Nearest station and its distance in metres.

    A linear scan over 1,307 rows is ~1ms and runs once per query; a spatial
    index here would be complexity bought for nothing. The distance is
    returned rather than swallowed because it is the honest caveat on the
    moisture signal — a station 90km away is describing a different
    rainstorm than the one that fell on the property.
    """
    stations = load_stations()
    best = min(stations, key=lambda s: haversine_m(lat, lon, s.lat, s.lon))
    return best, haversine_m(lat, lon, best.lat, best.lon)
