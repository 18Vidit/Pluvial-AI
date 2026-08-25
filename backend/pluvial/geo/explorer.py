"""Deterministic traversal for regional search.

Division of labour, following the principle already stated in
`calibrator.py` ("no reason to spend a model call computing a precision
score"): the agent parses a question into an objective and a bounding box,
and everything below decides where to look next. Cell selection by LLM would
be dozens of round trips to make choices a variance heuristic makes better
and instantly.

THE BOUNDARY THAT MATTERS: the objective score is a SEARCH HEURISTIC and
never a verdict. The design's core commitment is that no formula combines
the signals — the agents argue over thresholded facts. This function decides
only where to spend the next credit. Nothing it produces reaches a user as a
risk judgment; every ruling still comes from the adversarial cascade with
cited evidence. That boundary is the obvious thing for a reviewer to
challenge, so it is stated here, in the code that does the scoring.

All pure. Adaptive traversal is non-deterministic in its exploration path,
which makes the loop hard to test; the rules it follows are not, and they
are what is tested.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from pluvial.geo.sample_plan import haversine_m

Threat = Literal["foundation", "service_lines", "subsidence"]


@dataclass(frozen=True)
class BBox:
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.min_lat + self.max_lat) / 2, (self.min_lon + self.max_lon) / 2

    def as_dict(self) -> dict[str, float]:
        return {
            "min_lat": self.min_lat, "min_lon": self.min_lon,
            "max_lat": self.max_lat, "max_lon": self.max_lon,
        }

    def width_m(self) -> float:
        lat, _ = self.center
        return haversine_m(lat, self.min_lon, lat, self.max_lon)


@dataclass(frozen=True)
class Cell:
    level: int
    bbox: BBox

    @property
    def lat(self) -> float:
        return self.bbox.center[0]

    @property
    def lon(self) -> float:
        return self.bbox.center[1]


@dataclass(frozen=True)
class SearchObjective:
    """What "good ground" means for one query.

    `threats` is what the asker cares about; a query about foundations
    should not be steered by karst, and one about sinkholes should not be
    steered by shrink-swell.
    """

    threats: tuple[Threat, ...] = ("foundation", "service_lines", "subsidence")
    label: str = "ground least likely to damage what is built on it"

    def as_dict(self) -> dict[str, Any]:
        return {"threats": list(self.threats), "label": self.label}


def _value(profile: dict[str, Any], field: str) -> Any:
    entry = (profile or {}).get(field)
    return entry.get("value") if isinstance(entry, dict) else entry


# Thresholded facts, not weights. Each maps a class the literature already
# treats as discrete onto "how favourable is this for the threat", 0 worst to
# 1 best. Nothing here is fitted; changing a number changes where the search
# looks, never what a ruling says.
SHRINK_SWELL_FAVOURABILITY = {"low": 1.0, "moderate": 0.6, "high": 0.25, "very high": 0.0}
DRAINAGE_FAVOURABILITY = {
    "excessively drained": 0.9,
    "somewhat excessively drained": 0.95,
    "well drained": 1.0,
    "moderately well drained": 0.8,
    "somewhat poorly drained": 0.5,
    "poorly drained": 0.25,
    "very poorly drained": 0.1,
}


def _class_score(raw: Any, table: dict[str, float]) -> float | None:
    if raw is None:
        return None
    return table.get(str(raw).strip().lower())


def _erodibility_score(raw: Any) -> float | None:
    """SSURGO K factor, roughly 0.02 (very resistant) to 0.69 (very
    erodible). Clamped and inverted so 1 is resistant ground."""
    if raw is None:
        return None
    try:
        k = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, (0.45 - k) / 0.43))


def _bedrock_score(raw: Any) -> float | None:
    """Shallow bedrock amplifies movement — less soil column to absorb it.
    Flat above 2m, where the column stops being the constraint."""
    if raw is None:
        return None
    try:
        cm = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, cm / 200.0))


def _karst_score(profile: dict[str, Any]) -> float | None:
    in_karst = _value(profile, "in_karst_area")
    if in_karst is None:
        return None
    if not in_karst or str(in_karst).lower() in ("false", "no", "0"):
        return 1.0
    exposure = str(_value(profile, "karst_exposure_class") or "").strip().lower()
    return {"exposed": 0.0, "shallow": 0.2, "covered": 0.5, "deep": 0.7}.get(exposure, 0.3)


THREAT_COMPONENTS: dict[str, tuple[str, ...]] = {
    "foundation": ("shrink_swell", "bedrock", "drainage"),
    "service_lines": ("shrink_swell", "erodibility", "drainage"),
    "subsidence": ("karst", "erodibility", "drainage"),
}


def component_scores(profile: dict[str, Any]) -> dict[str, float | None]:
    return {
        "shrink_swell": _class_score(_value(profile, "soil_shrink_swell_class"), SHRINK_SWELL_FAVOURABILITY),
        "drainage": _class_score(_value(profile, "soil_drainage_class"), DRAINAGE_FAVOURABILITY),
        "erodibility": _erodibility_score(_value(profile, "soil_erodibility_k_factor")),
        "bedrock": _bedrock_score(_value(profile, "bedrock_depth_cm")),
        "karst": _karst_score(profile),
    }


def score_cell(profile: dict[str, Any], objective: SearchObjective) -> float | None:
    """How promising this ground looks FOR SEARCH PURPOSES, 0 to 1, or None
    when nothing relevant could be read.

    None is not zero and the difference is load-bearing. A cell mapped as
    Urban land has no soil answer; scoring it 0 would tell the traversal
    that it is bad ground, when what is true is that it is unmeasured ground.
    The explorer treats None as a reason not to refine rather than a reason
    to avoid, and the survivors it adjudicates are drawn only from cells that
    actually scored.
    """
    components = component_scores(profile)
    relevant: list[float] = []
    for threat in objective.threats:
        for name in THREAT_COMPONENTS[threat]:
            value = components.get(name)
            if value is not None:
                relevant.append(value)
    if not relevant:
        return None
    # A mean, not a weighted sum: there is no evidence for a weighting, and
    # inventing one would be exactly the "combine the signals into a single
    # number" move the whole design refuses. It is defensible only because
    # this decides where to look, never what is true.
    return sum(relevant) / len(relevant)


def initial_grid(bbox: BBox, divisions: int = 4) -> list[Cell]:
    """A sparse level-0 grid. `divisions` squared cells, each sampled at its
    centre."""
    cells = []
    lat_step = (bbox.max_lat - bbox.min_lat) / divisions
    lon_step = (bbox.max_lon - bbox.min_lon) / divisions
    for i in range(divisions):
        for j in range(divisions):
            cells.append(Cell(level=0, bbox=BBox(
                min_lat=bbox.min_lat + i * lat_step,
                min_lon=bbox.min_lon + j * lon_step,
                max_lat=bbox.min_lat + (i + 1) * lat_step,
                max_lon=bbox.min_lon + (j + 1) * lon_step,
            )))
    return cells


def subdivide(cell: Cell) -> list[Cell]:
    """Four quadrants, one level deeper. Note that the parent's centre point
    is not any child's centre, so every child is a genuinely new sample —
    but the parent's reading is still what justified refining here, which is
    why it stays on file rather than being discarded."""
    b = cell.bbox
    mid_lat, mid_lon = b.center
    return [
        Cell(cell.level + 1, BBox(b.min_lat, b.min_lon, mid_lat, mid_lon)),
        Cell(cell.level + 1, BBox(b.min_lat, mid_lon, mid_lat, b.max_lon)),
        Cell(cell.level + 1, BBox(mid_lat, b.min_lon, b.max_lat, mid_lon)),
        Cell(cell.level + 1, BBox(mid_lat, mid_lon, b.max_lat, b.max_lon)),
    ]


@dataclass(frozen=True)
class ScoredCell:
    cell: Cell
    score: float | None


def neighbour_disagreement(scored: list[ScoredCell], radius_multiplier: float = 1.6) -> dict[int, float]:
    """Per-cell spread of its neighbours' scores, keyed by index.

    High spread means a map-unit boundary probably runs through here, which
    is the second reason to refine: the cell's single centre reading is
    least trustworthy exactly where the ground is changing fastest.
    """
    out: dict[int, float] = {}
    for i, sc in enumerate(scored):
        if sc.score is None:
            out[i] = 0.0
            continue
        reach = sc.cell.bbox.width_m() * radius_multiplier
        nearby = [
            other.score
            for j, other in enumerate(scored)
            if j != i and other.score is not None
            and haversine_m(sc.cell.lat, sc.cell.lon, other.cell.lat, other.cell.lon) <= reach
        ]
        # One neighbour is enough to disagree with. Requiring two would make
        # every edge cell permanently certain, which is exactly backwards:
        # the edge of the search box is where the least is known.
        if not nearby:
            out[i] = 0.0
            continue
        out[i] = statistics.pstdev([sc.score, *nearby])
    return out


def select_for_subdivision(
    scored: list[ScoredCell],
    limit: int,
    promise_weight: float = 1.0,
    uncertainty_weight: float = 1.2,
) -> list[int]:
    """Indices of the cells worth refining, best first.

    Two criteria, both cheap and both defensible: *promising* (scores well
    for the objective, so finer detail there is what the asker wants) and
    *uncertain* (neighbours disagree, so the single reading is least
    reliable). Uncertainty is weighted slightly higher because refining
    contested ground buys information, while refining ground that is already
    uniformly good mostly buys resolution nobody asked for.
    """
    disagreement = neighbour_disagreement(scored)
    ranked = sorted(
        (
            (promise_weight * sc.score + uncertainty_weight * disagreement.get(i, 0.0), i)
            for i, sc in enumerate(scored)
            if sc.score is not None
        ),
        reverse=True,
    )
    return [i for _, i in ranked[:limit]]


def top_survivors(scored: Iterable[ScoredCell], n: int = 3) -> list[ScoredCell]:
    """The best-scoring measured cells, which are the only ones that go on to
    full adversarial adjudication. Unmeasured cells are excluded rather than
    ranked last — there is nothing to adjudicate at a point with no data."""
    return sorted(
        (sc for sc in scored if sc.score is not None), key=lambda sc: sc.score, reverse=True
    )[:n]
