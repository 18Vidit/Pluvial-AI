"""The traversal's rules, tested without a single fetch.

Adaptive search is non-deterministic in its exploration path, which makes
the loop itself awkward to test. The rules it follows are not, and they are
what decides where the credits go — so they are what is pinned here.
"""
from __future__ import annotations

import pytest

from pluvial.geo.explorer import (
    BBox,
    Cell,
    ScoredCell,
    SearchObjective,
    initial_grid,
    neighbour_disagreement,
    score_cell,
    select_for_subdivision,
    subdivide,
    top_survivors,
)

AUSTIN = BBox(30.05, -98.05, 30.55, -97.55)


def profile(**fields):
    return {k: {"value": v, "source": "test"} for k, v in fields.items()}


BENIGN = profile(
    soil_shrink_swell_class="Low",
    soil_drainage_class="Well drained",
    soil_erodibility_k_factor=0.10,
    bedrock_depth_cm=250,
    in_karst_area=False,
)
HOSTILE = profile(
    soil_shrink_swell_class="Very High",
    soil_drainage_class="Poorly drained",
    soil_erodibility_k_factor=0.55,
    bedrock_depth_cm=20,
    in_karst_area=True,
    karst_exposure_class="Exposed",
)
URBAN_LAND = profile(soil_map_unit_name="Urban land-Udorthents complex")


def test_benign_ground_outscores_hostile_ground():
    objective = SearchObjective()
    assert score_cell(BENIGN, objective) > 0.8
    assert score_cell(HOSTILE, objective) < 0.2


def test_unmeasured_ground_scores_none_not_zero():
    """The distinction is load-bearing. Zero would tell the traversal this is
    bad ground; None says it is unmeasured ground, which is a different fact
    and leads to a different decision."""
    assert score_cell(URBAN_LAND, SearchObjective()) is None


def test_objective_changes_what_the_score_responds_to():
    """A query about sinkholes must not be steered by shrink-swell, and a
    query about foundations must not be steered by karst."""
    karst_only = profile(
        soil_shrink_swell_class="Very High",  # terrible for foundations
        in_karst_area=False,                  # fine for subsidence
        soil_erodibility_k_factor=0.10,
        soil_drainage_class="Well drained",
    )
    foundation = score_cell(karst_only, SearchObjective(threats=("foundation",)))
    subsidence = score_cell(karst_only, SearchObjective(threats=("subsidence",)))
    assert subsidence > foundation


def test_karst_exposure_grades_rather_than_flipping():
    exposed = score_cell(
        profile(in_karst_area=True, karst_exposure_class="Exposed"),
        SearchObjective(threats=("subsidence",)),
    )
    covered = score_cell(
        profile(in_karst_area=True, karst_exposure_class="Covered"),
        SearchObjective(threats=("subsidence",)),
    )
    assert exposed < covered


def test_initial_grid_tiles_the_bbox_exactly():
    cells = initial_grid(AUSTIN, divisions=4)
    assert len(cells) == 16
    assert min(c.bbox.min_lat for c in cells) == pytest.approx(AUSTIN.min_lat)
    assert max(c.bbox.max_lat for c in cells) == pytest.approx(AUSTIN.max_lat)
    assert min(c.bbox.min_lon for c in cells) == pytest.approx(AUSTIN.min_lon)
    assert max(c.bbox.max_lon for c in cells) == pytest.approx(AUSTIN.max_lon)


def test_subdivision_quarters_a_cell_and_loses_no_area():
    parent = initial_grid(AUSTIN, divisions=2)[0]
    children = subdivide(parent)
    assert len(children) == 4
    assert all(c.level == parent.level + 1 for c in children)
    parent_area = (parent.bbox.max_lat - parent.bbox.min_lat) * (parent.bbox.max_lon - parent.bbox.min_lon)
    child_area = sum(
        (c.bbox.max_lat - c.bbox.min_lat) * (c.bbox.max_lon - c.bbox.min_lon) for c in children
    )
    assert child_area == pytest.approx(parent_area)


def test_a_child_centre_is_never_the_parent_centre():
    """Every child is a genuinely new sample, so subdividing always buys new
    information rather than re-fetching a point already on file."""
    parent = initial_grid(AUSTIN, divisions=2)[0]
    for child in subdivide(parent):
        assert (child.lat, child.lon) != (parent.lat, parent.lon)


def _row(lat, lon, score, level=0, size=0.05):
    return ScoredCell(cell=Cell(level, BBox(lat, lon, lat + size, lon + size)), score=score)


def test_disagreement_is_high_at_a_boundary_and_zero_on_uniform_ground():
    uniform = [_row(30.0 + i * 0.05, -98.0, 0.8) for i in range(4)]
    boundary = [_row(30.0, -98.0, 0.9), _row(30.05, -98.0, 0.1), _row(30.10, -98.0, 0.9)]
    assert max(neighbour_disagreement(uniform).values()) == pytest.approx(0.0, abs=1e-9)
    assert max(neighbour_disagreement(boundary).values()) > 0.3


def test_selection_prefers_contested_ground_over_uniformly_good_ground():
    """Refining ground that is already uniformly good mostly buys resolution
    nobody asked for; refining good ground whose neighbours disagree buys
    information about where the boundary actually runs."""
    cells = [
        _row(30.00, -98.00, 0.85),   # good, and its one neighbour agrees
        _row(30.05, -98.00, 0.85),
        _row(30.30, -98.00, 0.80),   # slightly worse, but a boundary runs through it
        _row(30.35, -98.00, 0.10),
    ]
    chosen = select_for_subdivision(cells, limit=1)
    assert chosen == [2]


def test_a_boundary_cell_beats_a_higher_scoring_uniform_one():
    uniform = [_row(30.00, -98.00, 0.90), _row(30.05, -98.00, 0.90)]
    contested = [_row(30.30, -98.00, 0.70), _row(30.35, -98.00, 0.10)]
    chosen = select_for_subdivision(uniform + contested, limit=1)
    assert chosen == [2]


def test_unmeasured_cells_are_never_selected_or_returned_as_survivors():
    cells = [_row(30.0, -98.0, None), _row(30.2, -98.0, 0.4)]
    assert select_for_subdivision(cells, limit=5) == [1]
    survivors = top_survivors(cells, n=3)
    assert len(survivors) == 1 and survivors[0].score == 0.4


def test_survivors_come_back_best_first():
    cells = [_row(30.0, -98.0, 0.2), _row(30.2, -98.0, 0.9), _row(30.4, -98.0, 0.5)]
    assert [sc.score for sc in top_survivors(cells, n=3)] == [0.9, 0.5, 0.2]


# --- budget enforcement -------------------------------------------------------

class _FakeClient:
    """Stands in for MireyeClient. Records what it was asked to fetch so the
    test can assert that an exhausted budget means the request was never
    sent — not that it was sent and the overspend noticed afterwards."""

    def __init__(self):
        self.calls: list[int] = []

    def fetch_batch(self, fields, locations, idempotency_key=None):
        self.calls.append(len(locations))
        return {"results": [{"ok": True, "fields": {"soil_shrink_swell_class": {"value": "Low"}}}
                            for _ in locations]}


def test_budget_stops_the_traversal_before_the_request_not_after():
    from pluvial.agents.region_search import _fetch_cells
    from pluvial.mireye.fields import ALL_FIELDS
    from pluvial.mireye.wrapper import RunBudget

    cells = initial_grid(AUSTIN, divisions=6)          # 36 cells -> two chunks
    client = _FakeClient()
    # Enough for the first chunk of 25 and nowhere near enough for the second.
    budget = RunBudget(ceiling=len(ALL_FIELDS) * 25 + 5)

    fetched, exhausted, upstream_error = _fetch_cells(client, cells, budget, search_id=1)

    assert exhausted is True
    assert upstream_error is None
    assert client.calls == [25], "the second chunk must never have been requested"
    assert len(fetched) == 25, "partial results are returned, not discarded"
    assert budget.spent <= budget.ceiling


def test_a_traversal_inside_its_budget_is_not_reported_as_exhausted():
    from pluvial.agents.region_search import _fetch_cells
    from pluvial.mireye.wrapper import RunBudget

    cells = initial_grid(AUSTIN, divisions=3)          # 9 cells, one chunk
    client = _FakeClient()
    fetched, exhausted, upstream_error = _fetch_cells(client, cells, RunBudget(ceiling=10_000), search_id=1)

    assert exhausted is False
    assert upstream_error is None
    assert len(fetched) == 9


def test_a_refused_location_stops_the_traversal_instead_of_recording_empty_ground():
    """The failure this pins was found live. When the Mireye account's monthly
    allowance ran out mid-traversal, every remaining cell was written with an
    empty profile — which scores as unmeasured ground and reads on the map as
    "no soil answer here". The traversal has to stop and say so."""
    from pluvial.agents.region_search import _fetch_cells
    from pluvial.mireye.wrapper import RunBudget

    class _RefusingClient:
        def __init__(self):
            self.calls = 0

        def fetch_batch(self, fields, locations, idempotency_key=None):
            self.calls += 1
            return {"results": [
                {"index": 0, "ok": False,
                 "error": {"error": "credits_exhausted", "message": "Monthly allowance exhausted"}}
                for _ in locations
            ]}

    cells = initial_grid(AUSTIN, divisions=3)
    client = _RefusingClient()
    fetched, exhausted, upstream_error = _fetch_cells(
        client, cells, RunBudget(ceiling=10_000), search_id=1
    )

    assert fetched == [], "nothing may be recorded from a refused batch"
    assert exhausted is False, "the budget was fine; Mireye was not"
    assert upstream_error and "exhausted" in upstream_error.lower()


def test_a_mireye_timeout_returns_what_was_already_found():
    """Mireye computes batches asynchronously and a 24-location chunk can sit
    in "still computing" for minutes before the client gives up. A traversal
    that has already scored a level must hand back what it found, labelled
    partial, rather than losing it to a traceback."""
    from pluvial.agents.region_search import _fetch_cells
    from pluvial.mireye.client import MireyeError
    from pluvial.mireye.fields import ALL_FIELDS
    from pluvial.mireye.wrapper import RunBudget

    class _SlowThenDeadClient:
        def __init__(self):
            self.calls = 0

        def fetch_batch(self, fields, locations, idempotency_key=None):
            self.calls += 1
            if self.calls == 1:
                return {"results": [{"index": i, "ok": True, "fields": {"elevation": {"value": 1}}}
                                    for i in range(len(locations))]}
            raise MireyeError("still not resolved on /v1/fetch/batch after 30 retries")

    cells = initial_grid(AUSTIN, divisions=7)      # 49 cells -> two chunks
    client = _SlowThenDeadClient()
    budget = RunBudget(ceiling=100_000)

    fetched, exhausted, upstream_error = _fetch_cells(client, cells, budget, search_id=1)

    assert len(fetched) == 25, "the first chunk's results survive"
    assert exhausted is False, "the budget was fine; Mireye was slow"
    assert upstream_error and "still not resolved" in upstream_error
    assert budget.spent == len(ALL_FIELDS) * 25, "the chunk that never arrived is not charged"
