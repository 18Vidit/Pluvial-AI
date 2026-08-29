"""Repair-proxy label computation for the repair backtest.

Houston does not publish pipeline repair/work-order records. These
functions construct progressively stronger proxies from the 311 complaint
data that already exists in memory:

  Tier 1 — Fast closure: a complaint that was opened and closed within
  a short window (default 7 days) likely represents an actual crew visit
  and repair, not an administrative closure months later.

  Tier 2 — Batch closure cluster: three or more complaints on the same
  segment whose closed_at dates fall within the same 7-day bucket almost
  certainly represent a single crew visit that resolved all related
  tickets.

Both tiers are combined into a single ``repair_label`` for a segment
over a given observation window (T to T + window). A segment is labelled
``confirmed_repair`` if either tier fires, ``no_repair`` if complaints
exist but none satisfy either tier, and ``no_evidence`` if there are
no closed complaints in the window at all.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any


# --- Tier 1: fast-closure proxy -------------------------------------------

DEFAULT_FAST_CLOSURE_DAYS = 7
"""A complaint closed within this many days of opening is assumed to
represent a real dispatch-and-fix event."""

ADMINISTRATIVE_CLOSURE_FLOOR_DAYS = 60
"""A complaint open longer than this before closing is assumed to be an
administrative bulk-close, not a targeted repair."""

ESCALATION_CASE_TYPES = frozenset(["Major Water Leak", "Water Main Valve"])
"""Case types that the city classifies as escalations.  A complaint
of one of these types that later closes is a strong repair signal
regardless of closure speed."""


def is_fast_closure(complaint: dict[str, Any], max_days: int = DEFAULT_FAST_CLOSURE_DAYS) -> bool:
    """True if this complaint was closed within ``max_days`` of creation."""
    created = complaint.get("created_at")
    closed = complaint.get("closed_at")
    if not created or not closed:
        return False
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    if isinstance(closed, str):
        closed = datetime.fromisoformat(closed)
    delta = closed - created
    return timedelta(0) <= delta <= timedelta(days=max_days)


def is_escalation_closure(complaint: dict[str, Any]) -> bool:
    """True if this is an escalation-type complaint that was closed."""
    case_type = complaint.get("incident_case_type", "")
    return (
        case_type in ESCALATION_CASE_TYPES
        and complaint.get("status") == "Closed"
        and complaint.get("closed_at") is not None
    )


# --- Tier 2: batch-closure clustering -------------------------------------

DEFAULT_CLUSTER_WINDOW_DAYS = 7
"""Complaints whose closed_at dates fall within this many days of each
other are considered part of the same batch-closure event."""

DEFAULT_CLUSTER_MIN_SIZE = 3
"""Minimum number of complaints in a batch-closure cluster to infer a
crew visit."""


def _parse_dt(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    return datetime.fromisoformat(str(val))


def find_batch_closures(
    complaints: list[dict[str, Any]],
    cluster_window_days: int = DEFAULT_CLUSTER_WINDOW_DAYS,
    min_cluster_size: int = DEFAULT_CLUSTER_MIN_SIZE,
) -> list[list[dict[str, Any]]]:
    """Find groups of complaints whose closed_at dates cluster within a
    sliding window of ``cluster_window_days`` days.

    Returns a list of clusters, each being a list of complaint dicts.
    Only clusters with at least ``min_cluster_size`` members are returned.
    """
    closed = [c for c in complaints if c.get("closed_at") is not None]
    if len(closed) < min_cluster_size:
        return []

    # Sort by closed_at
    closed.sort(key=lambda c: _parse_dt(c["closed_at"]))

    clusters: list[list[dict[str, Any]]] = []
    window = timedelta(days=cluster_window_days)

    # Sliding window: for each complaint, extend the cluster forward as
    # long as the next complaint's closed_at is within the window of the
    # first complaint in the cluster.
    i = 0
    while i < len(closed):
        cluster_start = _parse_dt(closed[i]["closed_at"])
        cluster = [closed[i]]
        j = i + 1
        while j < len(closed) and (_parse_dt(closed[j]["closed_at"]) - cluster_start) <= window:
            cluster.append(closed[j])
            j += 1
        if len(cluster) >= min_cluster_size:
            clusters.append(cluster)
            i = j  # skip past the cluster to avoid overlapping
        else:
            i += 1

    return clusters


# --- Combined label -------------------------------------------------------

LABEL_CONFIRMED_REPAIR = "confirmed_repair"
LABEL_NO_REPAIR = "no_repair"
LABEL_NO_EVIDENCE = "no_evidence"


def repair_label_for_segment(
    complaints_in_window: list[dict[str, Any]],
    fast_closure_days: int = DEFAULT_FAST_CLOSURE_DAYS,
    cluster_window_days: int = DEFAULT_CLUSTER_WINDOW_DAYS,
    cluster_min_size: int = DEFAULT_CLUSTER_MIN_SIZE,
) -> str:
    """Compute a repair-proxy label for a single segment over a
    given observation window.

    ``complaints_in_window`` should contain all complaints on this
    segment whose ``created_at`` falls within the label window
    (T to T + observation_months).

    Returns one of:
      - ``confirmed_repair``: strong evidence the city fixed something
      - ``no_repair``: complaints exist but no repair signal
      - ``no_evidence``: no closed complaints at all in the window
    """
    if not complaints_in_window:
        return LABEL_NO_EVIDENCE

    # Tier 1: any fast closure or escalation closure?
    for c in complaints_in_window:
        if is_fast_closure(c, max_days=fast_closure_days):
            return LABEL_CONFIRMED_REPAIR
        if is_escalation_closure(c):
            return LABEL_CONFIRMED_REPAIR

    # Tier 2: batch closure cluster?
    clusters = find_batch_closures(
        complaints_in_window,
        cluster_window_days=cluster_window_days,
        min_cluster_size=cluster_min_size,
    )
    if clusters:
        return LABEL_CONFIRMED_REPAIR

    # Complaints exist but none triggered a repair signal
    closed_any = any(c.get("closed_at") is not None for c in complaints_in_window)
    if not closed_any:
        return LABEL_NO_EVIDENCE

    return LABEL_NO_REPAIR
