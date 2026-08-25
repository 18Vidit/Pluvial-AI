"""Address-mode evaluation.

The shipped product no longer sees 311 complaints, so the existing
78.6% / 30.6% no longer describes it: that number scores whether the
cascade correctly flags *311 complaints that later escalate*, using
clustering as evidence address mode does not have.

This harness asks the address-mode question against the same ground and the
same labels: given only the physics under a segment — no complaint text, no
clustering, no neighbourhood history — does the `service_lines` ruling come
back `high` or `elevated` for the segments where a complaint went on to
escalate? It runs on the identical pinned cases via the existing
`--rescore` file, so the two numbers are measured over the same corpus.

Expect it to be lower, and report it either way. The `no_memory` ablation
already showed that removing prior-verdict and precedent access roughly
halves recall; removing the complaint entirely takes more. The GAP is the
finding: it quantifies what complaint evidence contributes on top of ground
physics, which is a thing worth knowing and not an embarrassment.

Temporal isolation: `frozen_at` reaches moisture_history and
precedent_search through AddressContext. Address mode has no
`dossier_lookup` — there is no segment and no dossier — so the known
frozen_at leak in that tool does not apply here.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import psycopg

from pluvial.agents.address_cascade import run_address_cascade
from pluvial.agents.context import AddressContext
from pluvial.eval.backtest import build_backtest_cases, frozen_guidance_version, score_results
from pluvial.ingest.ncei import HOUSTON_STATION
from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount, MireyeClient
from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget

# The threat the 311 label is actually about. A "water leak / standing
# water" complaint that later escalates is a buried-line failure; scoring it
# against `foundation` or `subsidence` would be measuring a different claim.
SCORED_THREAT = "service_lines"
FLAGGING_SEVERITIES = ("high", "elevated")

# The moisture series the Houston study area was built on. Address mode
# resolves region_key from coordinates, which now returns Hobby rather than
# IAH for downtown addresses — correct for a live query, wrong here, where
# the 117 recorded rows are IAH's and are the series these cases were
# originally judged under.
BACKTEST_REGION_KEY = HOUSTON_STATION


@dataclass
class AddressCaseResult:
    segment_id: int
    case_numbers: list[str]
    severity: str
    flagged: bool
    label: str


def _location_row(segment: dict) -> dict:
    """A segment, dressed as a location. Address mode reasons about a place,
    and a profiled segment centroid is a place — this is a view onto the
    same ground, not a new fetch."""
    return {
        "location_id": -segment["segment_id"],   # negative: never persisted
        "query_text": segment.get("name") or f"segment {segment['segment_id']}",
        "label": segment.get("name") or f"segment {segment['segment_id']}",
        "lat": segment["centroid_lat"],
        "lon": segment["centroid_lon"],
        "region_key": BACKTEST_REGION_KEY,
    }


def _sample_row(segment: dict) -> dict:
    return {
        "sample_id": segment["segment_id"],
        "location_id": -segment["segment_id"],
        "role": "property",
        "lat": segment["centroid_lat"],
        "lon": segment["centroid_lon"],
        "profile": segment.get("profile") or {},
        "soil_usable": bool(segment.get("soil_usable")),
    }


async def run_address_backtest(
    con: psycopg.Connection,
    account: MireyeAccount,
    frozen_at: str,
    label_window_days: int,
    escalation_case_types: list[str],
    max_cases: int | None = None,
    only_cases: list[str] | None = None,
) -> dict:
    cases = build_backtest_cases(
        con, frozen_at, label_window_days, escalation_case_types,
        max_cases=max_cases, only_cases=only_cases,
    )
    guidance_version = frozen_guidance_version(con, frozen_at)

    results = []
    with MireyeClient(account) as client:
        for case in cases:
            segment = dal.get_segment(con, case.segment_id)
            if segment is None or not segment.get("profile"):
                # No cached ground means nothing for address mode to reason
                # from. Skipped rather than scored as a miss: counting it
                # would measure the profiling coverage, not the agents.
                continue

            ctx = AddressContext(
                con=con,
                # ceiling=0 — this measures reasoning over ground already on
                # file. A backtest that bought new ground would also be
                # measuring today's Mireye against yesterday's labels.
                mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=0)),
                run_budget=RunBudget(ceiling=0),
                guidance_version=guidance_version,
                location=_location_row(segment),
                samples=[_sample_row(segment)],
                region_key=BACKTEST_REGION_KEY,
                frozen_at=frozen_at,
            )

            ruling, _, _ = await run_address_cascade(ctx, SCORED_THREAT)
            flagged = ruling.severity in FLAGGING_SEVERITIES
            results.append({
                "segment_id": case.segment_id,
                "case_numbers": case.case_numbers,
                "flagged": flagged,
                "severity": ruling.severity,
                "soil_usable": bool(segment.get("soil_usable")),
                "label": case.label,
            })

    scored = score_results(results)
    scored["mode"] = "address"
    scored["threat"] = SCORED_THREAT
    scored["flagging_severities"] = list(FLAGGING_SEVERITIES)
    scored["skipped_unprofiled"] = len(cases) - len(results)
    scored["severity_counts"] = {
        severity: sum(1 for r in results if r["severity"] == severity)
        for severity in ("high", "elevated", "low", "unresolved")
    }

    # Reported unconditionally, because the headline number without it is
    # misleading in both directions. Where the soil gate fires there is
    # nothing to reason from, so those cases measure SSURGO's coverage of
    # Houston, not the agents' judgement — and they dominate the pooled
    # number. Splitting them out says which is which instead of letting the
    # reader assume one and get the other.
    scored["by_soil_usable"] = {
        "usable": score_results([r for r in results if r["soil_usable"]]),
        "urban_land": score_results([r for r in results if not r["soil_usable"]]),
    }
    for stratum in scored["by_soil_usable"].values():
        stratum.pop("results", None)
    return scored
