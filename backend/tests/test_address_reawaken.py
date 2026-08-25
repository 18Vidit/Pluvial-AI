"""The "watch this address" loop.

An invalidation condition is only worth writing down if something acts on
it. These tests cover the deciding half — which rulings are candidates, and
whether a stated condition currently holds — against real Postgres, without
running any agents.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from pluvial.agents.reawaken import _address_condition_holds
from pluvial.memory import dal

REGION = "USW00013874"
OTHER_REGION = "USW00023062"


@pytest.fixture()
def location(db):
    return dal.create_location(db, "12 Test St", "12 Test St, Testville", 33.64, -84.43, REGION)


def _moisture(db, region_key, trigger_state, days_ago=0):
    dal.upsert_moisture_day(
        db, (date.today() - timedelta(days=days_ago)).isoformat(), region_key,
        precip_mm=20.0, tmax_c=30.0, a30=5.0, a60=8.0, a90=40.0,
        trigger_state=trigger_state, usdm_class=None, region_key=region_key,
    )


def _ruling(db, location_id, threat, severity, reopen_states):
    return dal.record_threat_ruling(db, dal.ThreatRulingRecord(
        location_id=location_id, threat=threat, severity=severity,
        reasoning={"adjudicator_explanation": "test"},
        cited_evidence=[{"field": "soil_shrink_swell_class", "value": "High", "sample_id": 1}],
        rejected_counter_argument="none",
        invalidation_condition={
            "reopen_if_trigger_state_in": reopen_states,
            "plain_english": "reopen when the clay starts re-swelling",
        } if reopen_states is not None else None,
        agent_version="v0",
    ))


def test_a_ruling_reopens_when_its_own_regions_trigger_state_arrives(db, location):
    _moisture(db, REGION, "rewetting")
    _ruling(db, location, "foundation", "low", ["rewetting"])
    candidate = dal.open_rulings_with_invalidation(db)[0]
    assert _address_condition_holds(db, candidate) is True


def test_another_regions_weather_does_not_reopen_this_address(db, location):
    """The whole reason moisture_history was repinned to (region_key, date).
    Before it, one metro's drought break would have reopened rulings across
    the country."""
    _moisture(db, OTHER_REGION, "rewetting")
    _moisture(db, REGION, "stable")
    _ruling(db, location, "foundation", "low", ["rewetting"])
    candidate = dal.open_rulings_with_invalidation(db)[0]
    assert _address_condition_holds(db, candidate) is False


def test_a_high_ruling_is_not_a_candidate(db, location):
    """`high` is already the top of the scale — there is nothing to reopen it
    into, and re-arguing it would churn the record for no decision."""
    _ruling(db, location, "foundation", "high", ["rewetting"])
    assert dal.open_rulings_with_invalidation(db) == []


def test_unresolved_rulings_are_candidates(db, location):
    """`unresolved` is the case most worth watching: the answer was not
    available, and a changed trigger state is a reason to look again."""
    _ruling(db, location, "subsidence", "unresolved", ["rewetting", "drying"])
    candidates = dal.open_rulings_with_invalidation(db)
    assert len(candidates) == 1 and candidates[0]["severity"] == "unresolved"


def test_a_ruling_with_no_condition_is_never_a_candidate(db, location):
    _ruling(db, location, "service_lines", "low", None)
    assert dal.open_rulings_with_invalidation(db) == []


def test_an_already_reopened_ruling_is_not_reopened_again(db, location):
    original = _ruling(db, location, "foundation", "low", ["rewetting"])
    successor = _ruling(db, location, "foundation", "elevated", ["drying"])
    db.execute(
        "UPDATE threat_rulings SET reawakened_from = %s WHERE ruling_id = %s",
        (original, successor),
    )
    ids = {c["ruling_id"] for c in dal.open_rulings_with_invalidation(db)}
    assert original not in ids
    assert successor in ids
