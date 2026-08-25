"""Pure-function tests for the sampling geometry. No network, no database —
the plan is decided before anything is quoted, so it is testable in
isolation, which is the point of keeping it pure."""
from __future__ import annotations

import pytest

from pluvial.geo.sample_plan import (
    FRONTAGE_RADIUS_M,
    NEIGHBOURHOOD_RADIUS_M,
    build_sample_plan,
    haversine_m,
    offset_m,
)

HOUSTON = (29.7604, -95.3698)
ANCHORAGE = (61.2181, -149.9003)
MIAMI = (25.7617, -80.1918)


def test_plan_has_nine_points_in_the_documented_mix():
    plan = build_sample_plan(*HOUSTON)
    assert len(plan) == 9
    roles = [p.role for p in plan]
    assert roles[0] == "property"
    assert roles.count("property") == 1
    assert roles.count("frontage") == 4
    assert roles.count("neighbourhood") == 4


def test_property_point_is_the_geocoded_coordinate_exactly():
    plan = build_sample_plan(*HOUSTON)
    assert (plan[0].lat, plan[0].lon) == HOUSTON


@pytest.mark.parametrize("origin", [HOUSTON, ANCHORAGE, MIAMI])
def test_spacing_matches_the_declared_radii_at_any_latitude(origin):
    """The cos(lat) term in offset_m is what keeps the cross from collapsing
    east-west near the poles: without it Anchorage's E/W points would sit
    about half as far out as Miami's."""
    plan = build_sample_plan(*origin)
    for point in plan[1:5]:
        d = haversine_m(*origin, point.lat, point.lon)
        assert d == pytest.approx(FRONTAGE_RADIUS_M, rel=0.02)
    for point in plan[5:]:
        d = haversine_m(*origin, point.lat, point.lon)
        assert d == pytest.approx(NEIGHBOURHOOD_RADIUS_M, rel=0.02)


def test_bearings_point_the_way_they_are_labelled():
    plan = build_sample_plan(*HOUSTON)
    by_bearing = {p.bearing: p for p in plan if p.bearing}
    lat, lon = HOUSTON
    assert by_bearing["N"].lat > lat and by_bearing["S"].lat < lat
    assert by_bearing["E"].lon > lon and by_bearing["W"].lon < lon
    assert by_bearing["NE"].lat > lat and by_bearing["NE"].lon > lon
    assert by_bearing["SW"].lat < lat and by_bearing["SW"].lon < lon


def test_every_point_is_distinct():
    coords = {(round(p.lat, 7), round(p.lon, 7)) for p in build_sample_plan(*HOUSTON)}
    assert len(coords) == 9


def test_offset_is_symmetric_about_the_origin():
    lat, lon = HOUSTON
    north = offset_m(lat, lon, 0, 100)
    south = offset_m(lat, lon, 0, -100)
    assert north[0] - lat == pytest.approx(lat - south[0], rel=1e-9)
