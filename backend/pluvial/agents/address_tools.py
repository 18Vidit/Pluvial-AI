"""Function tools for the address-mode cascade.

Different from `agents/tools.py` in one structural way that everything else
follows from: `sampled_profiles` returns per-point values keyed by
`sample_id` rather than one blended profile. That is what lets a claim name
the point it was read at, and it is the whole reason the map can bind an
argument to a coordinate.

`neighbourhood_complaints` and `dossier_lookup` are deliberately absent.
There is no 311 feed in the product and no street segment to hold a
dossier — an address-mode agent argues from ground physics and consequence,
or it does not argue.
"""
from __future__ import annotations

import json
from typing import Any

from agents import RunContextWrapper, function_tool

from pluvial.agents.context import AddressContext
from pluvial.memory import dal

# Fields that only exist where SSURGO has a real soil component. Claiming
# any of these on a point with soil_usable=False is what the Honesty Gate
# vetoes, so they are named once, here, rather than restated in prose in
# three prompts.
SOIL_DERIVED_FIELDS = (
    "soil_shrink_swell_class",
    "soil_available_water_capacity",
    "soil_drainage_class",
    "soil_erodibility_k_factor",
    "soil_hydrologic_group",
)


def _unwrap(profile: dict[str, Any] | None, field: str) -> Any:
    entry = (profile or {}).get(field)
    return entry.get("value") if isinstance(entry, dict) else entry


def summarise_sample(sample: dict[str, Any]) -> dict[str, Any]:
    """The compact per-point view the agents reason over. Full values with
    sources are one `sample_detail` call away — sending all 24 fields for
    all 9 points in the opening prompt would be ~200 lines of JSON before
    the agent has decided which points matter."""
    profile = sample.get("profile") or {}
    return {
        "sample_id": sample["sample_id"],
        "role": sample["role"],
        "lat": round(sample["lat"], 6),
        "lon": round(sample["lon"], 6),
        "soil_usable": sample.get("soil_usable"),
        "soil_map_unit_name": _unwrap(profile, "soil_map_unit_name"),
        "soil_shrink_swell_class": _unwrap(profile, "soil_shrink_swell_class"),
        "soil_drainage_class": _unwrap(profile, "soil_drainage_class"),
        "soil_erodibility_k_factor": _unwrap(profile, "soil_erodibility_k_factor"),
        "soil_hydrologic_group": _unwrap(profile, "soil_hydrologic_group"),
        "bedrock_depth_cm": _unwrap(profile, "bedrock_depth_cm"),
        "in_karst_area": _unwrap(profile, "in_karst_area"),
        "karst_exposure_class": _unwrap(profile, "karst_exposure_class"),
        "elevation": _unwrap(profile, "elevation"),
    }


@function_tool
def sampled_profiles(wrapper: RunContextWrapper[AddressContext]) -> str:
    """Every sampled point around this location, keyed by sample_id, with
    its soil class, drainage, erodibility, bedrock depth and karst status.
    Call this first. Each point is a separate reading of the ground: the
    property point, four frontage points 30m out on a cross, and four
    neighbourhood points 150m out on the diagonals. Points can disagree —
    a lot straddling two SSURGO map units is a real and important finding.
    Every claim you make from these values must carry the sample_id you
    read it at."""
    ctx = wrapper.context
    return json.dumps(
        {
            "location": {
                "label": ctx.location.get("label"),
                "lat": ctx.location.get("lat"),
                "lon": ctx.location.get("lon"),
            },
            "samples": [summarise_sample(s) for s in ctx.samples],
            "n_soil_usable": sum(1 for s in ctx.samples if s.get("soil_usable")),
            "n_total": len(ctx.samples),
        },
        default=str,
    )


@function_tool
def sample_detail(wrapper: RunContextWrapper[AddressContext], sample_id: int) -> str:
    """All 24 Mireye field values for one sampled point, each with the
    source Mireye cited for it. Use this when you intend to build a claim on
    a specific point and need the exact value and its provenance."""
    sample = wrapper.context.sample(sample_id)
    if sample is None:
        return json.dumps({"error": f"no sample {sample_id} at this location"})
    return json.dumps(
        {
            "sample_id": sample_id,
            "role": sample["role"],
            "lat": sample["lat"],
            "lon": sample["lon"],
            "soil_usable": sample.get("soil_usable"),
            "profile": sample.get("profile") or {},
        },
        default=str,
    )


@function_tool
def compare_samples(wrapper: RunContextWrapper[AddressContext], sample_id_a: int, sample_id_b: int) -> str:
    """Field-by-field difference between two sampled points. Use this to
    test whether the ground actually changes across the lot rather than
    asserting that it does."""
    ctx = wrapper.context
    a, b = ctx.sample(sample_id_a), ctx.sample(sample_id_b)
    if a is None or b is None:
        return json.dumps({"error": "one or both sample_ids are not at this location"})
    pa, pb = a.get("profile") or {}, b.get("profile") or {}
    differences = {}
    for field in sorted(set(pa) | set(pb)):
        va, vb = _unwrap(pa, field), _unwrap(pb, field)
        if va != vb:
            differences[field] = {"a": va, "b": vb}
    return json.dumps(
        {
            "a": {"sample_id": sample_id_a, "role": a["role"]},
            "b": {"sample_id": sample_id_b, "role": b["role"]},
            "differences": differences,
            "identical": not differences,
        },
        default=str,
    )


@function_tool
def moisture_history(wrapper: RunContextWrapper[AddressContext]) -> str:
    """The Movement Trigger State for this location's region: the antecedent
    -moisture trajectory from the nearest NOAA station, plus the coarse USDM
    drought corroborator. This is REGIONAL, not per-point — it tells you WHEN
    the ground is moving; the soil fields tell you WHERE it moves a lot. A
    claim built on this has no sample_id, and that is correct."""
    ctx = wrapper.context
    state = dal.current_trigger_state(ctx.con, as_of=ctx.frozen_at, region_key=ctx.region_key)
    if state is None:
        return json.dumps({
            "available": False,
            "reason": "no moisture series on file for this region yet",
        })
    return json.dumps({"available": True, "region_key": ctx.region_key, **state}, default=str)


@function_tool
def usgs_gage(wrapper: RunContextWrapper[AddressContext], sample_id: int) -> str:
    """The nearest USGS stream gage's discharge and distance, as read at one
    sampled point. These fields were already fetched with the rest of the
    profile, so this costs nothing — it is a view onto data you already have,
    not a new purchase."""
    sample = wrapper.context.sample(sample_id)
    if sample is None:
        return json.dumps({"error": f"no sample {sample_id} at this location"})
    profile = sample.get("profile") or {}
    return json.dumps(
        {"sample_id": sample_id, **{k: v for k, v in profile.items() if "usgs_gage" in k}},
        default=str,
    )


@function_tool
def consequence_surface(wrapper: RunContextWrapper[AddressContext]) -> str:
    """Who and what is nearby: school and hospital distances, housing units,
    population served by the public water system, tract population. This
    sets how much a failure would cost. It NEVER makes failure more or less
    likely — do not let it into your likelihood reasoning."""
    ctx = wrapper.context
    property_sample = next((s for s in ctx.samples if s["role"] == "property"), None)
    if property_sample is None:
        return json.dumps({"error": "no property sample at this location"})
    profile = property_sample.get("profile") or {}
    keys = (
        "nearest_school_distance_m", "nearest_hospital_distance_m", "nearest_major_road_class",
        "housing_units_within_1km", "public_water_system_population_served", "tract_population",
        "county_median_household_income", "water_system_name", "within_water_service_area",
    )
    return json.dumps(
        {"sample_id": property_sample["sample_id"], **{k: profile.get(k) for k in keys if k in profile}},
        default=str,
    )


@function_tool
def precedent_search(
    wrapper: RunContextWrapper[AddressContext],
    shrink_swell_class: str,
    trigger_state: str,
    symptom_class: str,
) -> str:
    """Resolved past cases with the same soil class and trigger state, and
    how they turned out. The recorded corpus is Houston water-main cases, so
    treat it as evidence about the mechanism (what this clay does in this
    trigger state), not as evidence about this address."""
    ctx = wrapper.context
    rows = dal.precedent_search(
        ctx.con, shrink_swell_class, trigger_state, symptom_class, as_of=ctx.frozen_at
    )
    return json.dumps(rows, default=str)


ADDRESS_TOOLS = [
    sampled_profiles,
    sample_detail,
    compare_samples,
    moisture_history,
    usgs_gage,
    consequence_surface,
    precedent_search,
]
