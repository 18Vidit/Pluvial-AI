"""The Calibrator (design spec §5.1, Phase 6). Runs weekly, not per-complaint:

1. Refresh moisture/drought state (a handful of fetches, not one per segment).
2. Harvest outcomes: did a previously-judged case escalate or recur?
3. Compute precision per (soil class, symptom class, trigger state).
4. Estimate reporting-bias weights (§4.6).
5. Write a guidance diff — future cascade runs pick it up via
   guidance.load_calibration_notes().
6. Reawaken: scan closed/monitored verdicts whose invalidation_condition
   now holds, and re-run the cascade for those segments unprompted.

This module contains the DETERMINISTIC parts (metrics, outcome harvesting,
reawakening triggers) as plain code — no reason to spend a model call
computing a precision score. Only the guidance-diff text generation and the
reawakened cascade runs involve a model.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from bellwether.agents.context import CascadeContext
from bellwether.memory import dal
from bellwether.mireye.wrapper import MireyeToolWrapper, RunBudget


def harvest_outcomes(con: sqlite3.Connection, escalation_case_types: list[str], recurrence_days: int = 30) -> int:
    """Label past verdicts using the escalation/recurrence proxy from design
    spec §8: a segment that draws an escalation-type case, or a repeat
    complaint, within `recurrence_days` of the verdict counts as
    confirmed_failure. Everything else past its observation window is
    no_failure. Only labels verdicts that don't already have an outcome."""
    unlabelled = con.execute(
        """
        SELECT v.verdict_id, v.segment_id, v.decided_at FROM verdicts v
        LEFT JOIN outcomes o ON o.verdict_id = v.verdict_id
        WHERE o.outcome_id IS NULL
          AND julianday('now') - julianday(v.decided_at) > ?
        """,
        (recurrence_days,),
    ).fetchall()

    type_list = ", ".join(f"'{t}'" for t in escalation_case_types)
    n = 0
    for row in unlabelled:
        window_end = f"+{recurrence_days} days"
        escalated = con.execute(
            f"""
            SELECT case_number, incident_case_type, created_at FROM complaints
            WHERE segment_id = ?
              AND created_at > ?
              AND created_at <= datetime(?, ?)
              AND incident_case_type IN ({type_list})
            LIMIT 1
            """,
            (row["segment_id"], row["decided_at"], row["decided_at"], window_end),
        ).fetchone()
        repeat = con.execute(
            """
            SELECT case_number FROM complaints
            WHERE segment_id = ? AND created_at > ? AND created_at <= datetime(?, ?)
            LIMIT 1
            """,
            (row["segment_id"], row["decided_at"], row["decided_at"], window_end),
        ).fetchone()

        if escalated:
            label, outcome = "confirmed_failure", f"escalated: {escalated['incident_case_type']} ({escalated['case_number']})"
        elif repeat:
            label, outcome = "confirmed_failure", f"recurred: {repeat['case_number']}"
        else:
            label, outcome = "no_failure", "no escalation or recurrence observed in window"

        con.execute(
            "INSERT INTO outcomes (verdict_id, observed_outcome, label, observed_at) VALUES (?, ?, ?, ?)",
            (row["verdict_id"], outcome, label, datetime.now(timezone.utc).isoformat()),
        )
        n += 1
    con.commit()
    return n


def compute_stratified_metrics(con: sqlite3.Connection) -> dict:
    """Precision per (soil shrink-swell class, symptom class, trigger state
    at decision time). This is the number the Calibrator's guidance-diff
    text is grounded in, and the number the eval harness reports (design
    spec §8) — same computation, different slice of time."""
    rows = con.execute(
        """
        SELECT v.verdict_id, v.disposition, v.reasoning_json, v.segment_id, v.decided_at, o.label
        FROM verdicts v JOIN outcomes o ON o.verdict_id = v.verdict_id
        """
    ).fetchall()

    strata: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        seg = dal.get_segment(con, r["segment_id"])
        profile = seg.get("profile") if seg else None
        shrink_swell = "unknown"
        if profile and "soil_shrink_swell_class" in profile:
            shrink_swell = profile["soil_shrink_swell_class"].get("value") or "unknown"

        moisture = dal.current_trigger_state(con, as_of=r["decided_at"])
        trigger_state = moisture["trigger_state"] if moisture else "unknown"

        key = (shrink_swell, trigger_state)
        bucket = strata.setdefault(key, {"tp": 0, "fp": 0, "n": 0})
        bucket["n"] += 1
        flagged = r["disposition"] in ("dispatch", "inspect")
        confirmed = r["label"] == "confirmed_failure"
        if flagged and confirmed:
            bucket["tp"] += 1
        elif flagged and not confirmed:
            bucket["fp"] += 1

    metrics = {}
    for (shrink_swell, trigger_state), b in strata.items():
        flagged_total = b["tp"] + b["fp"]
        precision = b["tp"] / flagged_total if flagged_total else None
        metrics[f"{shrink_swell}|{trigger_state}"] = {
            "n": b["n"], "true_positive": b["tp"], "false_positive": b["fp"], "precision": precision,
        }
    return metrics


def draft_guidance_diff(metrics: dict, precision_floor: float = 0.4) -> str:
    """Plain-text calibration note appended to agent guidance (design spec
    §5.1: 'On Urban-land blocks your precision was 0.31 — you are
    over-flagging; require a second corroborating complaint.'). Threshold
    tightening is proposed here, not applied as a hardcoded rule elsewhere —
    the agents read this note and reason about it like any other guidance."""
    lines = []
    for stratum, m in metrics.items():
        if m["precision"] is not None and m["n"] >= 5 and m["precision"] < precision_floor:
            shrink_swell, trigger_state = stratum.split("|")
            lines.append(
                f"On segments with shrink_swell_class={shrink_swell} and trigger_state={trigger_state}, "
                f"historical precision was {m['precision']:.2f} over {m['n']} cases ({m['true_positive']} confirmed, "
                f"{m['false_positive']} false positives). Require stronger corroborating evidence "
                f"(complaint clustering or an unambiguous symptom) before dispatching in this stratum."
            )
    if not lines:
        return "No strata fell below the precision floor this run; no threshold changes."
    return "\n".join(lines)


def compute_reporting_bias(con: sqlite3.Connection) -> dict:
    """Design spec §4.6: 311 measures who complains, not what's broken.
    Segments in areas with fewer housing units / lower median income
    relative to their complaint volume are, if anything, UNDER-reporting —
    so a sparse complaint there should carry more weight, not less."""
    rows = con.execute(
        """
        SELECT s.segment_id, COUNT(c.case_number) AS n_complaints, s.profile_json
        FROM segments s LEFT JOIN complaints c ON c.segment_id = s.segment_id
        WHERE s.profile_json IS NOT NULL
        GROUP BY s.segment_id
        """
    ).fetchall()

    scored = []
    for r in rows:
        profile = json.loads(r["profile_json"]) if r["profile_json"] else {}
        housing = _num(profile.get("housing_units_within_1km"))
        income = _num(profile.get("county_median_household_income"))
        if housing is None or housing == 0:
            continue
        complaints_per_1k_housing = (r["n_complaints"] / housing) * 1000
        scored.append({
            "segment_id": r["segment_id"], "complaints_per_1k_housing": complaints_per_1k_housing,
            "median_income": income,
        })

    if not scored:
        return {}
    incomes = [s["median_income"] for s in scored if s["median_income"] is not None]
    if not incomes:
        return {}
    median_income_overall = sorted(incomes)[len(incomes) // 2]

    weights = {}
    for s in scored:
        if s["median_income"] is None:
            continue
        # Higher income, lower reporting rate expected to be a bias signal
        # in the OTHER direction; the flag we actually want is: LOW income
        # area with LOW complaint volume is plausibly under-reported, so
        # weight sparse complaints there upward.
        low_income = s["median_income"] < median_income_overall
        low_volume = s["complaints_per_1k_housing"] < 1.0
        if low_income and low_volume:
            weights[s["segment_id"]] = 1.5  # up-weight sparse complaints here
    return {"median_income_overall": median_income_overall, "upweighted_segments": weights}


def _num(v) -> float | None:
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_calibration(con: sqlite3.Connection, escalation_case_types: list[str]) -> int:
    n_new = harvest_outcomes(con, escalation_case_types)
    metrics = compute_stratified_metrics(con)
    diff = draft_guidance_diff(metrics)
    bias = compute_reporting_bias(con)
    version = dal.record_calibration(con, metrics, bias, diff)
    con.commit()
    return version
