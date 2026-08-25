"""Account failover.

The failure this covers happened for real: partway through a regional
search, the only configured account's monthly allowance ran out. Every
subsequent location came back `credits_exhausted`, and before the fix that
was silently recorded as ground with no soil data.
"""
from __future__ import annotations

import pytest

from pluvial.mireye.accounts import (
    AllAccountsExhausted,
    MireyeClientPool,
    NoMireyeAccountConfigured,
    _batch_is_credit_exhausted,
)
from pluvial.mireye.client import MireyeAccount

A = MireyeAccount(label="account-1", api_key="k1")
B = MireyeAccount(label="account-2", api_key="k2")


def _ok(n=1):
    return {"results": [{"index": i, "ok": True, "fields": {"elevation": {"value": 1}}} for i in range(n)]}


def _exhausted(n=1, ok_first=0):
    results = [{"index": i, "ok": True, "fields": {}} for i in range(ok_first)]
    results += [
        {"index": i, "ok": False,
         "error": {"error": "credits_exhausted", "message": "Monthly credit allowance exhausted"}}
        for i in range(ok_first, n)
    ]
    return {"results": results}


class _FakeClient:
    def __init__(self, account, responses):
        self.account = account
        self._responses = list(responses)
        self.calls = 0

    def fetch_batch(self, fields, locations, idempotency_key=None):
        self.calls += 1
        return self._responses.pop(0)

    def quote(self, fields, locations=1):
        return {"credits": 23 * locations}

    def close(self):
        pass


def _pool(monkeypatch, responses_by_label):
    pool = MireyeClientPool(accounts=[A, B])
    clients = {
        label: _FakeClient(A if label == "account-1" else B, responses)
        for label, responses in responses_by_label.items()
    }
    monkeypatch.setattr(pool, "_client", lambda account: clients[account.label])
    return pool, clients


def test_a_batch_straddling_the_ceiling_counts_as_exhausted():
    """Any, not all: this is how the allowance actually runs out — the first
    few locations in a chunk succeed and the rest are refused."""
    assert _batch_is_credit_exhausted(_exhausted(n=5, ok_first=2)) is True
    assert _batch_is_credit_exhausted(_ok(5)) is False


def test_a_refused_batch_fails_over_to_the_next_account(monkeypatch):
    pool, clients = _pool(monkeypatch, {
        "account-1": [_exhausted(2)],
        "account-2": [_ok(2)],
    })
    response = pool.fetch_batch(["elevation"], [(30.0, -97.0), (30.1, -97.0)])
    assert response["results"][0]["ok"] is True
    assert clients["account-1"].calls == 1
    assert clients["account-2"].calls == 1
    assert pool.account.label == "account-2", "the spent account must stop being the payer"


def test_a_spent_account_is_not_tried_again(monkeypatch):
    pool, clients = _pool(monkeypatch, {
        "account-1": [_exhausted(1)],
        "account-2": [_ok(1), _ok(1)],
    })
    pool.fetch_batch(["elevation"], [(30.0, -97.0)])
    pool.fetch_batch(["elevation"], [(30.2, -97.0)])
    assert clients["account-1"].calls == 1, "exhaustion is remembered for the process"
    assert clients["account-2"].calls == 2


def test_running_out_everywhere_raises_rather_than_returning_nothing(monkeypatch):
    """The whole point. An empty result would be recorded as unmeasured
    ground, which is a false statement about the world."""
    pool, _ = _pool(monkeypatch, {
        "account-1": [_exhausted(1)],
        "account-2": [_exhausted(1)],
    })
    with pytest.raises(AllAccountsExhausted):
        pool.fetch_batch(["elevation"], [(30.0, -97.0)])


def test_no_accounts_configured_is_its_own_error():
    with pytest.raises(NoMireyeAccountConfigured):
        MireyeClientPool(accounts=[])
