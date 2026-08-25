"""Address mode's HTTP surface: plan, run, and chat.

Split into its own router rather than piled into `app.py`, which is already
the Houston evaluation surface (queue, segments, verdicts, stats). The two
sets of endpoints answer to different products and it should be obvious
from the file list which is which.

The split between `/analyze/plan` and `/analyze/run` is the credit gate, and
it is structural rather than advisory: `plan` has no code path that can
fetch, and `run` cannot be reached without a `location_id` that only `plan`
mints. Nothing spends a credit that a person did not click for.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pluvial import analyze
from pluvial.agents.address_stream import merge_to_queue, stream_all_threats
from pluvial.agents.context import AddressContext
from pluvial.api.events import Event, EventStream
from pluvial.memory import dal
from pluvial.mireye.accounts import NoMireyeAccountConfigured, primary_account
from pluvial.mireye.client import MireyeClient
from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget

router = APIRouter()

# Streaming responses must not be buffered by an intermediary, or every
# event arrives at once at the end — which would look exactly like the
# precomputed result this whole design exists to disprove.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class PlanRequest(BaseModel):
    address: str


@router.post("/analyze/plan")
def analyze_plan(req: PlanRequest) -> dict[str, Any]:
    """Geocode, lay out the nine sample points, and quote them. Spends nothing.

    The quote comes from Mireye's own /v1/fetch/quote, so the number shown
    to the user is the number that will be billed, not an estimate this
    codebase computed and hoped would match.
    """
    try:
        account = primary_account()
    except NoMireyeAccountConfigured as e:
        raise HTTPException(status_code=400, detail=str(e))

    with dal.connect() as con, MireyeClient(account, timeout=60.0) as client:
        try:
            plan = analyze.plan(con, req.address, client)
        except analyze.GeocodeFailed as e:
            raise HTTPException(status_code=404, detail=str(e))
    return {**plan.as_dict(), "credits_spent": 0}


async def _analysis_events(location_id: int, run_budget_ceiling: int) -> AsyncIterator[str]:
    """The whole run as SSE frames: fetch, then three concurrent cascades.

    Blocking work (the Mireye batch, the NOAA sync) is pushed to a thread so
    the event loop stays free to flush frames — otherwise the points would
    all paint at once when the batch returned, instead of the map filling in
    as results land.
    """
    stream = EventStream()

    try:
        account = primary_account()
    except NoMireyeAccountConfigured as e:
        yield stream.make("error", {"message": str(e)}).to_sse()
        return

    with dal.connect() as con, MireyeClient(account, timeout=60.0) as client:
        location = dal.get_location(con, location_id)
        if location is None:
            yield stream.make("error", {"message": f"no plan {location_id}; POST /analyze/plan first"}).to_sse()
            return

        planned = dal.location_samples(con, location_id)
        if any(s.get("profile") for s in planned):
            yield stream.make("error", {
                "message": f"plan {location_id} has already been fetched; "
                           "re-plan rather than paying for the same ground twice",
            }).to_sse()
            return

        yield stream.make("location", {
            "location_id": location_id,
            "label": location["label"],
            "query_text": location["query_text"],
            "lat": location["lat"],
            "lon": location["lon"],
            "region_key": location["region_key"],
        }).to_sse()
        for s in planned:
            yield stream.make("sample_planned", {
                "sample_id": s["sample_id"], "role": s["role"], "lat": s["lat"], "lon": s["lon"],
            }).to_sse()

        plan = analyze.AnalysisPlan(
            location_id=location_id, query_text=location["query_text"], label=location["label"],
            lat=location["lat"], lon=location["lon"], region_key=location["region_key"],
            station_name="", station_distance_m=0.0,
            samples=[{"sample_id": s["sample_id"], "role": s["role"], "lat": s["lat"], "lon": s["lon"]}
                     for s in planned],
            fields=[], quoted_credits=0, quote_raw={},
        )

        # The moisture sync is free (NOAA + USDM are keyless) and usually a
        # no-op, but the first time a region is seen it pulls 120 days from
        # NOAA and can take tens of seconds. It is announced rather than done
        # silently: the alternative is a stretch at the start of the run where
        # the map has points on it and nothing appears to be happening, which
        # is exactly the impression this design exists to avoid.
        yield stream.make("stage", {"stage": "moisture", "status": "started",
                                    "label": f"resolving regional moisture history ({location['region_key']})"}).to_sse()
        synced = await asyncio.to_thread(analyze.ensure_moisture, plan)
        yield stream.make("stage", {
            "stage": "moisture", "status": "finished",
            "label": f"synced {synced} days from NOAA" if synced else "moisture history already current",
        }).to_sse()

        yield stream.make("stage", {"stage": "fetch", "status": "started",
                                    "label": f"fetching {len(planned)} points from Mireye"}).to_sse()

        loop = asyncio.get_running_loop()
        point_queue: asyncio.Queue = asyncio.Queue()

        def on_point(record: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(point_queue.put_nowait, record)

        fetch_task = asyncio.create_task(
            asyncio.to_thread(analyze.fetch_samples, con, client, plan, on_point)
        )
        pending = len(planned)
        while pending:
            get = asyncio.create_task(point_queue.get())
            done, _ = await asyncio.wait({get, fetch_task}, return_when=asyncio.FIRST_COMPLETED)
            if get in done:
                record = get.result()
                pending -= 1
                profile = record["profile"]
                stream.spend(len(profile))
                yield stream.make("point_profiled", {
                    "sample_id": record["sample_id"],
                    "lat": record["lat"], "lon": record["lon"],
                    "soil_usable": record["soil_usable"],
                    "profile": profile,
                }).to_sse()
            elif fetch_task.done():
                get.cancel()
                failure = fetch_task.exception()
                if failure is not None:
                    # Including a refused location. The run stops here rather
                    # than arguing over ground it never read.
                    yield stream.make("error", {
                        "message": str(failure),
                        "points_fetched": len(planned) - pending,
                    }).to_sse()
                    return
                break
        await fetch_task
        yield stream.make("stage", {"stage": "fetch", "status": "finished"}).to_sse()

        samples = dal.location_samples(con, location_id)
        ctx = AddressContext(
            con=con,
            # ceiling=0: the ground was bought before the cascade started, on
            # a plan the user confirmed. An agent mid-argument does not get
            # to buy more.
            mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=0)),
            run_budget=RunBudget(ceiling=run_budget_ceiling),
            guidance_version=dal.latest_guidance_version(con),
            location=location,
            samples=samples,
            region_key=location["region_key"],
        )

        results: dict[str, Any] = {}

        async def produce(emit) -> None:
            _, res = await stream_all_threats(ctx, stream, emit)
            results.update(res)

        try:
            async for event in merge_to_queue(produce):
                yield event.to_sse()
        except Exception as exc:
            yield stream.make("error", {"message": str(exc)}).to_sse()
            return

        from pluvial.agents.address_cascade import record_rulings

        ruling_ids = record_rulings(con, location_id, ctx.guidance_version, results)
        con.commit()
        yield stream.make("done", {
            "location_id": location_id,
            "ruling_ids": ruling_ids,
            "credits_spent": stream.credits_spent,
        }).to_sse()


@router.get("/analyze/run")
async def analyze_run_get(location_id: int, run_budget_ceiling: int = 500) -> StreamingResponse:
    """The confirmed run: fetch the planned points and argue over them.

    GET rather than POST because SSE's browser client (`EventSource`) cannot
    send a body, and this request has nothing to send — the plan is already
    persisted and `location_id` identifies it. Calling this IS the
    confirmation; `/analyze/plan` never spends, and this is the only path
    that does. `POST` is accepted at the same path for `curl -N` parity.
    """
    return StreamingResponse(
        _analysis_events(location_id, run_budget_ceiling),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/analyze/run")
async def analyze_run_post(location_id: int, run_budget_ceiling: int = 500) -> StreamingResponse:
    return await analyze_run_get(location_id, run_budget_ceiling)


@router.get("/analyze/{location_id}")
def analysis_detail(location_id: int) -> dict[str, Any]:
    """Everything recorded about one analysed location: the samples with
    their raw field values and sources, and the rulings. This is what makes
    a result checkable after the stream has closed."""
    with dal.connect() as con:
        location = dal.get_location(con, location_id)
        if location is None:
            raise HTTPException(status_code=404, detail="location not found")
        samples = dal.location_samples(con, location_id)
        rulings = dal.location_rulings(con, location_id)
        moisture = dal.current_trigger_state(con, region_key=location["region_key"])
    return {"location": location, "samples": samples, "rulings": rulings, "moisture": moisture}
