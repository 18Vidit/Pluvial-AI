"""Lane isolation across concurrent SSE consumers.

A live chat-driven regional search demonstrated a real bug: `adjudicate_
survivors` reused the exact "foundation"/"service_lines"/"subsidence"/
"system" lanes the primary `/analyze/run` view was already using in the
same browser tab, so a survivor's own triage line and threat rulings
silently overwrote the address the user was looking at. These tests pin
the fix — that every event `stream_all_threats` emits carries the lane its
caller asked for — without spending a model call or a credit: the agents
and the SDK's streaming are stubbed out, and only the `lane=` argument each
`stream.make(...)` call receives is asserted.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pluvial.agents.models import (
    AddressTriageOutput,
    CitedClaim,
    InvestigatorOutput,
    SkepticOutput,
    ThreatInvalidationCondition,
    ThreatRuling,
)
from pluvial.api.events import EventStream


def _fake_investigator_out():
    return InvestigatorOutput(
        claims=[CitedClaim(field="soil_shrink_swell_class", value="High", interpretation="x", sample_id=1)],
        argument="the case",
        signals_referenced=["soil_movement_potential"],
    )


def _fake_skeptic_out():
    return SkepticOutput(claims=[], argument="the rebuttal", soil_claim_vetoed=False)


def _fake_ruling(threat):
    return ThreatRuling(
        threat=threat, severity="elevated", decisive_evidence=[],
        rejected_counter_argument="none",
        invalidation_condition=ThreatInvalidationCondition(plain_english="x"),
        unknowns=[], explanation="e",
    )


@pytest.fixture()
def stubbed_agents(monkeypatch):
    """Replace every real agent call with a fixed output, and make
    Runner.run_streamed produce no stream events — the fastest possible
    stand-in, since only lane tagging is under test here."""
    import pluvial.agents.address_stream as m

    async def fake_run_streamed(agent, prompt, ctx, lane, stage, stream, emit):
        await emit(stream.make("stage", {"stage": stage, "status": "started"}, lane=lane))
        await emit(stream.make("stage", {"stage": stage, "status": "finished"}, lane=lane))
        if stage == "triage":
            return AddressTriageOutput(decision="promote", reason="r")
        if stage == "investigator":
            return _fake_investigator_out()
        if stage == "skeptic":
            return _fake_skeptic_out()
        return _fake_ruling(getattr(ctx, "_current_threat", "foundation"))

    monkeypatch.setattr(m, "_run_streamed", fake_run_streamed)
    monkeypatch.setattr(m, "build_triage_agent", lambda con: object())
    monkeypatch.setattr(m, "build_address_agents", lambda con, threat: (object(), object(), object()))
    monkeypatch.setattr(m, "location_summary", lambda ctx: "summary")
    monkeypatch.setattr(m, "_triage_note", lambda triage: "")
    yield m


def test_the_primary_run_uses_plain_unprefixed_lanes(stubbed_agents):
    from pluvial.agents.address_stream import stream_all_threats

    async def go():
        ctx = SimpleNamespace(con=None)
        stream = EventStream()
        events = []

        async def emit(event):
            events.append(event)

        await stream_all_threats(ctx, stream, emit, threats=("foundation",))
        return events

    events = asyncio.run(go())
    lanes = {e.lane for e in events}
    assert lanes == {"system", "foundation"}, "unchanged default behaviour for the address a user is viewing"


def test_a_prefixed_run_never_touches_the_plain_lanes(stubbed_agents):
    """This is the exact call adjudicate_survivors makes per ranked area."""
    from pluvial.agents.address_stream import stream_all_threats

    async def go():
        ctx = SimpleNamespace(con=None)
        stream = EventStream()
        events = []

        async def emit(event):
            events.append(event)

        await stream_all_threats(ctx, stream, emit, threats=("foundation",), lane_prefix="region-1-")
        return events

    events = asyncio.run(go())
    lanes = {e.lane for e in events}
    assert lanes == {"region-1-system", "region-1-foundation"}
    assert "system" not in lanes
    assert "foundation" not in lanes, (
        "a survivor's ruling must never land on the bare 'foundation' lane — that is the "
        "lane the primary analysis view is reading its ruling from"
    )


def test_two_survivors_adjudicated_in_the_same_stream_cannot_collide(stubbed_agents):
    """adjudicate_survivors runs one lane_prefix per rank in the same
    process, sharing one EventStream. Two survivors' lanes must be
    distinguishable from each other, not just from the primary view."""
    from pluvial.agents.address_stream import stream_all_threats

    async def go():
        stream = EventStream()
        events = []

        async def emit(event):
            events.append(event)

        ctx = SimpleNamespace(con=None)
        await stream_all_threats(ctx, stream, emit, threats=("foundation",), lane_prefix="region-1-")
        await stream_all_threats(ctx, stream, emit, threats=("foundation",), lane_prefix="region-2-")
        return events

    events = asyncio.run(go())
    lanes = {e.lane for e in events}
    assert lanes == {"region-1-system", "region-1-foundation", "region-2-system", "region-2-foundation"}


def test_the_ruling_events_own_threat_field_is_unaffected_by_the_lane(stubbed_agents):
    """The lane is a transport tag; ruling.threat is domain data other code
    keys off (record_rulings groups by it) and must stay the real threat
    name regardless of which lane carried the event."""
    from pluvial.agents.address_stream import stream_all_threats

    async def go():
        ctx = SimpleNamespace(con=None)
        stream = EventStream()
        events = []

        async def emit(event):
            events.append(event)

        await stream_all_threats(ctx, stream, emit, threats=("foundation",), lane_prefix="region-3-")
        return events

    events = asyncio.run(go())
    ruling_events = [e for e in events if e.type == "ruling"]
    assert len(ruling_events) == 1
    assert ruling_events[0].payload["threat"] == "foundation"
    assert ruling_events[0].lane == "region-3-foundation"
