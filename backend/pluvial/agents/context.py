"""The run context shared by every agent in one cascade pass. Holds the
open Postgres connection, the wrapped Mireye tool, and the credit budget for
this single run — never shared across runs, per design spec §5.2."""
from __future__ import annotations

import psycopg
from dataclasses import dataclass
from typing import Any

from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget


@dataclass
class CascadeContext:
    con: psycopg.Connection
    mireye: MireyeToolWrapper
    run_budget: RunBudget
    guidance_version: int
    frozen_at: str | None = None  # set only by the eval harness for backtesting


@dataclass
class AddressContext:
    """The run context for one address-mode cascade.

    Separate from CascadeContext because the unit of analysis is different:
    there is no segment and no complaint, there are nine sampled points and
    one threat. `samples` is already fetched by the time any agent runs —
    the credits were spent before the cascade started, on a plan the user
    confirmed — so the agents read from it rather than being able to buy
    more ground mid-argument.
    """

    con: psycopg.Connection
    mireye: MireyeToolWrapper
    run_budget: RunBudget
    guidance_version: int
    location: dict[str, Any]
    samples: list[dict[str, Any]]
    threat: str | None = None
    region_key: str | None = None
    frozen_at: str | None = None  # set only by the eval harness for backtesting

    def sample(self, sample_id: int) -> dict[str, Any] | None:
        return next((s for s in self.samples if s["sample_id"] == sample_id), None)
