"""The bundled GHCN station list is what makes moisture history national
rather than Houston-only. These tests check the resolution, not the data."""
from __future__ import annotations

from pluvial.ingest import ncei
from pluvial.ingest.stations import load_stations, nearest_station


def test_bundled_list_is_loaded_and_covers_the_country():
    stations = load_stations()
    assert len(stations) > 1000
    assert len({s.state for s in stations}) >= 50


def test_houston_still_resolves_into_the_houston_metro():
    """The original study area's series was pulled from IAH. A Houston
    address must land on a Houston station or the 117 backfilled moisture
    rows describe someone else's weather."""
    station, distance_m = nearest_station(29.7604, -95.3698)
    assert station.state == "TX"
    assert "HOUSTON" in station.name
    assert distance_m < 40_000


def test_the_legacy_houston_station_is_in_the_list():
    ids = {s.station_id for s in load_stations()}
    assert ncei.HOUSTON_STATION in ids


def test_far_apart_addresses_resolve_to_different_regions():
    denver, _ = nearest_station(39.7392, -104.9903)
    nyc, _ = nearest_station(40.7128, -74.0060)
    assert denver.station_id != nyc.station_id
    assert denver.state == "CO" and nyc.state == "NY"
