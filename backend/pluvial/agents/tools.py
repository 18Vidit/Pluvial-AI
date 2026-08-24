"""Function tools available to the Investigator and Skeptic. Every tool
call is scoped to the current CascadeContext — a fresh dossier/mireye/budget
per complaint being judged, never leaking state across cases."""
from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from bellwether.agents.context import CascadeContext
from bellwether.memory import dal


@function_tool
def mireye_profile(wrapper: RunContextWrapper[CascadeContext], segment_id: int, lat: float, lng: float) -> str:
    """Get the cached physical profile (soil, drainage, karst, consequence
    fields) for a street segment. Cache-first; only reaches Mireye's API on
    a genuine miss, and refuses if that miss would exceed this run's credit
    budget. Returns JSON with `profile`, `soil_usable`, and `source`."""
    ctx = wrapper.context
    result = ctx.mireye.mireye_profile(segment_id, lat, lng)
    return json.dumps(result)


@function_tool
def dossier_lookup(wrapper: RunContextWrapper[CascadeContext], segment_id: int) -> str:
    """Get the full stored history for a segment: its profile plus every
    prior verdict recorded against it. Use this before arguing anything —
    it tells you what has already been decided about this ground."""
    ctx = wrapper.context
    seg = dal.get_segment(ctx.con, segment_id)
    verdicts = ctx.con.execute(
        "SELECT verdict_id, disposition, priority, decided_at, reasoning_json FROM verdicts "
        "WHERE segment_id = ? ORDER BY decided_at DESC LIMIT 10",
        (segment_id,),
    ).fetchall()
    return json.dumps({
        "segment": seg,
        "prior_verdicts": [dict(v) for v in verdicts],
    }, default=str)


@function_tool
def neighbourhood_complaints(wrapper: RunContextWrapper[CascadeContext], segment_id: int, days: int = 30) -> str:
    """Get other 311 complaints on this same street segment within the last
    N days. Use this to check for clustering (multiple independent reports)
    and for innocent explanations (e.g. a hydrant-flush complaint nearby)."""
    ctx = wrapper.context
    rows = dal.neighbourhood_complaints(ctx.con, segment_id, days=days)
    return json.dumps(rows, default=str)


@function_tool
def moisture_history(wrapper: RunContextWrapper[CascadeContext], as_of_date: str | None = None) -> str:
    """Get the current (or, during a backtest, the as-of) Movement Trigger
    State: the city-wide antecedent-moisture trajectory and the coarse
    USDM drought corroborator. This is city-wide, not per-segment — it
    tells you WHEN the ground is moving, soil tells you WHERE it moves a lot."""
    ctx = wrapper.context
    state = dal.current_trigger_state(ctx.con, as_of=as_of_date or ctx.frozen_at)
    return json.dumps(state, default=str)


@function_tool
def precedent_search(
    wrapper: RunContextWrapper[CascadeContext],
    shrink_swell_class: str,
    trigger_state: str,
    symptom_class: str,
) -> str:
    """Find resolved past cases with the same soil class, trigger state and
    symptom class, and how they turned out. Use to ground a claim in
    precedent rather than first-principles reasoning alone."""
    ctx = wrapper.context
    rows = ctx.con.execute(
        """
        SELECT p.*, v.disposition AS verdict_disposition, v.decided_at
        FROM precedents p JOIN verdicts v ON v.verdict_id = p.verdict_id
        WHERE p.shrink_swell_class = ? AND p.trigger_state = ? AND p.symptom_class = ?
        ORDER BY v.decided_at DESC LIMIT 10
        """,
        (shrink_swell_class, trigger_state, symptom_class),
    ).fetchall()
    return json.dumps([dict(r) for r in rows], default=str)


@function_tool
def usgs_gage(wrapper: RunContextWrapper[CascadeContext], lat: float, lng: float) -> str:
    """Check the nearest USGS stream gage's current discharge, to help rule
    out natural surface water as the source of a 'standing water' complaint."""
    ctx = wrapper.context
    result = ctx.mireye.mireye_profile(
        segment_id=-abs(hash((round(lat, 4), round(lng, 4)))) % (10**9),
        lat=lat, lng=lng,
    )
    profile = result.get("profile", {})
    gage_fields = {k: v for k, v in profile.items() if "usgs_gage" in k}
    return json.dumps(gage_fields, default=str)


ALL_TOOLS = [mireye_profile, dossier_lookup, neighbourhood_complaints, moisture_history, precedent_search, usgs_gage]
