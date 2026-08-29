"""Tests for repair-proxy label computation.

Pure-function tests — no database, no API keys. These exercise the label
logic from eval/repair_labels.py against hand-crafted complaint dicts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pluvial.eval.repair_labels import (
    LABEL_CONFIRMED_REPAIR,
    LABEL_NO_EVIDENCE,
    LABEL_NO_REPAIR,
    find_batch_closures,
    is_escalation_closure,
    is_fast_closure,
    repair_label_for_segment,
)


def _complaint(
    case_number: str = "C001",
    case_type: str = "Water Leak",
    status: str = "Closed",
    created_at: str = "2024-01-15T10:00:00+00:00",
    closed_at: str | None = "2024-01-17T10:00:00+00:00",
) -> dict:
    return {
        "case_number": case_number,
        "incident_case_type": case_type,
        "status": status,
        "created_at": created_at,
        "closed_at": closed_at,
        "segment_id": 1,
    }


# ---------- Tier 1: fast closure ------------------------------------------


class TestFastClosure:
    def test_closed_in_2_days(self):
        c = _complaint(created_at="2024-01-15T10:00:00+00:00", closed_at="2024-01-17T10:00:00+00:00")
        assert is_fast_closure(c) is True

    def test_closed_in_exactly_7_days(self):
        c = _complaint(created_at="2024-01-15T10:00:00+00:00", closed_at="2024-01-22T10:00:00+00:00")
        assert is_fast_closure(c) is True

    def test_closed_in_8_days_not_fast(self):
        c = _complaint(created_at="2024-01-15T10:00:00+00:00", closed_at="2024-01-23T10:00:00+00:00")
        assert is_fast_closure(c) is False

    def test_still_open(self):
        c = _complaint(closed_at=None, status="Open")
        assert is_fast_closure(c) is False

    def test_negative_delta_rejected(self):
        """closed_at before created_at — bad data, should not count."""
        c = _complaint(created_at="2024-01-17T10:00:00+00:00", closed_at="2024-01-15T10:00:00+00:00")
        assert is_fast_closure(c) is False

    def test_custom_max_days(self):
        c = _complaint(created_at="2024-01-15T10:00:00+00:00", closed_at="2024-01-18T10:00:00+00:00")
        assert is_fast_closure(c, max_days=2) is False
        assert is_fast_closure(c, max_days=3) is True

    def test_datetime_objects_work(self):
        """Accepts datetime objects, not just strings."""
        c = {
            "created_at": datetime(2024, 1, 15, 10, tzinfo=timezone.utc),
            "closed_at": datetime(2024, 1, 16, 10, tzinfo=timezone.utc),
        }
        assert is_fast_closure(c) is True


# ---------- Tier 1: escalation closure ------------------------------------


class TestEscalationClosure:
    def test_major_water_leak_closed(self):
        c = _complaint(case_type="Major Water Leak", status="Closed", closed_at="2024-01-20T10:00:00+00:00")
        assert is_escalation_closure(c) is True

    def test_water_main_valve_closed(self):
        c = _complaint(case_type="Water Main Valve", status="Closed", closed_at="2024-01-20T10:00:00+00:00")
        assert is_escalation_closure(c) is True

    def test_regular_leak_not_escalation(self):
        c = _complaint(case_type="Water Leak", status="Closed", closed_at="2024-01-20T10:00:00+00:00")
        assert is_escalation_closure(c) is False

    def test_escalation_still_open(self):
        c = _complaint(case_type="Major Water Leak", status="Open", closed_at=None)
        assert is_escalation_closure(c) is False


# ---------- Tier 2: batch closure clusters --------------------------------


class TestBatchClosures:
    def test_three_in_same_week(self):
        complaints = [
            _complaint("C1", closed_at="2024-03-10T10:00:00+00:00"),
            _complaint("C2", closed_at="2024-03-11T10:00:00+00:00"),
            _complaint("C3", closed_at="2024-03-12T10:00:00+00:00"),
        ]
        clusters = find_batch_closures(complaints)
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_two_not_enough(self):
        complaints = [
            _complaint("C1", closed_at="2024-03-10T10:00:00+00:00"),
            _complaint("C2", closed_at="2024-03-11T10:00:00+00:00"),
        ]
        assert find_batch_closures(complaints) == []

    def test_spread_across_two_months_no_cluster(self):
        complaints = [
            _complaint("C1", closed_at="2024-03-01T10:00:00+00:00"),
            _complaint("C2", closed_at="2024-03-15T10:00:00+00:00"),
            _complaint("C3", closed_at="2024-04-01T10:00:00+00:00"),
        ]
        assert find_batch_closures(complaints) == []

    def test_open_complaints_ignored(self):
        complaints = [
            _complaint("C1", closed_at="2024-03-10T10:00:00+00:00"),
            _complaint("C2", closed_at=None, status="Open"),
            _complaint("C3", closed_at="2024-03-12T10:00:00+00:00"),
        ]
        # Only 2 closed — below threshold
        assert find_batch_closures(complaints) == []

    def test_custom_min_size(self):
        complaints = [
            _complaint("C1", closed_at="2024-03-10T10:00:00+00:00"),
            _complaint("C2", closed_at="2024-03-11T10:00:00+00:00"),
        ]
        clusters = find_batch_closures(complaints, min_cluster_size=2)
        assert len(clusters) == 1

    def test_two_separate_clusters(self):
        complaints = [
            # Cluster 1: March
            _complaint("C1", closed_at="2024-03-10T10:00:00+00:00"),
            _complaint("C2", closed_at="2024-03-11T10:00:00+00:00"),
            _complaint("C3", closed_at="2024-03-12T10:00:00+00:00"),
            # Cluster 2: June
            _complaint("C4", closed_at="2024-06-05T10:00:00+00:00"),
            _complaint("C5", closed_at="2024-06-06T10:00:00+00:00"),
            _complaint("C6", closed_at="2024-06-07T10:00:00+00:00"),
        ]
        clusters = find_batch_closures(complaints)
        assert len(clusters) == 2


# ---------- Combined label ------------------------------------------------


class TestRepairLabel:
    def test_empty_list(self):
        assert repair_label_for_segment([]) == LABEL_NO_EVIDENCE

    def test_fast_closure_triggers_repair(self):
        complaints = [
            _complaint("C1", created_at="2024-03-01T10:00:00+00:00", closed_at="2024-03-03T10:00:00+00:00"),
        ]
        assert repair_label_for_segment(complaints) == LABEL_CONFIRMED_REPAIR

    def test_escalation_closure_triggers_repair(self):
        complaints = [
            _complaint("C1", case_type="Major Water Leak", status="Closed",
                       created_at="2024-03-01T10:00:00+00:00", closed_at="2024-04-15T10:00:00+00:00"),
        ]
        # Closed after 45 days — not fast, but escalation type
        assert repair_label_for_segment(complaints) == LABEL_CONFIRMED_REPAIR

    def test_batch_closure_triggers_repair(self):
        complaints = [
            _complaint("C1", created_at="2024-03-01T10:00:00+00:00", closed_at="2024-03-20T10:00:00+00:00"),
            _complaint("C2", created_at="2024-03-02T10:00:00+00:00", closed_at="2024-03-21T10:00:00+00:00"),
            _complaint("C3", created_at="2024-03-03T10:00:00+00:00", closed_at="2024-03-22T10:00:00+00:00"),
        ]
        # None closed fast (19-day gap), none escalation type, but 3 closed in same week
        assert repair_label_for_segment(complaints) == LABEL_CONFIRMED_REPAIR

    def test_slow_closure_no_cluster_is_no_repair(self):
        complaints = [
            _complaint("C1", created_at="2024-01-01T10:00:00+00:00", closed_at="2024-04-01T10:00:00+00:00"),
            _complaint("C2", created_at="2024-02-01T10:00:00+00:00", closed_at="2024-06-01T10:00:00+00:00"),
        ]
        assert repair_label_for_segment(complaints) == LABEL_NO_REPAIR

    def test_all_open_is_no_evidence(self):
        complaints = [
            _complaint("C1", status="Open", closed_at=None),
            _complaint("C2", status="Open", closed_at=None),
        ]
        assert repair_label_for_segment(complaints) == LABEL_NO_EVIDENCE

    def test_temporal_isolation_caller_responsibility(self):
        """The label function itself does not enforce temporal isolation —
        that is the caller's job (build_repair_cases passes only post-T
        complaints). This test documents the contract: if you pass
        pre-T data, the label function will happily use it."""
        complaints = [
            _complaint("C1", created_at="2023-06-01T10:00:00+00:00", closed_at="2023-06-02T10:00:00+00:00"),
        ]
        # This WOULD be confirmed_repair, but the caller should never
        # have included a complaint from before the freeze date
        assert repair_label_for_segment(complaints) == LABEL_CONFIRMED_REPAIR
