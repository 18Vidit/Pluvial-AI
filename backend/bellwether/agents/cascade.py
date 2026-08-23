"""The Triage -> (Investigator, Skeptic) -> Adjudicator cascade (design
spec §5.1). Built bottom-up per the implementation plan: the Adjudicator's
Verdict contract (agents/models.py) was defined first, everything upstream
produces evidence for it.
"""
from __future__ import annotations

from agents import Agent, ModelSettings, Runner

from bellwether.agents.context import CascadeContext
from bellwether.agents.guidance import compose_guidance, load_calibration_notes
from bellwether.agents.models import InvestigatorOutput, SkepticOutput, TriageOutput, Verdict
from bellwether.agents.tools import ALL_TOOLS

TRIAGE_MODEL = "gpt-4o-mini"
REASONING_MODEL = "gpt-4o"

TRIAGE_ROLE = """
You are Triage. You see one new 311 complaint plus its segment's cached
dossier summary (soil class, current trigger state, recent complaint
count on this segment) — you do NOT have live tool access; do not attempt
to fetch anything. Decide fast:

- discard: routine noise with nothing in the dossier suggesting otherwise
  (e.g. a single low-pressure report on Urban-land soil in a stable
  trigger state, no prior complaints).
- promote: send to full investigation. Promote when soil is movement-prone
  AND/OR the trigger state is drying/rewetting AND/OR there's already a
  recent complaint on this segment — clustering is itself a promotion
  reason even on unfavourable soil.
- fast_path: the complaint's own case type is already an emergency
  (e.g. "Major Water Leak") — skip straight to dispatch-level urgency
  without discarding the adversarial review.
"""

INVESTIGATOR_ROLE = """
You are the Investigator. Build the strongest evidence-based case that this
complaint is an early symptom of imminent main failure. Call
dossier_lookup and mireye_profile first, then moisture_history, then
neighbourhood_complaints and precedent_search as needed. Every claim needs
a field, a value, and an interpretation. If soil_usable is False for this
segment, do not make a shrink-swell or drainage claim — argue from
complaint clustering and moisture trigger alone if the case exists there.
"""

SKEPTIC_ROLE = """
You are the Skeptic. Build the strongest case that this complaint has an
innocent, non-structural explanation: hydrant flushing nearby (check
neighbourhood_complaints for flush-type entries), a private-side leak
(inside the property line, not the city's main), recent main work already
scheduled, or natural surface water (check usgs_gage). You hold the
HONESTY GATE: if the Investigator's argument relies on a soil signal (shrink-
swell, drainage, erodibility) for a segment where soil_usable is False, set
soil_claim_vetoed=True and explain why — the soil claim is invalid, not
necessarily the whole case. A complaint cluster or a dangerous trigger-state
transition is still valid evidence even when soil is silent.
"""

ADJUDICATOR_ROLE = """
You are the Adjudicator. You receive the Investigator's case and the
Skeptic's rebuttal (including any soil-claim veto). Rule dispatch, inspect,
monitor, or close. decisive_evidence must be drawn only from claims
actually presented by the Investigator or Skeptic — do not invent new
evidence. If soil_claim_vetoed is True, you may not cite a soil field as
decisive evidence; you must decide on the remaining evidence (clustering,
trigger state, symptom type) alone, and say so explicitly if that leaves
the case weaker.

If disposition is 'close' or 'monitor', you MUST set invalidation_condition:
state the specific trigger-state or complaint-clustering change that would
require re-opening this case unprompted. This is not optional — a closed
case with no invalidation condition can never be reawakened.
"""


def build_agents(con, guidance_version: int | None = None, ablation: str | None = None) -> tuple[Agent, Agent, Agent, Agent]:
    """ablation: None for the real cascade, or 'no_moisture'/'no_memory' to
    run the design spec §8 ablation study — same tools, same prompts, minus
    one instruction block, so the comparison isolates what that signal adds."""
    from bellwether.eval.ablation import apply_ablation

    notes = load_calibration_notes(con)

    triage = Agent[CascadeContext](
        name="Triage",
        instructions=apply_ablation(compose_guidance(TRIAGE_ROLE, notes), ablation),
        model=TRIAGE_MODEL,
        output_type=TriageOutput,
    )

    investigator = Agent[CascadeContext](
        name="Investigator",
        instructions=apply_ablation(compose_guidance(INVESTIGATOR_ROLE, notes), ablation),
        model=REASONING_MODEL,
        tools=ALL_TOOLS,
        output_type=InvestigatorOutput,
    )

    skeptic = Agent[CascadeContext](
        name="Skeptic",
        instructions=apply_ablation(compose_guidance(SKEPTIC_ROLE, notes), ablation),
        model=REASONING_MODEL,
        tools=ALL_TOOLS,
        output_type=SkepticOutput,
    )

    adjudicator = Agent[CascadeContext](
        name="Adjudicator",
        instructions=compose_guidance(ADJUDICATOR_ROLE, notes),
        model=REASONING_MODEL,
        output_type=Verdict,
    )

    return triage, investigator, skeptic, adjudicator


async def run_cascade(
    con,
    ctx: CascadeContext,
    complaint_summary: str,
    dossier_summary: str,
    ablation: str | None = None,
) -> tuple[TriageOutput, Verdict | None, InvestigatorOutput | None, SkepticOutput | None]:
    triage, investigator, skeptic, adjudicator = build_agents(con, ablation=ablation)

    triage_result = await Runner.run(
        triage,
        f"New complaint:\n{complaint_summary}\n\nDossier summary:\n{dossier_summary}",
        context=ctx,
    )
    triage_out: TriageOutput = triage_result.final_output

    if triage_out.decision == "discard":
        return triage_out, None, None, None

    urgency_note = "\n\nNOTE: Triage fast-pathed this as an emergency case type." if triage_out.decision == "fast_path" else ""

    investigator_result = await Runner.run(
        investigator,
        f"Complaint:\n{complaint_summary}{urgency_note}",
        context=ctx,
    )
    investigator_out: InvestigatorOutput = investigator_result.final_output

    skeptic_result = await Runner.run(
        skeptic,
        f"Complaint:\n{complaint_summary}\n\nInvestigator's case:\n{investigator_out.model_dump_json()}",
        context=ctx,
    )
    skeptic_out: SkepticOutput = skeptic_result.final_output

    adjudicator_result = await Runner.run(
        adjudicator,
        (
            f"Complaint:\n{complaint_summary}\n\n"
            f"Investigator's case:\n{investigator_out.model_dump_json()}\n\n"
            f"Skeptic's rebuttal:\n{skeptic_out.model_dump_json()}"
        ),
        context=ctx,
    )
    verdict: Verdict = adjudicator_result.final_output

    return triage_out, verdict, investigator_out, skeptic_out
