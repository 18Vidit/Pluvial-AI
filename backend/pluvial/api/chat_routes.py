"""The conversational surface.

Same SSE envelope as `/analyze/run`, on purpose: when a chat turn proposes a
fetch and the person confirms it, the map paints exactly as it does for the
address box. One event contract, one client reducer, one set of visual
states — the alternative is a second, subtly different path through the same
UI that drifts the first time either side changes.

Thread state lives in this process, keyed by session id, and dies with the
server. That is a deliberate scope call from the design: persisted chat
memory was one of the expensive parts of the Neon port design and none of it
is needed to interrogate a result you are looking at.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from agents import Runner
from agents.items import ToolCallItem
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pluvial import analyze
from pluvial.agents.address_stream import merge_to_queue
from pluvial.agents.orchestrator import OrchestratorContext, PendingSpend, build_orchestrator
from pluvial.api.analyze_routes import SSE_HEADERS
from pluvial.api.events import EventStream
from pluvial.memory import dal
from pluvial.mireye.accounts import MireyeClientPool, NoMireyeAccountConfigured
from pluvial.mireye.fields import ALL_FIELDS, is_soil_usable
from pluvial.mireye.profile_job import extract_batch_result

router = APIRouter()

TOOL_LABELS = {
    "explain_ruling": "reading back the ruling record",
    "explain_veto": "reading the Skeptic's veto",
    "compare_samples": "comparing two sampled points",
    "sample_point": "quoting one new point",
    "analyze_location": "quoting a new address",
    "search_region": "quoting a regional search",
}


class Session:
    """One conversation about one location. `history` is the SDK's own input
    list, so a follow-up question sees the previous turn's tool results
    rather than re-deriving them."""

    def __init__(self, session_id: str, location_id: int):
        self.session_id = session_id
        self.location_id = location_id
        self.history: list[Any] = []
        self.pending: dict[str, PendingSpend] = {}


SESSIONS: dict[str, Session] = {}


def _session(session_id: str, location_id: int) -> Session:
    session = SESSIONS.get(session_id)
    if session is None or session.location_id != location_id:
        session = Session(session_id, location_id)
        SESSIONS[session_id] = session
    return session


class ChatRequest(BaseModel):
    session_id: str
    location_id: int
    message: str


async def _chat_events(req: ChatRequest) -> AsyncIterator[str]:
    stream = EventStream()

    try:
        pool = MireyeClientPool()
    except NoMireyeAccountConfigured as e:
        yield stream.make("error", {"message": str(e)}).to_sse()
        return

    with dal.connect() as con, pool as client:
        location = dal.get_location(con, req.location_id)
        if location is None:
            yield stream.make("error", {"message": "unknown location"}).to_sse()
            return

        session = _session(req.session_id, req.location_id)
        ctx = OrchestratorContext(
            con=con,
            location=location,
            samples=dal.location_samples(con, req.location_id),
            rulings=dal.location_rulings(con, req.location_id),
            region_key=location["region_key"],
            stream=stream,
            emit=lambda event: None,   # replaced below, once the queue exists
            client=client,
            pending=session.pending,
        )
        agent = build_orchestrator(con, ctx.location, ctx.samples, ctx.rulings)

        async def produce(emit) -> None:
            ctx.emit = emit
            result = Runner.run_streamed(
                agent, session.history + [{"role": "user", "content": req.message}], context=ctx
            )
            async for event in result.stream_events():
                if event.type != "run_item_stream_event":
                    continue
                if event.name == "tool_called" and isinstance(event.item, ToolCallItem):
                    name = getattr(event.item.raw_item, "name", "tool")
                    await emit(stream.make("tool_call", {
                        "tool": name, "label": TOOL_LABELS.get(name, name), "status": "called",
                    }, lane="chat"))
            session.history = result.to_input_list()
            await emit(stream.make(
                "message", {"side": "assistant", "text": str(result.final_output)}, lane="chat"
            ))

        try:
            async for event in merge_to_queue(produce):
                yield event.to_sse()
        except Exception as exc:
            yield stream.make("error", {"message": str(exc)}).to_sse()
            return

        yield stream.make("done", {"credits_spent": stream.credits_spent}, lane="chat").to_sse()


@router.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """One turn. Spends nothing by itself — a turn that wants to buy ground
    emits a `quote` and stops there."""
    return StreamingResponse(_chat_events(req), media_type="text/event-stream", headers=SSE_HEADERS)


async def _confirm_events(session_id: str, pending_id: str) -> AsyncIterator[str]:
    """Honour a quote the orchestrator put up. This is the ONLY path by which
    a chat turn can spend, and it exists behind a separate request precisely
    so the agent cannot reach it."""
    stream = EventStream()
    session = SESSIONS.get(session_id)
    pending = session.pending.get(pending_id) if session else None
    if pending is None:
        yield stream.make("error", {"message": "no such quote, or it expired with the session"}).to_sse()
        return

    try:
        pool = MireyeClientPool()
    except NoMireyeAccountConfigured as e:
        yield stream.make("error", {"message": str(e)}).to_sse()
        return

    # Consumed on use: a quote is honoured once. Without this, a repeated
    # confirm would re-buy ground already paid for.
    del session.pending[pending_id]

    if pending.kind == "search_region":
        with dal.connect() as con, pool as client:
            from pluvial.agents.region_search import adjudicate_survivors, run_region_search

            async def produce(emit) -> None:
                result = await run_region_search(
                    con, client, pending.payload["query"], pending.payload["credit_budget"],
                    stream, emit,
                )
                if result is None:
                    return
                summary = await adjudicate_survivors(con, client, result, stream, emit)
                await emit(stream.make("message", {
                    "side": "system",
                    "text": (
                        f"Top {len(summary)} areas adjudicated. "
                        f"Spent {result.credits_spent} of a {result.credit_budget} ceiling"
                        + (" — budget exhausted, partial results." if result.exhausted_budget else ".")
                    ),
                    "areas": summary,
                    "exhausted_budget": result.exhausted_budget,
                }, lane="region"))

            try:
                async for event in merge_to_queue(produce):
                    yield event.to_sse()
            except Exception as exc:
                yield stream.make("error", {"message": str(exc)}).to_sse()
                return
        yield stream.make("done", {"credits_spent": stream.credits_spent}, lane="region").to_sse()
        return

    if pending.kind == "analyze_location":
        yield stream.make("confirmed" if False else "message", {
            "side": "system",
            "text": "Confirmed. Running the full pipeline on the new address.",
            "location_id": pending.payload["location_id"],
            "handoff": "analyze_run",
        }, lane="chat").to_sse()
        yield stream.make("done", {
            "handoff": "analyze_run", "location_id": pending.payload["location_id"],
        }, lane="chat").to_sse()
        return

    sample_id = pending.payload["sample_id"]
    lat, lon = pending.payload["lat"], pending.payload["lon"]

    with dal.connect() as con, pool as client:
        def fetch() -> dict[str, Any]:
            resp = client.fetch_batch(ALL_FIELDS, [(lat, lon)],
                                      idempotency_key=f"chat-sample-{sample_id}")
            results = resp.get("results") or resp.get("locations") or []
            if not results:
                raise RuntimeError("Mireye returned no result for that point")
            return extract_batch_result(results[0], strict=True)

        try:
            values = await asyncio.to_thread(fetch)
        except Exception as exc:
            yield stream.make("error", {"message": f"fetch failed: {exc}"}).to_sse()
            return

        soil_usable = is_soil_usable(values)
        dal.record_sample_profile(con, sample_id, values, soil_usable, client.account.label)
        con.commit()

        stream.spend(len(values))
        yield stream.make("point_profiled", {
            "sample_id": sample_id, "lat": lat, "lon": lon,
            "soil_usable": soil_usable, "profile": values,
        }).to_sse()
        yield stream.make("message", {
            "side": "system",
            "text": (
                f"Point {sample_id} fetched. "
                + ("SSURGO has a real soil component here."
                   if soil_usable
                   else "The dominant component here is Urban land — no soil answer at this point.")
            ),
        }, lane="chat").to_sse()
        yield stream.make("done", {"credits_spent": stream.credits_spent}, lane="chat").to_sse()


@router.post("/chat/confirm")
async def chat_confirm(session_id: str, pending_id: str) -> StreamingResponse:
    return StreamingResponse(
        _confirm_events(session_id, pending_id), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/chat/confirm")
async def chat_confirm_get(session_id: str, pending_id: str) -> StreamingResponse:
    return await chat_confirm(session_id, pending_id)
