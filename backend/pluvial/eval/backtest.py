"""Backtest harness (design spec §8). Freezes at date T, feeds only
complaints <= T through the cascade, checks the escalation/recurrence label
at T+30d.

TEMPORAL ISOLATION is the part most backtests get wrong here, because
Pluvial-AI's memory persists by design: a naive backtest would let the
Investigator call dossier_lookup and see verdicts or calibration notes that
were only written AFTER T, leaking the future into the prediction. Every
tool in agents/tools.py that reads history must be filtered to `frozen_at`.
This module additionally re-derives a frozen guidance snapshot (only
calibration versions recorded before T) rather than reusing live guidance.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta

import psycopg

from pluvial.agents.cascade import run_cascade
from pluvial.agents.context import CascadeContext
from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount
from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget


@dataclass
class BacktestCase:
    segment_id: int
    case_numbers: list[str]
    complaint_summary: str
    label: str  # confirmed_failure | no_failure, computed from data AFTER T, never shown to the agent


def frozen_guidance_version(con: psycopg.Connection, frozen_at: str) -> int:
    return dal.frozen_guidance_version(con, frozen_at)


def build_backtest_cases(
    con: psycopg.Connection, frozen_at: str, label_window_days: int, escalation_case_types: list[str],
    max_cases: int | None = None,
    only_cases: list[str] | None = None,
) -> list[BacktestCase]:
    """Select complaints created on/before T, and compute their label from
    data strictly AFTER their own created_at + window — never from data
    the agent could see at decision time.

    only_cases pins the exact complaint set instead of taking the first N.
    Re-scoring a prior run's cases is the only way to compare two runs
    like for like, since any change to the corpus or the ordering shifts
    which complaints "the first N" refers to.

    max_cases is applied BEFORE labelling, not after. Labelling costs two
    queries per complaint, and against a remote Postgres those are network
    round trips: labelling the whole pre-T corpus (~390k complaints) issues
    ~780k sequential round trips and takes hours before the first agent
    runs. Truncating first is exactly equivalent — labelling neither
    reorders nor filters the candidates — but turns that into 2*max_cases
    queries. Under the old local SQLite store these were in-process calls,
    so the cost only became visible after the move to Neon."""
    rows = dal.complaints_up_to(con, frozen_at)
    rows = [r for r in rows if r["segment_id"] is not None]
    if only_cases:
        wanted = set(only_cases)
        rows = [r for r in rows if r["case_number"] in wanted]
        missing = wanted - {r["case_number"] for r in rows}
        if missing:
            raise ValueError(
                f"{len(missing)} pinned case(s) not found on/before {frozen_at}: "
                f"{sorted(missing)[:5]}"
            )
    elif max_cases:
        rows = rows[:max_cases]

    cases = []
    for r in rows:
        window_end = r["created_at"] + timedelta(days=label_window_days)
        escalated = dal.escalating_complaint(
            con, r["segment_id"], r["created_at"], window_end, escalation_case_types
        )
        repeat = dal.repeat_complaint(
            con, r["segment_id"], r["created_at"], window_end, exclude_case=r["case_number"]
        )
        label = "confirmed_failure" if (escalated or repeat) else "no_failure"

        cases.append(BacktestCase(
            segment_id=r["segment_id"],
            case_numbers=[r["case_number"]],
            complaint_summary=json.dumps(r, default=str),
            label=label,
        ))
    return cases


async def run_backtest(
    con: psycopg.Connection,
    account: MireyeAccount,
    frozen_at: str,
    label_window_days: int,
    escalation_case_types: list[str],
    run_budget_ceiling: int,
    max_cases: int | None = None,
    ablation: str | None = None,
    only_cases: list[str] | None = None,
) -> dict:
    """ablation=None runs the real cascade (used for the headline eval
    numbers and the NYC negative control); ablation='no_moisture' or
    'no_memory' runs the design spec §8 ablation study against the same
    frozen cases, so the comparison is apples-to-apples."""
    from pluvial.mireye.client import MireyeClient

    cases = build_backtest_cases(
        con, frozen_at, label_window_days, escalation_case_types,
        max_cases=max_cases, only_cases=only_cases,
    )

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
