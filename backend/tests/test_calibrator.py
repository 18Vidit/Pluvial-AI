import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bellwether.agents import calibrator
from bellwether.memory import dal

ESCALATION_TYPES = ["Major Water Leak", "Water Main Valve"]


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "test.db"
    dal.init_db(path)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    yield con
    con.close()


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _backdate_verdict(con: sqlite3.Connection, verdict_id: int, days: int) -> None:
    """record_verdict always stamps decided_at=now(); tests that need an
    old verdict (so the outcome window has closed) must backdate it directly."""
    con.execute("UPDATE verdicts SET decided_at = ? WHERE verdict_id = ?", (_days_ago(days), verdict_id))
    con.commit()


def test_harvest_outcomes_labels_escalation_as_confirmed_failure(db):
    dal.upsert_segment(db, 1, "Test St", "residential", 29.7, -95.4, profile={}, soil_usable=True)
    db.execute(
        "INSERT INTO complaints (case_number, segment_id, incident_case_type, created_at) VALUES (?,?,?,?)",
        ("C1", 1, "Water Leak", _days_ago(40)),
    )
    # escalation within the 30-day window after the original complaint
    db.execute(
        "INSERT INTO complaints (case_number, segment_id, incident_case_type, created_at) VALUES (?,?,?,?)",
        ("C2", 1, "Major Water Leak", _days_ago(35)),
    )
    vid = dal.record_verdict(db, dal.VerdictRecord(
        segment_id=1, case_numbers=["C1"], disposition="inspect", priority="medium",
        reasoning={}, cited_evidence=[], rejected_counter_argument=None,
        invalidation_condition=None, agent_version="v0",
    ))
    _backdate_verdict(db, vid, 40)

    n = calibrator.harvest_outcomes(db, ESCALATION_TYPES, recurrence_days=30)
    assert n == 1
    row = db.execute("SELECT label, observed_outcome FROM outcomes WHERE verdict_id = ?", (vid,)).fetchone()
    assert row["label"] == "confirmed_failure"
    assert "escalated" in row["observed_outcome"]


def test_harvest_outcomes_labels_quiet_segment_as_no_failure(db):
    dal.upsert_segment(db, 2, "Quiet St", "residential", 29.8, -95.5, profile={}, soil_usable=True)
    db.execute(
        "INSERT INTO complaints (case_number, segment_id, incident_case_type, created_at) VALUES (?,?,?,?)",
        ("C3", 2, "Water Quality", _days_ago(40)),
    )
    vid = dal.record_verdict(db, dal.VerdictRecord(
        segment_id=2, case_numbers=["C3"], disposition="close", priority="low",
        reasoning={}, cited_evidence=[], rejected_counter_argument=None,
        invalidation_condition={"plain_english": "reopen if it rains"}, agent_version="v0",
    ))
    _backdate_verdict(db, vid, 40)

    n = calibrator.harvest_outcomes(db, ESCALATION_TYPES, recurrence_days=30)
    assert n == 1
    row = db.execute("SELECT label FROM outcomes WHERE verdict_id = ?", (vid,)).fetchone()
    assert row["label"] == "no_failure"


def test_harvest_outcomes_skips_verdicts_still_inside_window(db):
    dal.upsert_segment(db, 3, "Recent St", "residential", 29.9, -95.6, profile={}, soil_usable=True)
    db.execute(
        "INSERT INTO complaints (case_number, segment_id, incident_case_type, created_at) VALUES (?,?,?,?)",
        ("C4", 3, "Water Quality", _days_ago(5)),
    )
    dal.record_verdict(db, dal.VerdictRecord(
        segment_id=3, case_numbers=["C4"], disposition="close", priority="low",
        reasoning={}, cited_evidence=[], rejected_counter_argument=None,
        invalidation_condition=None, agent_version="v0",
    ))
    db.commit()
    n = calibrator.harvest_outcomes(db, ESCALATION_TYPES, recurrence_days=30)
    assert n == 0  # only 5 days old, window (30d) hasn't closed yet


def test_draft_guidance_diff_flags_low_precision_strata():
    metrics = {
        "High|rewetting": {"n": 10, "true_positive": 2, "false_positive": 8, "precision": 0.2},
        "Low|stable": {"n": 10, "true_positive": 9, "false_positive": 1, "precision": 0.9},
        "Moderate|drying": {"n": 3, "true_positive": 0, "false_positive": 1, "precision": 0.0},  # too few (n<5), ignored
    }
    diff = calibrator.draft_guidance_diff(metrics, precision_floor=0.4)
    assert "High" in diff and "rewetting" in diff
    assert "Low|stable" not in diff.replace(" ", "")  # good stratum not flagged... actually check absence of Low/stable text
    assert "Moderate" not in diff  # excluded for n < 5


def test_draft_guidance_diff_empty_when_all_strata_healthy():
    metrics = {"Low|stable": {"n": 20, "true_positive": 18, "false_positive": 2, "precision": 0.9}}
    diff = calibrator.draft_guidance_diff(metrics, precision_floor=0.4)
    assert "No strata fell below" in diff
