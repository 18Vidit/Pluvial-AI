"""Where to sample the ground around one address.

Pure functions, no I/O — the sample plan is decided before a single credit
is quoted, so it can be unit-tested and shown on the map before the user
confirms anything.

The shape (design spec §"Query flow") is nine points:

    1 property       the geocoded coordinate itself
    4 frontage       a 30m cross, N/S/E/W
    4 neighbourhood  150m out on the diagonals

Frontage sampling is load-bearing rather than decorative. A buried service
line runs down the street, not through the lot centroid, and SSURGO map
units change across short distances — so this is what lets the system say
"this lot straddles two map units". A single point cannot produce that
finding, and parcel geometry, which would give the real boundary, is out of
scope for the program.

30m and 150m are starting values chosen against SSURGO's mapping scale, not
measured optima. They are worth tuning once real queries show how often the
frontage points differ from the property point.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

SampleRole = Literal["property", "frontage", "neighbourhood"]

FRONTAGE_RADIUS_M = 30.0
NEIGHBOURHOOD_RADIUS_M = 150.0

EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class SamplePoint:
    role: SampleRole
    lat: float
    lon: float
    bearing: str | None = None  # N/S/E/W or NE/SE/SW/NW; None for the property point

    @property
    def label(self) -> str:
        return self.role if self.bearing is None else f"{self.role} {self.bearing}"


def offset_m(lat: float, lon: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    """Move dy_m north and dx_m east of (lat, lon).

    Equirectangular, which at these distances (<= 150m) is accurate to well
    under a metre — far inside the resolution of anything we sample. The
    cos(lat) term is what stops the east/west points collapsing toward each
    other at high latitude; without it a query in Anchorage would sample a
    narrower cross than one in Miami.
    """
    dlat = math.degrees(dy_m / EARTH_RADIUS_M)
    dlon = math.degrees(dx_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# (bearing, east component, north component) as unit vectors.
_CROSS = [("N", 0.0, 1.0), ("S", 0.0, -1.0), ("E", 1.0, 0.0), ("W", -1.0, 0.0)]
_DIAGONALS = [
    ("NE", math.sqrt(0.5), math.sqrt(0.5)),
    ("SE", math.sqrt(0.5), -math.sqrt(0.5)),
    ("SW", -math.sqrt(0.5), -math.sqrt(0.5)),
    ("NW", -math.sqrt(0.5), math.sqrt(0.5)),
]


def build_sample_plan(
    lat: float,
    lon: float,
    frontage_radius_m: float = FRONTAGE_RADIUS_M,
    neighbourhood_radius_m: float = NEIGHBOURHOOD_RADIUS_M,
) -> list[SamplePoint]:
    """The nine points to fetch for one address. Order is stable: property
    first, then the cross, then the diagonals — the map draws them in this
    order and the demo reads better when the property point lands first."""
    points = [SamplePoint(role="property", lat=lat, lon=lon)]
    for bearing, ex, ny in _CROSS:
        plat, plon = offset_m(lat, lon, ex * frontage_radius_m, ny * frontage_radius_m)
        points.append(SamplePoint(role="frontage", lat=plat, lon=plon, bearing=bearing))
    for bearing, ex, ny in _DIAGONALS:
        plat, plon = offset_m(lat, lon, ex * neighbourhood_radius_m, ny * neighbourhood_radius_m)
        points.append(SamplePoint(role="neighbourhood", lat=plat, lon=plon, bearing=bearing))
    return points
