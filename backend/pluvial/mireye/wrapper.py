"""The only Mireye surface agents are allowed to call.

Design spec §5.2: "Mireye is wrapped, not exposed raw. Anything the model
can call, it will call." Agents get `mireye_profile(segment_id)` — never
raw quote/fetch — so quote-first and cache-first cannot be bypassed by a
model deciding to be thorough. The study area is pre-profiled in bulk
before any agent runs (Phase 2); this wrapper's fetch path exists only for
the rare segment that slips through un-profiled (e.g. a complaint outside
the original stratified sample), and it enforces a hard per-run ceiling so
one agent run cannot blow the budget.
"""
from __future__ import annotations

import psycopg
from dataclasses import dataclass
from typing import Any

from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount, MireyeClient, QuoteExceedsCeilingError
from pluvial.mireye.fields import ALL_FIELDS, is_soil_usable


class CreditCeilingExceeded(RuntimeError):
    pass


@dataclass
class RunBudget:
    """Tracks credit spend for a single agent run (one cascade pass over one
    complaint). Reset per run, never shared across runs."""

    ceiling: int
    spent: int = 0

    def charge(self, credits: int) -> None:
        if self.spent + credits > self.ceiling:
            raise CreditCeilingExceeded(
                f"run would spend {self.spent + credits} credits, ceiling is {self.ceiling}"
            )
        self.spent += credits


class MireyeToolWrapper:
    def __init__(self, con: psycopg.Connection, client: MireyeClient, run_budget: RunBudget):
        self.con = con
        self.client = client
        self.run_budget = run_budget

    def mireye_profile(
        self, segment_id: int, lat: float, lng: float, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Cache-first physical profile for a segment. Never bypassed by
        agents: this is the only path an agent has to Mireye.
        force_refresh=True (admin reprofile only, never set by an agent)
        skips the cache read but still goes through quote-then-charge-then-
        fetch, so the ceiling guard still applies."""
        cached = dal.get_segment(self.con, segment_id)
        if not force_refresh and cached and cached.get("profile"):
            return {
                "segment_id": segment_id,
                "profile": cached["profile"],
                "soil_usable": bool(cached["soil_usable"]),
                "source": "cache",
                "profiled_at": cached["profiled_at"],
            }

        # Cache miss: quote, charge against this run's budget, then fetch.
        quote = self.client.quote(ALL_FIELDS, locations=1)
        credits = quote.get("credits") or quote.get("total_credits") or len(ALL_FIELDS)
        try:
            self.run_budget.charge(int(credits))
        except CreditCeilingExceeded:
            raise

        result = self.client.fetch_one(ALL_FIELDS, lat, lng)
        values = _extract_field_values(result)
        soil_usable = is_soil_usable(values)

        dal.upsert_segment(
            self.con, segment_id, name=None, highway_class=None,
            centroid_lat=lat, centroid_lon=lng,
            profile=values, soil_usable=soil_usable,
            mireye_account=self.client.account.label,
        )
        self.con.commit()

        return {
            "segment_id": segment_id,
            "profile": values,
            "soil_usable": soil_usable,
            "source": "live_fetch",
            "profiled_at": dal.now_iso(),
        }


def _extract_field_values(fetch_response: dict[str, Any]) -> dict[str, Any]:
    """Normalize a /v1/fetch response into {field_name: value}, keeping the
    per-field source citation alongside the value (design spec rule 5:
    every value carries the field it drove and the source it came from)."""
    fields = fetch_response.get("fields") or fetch_response.get("data") or fetch_response
    out: dict[str, Any] = {}
    if isinstance(fields, dict):
        for name, entry in fields.items():
            if isinstance(entry, dict) and "value" in entry:
                out[name] = {"value": entry.get("value"), "source": entry.get("source")}
            else:
                out[name] = {"value": entry, "source": None}
    return out
