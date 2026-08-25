"""Transport-level resilience in the Mireye client.

A batch fetch is polled for minutes at a time, which is long enough for a
connection to be dropped mid-flight. These tests pin the retry behaviour
without touching the network.
"""
from __future__ import annotations

import httpx
import pytest

from pluvial.mireye.client import MireyeAccount, MireyeClient, MireyeError

ACCOUNT = MireyeAccount(label="test", api_key="k")


class _Response:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {"ok": True}
        self.text = text
        self.headers = {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=httpx.Request("POST", "http://x"), response=None)


def _client(monkeypatch, side_effects):
    client = MireyeClient(ACCOUNT)
    calls = {"n": 0}

    def post(path, json=None, headers=None):
        effect = side_effects[min(calls["n"], len(side_effects) - 1)]
        calls["n"] += 1
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr(client._client, "post", post)
    monkeypatch.setattr(client, "_respect_rate_limit", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return client, calls


def test_a_dropped_connection_is_retried_not_raised(monkeypatch):
    """The exact failure that killed a live regional search: an SSL EOF
    surfaces as httpx.ConnectError, which is a TransportError but not a
    TimeoutException, so the old handler let it through."""
    client, calls = _client(monkeypatch, [
        httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"),
        _Response(json_body={"credits": 23}),
    ])
    assert client.quote(["elevation"], locations=1) == {"credits": 23}
    assert calls["n"] == 2


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("refused"),
    httpx.ReadError("reset"),
    httpx.RemoteProtocolError("half-closed"),
    httpx.ConnectTimeout("slow"),
])
def test_every_transport_failure_is_retried(monkeypatch, failure):
    client, calls = _client(monkeypatch, [failure, _Response(json_body={"ok": True})])
    assert client.quote(["elevation"]) == {"ok": True}
    assert calls["n"] == 2


def test_it_gives_up_eventually_rather_than_retrying_forever(monkeypatch):
    client, _ = _client(monkeypatch, [httpx.ConnectError("down")])
    with pytest.raises(MireyeError, match="still not resolved"):
        client.quote(["elevation"])


def test_a_validation_error_is_not_retried(monkeypatch):
    """422 means the request is wrong. Repeating it 30 times only delays the
    error message."""
    client, calls = _client(monkeypatch, [_Response(status_code=422, text="bad field")])
    with pytest.raises(MireyeError, match="validation error"):
        client.quote(["not_a_field"])
    assert calls["n"] == 1
