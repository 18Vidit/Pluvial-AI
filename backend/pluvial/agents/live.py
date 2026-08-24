"""The actual production entrypoint (design spec §5.1/§12 Phase 4): run the
cascade over real, not-yet-adjudicated complaints and write verdicts to
memory. This is what populates `GET /queue` and gives the Calibrator/
reawaken loop something to work with — nothing else in the codebase calls
dal.record_verdict except reawaken.py, which requires a verdict to already
exist to reopen. Without this entrypoint the dispatcher board has nothing
to show and the Calibrator has nothing to harvest.
"""
from __future__ import annotations

import json

import psycopg

from pluvial.agents.cascade import run_cascade
from pluvial.agents.context import CascadeContext
from pluvial.agents.models import InvestigatorOutput, SkepticOutput, Verdict
from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount, MireyeClient
from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget


def record_cascade_result(
    con: psycopg.Connection,
    segment_id: int,
    case_numbers: list[str],
    guidance_version: int,
    verdict: Verdict | None,
    investigator_out: InvestigatorOutput | None,
    skeptic_out: SkepticOutput | None,
    reawakened_from: int | None = None,
) -> int | None:
    """Shared by the live entrypoint and reawaken.py — one place that turns
    a cascade result into a memory row, so both write identically-shaped
    verdicts."""
    if verdict is None:
        return None
    return dal.record_verdict(
        con,
        dal.VerdictRecord(
            segment_id=segment_id,
            case_numbers=case_numbers,
            disposition=verdict.disposition,
            priority=verdict.priority,
            reasoning={
                "investigator": investigator_out.model_dump() if investigator_out else None,
                "skeptic": skeptic_out.model_dump() if skeptic_out else None,
                "adjudicator_explanation": verdict.explanation,
            },
            cited_evidence=[c.model_dump() for c in verdict.decisive_evidence],
            rejected_counter_argument=verdict.rejected_counter_argument,
            invalidation_condition=(
                verdict.invalidation_condition.model_dump() if verdict.invalidation_condition else None
            ),
            agent_version=f"v{guidance_version}",
            reawakened_from=reawakened_from,
        ),
    )


def unadjudicated_complaints(
    con: psycopg.Connection, since: str | None, until: str | None, max_cases: int | None
) -> list[dict]:
    """Complaints on a profiled (cached) segment that don't yet have any
    verdict covering them. One complaint = one case, matching how the
    backtest harness already treats cases — a segment can accumulate
    several verdicts over time as new complaints come in. `until` lets a
    caller target an older slice (e.g. to demonstrate the Calibrator
    against complaints whose 30-day outcome window has already closed in
    real historical data, rather than waiting on calendar time)."""
    already_covered = dal.covered_case_numbers(con)
    rows = dal.uncovered_complaints_on_profiled_segments(con, since, until)
    rows = [r for r in rows if r["case_number"] not in already_covered]
    return rows[:max_cases] if max_cases else rows


async def process_new_complaints(
    con: psycopg.Connection,
    account: MireyeAccount,
    run_budget_ceiling: int,
    guidance_version: int,
    since: str | None = None,
    until: str | None = None,
    max_cases: int | None = None,
) -> list[int]:
    """The production loop: pick up complaints nobody's judged yet and run
    them through the cascade, writing a real verdict for each."""
    cases = unadjudicated_complaints(con, since, until, max_cases)
    new_ids: list[int] = []

    with MireyeClient(account) as client:
        for complaint in cases:
            seg = dal.get_segment(con, complaint["segment_id"])
            ctx = CascadeContext(
                con=con,
                mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=run_budget_ceiling)),
                run_budget=RunBudget(ceiling=run_budget_ceiling),
                guidance_version=guidance_version,
            )
            dossier_summary = json.dumps({"segment": seg}, default=str)
            complaint_summary = json.dumps(complaint, default=str)

            _, verdict, investigator_out, skeptic_out = await run_cascade(con, ctx, complaint_summary, dossier_summary)
            vid = record_cascade_result(
                con, complaint["segment_id"], [complaint["case_number"]], guidance_version,
                verdict, investigator_out, skeptic_out,
            )
            con.commit()
            if vid:
                new_ids.append(vid)

    return new_ids
