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


class TriageOutput(BaseModel):
    decision: TriageDecision
    reason: str = Field(description="One sentence: why promote/fast_path/discard, citing the dossier summary only")


class CitedClaim(BaseModel):
    """One fact used in an argument. field/value/source trace back to a
    specific Mireye field or a specific 311/NCEI/USDM record — never a bare
    assertion (design spec rule 5)."""

    field: str = Field(description="The Mireye field name, or 'moisture_history'/'complaint_history' for non-Mireye evidence")
    value: str
    source: str | None = Field(default=None, description="Mireye's cited source for this field, when applicable")
    interpretation: str = Field(description="What this fact means for this case, one sentence")


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
