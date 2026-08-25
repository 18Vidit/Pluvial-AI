"""Structured output contracts for the cascade. Defined first, per the
implementation plan: the Adjudicator's output shape is what the whole
pipeline is built around, and every other agent's output feeds it.

These are Pydantic models used as `output_type` on the OpenAI Agents SDK
agents, so the SDK enforces the shape itself rather than us parsing free
text — a verdict without cited_evidence is a schema validation failure,
not a code-review nit.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TriageDecision = Literal["promote", "fast_path", "discard"]
Disposition = Literal["dispatch", "inspect", "monitor", "close"]
Priority = Literal["critical", "high", "medium", "low"]

# Address mode. Three threats, each independently adjudicated, mapped onto
# mechanisms the system already models:
#   foundation    shrink-swell, bedrock depth, moisture trigger state
#   service_lines shrink-swell + erodibility + drainage
#   subsidence    erodibility, hydrologic group, karst
Threat = Literal["foundation", "service_lines", "subsidence"]

# `unresolved` is the Honesty Gate's output, not a failure state. Where the
# dominant SSURGO component is Urban land there is no soil answer, and
# saying `low` there would read as "safe" — which is a different claim, and
# an unsupported one.
Severity = Literal["high", "elevated", "low", "unresolved"]


class TriageOutput(BaseModel):
    decision: TriageDecision
    reason: str = Field(description="One sentence: why promote/fast_path/discard, citing the dossier summary only")


class AddressTriageOutput(BaseModel):
    """Address mode's triage contract, deliberately missing `discard`.

    Someone typed an address and paid to have the ground under it fetched;
    returning nothing is not an available answer. In particular a location
    where the soil gate fires at every point is the case that most needs to
    reach an `unresolved` ruling with named unknowns — discarding it would
    quietly hand back silence. Enforcing that in the type rather than in the
    prompt is the same choice made everywhere else here: a shape the model
    cannot produce beats an instruction it can overlook.
    """

    decision: Literal["promote", "fast_path"]
    reason: str = Field(description="One sentence: what in the sampled ground drives this, citing the summary only")
    focus: str = Field(
        default="",
        description="Optional: which threat or which sampled points the arguments should concentrate on",
    )


class CitedClaim(BaseModel):
    """One fact used in an argument. field/value/source trace back to a
    specific Mireye field or a specific 311/NCEI/USDM record — never a bare
    assertion (design spec rule 5)."""

    field: str = Field(description="The Mireye field name, or 'moisture_history'/'complaint_history' for non-Mireye evidence")
    value: str
    source: str | None = Field(default=None, description="Mireye's cited source for this field, when applicable")
    interpretation: str = Field(description="What this fact means for this case, one sentence")
    sample_id: int | None = Field(
        default=None,
        description="Which sampled point this fact was read at. Required for any Mireye field in "
        "address mode. Null only for evidence that genuinely has no point — moisture_history is "
        "regional by design, and complaint_history belongs to a street segment, not a sample.",
    )


class InvestigatorOutput(BaseModel):
    claims: list[CitedClaim]
    argument: str = Field(description="The case for imminent failure, built only from the claims above")
    signals_referenced: list[Literal["soil_movement_potential", "movement_trigger_state", "void_formation_likelihood"]]


class SkepticOutput(BaseModel):
    claims: list[CitedClaim]
    argument: str = Field(description="The innocent explanation, built only from the claims above")
    soil_claim_vetoed: bool = Field(
        description="True if the Investigator asserted a soil-based signal on a segment where soil_usable=False "
        "(dominant component is Urban land) — the honesty gate. Vetoes the soil claim only, not the verdict."
    )
    veto_reason: str | None = None
    vetoed_sample_ids: list[int] = Field(
        default_factory=list,
        description="The sampled points the veto invalidates. Naming them is what lets the map grey "
        "out exactly the ground that has no soil answer, instead of the whole location.",
    )


class InvalidationCondition(BaseModel):
    """The physical precondition under which this verdict should be
    reconsidered without anyone asking (design spec §5.1, the Calibrator's
    reawakening loop)."""

    reopen_if_trigger_state_in: list[str] = Field(default_factory=list)
    reopen_if_new_complaints_within_days: int | None = None
    reopen_if_new_complaints_within_m: int | None = None
    reopen_if_new_complaint_count_at_least: int | None = None
    plain_english: str


class Verdict(BaseModel):
    disposition: Disposition
    priority: Priority
    decisive_evidence: list[CitedClaim]
    rejected_counter_argument: str = Field(description="The Skeptic's strongest point and why it didn't change the ruling")
    invalidation_condition: InvalidationCondition | None = Field(
        default=None, description="Required when disposition is 'close' or 'monitor'"
    )
    explanation: str = Field(description="Plain-English verdict summary for the dispatcher board card")


class ThreatInvalidationCondition(BaseModel):
    """Address mode's reopen condition, and deliberately narrower than
    `InvalidationCondition`.

    The 311 version carries complaint-clustering clauses. Address mode has
    no complaint feed and no street segment to cluster on, so a condition
    like "reopen if new complaints emerge within 100m" is not merely inert —
    it is a promise to watch something nothing is watching. A live run
    produced exactly that before this model existed. Removing the fields
    makes the unkeepable promise unrepresentable rather than discouraged.
    """

    reopen_if_trigger_state_in: list[str] = Field(
        default_factory=list,
        description="Moisture trigger states that should reopen this ruling: drying, "
        "sustained_dry, rewetting, stable. This is the ONLY condition that is actually "
        "monitored, so it is the only one worth stating.",
    )
    plain_english: str


class ThreatRuling(BaseModel):
    """Address mode's adjudicated output — the same contract as `Verdict`,
    carrying a threat and a severity instead of a dispatcher disposition and
    priority. Kept as a separate model rather than widened unions on
    `Verdict` so the eval path's shape never moves."""

    threat: Threat
    severity: Severity
    decisive_evidence: list[CitedClaim]
    rejected_counter_argument: str = Field(description="The Skeptic's strongest point and why it didn't change the ruling")
    invalidation_condition: ThreatInvalidationCondition | None = Field(
        default=None, description="Required when severity is anything other than 'high'"
    )
    unknowns: list[str] = Field(
        default_factory=list,
        description="Required when severity is 'unresolved': what specifically is unknown, and what "
        "evidence would settle it. An unresolved ruling with no unknowns is a shrug, not an answer.",
    )
    explanation: str = Field(description="Plain-English summary of this ruling for the person who asked")
