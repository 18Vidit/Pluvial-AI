"""Which Mireye account pays for a given fetch.

The bulk profiling job shards by geography across accounts so no one
account's monthly allowance gates the whole study area (design spec §9).
That sharding is deliberate and stays where it is: the ETL knows which
account owns which longitude band.

Interactive paths — address mode, chat, regional search — have no such
structure to exploit. They need *a working account*, and the failure mode
that matters is an account whose monthly allowance has run out mid-session,
which happened for real on 2026-08-25 partway through a regional search.
`MireyeClientPool` handles exactly that: it fails over to the next
configured account and remembers, for the life of the process, which ones
are spent.
"""
from __future__ import annotations

import os
from typing import Any

from pluvial.mireye.client import MireyeAccount, MireyeClient, MireyeError

ENV_KEYS = ("MIREYE_API_KEY_1", "MIREYE_API_KEY_2", "MIREYE_API_KEY_3")

CREDITS_EXHAUSTED = "credits_exhausted"


class NoMireyeAccountConfigured(RuntimeError):
    pass


class AllAccountsExhausted(RuntimeError):
    """Every configured account has spent its monthly allowance.

    Raised rather than degraded into empty profiles, for the same reason
    `BatchLocationFailed` exists: ground we could not buy must never be
    recorded as ground with nothing under it.
    """


def available_accounts() -> list[MireyeAccount]:
    return [
        MireyeAccount(label=key.lower().replace("mireye_api_key_", "account-"), api_key=os.environ[key])
        for key in ENV_KEYS
        if os.environ.get(key)
    ]


def primary_account() -> MireyeAccount:
    accounts = available_accounts()
    if not accounts:
        raise NoMireyeAccountConfigured(
            f"set one of {', '.join(ENV_KEYS)} — address mode fetches live and cannot run without it"
        )
    return accounts[0]


def _batch_is_credit_exhausted(response: dict[str, Any]) -> bool:
    """True if ANY location in the batch came back credits_exhausted.

    Any, not all, because that is how the allowance actually runs out: a
    batch straddles the ceiling and the first few locations succeed. Retrying
    the whole chunk on the next account re-buys those few, which is a handful
    of credits spent to keep the batch atomic — much cheaper than reasoning
    about a half-filled result.
    """
    for result in response.get("results") or response.get("locations") or []:
        error = result.get("error") or {}
        if not result.get("ok", True) and error.get("error") == CREDITS_EXHAUSTED:
            return True
    return False


class MireyeClientPool:
    """Presents the slice of MireyeClient that interactive callers use, over
    an ordered list of accounts, failing over when one is spent.

    `account` reports whichever account is currently paying, so the account
    label recorded against a fetched sample stays truthful.
    """

    def __init__(self, accounts: list[MireyeAccount] | None = None, timeout: float = 60.0):
        self.accounts = accounts if accounts is not None else available_accounts()
        if not self.accounts:
            raise NoMireyeAccountConfigured(
                f"set one of {', '.join(ENV_KEYS)} — address mode fetches live and cannot run without it"
            )
        self._timeout = timeout
        self._clients: dict[str, MireyeClient] = {}
        self._exhausted: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "MireyeClientPool":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # -- account selection -------------------------------------------------

    def _client(self, account: MireyeAccount) -> MireyeClient:
        if account.label not in self._clients:
            self._clients[account.label] = MireyeClient(account, timeout=self._timeout)
        return self._clients[account.label]

    def _live(self) -> list[MireyeAccount]:
        return [a for a in self.accounts if a.label not in self._exhausted]

    @property
    def account(self) -> MireyeAccount:
        live = self._live()
        return live[0] if live else self.accounts[0]

    def _spent(self, account: MireyeAccount) -> None:
        self._exhausted.add(account.label)
        print(
            f"[mireye] {account.label} has exhausted its monthly allowance; "
            f"failing over to {[a.label for a in self._live()] or 'nothing left'}",
            flush=True,
        )

    # -- the surface callers use ------------------------------------------

    def quote(self, fields: list[str], locations: int = 1) -> dict[str, Any]:
        """Quotes are free, so this does not fail over — a quote from a spent
        account still returns the right price."""
        return self._client(self.account).quote(fields, locations=locations)

    def fetch_batch(
        self, fields: list[str], locations: list[tuple[float, float]], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for account in list(self._live()):
            try:
                response = self._client(account).fetch_batch(
                    fields, locations, idempotency_key=idempotency_key
                )
            except MireyeError as e:
                if CREDITS_EXHAUSTED not in str(e):
                    raise
                self._spent(account)
                last_error = e
                continue
            if _batch_is_credit_exhausted(response):
                self._spent(account)
                continue
            return response
        raise AllAccountsExhausted(
            f"all {len(self.accounts)} configured Mireye account(s) have spent their monthly "
            f"allowance; nothing was fetched"
        ) from last_error

    def fetch_one(self, fields: list[str], lat: float, lng: float) -> dict[str, Any]:
        for account in list(self._live()):
            try:
                return self._client(account).fetch_one(fields, lat, lng)
            except MireyeError as e:
                if CREDITS_EXHAUSTED not in str(e):
                    raise
                self._spent(account)
        raise AllAccountsExhausted("all configured Mireye accounts have spent their monthly allowance")
