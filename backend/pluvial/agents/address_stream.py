"""Streaming wrapper around the address-mode cascade.

Everything here exists for one reason: an adversarial cascade takes 60-90
seconds and the demo's whole claim is that nothing is precomputed. A spinner
would make live work look exactly like a cached result. Tool calls streamed
as they happen are what prove otherwise, and they are the events that keep
the interface moving during the long gaps between an agent starting and an
agent producing structured output.

Three cascades run concurrently and are merged into a single asyncio.Queue,
each event tagged with its lane. The queue is the merge point rather than
three separate SSE streams because the credit counter, the map and the
lanes all have to stay in step with each other, and three channels would
mean three clocks.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable

from agents import Agent, Runner
from agents.items import ToolCallItem, ToolCallOutputItem

from pluvial.agents.address_cascade import (
    THREATS,
    _triage_note,
    build_address_agents,
    build_triage_agent,
    location_summary,
)
from pluvial.agents.context import AddressContext
from pluvial.agents.models import (
    AddressTriageOutput,
    InvestigatorOutput,
    SkepticOutput,
    ThreatRuling,
)
from pluvial.api.events import Event, EventStream

Emit = Callable[[Event], Any]

# The SDK reports a tool call by function name; these are what the UI shows
# instead, so a lane reads as an argument being built rather than as a log.
TOOL_LABELS = {
    "sampled_profiles": "reading all sampled points",
    "sample_detail": "pulling full field values for a point",
    "compare_samples": "comparing two points",
    "moisture_history": "checking the regional moisture trigger",
    "usgs_gage": "checking the nearest stream gage",
    "consequence_surface": "checking what a failure would cost",
    "precedent_search": "searching resolved precedents",
}


def _tool_name(item: ToolCallItem) -> str:
    raw = item.raw_item
    return getattr(raw, "name", None) or getattr(raw, "type", "tool")


def _tool_arguments(item: ToolCallItem) -> dict[str, Any]:
    import json

    raw_args = getattr(item.raw_item, "arguments", None)
    if not raw_args:
        return {}
    try:
        parsed = json.loads(raw_args)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _run_streamed(
    agent: Agent, prompt: str, ctx: AddressContext, lane: str, stage: str,
    stream: EventStream, emit: Emit,
) -> Any:
    """Run one agent, translating SDK stream events into our envelope.

    Only tool calls are surfaced. Token deltas are deliberately not: the
    agents produce structured output, so a partial token stream would show
    half-formed JSON, and the useful signal — which evidence is being
    pulled — is exactly what a tool call carries.
    """
    await emit(stream.make("stage", {"stage": stage, "agent": agent.name, "status": "started"}, lane=lane))

    result = Runner.run_streamed(agent, prompt, context=ctx)
    async for event in result.stream_events():
        if event.type != "run_item_stream_event":
            continue
        if event.name == "tool_called" and isinstance(event.item, ToolCallItem):
            name = _tool_name(event.item)
            args = _tool_arguments(event.item)
            await emit(stream.make(
                "tool_call",
                {
                    "stage": stage,
                    "tool": name,
                    "label": TOOL_LABELS.get(name, name),
                    "sample_id": args.get("sample_id"),
                    "arguments": args,
                    "status": "called",
                },
                lane=lane,
            ))
        elif event.name == "tool_output" and isinstance(event.item, ToolCallOutputItem):
            await emit(stream.make(
                "tool_call",
                {"stage": stage, "status": "returned"},
                lane=lane,
            ))

    await emit(stream.make("stage", {"stage": stage, "agent": agent.name, "status": "finished"}, lane=lane))
    return result.final_output


async def stream_threat_cascade(
    ctx: AddressContext, threat: str, summary: str, stream: EventStream, emit: Emit,
    lane: str | None = None,
) -> tuple[ThreatRuling, InvestigatorOutput, SkepticOutput]:
    """`lane` is the SSE tag events are streamed under and defaults to
    `threat` — the primary address flow's normal behaviour. Region search's
    survivor adjudication passes a distinct, prefixed lane (see
    `stream_all_threats`'s `lane_prefix`) so its claims and rulings cannot
    land on whatever "foundation"/"service_lines"/"subsidence" lane a
    browser tab already has mounted for an unrelated address. `ruling.threat`
    is untouched by this — that field is the true threat and other code
    keys off it."""
    lane = lane or threat
    investigator, skeptic, adjudicator = build_address_agents(ctx.con, threat)

    investigator_out: InvestigatorOutput = await _run_streamed(
        investigator, f"Location and sampled ground:\n{summary}",
        ctx, lane, "investigator", stream, emit,
    )
    for claim in investigator_out.claims:
        await emit(stream.make("claim", {"side": "investigator", **claim.model_dump()}, lane=lane))
    await emit(stream.make(
        "message",
        {"side": "investigator", "text": investigator_out.argument},
        lane=lane,
    ))

    skeptic_out: SkepticOutput = await _run_streamed(
        skeptic,
        f"Location and sampled ground:\n{summary}\n\n"
        f"Investigator's case:\n{investigator_out.model_dump_json()}",
        ctx, lane, "skeptic", stream, emit,
    )
    for claim in skeptic_out.claims:
        await emit(stream.make("claim", {"side": "skeptic", **claim.model_dump()}, lane=lane))
    await emit(stream.make("message", {"side": "skeptic", "text": skeptic_out.argument}, lane=lane))
    if skeptic_out.soil_claim_vetoed:
        await emit(stream.make(
            "veto",
            {
                "reason": skeptic_out.veto_reason,
                "sample_ids": skeptic_out.vetoed_sample_ids,
            },
            lane=lane,
        ))

    ruling: ThreatRuling = await _run_streamed(
        adjudicator,
        f"Location and sampled ground:\n{summary}\n\n"
        f"Investigator's case:\n{investigator_out.model_dump_json()}\n\n"
        f"Skeptic's rebuttal:\n{skeptic_out.model_dump_json()}",
        ctx, lane, "adjudicator", stream, emit,
    )
    ruling.threat = threat  # type: ignore[assignment]
    await emit(stream.make("ruling", ruling.model_dump(), lane=lane))
    return ruling, investigator_out, skeptic_out


async def stream_all_threats(
    ctx: AddressContext, stream: EventStream, emit: Emit, threats: tuple[str, ...] = THREATS,
    lane_prefix: str = "",
) -> tuple[AddressTriageOutput, dict[str, tuple[ThreatRuling, InvestigatorOutput, SkepticOutput]]]:
    """lane_prefix scopes every event this run emits to lanes the primary
    address view cannot mistake for its own.

    Every caller of this function shares one browser tab's event stream:
    the primary `/analyze/run`, a chat-driven `analyze_location`, and
    region search's per-survivor adjudication in `adjudicate_survivors` can
    all be in flight against the same session. Without a prefix, a survivor
    cascade's own "foundation" claims and "system" triage would land on
    literally the same lanes the primary analysis is using, and a live run
    demonstrated exactly that: a chat-driven regional search overwrote the
    triage line and threat-lane rulings of the Georgetown address the user
    was actively looking at. The default ("") preserves the primary flow's
    plain lane names unchanged.
    """
    summary = location_summary(ctx)
    system_lane = f"{lane_prefix}system"

    triage_out: AddressTriageOutput = await _run_streamed(
        build_triage_agent(ctx.con), summary, ctx, system_lane, "triage", stream, emit,
    )
    await emit(stream.make("triage", triage_out.model_dump(), lane=system_lane))
    summary += _triage_note(triage_out)

    results = await asyncio.gather(
        *(
            stream_threat_cascade(ctx, threat, summary, stream, emit, lane=f"{lane_prefix}{threat}")
            for threat in threats
        )
    )
    return triage_out, dict(zip(threats, results))


async def merge_to_queue(
    producer: Callable[[Emit], Any], queue_maxsize: int = 0,
) -> AsyncIterator[Event]:
    """Run `producer`, yielding every event it emits as it emits them.

    The producer is given an `emit` that puts onto a queue; a sentinel closes
    the iteration when it finishes or raises. Exceptions are re-raised on the
    consumer side rather than swallowed, so a failed cascade becomes a
    visible `error` event instead of a stream that just stops.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
    DONE = object()

    async def emit(event: Event) -> None:
        await queue.put(event)

    async def run() -> None:
        try:
            await producer(emit)
        except Exception as exc:  # surfaced to the client as an error event
            await queue.put(exc)
        finally:
            await queue.put(DONE)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        if not task.done():
            task.cancel()
