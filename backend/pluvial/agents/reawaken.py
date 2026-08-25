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


# --- address mode -------------------------------------------------------------

def _address_condition_holds(con: psycopg.Connection, ruling: dict) -> bool:
    """Address mode's invalidation conditions are moisture conditions.

    The complaint-clustering clauses that reopen a triage verdict have no
    meaning here — there is no 311 feed and no street segment to cluster on
    — so only the trigger-state clause is evaluated, against the ruling's own
    region rather than Houston's.
    """
    condition = ruling["invalidation_condition"] or {}
    wanted = condition.get("reopen_if_trigger_state_in") or []
    if not wanted:
        return False
    trigger = dal.current_trigger_state(con, region_key=ruling.get("region_key"))
    return bool(trigger and trigger["trigger_state"] in wanted)


async def scan_and_reawaken_addresses(
    con: psycopg.Connection,
    account: MireyeAccount,
    guidance_version: int,
) -> list[int]:
    """Re-argue rulings whose stated physical precondition now holds.

    This is what turns an invalidation condition into "watch this address":
    when the moisture state flips to rewetting, the ruling reopens without
    anyone asking. It re-reasons over the ground already on file and never
    buys more — the ceiling is 0 — because what changed is the trigger
    state, not the soil.
    """
    from pluvial.agents.address_cascade import record_rulings, run_all_threats
    from pluvial.agents.context import AddressContext

    candidates = dal.open_rulings_with_invalidation(con)
    reopened: list[int] = []
    seen_locations: set[int] = set()

    with MireyeClient(account) as client:
        for ruling in candidates:
            location_id = ruling["location_id"]
            if location_id in seen_locations:
                continue  # one re-argument per location covers all three threats
            if not _address_condition_holds(con, ruling):
                continue
            seen_locations.add(location_id)

            location = dal.get_location(con, location_id)
            samples = dal.location_samples(con, location_id)
            if not any(s.get("profile") for s in samples):
                continue  # never fetched; nothing to re-reason over

            ctx = AddressContext(
                con=con,
                mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=0)),
                run_budget=RunBudget(ceiling=0),
                guidance_version=guidance_version,
                location=location,
                samples=samples,
                region_key=location["region_key"],
            )
            _, results = await run_all_threats(ctx)
            prior_by_threat = {r["threat"]: r["ruling_id"] for r in dal.location_rulings(con, location_id)}
            for threat, (new_ruling, investigator_out, skeptic_out) in results.items():
                ids = record_rulings(
                    con, location_id, guidance_version, {threat: (new_ruling, investigator_out, skeptic_out)}
                )
                new_id = ids[threat]
                con.execute(
                    "UPDATE threat_rulings SET reawakened_from = %s WHERE ruling_id = %s",
                    (prior_by_threat.get(threat), new_id),
                )
                reopened.append(new_id)
            con.commit()

    return reopened
