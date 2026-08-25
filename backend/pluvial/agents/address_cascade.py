"""The address-mode cascade: Triage once, then one independent
Investigator -> Skeptic -> Adjudicator argument per threat, three running
concurrently.

Kept separate from `agents/cascade.py` rather than replacing it. The unit of
analysis is genuinely different — a location and a threat, not a complaint
on a segment — and the triage-mode cascade is still the evaluation proving
ground (see `backtest --mode address`, which reports the two side by side).
Folding both into one module would mean one set of prompts trying to be
about both, which is how prompts get vague.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from agents import Agent, Runner

from pluvial.agents.address_tools import ADDRESS_TOOLS, summarise_sample
from pluvial.agents.context import AddressContext
from pluvial.agents.guidance import ADDRESS_PHYSICS, compose_guidance, load_calibration_notes
from pluvial.agents.models import (
    AddressTriageOutput,
    InvestigatorOutput,
    SkepticOutput,
    Threat,
    ThreatRuling,
)

TRIAGE_MODEL = "gpt-4o-mini"
REASONING_MODEL = "gpt-4o"

THREATS: tuple[Threat, ...] = ("foundation", "service_lines", "subsidence")

# What each threat is actually about, injected into all three roles so the
# three concurrent cascades argue about different things instead of
# producing the same argument under three headings.
THREAT_BRIEFS: dict[str, str] = {
    "foundation": (
        "FOUNDATION: whether seasonal soil movement will damage the structure's foundation. "
        "Driven by soil_shrink_swell_class, soil_available_water_capacity, bedrock_depth_cm and "
        "the regional movement trigger state. The mechanism is differential movement: uniform "
        "heave lifts a slab intact, uneven heave cracks it — so disagreement BETWEEN sampled "
        "points is itself evidence, and agreement across all nine is evidence against."
    ),
    "service_lines": (
        "SERVICE LINES: whether buried water and sewer lines running from the structure to the "
        "street will be damaged or will leak. Driven by soil_shrink_swell_class, "
        "soil_erodibility_k_factor and soil_drainage_class. The frontage points matter most here: "
        "a service line runs out to the street, not through the lot centroid, so the ground it "
        "actually crosses is what the frontage cross samples. A leak into erodible, poorly "
        "drained soil scours a void and surfaces late."
    ),
    "subsidence": (
        "SUBSIDENCE: whether the ground itself will settle or collapse beneath the property. "
        "Driven by in_karst_area, karst_exposure_class, soil_erodibility_k_factor and "
        "soil_hydrologic_group. Karst fields come from USGS mapping rather than SSURGO "
        "components, so this threat can still be resolved at points where the soil gate has "
        "fired and the other two threats cannot be."
    ),
}

TRIAGE_ROLE = """
You are Triage. You see a summary of every sampled point around one
location. You have NO tool access; do not attempt to fetch anything.

Someone asked about this address and the ground under it has already been
bought, so every location is adjudicated. Your job is to set the pitch and
point the arguments at what matters here, not to decide whether to answer:

- promote: the ordinary case.
- fast_path: the ground is unambiguously severe — High or Very High
  shrink-swell with a drying or rewetting trigger state, or mapped karst
  exposure. Adjudicate with urgency; do not skip the adversarial review.

Use `focus` to name the threat or the specific sampled points the arguments
should concentrate on — for example that the frontage points disagree with
the property point, or that the soil gate has fired everywhere and the only
answerable threat is subsidence.
"""

INVESTIGATOR_ROLE = """
You are the Investigator. Build the strongest evidence-based case that this
location's ground poses a real, near-term threat of the specific kind named
below.

Call sampled_profiles first. Then use sample_detail on the points that
matter, compare_samples where you suspect the ground changes across the lot,
moisture_history for the trigger state, consequence_surface for what a
failure would cost, and precedent_search where a comparable case exists.

Every claim needs a field, a value, an interpretation, and the sample_id it
was read at. Regional moisture history is the one exception and carries no
sample_id.

If soil_usable is False at a point, do not make a shrink-swell, drainage,
erodibility or hydrologic-group claim from that point. Use a different
point that has one, or argue from karst, bedrock depth, elevation and the
trigger state, which do not depend on an SSURGO component. If no point
supports your case, say so — an argument built on ground you have no data
for is worse than no argument.
"""

SKEPTIC_ROLE = """
You are the Skeptic. Build the strongest case that this location's ground
does NOT pose the threat named below, and that the Investigator has
overreached.

Your lines of attack, in order of strength:
- The Honesty Gate. If the Investigator built a claim on a soil-derived
  field (shrink-swell, available water capacity, drainage, erodibility,
  hydrologic group) at a point where soil_usable is False, set
  soil_claim_vetoed=True, list those sample_ids in vetoed_sample_ids, and
  explain what is actually unknown there. This vetoes those claims, not
  necessarily the whole case: karst, bedrock depth and consequence fields
  survive the gate.
- Uniformity. If every sampled point agrees, differential movement — the
  mechanism that actually breaks things — is much less likely than a raw
  shrink-swell class suggests. Use compare_samples to show it.
- Wrong threat. Check whether the cited fields actually drive the threat
  under argument, rather than a different one.
- Trigger state. A stable moisture regime means the mechanism is not
  currently active, whatever the soil can do in principle.
- Natural surface water. usgs_gage, where a nearby gage is relevant.

Every claim you make carries a field, a value and a sample_id, on the same
terms as the Investigator's. You do not get to assert safety without
evidence either.
"""

ADJUDICATOR_ROLE = """
You are the Adjudicator. You receive the Investigator's case and the
Skeptic's rebuttal for ONE threat at ONE location. Rule on severity:

- high: the mechanism is present, the trigger is active, and the evidence
  is specific to this ground.
- elevated: the mechanism is present but something material is missing —
  the trigger is stable, or the evidence rests on neighbourhood points
  rather than the property itself.
- low: the ground was actually measured and does not support this threat.
- unresolved: the evidence needed does not exist here.

decisive_evidence must be drawn only from claims actually presented by the
Investigator or the Skeptic; do not invent evidence, and carry each claim's
sample_id through unchanged.

MANDATORY, when soil_claim_vetoed is True and every soil-derived claim for
this threat was vetoed: you may NOT rule `low`. `low` asserts the ground was
measured and is fine, which is precisely what did not happen. What you rule
instead depends on what survived the veto:

- If measured non-soil evidence supports a finding — in_karst_area and
  karst_exposure_class (USGS karst mapping, not an SSURGO component),
  bedrock_depth_cm, elevation, the regional trigger state — rule on that
  evidence, at `high` or `elevated` as it warrants, and say explicitly in
  the explanation that the soil-derived contribution could not be measured.
  Discarding a real karst reading to return `unresolved` throws away an
  answer someone paid for.
- If nothing survived, rule `unresolved`.

`unresolved` states that no answer exists at these points. It is a finding,
not a failure, and it must be as specific as any other ruling.

When severity is `unresolved`, `unknowns` is required: name exactly what is
unknown and what evidence would settle it (for example, a geotechnical
boring log, or sampling further from the built-up parcel).

When severity is anything other than `high`, you MUST set
invalidation_condition: the specific trigger-state change that would
require reopening this ruling unprompted. This is not optional — a ruling
with no invalidation condition can never be reawakened, and reawakening is
what makes this a watch rather than a report.
"""


def build_address_agents(con, threat: str) -> tuple[Agent, Agent, Agent]:
    notes = load_calibration_notes(con)
    brief = f"\n\nTHREAT UNDER ARGUMENT — {THREAT_BRIEFS[threat]}\n"

    investigator = Agent[AddressContext](
        name=f"Investigator[{threat}]",
        instructions=compose_guidance(INVESTIGATOR_ROLE + brief, notes, physics=ADDRESS_PHYSICS),
        model=REASONING_MODEL,
        tools=ADDRESS_TOOLS,
        output_type=InvestigatorOutput,
    )
    skeptic = Agent[AddressContext](
        name=f"Skeptic[{threat}]",
        instructions=compose_guidance(SKEPTIC_ROLE + brief, notes, physics=ADDRESS_PHYSICS),
        model=REASONING_MODEL,
        tools=ADDRESS_TOOLS,
        output_type=SkepticOutput,
    )
    adjudicator = Agent[AddressContext](
        name=f"Adjudicator[{threat}]",
        instructions=compose_guidance(ADJUDICATOR_ROLE + brief, notes, physics=ADDRESS_PHYSICS),
        model=REASONING_MODEL,
        output_type=ThreatRuling,
    )
    return investigator, skeptic, adjudicator


def build_triage_agent(con) -> Agent:
    return Agent[AddressContext](
        name="Triage",
        instructions=compose_guidance(TRIAGE_ROLE, load_calibration_notes(con), physics=ADDRESS_PHYSICS),
        model=TRIAGE_MODEL,
        output_type=AddressTriageOutput,
    )


def location_summary(ctx: AddressContext) -> str:
    """The shared opening brief: what was sampled, what came back, and how
    much of it is usable. Given to Triage (which has no tools) and to every
    Investigator as its starting prompt."""
    trigger = None
    from pluvial.memory import dal

    state = dal.current_trigger_state(ctx.con, as_of=ctx.frozen_at, region_key=ctx.region_key)
    if state:
        trigger = {
            "trigger_state": state.get("trigger_state"),
            "as_of": state.get("date"),
            "antecedent_30d_mm": state.get("antecedent_30d_mm"),
            "usdm_class": state.get("usdm_class"),
            "region_key": ctx.region_key,
        }
    return json.dumps(
        {
            "location": {
                "query": ctx.location.get("query_text"),
                "label": ctx.location.get("label"),
                "lat": ctx.location.get("lat"),
                "lon": ctx.location.get("lon"),
            },
            "samples": [summarise_sample(s) for s in ctx.samples],
            "soil_usable_points": sum(1 for s in ctx.samples if s.get("soil_usable")),
            "total_points": len(ctx.samples),
            "moisture": trigger,
        },
        default=str,
    )


def _triage_note(triage: AddressTriageOutput) -> str:
    """Triage's read, appended to every lane's opening brief.

    Without this the shared Triage call is decorative: three cascades would
    each rederive from the same raw summary and Triage's only effect would be
    a line on screen. `focus` in particular is where it earns its keep — it is
    how "the frontage points disagree with the property point" reaches the
    argument that should be built on it.
    """
    lines = [f"\n\nTRIAGE ({triage.decision}): {triage.reason}"]
    if triage.focus:
        lines.append(f"TRIAGE FOCUS: {triage.focus}")
    if triage.decision == "fast_path":
        lines.append("NOTE: Triage flagged this ground as unambiguously severe. Argue with urgency.")
    return "\n".join(lines) + "\n"


async def run_triage(ctx: AddressContext) -> AddressTriageOutput:
    result = await Runner.run(build_triage_agent(ctx.con), location_summary(ctx), context=ctx)
    return result.final_output


async def run_address_cascade(
    ctx: AddressContext, threat: str, summary: str | None = None
) -> tuple[ThreatRuling, InvestigatorOutput, SkepticOutput]:
    """One threat, argued end to end. `ctx` carries the already-fetched
    samples, so nothing here spends a credit."""
    investigator, skeptic, adjudicator = build_address_agents(ctx.con, threat)
    summary = summary or location_summary(ctx)

    inv_result = await Runner.run(investigator, f"Location and sampled ground:\n{summary}", context=ctx)
    investigator_out: InvestigatorOutput = inv_result.final_output

    skep_result = await Runner.run(
        skeptic,
        f"Location and sampled ground:\n{summary}\n\n"
        f"Investigator's case:\n{investigator_out.model_dump_json()}",
        context=ctx,
    )
    skeptic_out: SkepticOutput = skep_result.final_output

    adj_result = await Runner.run(
        adjudicator,
        f"Location and sampled ground:\n{summary}\n\n"
        f"Investigator's case:\n{investigator_out.model_dump_json()}\n\n"
        f"Skeptic's rebuttal:\n{skeptic_out.model_dump_json()}",
        context=ctx,
    )
    ruling: ThreatRuling = adj_result.final_output
    # The Adjudicator names the threat itself; pin it, because a mislabelled
    # ruling would be filed under the wrong lane on the map.
    ruling.threat = threat  # type: ignore[assignment]
    return ruling, investigator_out, skeptic_out


async def run_all_threats(
    ctx: AddressContext, threats: tuple[str, ...] = THREATS
) -> tuple[AddressTriageOutput, dict[str, tuple[ThreatRuling, InvestigatorOutput, SkepticOutput]]]:
    """One shared Triage, then all three cascades concurrently. The three
    arguments are genuinely independent — they cite different fields and can
    reach different severities — so there is no ordering between them and
    running them in sequence would triple the wait for no gain."""
    triage_out = await run_triage(ctx)
    summary = location_summary(ctx) + _triage_note(triage_out)

    results = await asyncio.gather(
        *(run_address_cascade(ctx, threat, summary) for threat in threats)
    )
    return triage_out, dict(zip(threats, results))


def record_rulings(
    con, location_id: int, guidance_version: int,
    results: dict[str, tuple[ThreatRuling, InvestigatorOutput, SkepticOutput]],
) -> dict[str, int]:
    """Persist one ruling per threat. Shape mirrors `live.record_cascade_result`
    so both modes write the same evidence contract."""
    from pluvial.memory import dal

    ids = {}
    for threat, (ruling, investigator_out, skeptic_out) in results.items():
        ids[threat] = dal.record_threat_ruling(
            con,
            dal.ThreatRulingRecord(
                location_id=location_id,
                threat=threat,
                severity=ruling.severity,
                reasoning={
                    "investigator": investigator_out.model_dump() if investigator_out else None,
                    "skeptic": skeptic_out.model_dump() if skeptic_out else None,
                    "adjudicator_explanation": ruling.explanation,
                    "unknowns": ruling.unknowns,
                },
                cited_evidence=[c.model_dump() for c in ruling.decisive_evidence],
                rejected_counter_argument=ruling.rejected_counter_argument,
                invalidation_condition=(
                    ruling.invalidation_condition.model_dump() if ruling.invalidation_condition else None
                ),
                agent_version=f"v{guidance_version}",
            ),
        )
    return ids
