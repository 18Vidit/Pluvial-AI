"""Repair backtest harness: compare agent predictions to repair-proxy labels
over a rolling 6-month window.

The existing backtest (backtest.py) uses escalation/recurrence proxies
with a 30-day window: "did more complaints appear?"  This module asks a
different question: "did the city actually fix something here within 6
months of the complaint?"

The answer is not ground truth — Houston does not publish utility repair
records — but it is a stronger proxy than recurrence alone, because the
label looks at HOW a complaint closed (fast closure = crew visit, batch
closure = crew resolved multiple tickets) rather than just whether more
appeared.

TEMPORAL ISOLATION follows the same contract as backtest.py: the agent
is frozen at date T and sees nothing after it.  The repair label is
computed from data strictly within [T, T + 6 months], which the agent
never sees.

ROLLING SWEEP: instead of a single freeze date, this module sweeps
across multiple dates spaced 6 months apart (or at a configurable
interval) to detect seasonal bias and ensure results are stable.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg

from pluvial.agents.cascade import run_cascade
from pluvial.agents.context import CascadeContext
from pluvial.eval.backtest import frozen_guidance_version, score_results
from pluvial.eval.repair_labels import (
    LABEL_CONFIRMED_REPAIR,
    LABEL_NO_EVIDENCE,
    LABEL_NO_REPAIR,
    repair_label_for_segment,
)
from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount
from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget


@dataclass
class RepairBacktestCase:
    segment_id: int
    case_numbers: list[str]
    complaint_summary: str
    repair_label: str  # confirmed_repair | no_repair | no_evidence


@dataclass
class WindowResult:
    frozen_at: str
    window_end: str
    n: int
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: float | None
    recall: float | None
    label_distribution: dict[str, int]
    results: list[dict[str, Any]]


DEFAULT_OBSERVATION_MONTHS = 6
DEFAULT_FREEZE_DATES = [
    "2023-01-01",
    "2023-07-01",
    "2024-01-01",
    "2024-07-01",
    "2025-01-01",
    "2025-07-01",
]


def _add_months(dt_str: str, months: int) -> str:
    """Add ``months`` to an ISO date string.  Good enough for 6-month
    windows — does not handle edge cases like Jan 31 + 1 month."""
    dt = datetime.fromisoformat(dt_str)
    month = dt.month + months
    year = dt.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(dt.day, 28)  # safe floor
    return datetime(year, month, day, tzinfo=dt.tzinfo).isoformat()


def build_repair_cases(
    con: psycopg.Connection,
    frozen_at: str,
    observation_months: int = DEFAULT_OBSERVATION_MONTHS,
    max_segments: int | None = None,
) -> list[RepairBacktestCase]:
    """Select segments with pre-T complaints and compute repair-proxy labels
    from data strictly in [T, T + observation_months].

    Each segment becomes one case. If a segment has multiple pre-T
    complaints, the earliest one is used as the prediction trigger (the
    cascade sees the complaint that started the investigation)."""
    window_end = _add_months(frozen_at, observation_months)

    # Candidate segments: had complaints before T AND are profiled
    candidate_ids = dal.segments_with_complaints_in_window(con, frozen_at, require_profile=True)
    if max_segments:
        candidate_ids = candidate_ids[:max_segments]

    cases: list[RepairBacktestCase] = []
    for seg_id in candidate_ids:
        # Pre-T complaints: what the agent will see
        pre_t = dal.repair_events_on_segment(con, seg_id, "1970-01-01", frozen_at)
        if not pre_t:
            continue

        # Post-T complaints: what happened after, for the label
        post_t = dal.repair_events_on_segment(con, seg_id, frozen_at, window_end)
        label = repair_label_for_segment(post_t)

        # Use the most recent pre-T complaint as the prediction trigger
        trigger = pre_t[-1]
        cases.append(RepairBacktestCase(
            segment_id=seg_id,
            case_numbers=[c["case_number"] for c in pre_t[-5:]],  # last 5 for context
            complaint_summary=json.dumps(trigger, default=str),
            repair_label=label,
        ))

    return cases


async def run_repair_backtest_window(
    con: psycopg.Connection,
    account: MireyeAccount,
    frozen_at: str,
    observation_months: int = DEFAULT_OBSERVATION_MONTHS,
    run_budget_ceiling: int = 0,
    max_segments: int | None = None,
) -> WindowResult:
    """Run the cascade for a single freeze date and score against
    repair-proxy labels.

    ``run_budget_ceiling=0`` keeps this cache-only (no live Mireye spend),
    which is the right default: these segments are already profiled."""
    from pluvial.mireye.client import MireyeClient

    window_end = _add_months(frozen_at, observation_months)
    cases = build_repair_cases(con, frozen_at, observation_months, max_segments)
    guidance_version = frozen_guidance_version(con, frozen_at)

    results: list[dict[str, Any]] = []
    with MireyeClient(account) as client:
        for case in cases:
            # Skip segments with no evidence — we can't call them TP/FP/FN/TN
            if case.repair_label == LABEL_NO_EVIDENCE:
                results.append({
                    "segment_id": case.segment_id,
                    "case_numbers": case.case_numbers,
                    "flagged": False,
                    "disposition": "skipped_no_evidence",
                    "label": case.repair_label,
                })
                continue

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
                con, ctx, case.complaint_summary, dossier_summary,
            )
            flagged = verdict is not None and verdict.disposition in ("dispatch", "inspect")
            results.append({
                "segment_id": case.segment_id,
                "case_numbers": case.case_numbers,
                "flagged": flagged,
                "disposition": verdict.disposition if verdict else "discarded_at_triage",
                "label": case.repair_label,
            })

    # Score only cases with a definitive label
    scored = [r for r in results if r["label"] in (LABEL_CONFIRMED_REPAIR, LABEL_NO_REPAIR)]
    tp = sum(1 for r in scored if r["flagged"] and r["label"] == LABEL_CONFIRMED_REPAIR)
    fp = sum(1 for r in scored if r["flagged"] and r["label"] == LABEL_NO_REPAIR)
    fn = sum(1 for r in scored if not r["flagged"] and r["label"] == LABEL_CONFIRMED_REPAIR)
    tn = sum(1 for r in scored if not r["flagged"] and r["label"] == LABEL_NO_REPAIR)

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    label_dist = {
        LABEL_CONFIRMED_REPAIR: sum(1 for r in results if r["label"] == LABEL_CONFIRMED_REPAIR),
        LABEL_NO_REPAIR: sum(1 for r in results if r["label"] == LABEL_NO_REPAIR),
        LABEL_NO_EVIDENCE: sum(1 for r in results if r["label"] == LABEL_NO_EVIDENCE),
    }

    return WindowResult(
        frozen_at=frozen_at,
        window_end=window_end,
        n=len(scored),
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=tn,
        precision=precision,
        recall=recall,
        label_distribution=label_dist,
        results=results,
    )


async def run_repair_backtest_sweep(
    con: psycopg.Connection,
    account: MireyeAccount,
    freeze_dates: list[str] | None = None,
    observation_months: int = DEFAULT_OBSERVATION_MONTHS,
    run_budget_ceiling: int = 0,
    max_segments: int | None = None,
) -> dict[str, Any]:
    """Run the repair backtest across multiple freeze dates and aggregate.

    Returns a summary dict with per-window results and pooled metrics."""
    dates = freeze_dates or DEFAULT_FREEZE_DATES
    windows: list[WindowResult] = []

    for frozen_at in dates:
        result = await run_repair_backtest_window(
            con, account, frozen_at, observation_months,
            run_budget_ceiling, max_segments,
        )
        windows.append(result)

    # Pooled scoring across all windows
    all_scored = []
    for w in windows:
        all_scored.extend(
            r for r in w.results if r["label"] in (LABEL_CONFIRMED_REPAIR, LABEL_NO_REPAIR)
        )
    pooled_tp = sum(1 for r in all_scored if r["flagged"] and r["label"] == LABEL_CONFIRMED_REPAIR)
    pooled_fp = sum(1 for r in all_scored if r["flagged"] and r["label"] == LABEL_NO_REPAIR)
    pooled_fn = sum(1 for r in all_scored if not r["flagged"] and r["label"] == LABEL_CONFIRMED_REPAIR)
    pooled_tn = sum(1 for r in all_scored if not r["flagged"] and r["label"] == LABEL_NO_REPAIR)
    pooled_precision = pooled_tp / (pooled_tp + pooled_fp) if (pooled_tp + pooled_fp) else None
    pooled_recall = pooled_tp / (pooled_tp + pooled_fn) if (pooled_tp + pooled_fn) else None

    return {
        "mode": "repair_backtest",
        "observation_months": observation_months,
        "freeze_dates": dates,
        "pooled": {
            "n": len(all_scored),
            "true_positive": pooled_tp,
            "false_positive": pooled_fp,
            "false_negative": pooled_fn,
            "true_negative": pooled_tn,
            "precision": pooled_precision,
            "recall": pooled_recall,
        },
        "per_window": [
            {
                "frozen_at": w.frozen_at,
                "window_end": w.window_end,
                "n": w.n,
                "precision": w.precision,
                "recall": w.recall,
                "label_distribution": w.label_distribution,
                "true_positive": w.true_positive,
                "false_positive": w.false_positive,
                "false_negative": w.false_negative,
                "true_negative": w.true_negative,
                "results": w.results,
            }
            for w in windows
        ],
    }
