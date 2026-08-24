"""The reawakening loop: scan closed/monitored verdicts whose stored
invalidation condition no longer holds, and re-run the cascade for those
segments unprompted (design spec §5.1, the strongest anti-dashboard proof).

Split from calibrator.py because this is the one part of calibration that
spends model calls and Mireye credits — it re-invokes the full cascade, not
just recomputing metrics.
"""
from __future__ import annotations

import json

import psycopg

from pluvial.agents.cascade import run_cascade
from pluvial.agents.context import CascadeContext
from pluvial.agents.live import record_cascade_result
from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount, MireyeClient
from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget


def _condition_currently_holds(con: psycopg.Connection, segment_id: int, condition: dict) -> bool:
    trigger = dal.current_trigger_state(con)
    trigger_state = trigger["trigger_state"] if trigger else None

    if condition.get("reopen_if_trigger_state_in") and trigger_state in condition["reopen_if_trigger_state_in"]:
        return True

    days = condition.get("reopen_if_new_complaints_within_days")
    min_count = condition.get("reopen_if_new_complaint_count_at_least", 1)
    if days:
        recent = dal.neighbourhood_complaints(con, segment_id, days=days)
        if len(recent) >= min_count:
            return True

    return False


async def scan_and_reawaken(
    con: psycopg.Connection,
    account: MireyeAccount,
    run_budget_ceiling: int,
    guidance_version: int,
) -> list[int]:
    """Returns the list of new verdict_ids created by reawakening."""
    candidates = dal.open_verdicts_with_invalidation(con)
    new_verdict_ids: list[int] = []

    with MireyeClient(account) as client:
        for v in candidates:
            condition = v["invalidation_condition"]
            if not _condition_currently_holds(con, v["segment_id"], condition):
                continue

            seg = dal.get_segment(con, v["segment_id"])
            case_numbers = v["case_numbers"]
            complaints = dal.complaints_by_case_numbers(con, case_numbers)

            ctx = CascadeContext(
                con=con,
                mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=run_budget_ceiling)),
                run_budget=RunBudget(ceiling=run_budget_ceiling),
                guidance_version=guidance_version,
            )

            complaint_summary = json.dumps(complaints, default=str)
            dossier_summary = json.dumps({
                "segment": seg,
                "reason_reawakened": condition.get("plain_english"),
                "prior_verdict_id": v["verdict_id"],
            }, default=str)

            triage_out, verdict, investigator_out, skeptic_out = await run_cascade(
                con, ctx, complaint_summary, dossier_summary
            )
            if verdict is None:
                continue  # triage discarded even the reawakened case; leave prior verdict as-is

            new_id = record_cascade_result(
                con, v["segment_id"], case_numbers, guidance_version,
                verdict, investigator_out, skeptic_out, reawakened_from=v["verdict_id"],
            )
            con.commit()
            new_verdict_ids.append(new_id)

    return new_verdict_ids
