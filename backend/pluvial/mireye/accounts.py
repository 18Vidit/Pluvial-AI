"""Which Mireye account pays for a given fetch.

The bulk profiling job shards by geography across three accounts so no one
account's monthly allowance gates the whole study area (design spec §9).
Address mode is interactive and small — 216 credits a query — so it just
needs a working account; this picks the first one configured and reports
plainly when none is.
"""
from __future__ import annotations

import os

from pluvial.mireye.client import MireyeAccount

ENV_KEYS = ("MIREYE_API_KEY_1", "MIREYE_API_KEY_2", "MIREYE_API_KEY_3")


class NoMireyeAccountConfigured(RuntimeError):
    pass


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
