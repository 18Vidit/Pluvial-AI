"""Backtest harness (design spec §8). Freezes at date T, feeds only
complaints <= T through the cascade, checks the escalation/recurrence label
at T+30d.

TEMPORAL ISOLATION is the part most backtests get wrong here, because
Bellwether's memory persists by design: a naive backtest would let the
Investigator call dossier_lookup and see verdicts or calibration notes that
were only written AFTER T, leaking the future into the prediction. Every
tool in agents/tools.py that reads history must be filtered to `frozen_at`.
This module additionally re-derives a frozen guidance snapshot (only
calibration versions recorded before T) rather than reusing live guidance.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass

from bellwether.agents.cascade import run_cascade
from bellwether.agents.context import CascadeContext
from bellwether.memory import dal
from bellwether.mireye.client import MireyeAccount
from bellwether.mireye.wrapper import MireyeToolWrapper, RunBudget


@dataclass
class BacktestCase:
    segment_id: int
    case_numbers: list[str]
    complaint_summary: str
    label: str  # confirmed_failure | no_failure, computed from data AFTER T, never shown to the agent


def frozen_guidance_version(con: sqlite3.Connection, frozen_at: str) -> int:
    row = con.execute("SELECT MAX(version) AS v FROM calibration WHERE run_at <= ?", (frozen_at,)).fetchone()
    return row["v"] or 0


def build_backtest_cases(
    con: sqlite3.Connection, frozen_at: str, label_window_days: int, escalation_case_types: list[str]
) -> list[BacktestCase]:
    """Select complaints created on/before T, and compute their label from
    data strictly AFTER their own created_at + window — never from data
    the agent could see at decision time."""
    rows = con.execute(
        "SELECT * FROM complaints WHERE created_at <= ? ORDER BY created_at", (frozen_at,)
    ).fetchall()

    type_list = ", ".join(f"'{t}'" for t in escalation_case_types)
    cases = []
    for r in rows:
        if r["segment_id"] is None:
            continue
        window_end = f"+{label_window_days} days"
        escalated = con.execute(
            f"""
            SELECT case_number FROM complaints
            WHERE segment_id = ? AND created_at > ? AND created_at <= datetime(?, ?)
              AND incident_case_type IN ({type_list})
            LIMIT 1
            """,
            (r["segment_id"], r["created_at"], r["created_at"], window_end),
        ).fetchone()
        repeat = con.execute(
            """
            SELECT case_number FROM complaints
            WHERE segment_id = ? AND created_at > ? AND created_at <= datetime(?, ?) AND case_number != ?
            LIMIT 1
            """,
            (r["segment_id"], r["created_at"], r["created_at"], window_end, r["case_number"]),
        ).fetchone()
        label = "confirmed_failure" if (escalated or repeat) else "no_failure"

        cases.append(BacktestCase(
            segment_id=r["segment_id"],
            case_numbers=[r["case_number"]],
            complaint_summary=json.dumps(dict(r), default=str),
            label=label,
        ))
    return cases


async def run_backtest(
    con: sqlite3.Connection,
    account: MireyeAccount,
    frozen_at: str,
    label_window_days: int,
    escalation_case_types: list[str],
    run_budget_ceiling: int,
    max_cases: int | None = None,
    ablation: str | None = None,
) -> dict:
    """ablation=None runs the real cascade (used for the headline eval
    numbers and the NYC negative control); ablation='no_moisture' or
    'no_memory' runs the design spec §8 ablation study against the same
    frozen cases, so the comparison is apples-to-apples."""
    from bellwether.mireye.client import MireyeClient

    cases = build_backtest_cases(con, frozen_at, label_window_days, escalation_case_types)
    if max_cases:
        cases = cases[:max_cases]

    guidance_version = frozen_guidance_version(con, frozen_at)

    results = []
    with MireyeClient(account) as client:
        for case in cases:
            ctx = CascadeContext(
                con=con,
                mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=run_budget_ceiling)),
                run_budget=RunBudget(ceiling=run_budget_ceiling),
                guidance_version=guidance_version,
                frozen_at=frozen_at,
            )
            seg = dal.get_segment(con, case.segment_id)
            dossier_summary = json.dumps({"segment": seg, "as_of": frozen_at}, default=str)

            triage_out, verdict, _, _ = await run_cascade(
                con, ctx, case.complaint_summary, dossier_summary, ablation=ablation
            )
            flagged = verdict is not None and verdict.disposition in ("dispatch", "inspect")
            results.append({
                "segment_id": case.segment_id,
                "case_numbers": case.case_numbers,
                "flagged": flagged,
                "disposition": verdict.disposition if verdict else "discarded_at_triage",
                "label": case.label,
            })

    return score_results(results)


def score_results(results: list[dict]) -> dict:
    tp = sum(1 for r in results if r["flagged"] and r["label"] == "confirmed_failure")
    fp = sum(1 for r in results if r["flagged"] and r["label"] == "no_failure")
    fn = sum(1 for r in results if not r["flagged"] and r["label"] == "confirmed_failure")
    tn = sum(1 for r in results if not r["flagged"] and r["label"] == "no_failure")

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    return {
        "n": len(results),
        "true_positive": tp, "false_positive": fp, "false_negative": fn, "true_negative": tn,
        "precision": precision, "recall": recall,
        "results": results,  # kept for per-case failure inspection (design brief rule 6)
    }
