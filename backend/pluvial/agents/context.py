"""The run context shared by every agent in one cascade pass. Holds the
open SQLite connection, the wrapped Mireye tool, and the credit budget for
this single run — never shared across runs, per design spec §5.2."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget


@dataclass
class CascadeContext:
    con: sqlite3.Connection
    mireye: MireyeToolWrapper
    run_budget: RunBudget
    guidance_version: int
    frozen_at: str | None = None  # set only by the eval harness for backtesting
