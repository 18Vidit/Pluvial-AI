"""Inverse search: "find me ground near X that is less likely to move".

The repo this project was benchmarked against states it does not do inverse
search at all, so this is a differentiator rather than parity.

Two halves, deliberately split:

- An agent turns a question into a `SearchObjective` and a bounding box. That
  is language work — resolving "near Austin" to a metro extent, deciding
  whether "safe" means foundations or sinkholes.
- `pluvial/geo/explorer.py` runs the traversal. That is arithmetic, and doing
  it with model calls would be dozens of round trips to make choices a
  variance heuristic makes better and instantly.

BUDGET-BOUNDED, NOT TARGET-BOUNDED. The caller sets a credit ceiling; the
traversal spends up to it and returns the best ground found within it,
reporting what it spent. A search that runs out returns partial results
labelled `exhausted_budget=True`. It never silently overspends and it never
silently truncates.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from agents import Agent, Runner
from pydantic import BaseModel, Field

from pluvial.api.events import Event, EventStream
from pluvial.geo.explorer import (
    BBox,
    Cell,
    ScoredCell,
    SearchObjective,
    initial_grid,
    score_cell,
    select_for_subdivision,
    subdivide,
    top_survivors,
)
from pluvial.memory import dal
from pluvial.mireye.client import MireyeClient, MireyeError, chunk_locations
from pluvial.mireye.fields import ALL_FIELDS, is_soil_usable
from pluvial.mireye.accounts import AllAccountsExhausted
from pluvial.mireye.profile_job import BatchLocationFailed, extract_batch_result
from pluvial.mireye.wrapper import CreditCeilingExceeded, RunBudget

PARSER_MODEL = "gpt-4o-mini"

MAX_LEVELS = 3
GRID_DIVISIONS = 4
SUBDIVIDE_PER_LEVEL = 6

PARSER_ROLE = """
You turn a question about a region into a search objective and a bounding
box. You have no tools; answer from what you know.

- `threats`: which of foundation, service_lines, subsidence the asker
  actually cares about. If they said "safe to build on" that is foundation
  and service_lines; "sinkholes" is subsidence; if they were general,
  include all three.
- The bounding box should cover the metro or county they named, in decimal
  degrees. Keep it to roughly 60km across for a metro — a box covering half
  a state samples ground nobody asked about and spends the budget on it.
- `label`: restate what they are looking for in one plain phrase.
- `region_name`: the place, as you understood it.

If the question names no place you can locate, set `resolved` false and say
what you would need.
"""


class RegionQuery(BaseModel):
    resolved: bool
    region_name: str = ""
    label: str = ""
    threats: list[str] = Field(default_factory=list)
    min_lat: float = 0.0
    min_lon: float = 0.0
    max_lat: float = 0.0
    max_lon: float = 0.0
    note: str = Field(default="", description="If unresolved: what is missing")


def build_query_parser() -> Agent:
    return Agent(
        name="RegionQueryParser",
        instructions=PARSER_ROLE,
        model=PARSER_MODEL,
        output_type=RegionQuery,
    )


async def parse_query(question: str) -> RegionQuery:
    result = await Runner.run(build_query_parser(), question)
    return result.final_output


@dataclass
class RegionSearchResult:
    search_id: int
    objective: SearchObjective
    bbox: BBox
    scored: list[ScoredCell]
    survivors: list[ScoredCell]
    credits_spent: int
    credit_budget: int
    exhausted_budget: bool
    levels_completed: int


def _fetch_cells(
    client: MireyeClient, cells: list[Cell], budget: RunBudget, search_id: int, level: int = 0,
) -> tuple[list[tuple[Cell, dict[str, Any]]], bool, str | None]:
    """Fetch every cell's centre point, stopping cleanly at the ceiling.

    The charge is made BEFORE the request, per chunk, so an exhausted budget
    means we did not send the request — not that we sent it and then noticed.

    Returns (fetched, budget_exhausted, upstream_error). A location Mireye
    refuses is NOT recorded as unmeasured ground: writing an empty profile
    would make a failed fetch indistinguishable from real Urban land, and
    the traversal would then score and report ground it never actually read.
    """
    fetched: list[tuple[Cell, dict[str, Any]]] = []
    exhausted = False

    for chunk in chunk_locations([(i, c.lat, c.lon) for i, c in enumerate(cells)], size=25):
        cost = len(ALL_FIELDS) * len(chunk)
        try:
            budget.charge(cost)
        except CreditCeilingExceeded:
            exhausted = True
            break
        try:
            resp = client.fetch_batch(
                ALL_FIELDS, [(lat, lon) for _, lat, lon in chunk],
                idempotency_key=f"region-{search_id}-L{level}-{chunk[0][0]}-{len(chunk)}",
            )
        except (MireyeError, AllAccountsExhausted) as failure:
            # Mireye computes batches asynchronously and a 24-location chunk
            # can sit in "still computing" for minutes; the client gives up
            # after 30 attempts. A traversal that has already scored a level
            # should hand back what it found, labelled partial, rather than
            # losing it to a traceback because the next level was slow.
            budget.spent = max(0, budget.spent - cost)
            return fetched, exhausted, str(failure)
        results = resp.get("results") or resp.get("locations") or []
        for (index, _, _), result in zip(chunk, results):
            try:
                values = extract_batch_result(result, strict=True)
            except BatchLocationFailed as failure:
                # Refund what was not delivered, and stop: whatever refused
                # one location (an exhausted allowance, most likely) will
                # refuse the rest, and spending the remaining ceiling to
                # confirm that helps nobody.
                budget.spent = max(0, budget.spent - len(ALL_FIELDS) * (len(chunk) - len(fetched)))
                return fetched, exhausted, str(failure)
            fetched.append((cells[index], values))

    return fetched, exhausted, None


async def run_region_search(
    con,
    client: MireyeClient,
    question: str,
    credit_budget: int,
    stream: EventStream,
    emit: Callable[[Event], Any],
    lane: str = "region",
) -> RegionSearchResult | None:
    """The traversal. Returns None when the question named no locatable place."""
    parsed = await parse_query(question)
    if not parsed.resolved:
        await emit(stream.make("error", {
            "message": parsed.note or "could not resolve a region from that question",
        }, lane=lane))
        return None

    threats = tuple(t for t in parsed.threats if t in ("foundation", "service_lines", "subsidence"))
    objective = SearchObjective(
        threats=threats or ("foundation", "service_lines", "subsidence"),
        label=parsed.label or question,
    )
    bbox = BBox(parsed.min_lat, parsed.min_lon, parsed.max_lat, parsed.max_lon)

    search_id = dal.create_region_search(con, question, objective.as_dict(), bbox.as_dict(), credit_budget)
    con.commit()

    budget = RunBudget(ceiling=credit_budget)
    scored: list[ScoredCell] = []
    frontier = initial_grid(bbox, GRID_DIVISIONS)
    exhausted = False
    levels_completed = 0

    await emit(stream.make("message", {
        "side": "system",
        "text": f"Searching {parsed.region_name} for {objective.label}. "
                f"Budget {credit_budget} credits; {len(frontier)} cells at level 0.",
    }, lane=lane))

    try:
        levels_completed, exhausted = await _traverse(
            con, client, search_id, frontier, objective, budget, scored, stream, emit, lane,
        )
    finally:
        # Whatever happened, the spend is real and gets recorded. An
        # incomplete search that claims it cost nothing is worse than an
        # incomplete search.
        dal.record_region_spend(con, search_id, budget.spent)
        con.commit()

    survivors = top_survivors(scored, n=3)
    dal.finish_region_search(con, search_id, budget.spent, exhausted)
    con.commit()

    await emit(stream.make("message", {
        "side": "system",
        "text": (
            f"Searched {len(scored)} cells across {levels_completed} level(s) for {budget.spent} credits"
            + (" — budget exhausted, these results are partial." if exhausted else ".")
        ),
    }, lane=lane))

    return RegionSearchResult(
        search_id=search_id,
        objective=objective,
        bbox=bbox,
        scored=scored,
        survivors=survivors,
        credits_spent=budget.spent,
        credit_budget=credit_budget,
        exhausted_budget=exhausted,
        levels_completed=levels_completed,
    )


async def _traverse(
    con, client, search_id: int, frontier: list[Cell], objective: SearchObjective,
    budget: RunBudget, scored: list[ScoredCell], stream: EventStream,
    emit: Callable[[Event], Any], lane: str,
) -> tuple[int, bool]:
    """The level-by-level loop. Returns (levels_completed, budget_exhausted)."""
    exhausted = False
    levels_completed = 0

    for level in range(MAX_LEVELS):
        if not frontier:
            break

        fetched, exhausted, upstream_error = await asyncio.to_thread(
            _fetch_cells, client, frontier, budget, search_id, level
        )
        stream.credits_spent = budget.spent
        if upstream_error:
            await emit(stream.make("error", {
                "message": f"Mireye refused the fetch at level {level}: {upstream_error}",
                "partial": True,
            }, lane=lane))

        level_scored: list[ScoredCell] = []
        for cell, profile in fetched:
            cell_id = dal.create_region_cell(con, search_id, cell.level, cell.lat, cell.lon, cell.bbox.as_dict())
            value = score_cell(profile, objective)
            dal.record_cell_profile(con, cell_id, profile, is_soil_usable(profile), value)
            level_scored.append(ScoredCell(cell=cell, score=value))
            await emit(stream.make("cell_scored", {
                "cell_id": cell_id,
                "level": cell.level,
                "lat": cell.lat, "lon": cell.lon,
                "bbox": cell.bbox.as_dict(),
                "score": value,
                "soil_usable": is_soil_usable(profile),
                "soil_map_unit_name": (profile.get("soil_map_unit_name") or {}).get("value"),
            }, lane=lane))
        con.commit()

        scored.extend(level_scored)
        # Only count a level that actually produced cells. A level whose
        # fetch was refused outright contributed nothing, and reporting
        # "2 levels" for one level of data overstates the search.
        if level_scored:
            levels_completed = level + 1

        if exhausted or upstream_error or level == MAX_LEVELS - 1:
            break

        chosen = select_for_subdivision(level_scored, limit=SUBDIVIDE_PER_LEVEL)
        next_frontier: list[Cell] = []
        for index in chosen:
            parent = level_scored[index]
            next_frontier.extend(subdivide(parent.cell))
            await emit(stream.make("cell_subdivided", {
                "level": parent.cell.level,
                "bbox": parent.cell.bbox.as_dict(),
                "score": parent.score,
            }, lane=lane))
        frontier = next_frontier

    return levels_completed, exhausted


async def adjudicate_survivors(
    con,
    client: MireyeClient,
    result: RegionSearchResult,
    stream: EventStream,
    emit: Callable[[Event], Any],
) -> list[dict[str, Any]]:
    """Full three-cascade adversarial adjudication on the top 3 cells only.

    Each survivor becomes a location carrying ONE sampled point — the cell
    centre that was already fetched during the traversal — so this costs no
    additional credits. That is also the honest framing: a grid returns
    areas, not addresses, and a ruling built on a single centre reading is
    weaker than one built on a nine-point plan. The rulings say so, and the
    two-stage journey is the point: screen the metro here, then analyse a
    specific address properly through the address flow.
    """
    from pluvial.agents.address_stream import stream_all_threats
    from pluvial.agents.context import AddressContext
    from pluvial.agents.address_cascade import record_rulings
    from pluvial.ingest.stations import nearest_station
    from pluvial.mireye.wrapper import MireyeToolWrapper

    from pluvial.ingest import moisture_sync

    out: list[dict[str, Any]] = []
    for rank, survivor in enumerate(result.survivors, start=1):
        cell = survivor.cell
        station, _ = nearest_station(cell.lat, cell.lon)
        # Free (NOAA and the USDM feature service are both keyless) and
        # skipped when the region is already current. Without it a survivor
        # in an unseen metro gets adjudicated with "no moisture history
        # available for this region", which is a gap we chose rather than
        # one the data imposed — a live Austin search produced exactly that.
        await asyncio.to_thread(moisture_sync.ensure_region, station.station_id, cell.lat, cell.lon)
        location_id = dal.create_location(
            con,
            query_text=f"{result.objective.label} — area {rank} of {len(result.survivors)}",
            label=f"area {rank}: {cell.lat:.4f}, {cell.lon:.4f} (level {cell.level} cell)",
            lat=cell.lat, lon=cell.lon, region_key=station.station_id,
        )
        sample_id = dal.create_samples(con, location_id, [("property", cell.lat, cell.lon)])[0]

        stored = dal.search_cells(con, result.search_id)
        profile = next(
            (c["profile"] for c in stored
             if abs(c["lat"] - cell.lat) < 1e-9 and abs(c["lon"] - cell.lon) < 1e-9),
            {},
        ) or {}
        dal.record_sample_profile(con, sample_id, profile, is_soil_usable(profile), client.account.label)
        con.commit()

        location = dal.get_location(con, location_id)
        ctx = AddressContext(
            con=con,
            mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=0)),
            run_budget=RunBudget(ceiling=0),
            guidance_version=dal.latest_guidance_version(con),
            location=location,
            samples=dal.location_samples(con, location_id),
            region_key=location["region_key"],
        )

        await emit(stream.make("message", {
            "side": "system",
            "text": f"Adjudicating area {rank} ({cell.lat:.4f}, {cell.lon:.4f}) — one sampled point, "
                    "so these rulings are area-level guidance, not an address-level answer.",
        }, lane="region"))

        _, results = await stream_all_threats(ctx, stream, emit)
        record_rulings(con, location_id, ctx.guidance_version, results)
        con.commit()

        out.append({
            "rank": rank,
            "location_id": location_id,
            "lat": cell.lat, "lon": cell.lon,
            "objective_score": survivor.score,
            "rulings": {threat: ruling.severity for threat, (ruling, _, _) in results.items()},
        })

    return out
