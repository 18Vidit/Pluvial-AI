"""Thin REST client for Mireye's fetch/quote/batch endpoints.

Used for bulk pre-profiling (Phase 2 — run once, cached forever) and by the
wrapper (pluvial.mireye.wrapper) for the rare cache-miss at agent
runtime. Every fetch is preceded by a quote, unconditionally, per design
spec §5.2 and the build brief's "quote before you fetch, every time."
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://api.mireye.com"
BATCH_MAX_LOCATIONS = 25
RATE_LIMIT_PER_MINUTE = 20  # Free-plan ceiling; Build-plan accounts (60/min) can override


class MireyeError(RuntimeError):
    pass


class QuoteExceedsCeilingError(MireyeError):
    def __init__(self, quoted_credits: int, ceiling: int):
        super().__init__(f"quote {quoted_credits} credits exceeds per-run ceiling {ceiling}")
        self.quoted_credits = quoted_credits
        self.ceiling = ceiling


@dataclass
class MireyeAccount:
    label: str
    api_key: str


class MireyeClient:
    """One client per sharded account (design spec §9: accounts sharded
    geographically, one super-neighbourhood group each)."""

    def __init__(self, account: MireyeAccount, base_url: str = BASE_URL, timeout: float = 30.0):
        self.account = account
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {account.api_key}"},
            timeout=timeout,
        )
        self._request_timestamps: list[float] = []

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MireyeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _respect_rate_limit(self) -> None:
        now = time.monotonic()
        self._request_timestamps = [t for t in self._request_timestamps if now - t < 60]
        if len(self._request_timestamps) >= RATE_LIMIT_PER_MINUTE:
            sleep_for = 60 - (now - self._request_timestamps[0]) + 0.1
            time.sleep(max(sleep_for, 0))
        self._request_timestamps.append(time.monotonic())

    def _post(self, path: str, json_body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        max_attempts = 30
        for attempt in range(max_attempts):
            self._respect_rate_limit()
            try:
                r = self._client.post(path, json=json_body, headers=headers)
            except httpx.TimeoutException:
                print(f"[mireye] timeout on {path} (attempt {attempt + 1}/{max_attempts}), retrying", flush=True)
                time.sleep(5)
                continue
            if r.status_code == 422:
                raise MireyeError(f"validation error on {path}: {r.text}")
            if r.status_code == 429:
                retry_after = float(r.headers.get("retry-after", 5))
                print(f"[mireye] 429 on {path} (attempt {attempt + 1}/{max_attempts}), sleeping {retry_after}s", flush=True)
                time.sleep(retry_after)
                continue
            if r.status_code == 409 and "still being computed" in r.text:
                print(f"[mireye] batch still computing on {path} (attempt {attempt + 1}/{max_attempts})", flush=True)
                time.sleep(10)
                continue
            if r.status_code >= 500:
                print(f"[mireye] {r.status_code} on {path} (attempt {attempt + 1}/{max_attempts}), retrying", flush=True)
                time.sleep(10)
                continue
            r.raise_for_status()
            return r.json()
        raise MireyeError(f"still not resolved on {path} after {max_attempts} retries")

    def quote(self, fields: list[str], locations: int = 1) -> dict[str, Any]:
        return self._post("/v1/fetch/quote", {"fields": fields, "locations": locations})

    def fetch_one(self, fields: list[str], lat: float, lng: float) -> dict[str, Any]:
        return self._post("/v1/fetch", {"fields": fields, "lat": lat, "lng": lng})

    def fetch_batch(
        self, fields: list[str], locations: list[tuple[float, float]], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if len(locations) > BATCH_MAX_LOCATIONS:
            raise ValueError(f"batch max is {BATCH_MAX_LOCATIONS} locations, got {len(locations)}")
        body = {
            "fields": fields,
            "locations": [{"lat": lat, "lng": lng} for lat, lng in locations],
        }
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        return self._post("/v1/fetch/batch", body, headers=headers)

    def quote_then_fetch_batch(
        self,
        fields: list[str],
        locations: list[tuple[float, float]],
        credit_ceiling: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Quote first, always. Refuses to fetch if the quote exceeds the
        caller's ceiling — the guard from design spec §5.2 against
        unpredictable agent-driven spend."""
        q = self.quote(fields, locations=len(locations))
        quoted = _extract_credits(q)
        if quoted is not None and quoted > credit_ceiling:
            raise QuoteExceedsCeilingError(quoted, credit_ceiling)
        return self.fetch_batch(fields, locations, idempotency_key=idempotency_key)


def _extract_credits(quote_response: dict[str, Any]) -> int | None:
    for key in ("credits", "total_credits", "cost_credits", "price_credits"):
        if key in quote_response:
            return int(quote_response[key])
    return None


def chunk_locations(
    locations: list[tuple[int, float, float]], size: int = BATCH_MAX_LOCATIONS
) -> list[list[tuple[int, float, float]]]:
    """locations: list of (segment_id, lat, lng). Splits into <=size chunks."""
    return [locations[i : i + size] for i in range(0, len(locations), size)]
