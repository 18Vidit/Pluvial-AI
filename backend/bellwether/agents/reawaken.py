"""The reawakening loop: scan closed/monitored verdicts whose stored
invalidation condition no longer holds, and re-run the cascade for those
segments unprompted (design spec §5.1, the strongest anti-dashboard proof).

Split from calibrator.py because this is the one part of calibration that
spends model calls and Mireye credits — it re-invokes the full cascade, not
just recomputing metrics.
"""
from __future__ import annotations

import json
import sqlite3

from bellwether.agents.cascade import run_cascade
from bellwether.agents.context import CascadeContext
from bellwether.memory import dal
from bellwether.mireye.client import MireyeAccount, MireyeClient
from bellwether.mireye.wrapper import MireyeToolWrapper, RunBudget


def _condition_currently_holds(con: sqlite3.Connection, segment_id: int, condition: dict) -> bool:
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
    con: sqlite3.Connection,
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
            case_numbers = json.loads(v["case_numbers"])
            complaints = [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM complaints WHERE case_number IN (%s)"
                    % ",".join("?" for _ in case_numbers),
                    case_numbers,
                ).fetchall()
            ]

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

            new_id = dal.record_verdict(
                con,
                dal.VerdictRecord(
                    segment_id=v["segment_id"],
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
                    reawakened_from=v["verdict_id"],
                ),
            )
            con.commit()
            new_verdict_ids.append(new_id)

    return new_verdict_ids
