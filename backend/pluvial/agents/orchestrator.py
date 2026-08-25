"""The conversational layer over a completed analysis.

Rulings landing is where a reader becomes most curious, and a form that
returns three cards is a report generator. Being able to interrogate the
result is what makes this read as an agent rather than a pipeline.

The toolset is deliberately small and typed. Two of the five tools spend
credits, and neither can spend them: they propose a purchase and emit a
`quote`, and only a click confirms it. That is the same rule
`MireyeToolWrapper` enforces on the cascade — an agent may ask, a person
authorises — applied to conversation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import psycopg
from agents import Agent, RunContextWrapper, function_tool

from pluvial.agents.address_tools import summarise_sample
from pluvial.agents.guidance import ADDRESS_PHYSICS
from pluvial.api.events import Event, EventStream
from pluvial.memory import dal

ORCHESTRATOR_MODEL = "gpt-4o"

ORCHESTRATOR_ROLE = """
You are answering questions about ground that has already been sampled and
adjudicated at one location. Three rulings exist — foundation,
service_lines and subsidence — each with cited evidence, a rejected
counter-argument and an invalidation condition.

THE RULE THAT MATTERS MOST: answer only from evidence that has been
fetched, or fetch more. Never speculate about ground you have no data for.
This is the same discipline the Honesty Gate enforces on the cascade,
applied to conversation. If someone asks about a part of the lot that was
not sampled, do not guess what is under it — propose sampling it.

Tools:
- explain_ruling(threat): the claims, the rejected counter-argument and the
  invalidation condition behind one ruling.
- explain_veto(sample_id): why the Honesty Gate fired at a point and what
  evidence would resolve it.
- compare_samples(a, b): field-by-field difference between two points.
- sample_point(lat, lon): propose one new point at this location. Quotes,
  spends nothing. The person confirms.
- analyze_location(address): propose the full nine-point pipeline on a new
  address. Quotes, spends nothing. The person confirms.
- search_region(query, credit_budget): propose an adaptive search across a
  metro or county for better ground. Quotes a ceiling, spends nothing.

When you call sample_point or analyze_location, say plainly that you have
put a quote up and that nothing is spent until they confirm it. You cannot
confirm it yourself.

WHAT YOU CANNOT DO, and must say plainly rather than guess at:
- National aggregate questions ("which counties have the worst
  shrink-swell") need a pre-built national index that does not exist here.
  Building one would mean sampling every county centroid once; it is a
  deliberate deferral, not an oversight.
- Parcel-level inverse search — finding specific properties rather than
  areas — needs parcel geometry, which is out of scope for this build.
  search_region returns AREAS. Say so when you offer it: the honest
  journey is screen the metro, shortlist a neighbourhood, then analyse a
  specific address properly.
- Anything about the interior of a structure, its construction, or its
  maintenance history. This system knows about ground.

Be direct and concrete. Cite sample_ids when you refer to points, and field
names and values when you refer to evidence.
"""


@dataclass
class PendingSpend:
    """A proposed purchase waiting on a human. Held in memory and keyed by
    session, because it is meaningless once the tab closes — an unconfirmed
    quote should expire, not linger in a database waiting to be honoured."""

    pending_id: str
    kind: str                      # sample_point | analyze_location
    quoted_credits: int
    payload: dict[str, Any]


@dataclass
class OrchestratorContext:
    con: psycopg.Connection
    location: dict[str, Any]
    samples: list[dict[str, Any]]
    rulings: list[dict[str, Any]]
    region_key: str | None
    stream: EventStream
    emit: Callable[[Event], Any]
    client: Any = None             # MireyeClient, for quoting only
    pending: dict[str, PendingSpend] = field(default_factory=dict)

    def sample(self, sample_id: int) -> dict[str, Any] | None:
        return next((s for s in self.samples if s["sample_id"] == sample_id), None)

    def ruling(self, threat: str) -> dict[str, Any] | None:
        return next((r for r in self.rulings if r["threat"] == threat), None)


@function_tool
async def explain_ruling(wrapper: RunContextWrapper[OrchestratorContext], threat: str) -> str:
    """The full record behind one ruling: every cited claim with its field,
    value, source and sample_id; the Skeptic's strongest point and why it did
    not change the ruling; and the condition that would reopen it."""
    ruling = wrapper.context.ruling(threat)
    if ruling is None:
        return json.dumps({"error": f"no ruling for {threat!r} at this location"})
    return json.dumps(
        {
            "threat": ruling["threat"],
            "severity": ruling["severity"],
            "cited_evidence": ruling["cited_evidence"],
            "rejected_counter_argument": ruling["rejected_counter_argument"],
            "invalidation_condition": ruling["invalidation_condition"],
            "explanation": (ruling["reasoning"] or {}).get("adjudicator_explanation"),
            "unknowns": (ruling["reasoning"] or {}).get("unknowns", []),
        },
        default=str,
    )


@function_tool
async def explain_veto(wrapper: RunContextWrapper[OrchestratorContext], sample_id: int) -> str:
    """Why the Honesty Gate fired at one point, in the Skeptic's own words,
    across every threat where it fired — plus what is actually unknown there
    and what evidence would settle it."""
    ctx = wrapper.context
    sample = ctx.sample(sample_id)
    if sample is None:
        return json.dumps({"error": f"no sample {sample_id} at this location"})

    vetoes = []
    for ruling in ctx.rulings:
        skeptic = (ruling["reasoning"] or {}).get("skeptic") or {}
        if sample_id in (skeptic.get("vetoed_sample_ids") or []):
            vetoes.append({
                "threat": ruling["threat"],
                "veto_reason": skeptic.get("veto_reason"),
                "resulting_severity": ruling["severity"],
                "unknowns": (ruling["reasoning"] or {}).get("unknowns", []),
            })

    profile = sample.get("profile") or {}
    map_unit = profile.get("soil_map_unit_name")
    return json.dumps(
        {
            "sample_id": sample_id,
            "role": sample["role"],
            "soil_usable": sample.get("soil_usable"),
            "soil_map_unit_name": map_unit,
            "vetoes": vetoes,
            "note": (
                "No veto was recorded at this point."
                if not vetoes
                else "A veto invalidates soil-derived claims at this point only. Karst, bedrock "
                     "depth, elevation and the consequence fields are unaffected."
            ),
        },
        default=str,
    )


@function_tool
async def compare_samples(
    wrapper: RunContextWrapper[OrchestratorContext], sample_id_a: int, sample_id_b: int
) -> str:
    """Field-by-field difference between two sampled points, with sources."""
    ctx = wrapper.context
    a, b = ctx.sample(sample_id_a), ctx.sample(sample_id_b)
    if a is None or b is None:
        return json.dumps({"error": "one or both sample_ids are not at this location"})
    pa, pb = a.get("profile") or {}, b.get("profile") or {}
    differences = {}
    for name in sorted(set(pa) | set(pb)):
        va = pa.get(name, {}).get("value") if isinstance(pa.get(name), dict) else pa.get(name)
        vb = pb.get(name, {}).get("value") if isinstance(pb.get(name), dict) else pb.get(name)
        if va != vb:
            differences[name] = {"a": va, "b": vb}
    return json.dumps(
        {
            "a": summarise_sample(a),
            "b": summarise_sample(b),
            "differences": differences,
            "identical": not differences,
        },
        default=str,
    )


@function_tool
async def sample_point(wrapper: RunContextWrapper[OrchestratorContext], lat: float, lon: float) -> str:
    """Propose one additional sampled point at this location — for example
    the back of the lot, or across the street. Quotes it and spends NOTHING.
    The person confirms before any credit is spent; you cannot confirm it."""
    from pluvial.geo.sample_plan import haversine_m
    from pluvial.mireye.fields import ALL_FIELDS

    ctx = wrapper.context
    distance_m = haversine_m(ctx.location["lat"], ctx.location["lon"], lat, lon)
    if distance_m > 2_000:
        return json.dumps({
            "error": f"that point is {round(distance_m)}m from this address. Beyond about 2km it is "
                     "a different location — use analyze_location instead so it gets a full plan.",
        })

    sample_id = dal.create_samples(ctx.con, ctx.location["location_id"], [("neighbourhood", lat, lon)])[0]
    ctx.con.commit()

    quote = ctx.client.quote(ALL_FIELDS, locations=1)
    credits = int(quote.get("credits") or quote.get("total_credits") or len(ALL_FIELDS))

    pending_id = f"sample-{sample_id}"
    ctx.pending[pending_id] = PendingSpend(
        pending_id=pending_id, kind="sample_point", quoted_credits=credits,
        payload={"sample_id": sample_id, "lat": lat, "lon": lon,
                 "location_id": ctx.location["location_id"]},
    )

    await ctx.emit(ctx.stream.make("sample_planned", {
        "sample_id": sample_id, "role": "neighbourhood", "lat": lat, "lon": lon,
    }))
    await ctx.emit(ctx.stream.make("quote", {
        "pending_id": pending_id,
        "kind": "sample_point",
        "quoted_credits": credits,
        "label": f"one point {round(distance_m)}m from the property",
        "sample_id": sample_id,
        "lat": lat, "lon": lon,
    }))

    return json.dumps({
        "proposed": True, "sample_id": sample_id, "quoted_credits": credits,
        "distance_from_property_m": round(distance_m),
        "note": "Quote is on screen. Nothing is spent until the person confirms it.",
    })


@function_tool
async def analyze_location(wrapper: RunContextWrapper[OrchestratorContext], address: str) -> str:
    """Propose the full nine-point pipeline on a NEW address. Quotes and
    spends nothing; the person confirms. Use this when the question is about
    somewhere else, not about more of this lot."""
    from pluvial import analyze

    ctx = wrapper.context
    try:
        plan = analyze.plan(ctx.con, address, ctx.client)
    except analyze.GeocodeFailed as e:
        return json.dumps({"error": str(e)})

    pending_id = f"location-{plan.location_id}"
    ctx.pending[pending_id] = PendingSpend(
        pending_id=pending_id, kind="analyze_location", quoted_credits=plan.quoted_credits,
        payload={"location_id": plan.location_id, "label": plan.label},
    )

    await ctx.emit(ctx.stream.make("quote", {
        "pending_id": pending_id,
        "kind": "analyze_location",
        "quoted_credits": plan.quoted_credits,
        "label": plan.label,
        "location_id": plan.location_id,
        "lat": plan.lat, "lon": plan.lon,
        "n_points": len(plan.samples),
    }))

    return json.dumps({
        "proposed": True, "location_id": plan.location_id, "label": plan.label,
        "quoted_credits": plan.quoted_credits,
        "note": "Quote is on screen. Nothing is spent until the person confirms it.",
    })


@function_tool
async def search_region(
    wrapper: RunContextWrapper[OrchestratorContext], query: str, credit_budget: int = 2500
) -> str:
    """Propose an adaptive search across a region for ground that is less
    likely to move — "where near Austin is the ground better?". Quotes a
    CEILING and spends NOTHING; the person confirms.

    Be honest about what this returns when you describe it: a grid returns
    AREAS, not addresses. SSURGO map units are often 100m to 1km, so even the
    refined cells are neighbourhood-level guidance. The truthful framing is a
    two-stage journey — screen the metro, shortlist neighbourhoods, then
    analyse a specific address properly with analyze_location.
    """
    ctx = wrapper.context
    budget = max(500, min(int(credit_budget), 6000))

    pending_id = f"region-{len(ctx.pending)}-{abs(hash(query)) % 10**6}"
    ctx.pending[pending_id] = PendingSpend(
        pending_id=pending_id, kind="search_region", quoted_credits=budget,
        payload={"query": query, "credit_budget": budget},
    )

    await ctx.emit(ctx.stream.make("quote", {
        "pending_id": pending_id,
        "kind": "search_region",
        "quoted_credits": budget,
        "label": f"adaptive regional search — up to {budget} credits, and it stops at that ceiling",
        "query": query,
    }))

    return json.dumps({
        "proposed": True, "credit_ceiling": budget,
        "note": "This is a CEILING, not a price — the traversal stops when it hits it and labels "
                "partial results as partial. Nothing is spent until the person confirms.",
    })


ORCHESTRATOR_TOOLS = [
    explain_ruling, explain_veto, compare_samples, sample_point, analyze_location, search_region,
]


def build_orchestrator(con, location: dict[str, Any], samples: list[dict[str, Any]],
                       rulings: list[dict[str, Any]]) -> Agent:
    """The analysis is summarised into the instructions rather than fetched
    through a tool: it is small, it is always relevant, and making the agent
    spend a turn asking what it is already looking at would be theatre."""
    from pluvial.agents.guidance import compose_guidance, load_calibration_notes

    context_block = "\n\nTHIS LOCATION:\n" + json.dumps(
        {
            "label": location.get("label"),
            "lat": location.get("lat"),
            "lon": location.get("lon"),
            "samples": [summarise_sample(s) for s in samples],
            "rulings": [
                {
                    "threat": r["threat"],
                    "severity": r["severity"],
                    "explanation": (r["reasoning"] or {}).get("adjudicator_explanation"),
                    "cited_sample_ids": sorted({
                        c.get("sample_id") for c in (r["cited_evidence"] or []) if c.get("sample_id")
                    }),
                }
                for r in rulings
            ],
        },
        default=str,
    )

    return Agent[OrchestratorContext](
        name="Orchestrator",
        instructions=compose_guidance(
            ORCHESTRATOR_ROLE + context_block, load_calibration_notes(con), physics=ADDRESS_PHYSICS
        ),
        model=ORCHESTRATOR_MODEL,
        tools=ORCHESTRATOR_TOOLS,
    )
