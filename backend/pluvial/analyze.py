"""The address-mode pipeline, from a typed address to nine fetched points.

This is the part that spends money, so it is split at exactly the point
where money starts being spent:

    plan(...)   geocode, build the sample plan, quote.  Spends NOTHING.
    fetch(...)  batch-fetch the planned points.          Spends the quote.

Nothing calls `fetch` without a user having seen and confirmed a `plan`.
That gate is the same one `MireyeToolWrapper` enforces on agents: the system
may propose a purchase, only a person may authorise one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator

import httpx
import psycopg

from pluvial.geo.sample_plan import SamplePoint, build_sample_plan
from pluvial.ingest import moisture_sync
from pluvial.ingest.stations import nearest_station
from pluvial.memory import dal
from pluvial.mireye.client import MireyeClient, chunk_locations
from pluvial.mireye.fields import ALL_FIELDS, is_soil_usable
from pluvial.mireye.profile_job import BatchLocationFailed, extract_batch_result

NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "pluvial-ai-address-mode/1.0"


class GeocodeFailed(RuntimeError):
    pass


@dataclass
class Geocoded:
    lat: float
    lon: float
    label: str


def geocode(address: str, timeout: float = 10.0) -> Geocoded:
    """Free-text address to a coordinate, nationally.

    Note the absence of a ", Houston, TX" suffix, which `GET /lookup` still
    appends. Address mode works anywhere, so pinning the query to one city
    would be actively wrong — a Denver address would silently geocode to
    whatever Nominatim thought was closest in Harris County.
    """
    with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        r = client.get(
            NOMINATIM_ENDPOINT,
            params={"q": address, "format": "json", "limit": 1, "countrycodes": "us"},
        )
        r.raise_for_status()
        hits = r.json()
    if not hits:
        raise GeocodeFailed(f"no US match for {address!r}")
    return Geocoded(float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("display_name", address))


@dataclass
class AnalysisPlan:
    location_id: int
    query_text: str
    label: str
    lat: float
    lon: float
    region_key: str
    station_name: str
    station_distance_m: float
    samples: list[dict[str, Any]]      # sample_id, role, lat, lon
    fields: list[str]
    quoted_credits: int
    quote_raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "query_text": self.query_text,
            "label": self.label,
            "lat": self.lat,
            "lon": self.lon,
            "region_key": self.region_key,
            "station": {"name": self.station_name, "distance_m": round(self.station_distance_m, 1)},
            "samples": self.samples,
            "n_points": len(self.samples),
            "n_fields": len(self.fields),
            "quoted_credits": self.quoted_credits,
        }


def plan(con: psycopg.Connection, address: str, client: MireyeClient) -> AnalysisPlan:
    """Geocode, resolve the region, lay out nine points, and quote them.

    The quote is real: it calls Mireye's /v1/fetch/quote, which is free and
    is the number the user is shown before deciding. It is not a local
    multiplication of fields by points — a computed estimate that later
    diverged from the bill would make the confirm gate meaningless.
    """
    hit = geocode(address)
    station, distance_m = nearest_station(hit.lat, hit.lon)

    location_id = dal.create_location(con, address, hit.label, hit.lat, hit.lon, station.station_id)
    points: list[SamplePoint] = build_sample_plan(hit.lat, hit.lon)
    sample_ids = dal.create_samples(con, location_id, [(p.role, p.lat, p.lon) for p in points])
    con.commit()

    quote = client.quote(ALL_FIELDS, locations=len(points))
    quoted = quote.get("credits") or quote.get("total_credits") or len(ALL_FIELDS) * len(points)

    return AnalysisPlan(
        location_id=location_id,
        query_text=address,
        label=hit.label,
        lat=hit.lat,
        lon=hit.lon,
        region_key=station.station_id,
        station_name=station.name,
        station_distance_m=distance_m,
        samples=[
            {"sample_id": sid, "role": p.role, "bearing": p.bearing, "lat": p.lat, "lon": p.lon}
            for sid, p in zip(sample_ids, points)
        ],
        fields=list(ALL_FIELDS),
        quoted_credits=int(quoted),
        quote_raw=quote,
    )


def fetch_samples(
    con: psycopg.Connection,
    client: MireyeClient,
    plan: AnalysisPlan,
    on_point: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Batch-fetch every planned point and write the results to memory.

    Nine points fit in one 25-location batch, so this is normally a single
    call. `on_point` is invoked per point as results land, which is what
    paints the map while the agents are still starting up.
    """
    locations = [(s["sample_id"], s["lat"], s["lon"]) for s in plan.samples]
    fetched: list[dict[str, Any]] = []

    for chunk in chunk_locations(locations, size=25):
        resp = client.fetch_batch(
            ALL_FIELDS,
            [(lat, lon) for _, lat, lon in chunk],
            idempotency_key=f"address-{plan.location_id}-{chunk[0][0]}",
        )
        results = resp.get("results") or resp.get("locations") or []
        for (sample_id, lat, lon), result in zip(chunk, results):
            # strict: a point Mireye could not answer for must surface as an
            # error, never be written as a point with no soil data.
            values = extract_batch_result(result, strict=True)
            soil_usable = is_soil_usable(values)
            dal.record_sample_profile(con, sample_id, values, soil_usable, client.account.label)
            record = {
                "sample_id": sample_id, "lat": lat, "lon": lon,
                "profile": values, "soil_usable": soil_usable,
            }
            fetched.append(record)
            if on_point:
                on_point(record)
        con.commit()

    return fetched


def ensure_moisture(plan: AnalysisPlan) -> int:
    """Make sure this location's region has a moisture series on file.

    Free (NOAA NCEI and the USDM feature service are both keyless) and
    skipped entirely when the region was synced within the last two days,
    so repeating a demo in the same metro costs nothing and waits on
    nothing.
    """
    return moisture_sync.ensure_region(plan.region_key, plan.lat, plan.lon)
