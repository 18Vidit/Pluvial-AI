"""The traversal loop itself, against a real database and a fake Mireye.

Separate from test_explorer.py, which covers the pure rules. What is
covered here is the loop's bookkeeping under partial failure — the case
that actually happened live, where Mireye answered one level and then
refused the next.
"""
from __future__ import annotations

import asyncio

import pytest

from pluvial.agents.region_search import _traverse
from pluvial.api.events import EventStream
from pluvial.geo.explorer import BBox, SearchObjective, initial_grid
from pluvial.memory import dal
from pluvial.mireye.wrapper import RunBudget

AUSTIN = BBox(30.05, -98.05, 30.55, -97.55)


def _good_soil(n):
    return {"results": [
        {"index": i, "ok": True, "fields": {
            "soil_shrink_swell_class": {"value": "Low"},
            "soil_drainage_class": {"value": "Well drained"},
            "soil_erodibility_k_factor": {"value": 0.1},
            "soil_map_unit_name": {"value": "Test silty clay"},
            "bedrock_depth_cm": {"value": 200},
            "in_karst_area": {"value": False},
        }}
        for i in range(n)
    ]}


def _at_capacity(n):
    return {"results": [
        {"index": i, "ok": False,
         "error": {"error": "service_at_capacity",
                   "message": "The /fetch service is at capacity on this worker — retry shortly."}}
        for i in range(n)
    ]}


class _OneGoodLevelClient:
    """Answers the first chunk, then reports itself at capacity — exactly
    what Mireye did during a live Austin search."""

    def __init__(self):
        self.calls = 0

    def fetch_batch(self, fields, locations, idempotency_key=None):
        self.calls += 1
        return _good_soil(len(locations)) if self.calls == 1 else _at_capacity(len(locations))


async def _run(db, client, budget):
    search_id = dal.create_region_search(
        db, "test", SearchObjective().as_dict(), AUSTIN.as_dict(), budget.ceiling
    )
    scored = []
    stream = EventStream()
    events = []

    async def emit(event):
        events.append(event)

    levels, exhausted = await _traverse(
        db, client, search_id, initial_grid(AUSTIN, 4), SearchObjective(),
        budget, scored, stream, emit, "region",
    )
    return levels, exhausted, scored, events


def test_a_refused_level_keeps_the_level_before_it(db):
    client = _OneGoodLevelClient()
    budget = RunBudget(ceiling=100_000)

    levels, exhausted, scored, events = asyncio.run(_run(db, client, budget))

    assert len(scored) == 16, "the level Mireye did answer is kept"
    assert levels == 1, "a level that fetched nothing is not counted as completed"
    assert exhausted is False, "the budget was fine; Mireye was at capacity"
    assert any(e.type == "error" and e.payload.get("partial") for e in events), \
        "the refusal must reach the client as a labelled partial, not silence"


def test_the_refused_level_is_not_charged(db):
    client = _OneGoodLevelClient()
    budget = RunBudget(ceiling=100_000)
    asyncio.run(_run(db, client, budget))

    from pluvial.mireye.fields import ALL_FIELDS
    assert budget.spent == len(ALL_FIELDS) * 16, "only the level that arrived is paid for"


def test_no_cell_row_is_written_for_ground_that_was_never_read(db):
    """The rule the whole strict-fetch change exists for: an empty profile
    would score as unmeasured ground and read on the map as 'no soil answer
    here', which is a false statement about the world."""
    client = _OneGoodLevelClient()
    asyncio.run(_run(db, client, RunBudget(ceiling=100_000)))

    rows = db.execute("SELECT profile_json FROM region_cells").fetchall()
    assert len(rows) == 16
    assert all(r["profile_json"] for r in rows)
