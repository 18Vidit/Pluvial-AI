"""Pull daily precipitation/temperature from NOAA NCEI for Houston stations
and derive the Movement Trigger State (design spec §4.2).

Free, keyless, verified during design research: Houston IAH (station
USW00012960) returns clean daily PRCP/TMAX. This is the primary, high-
resolution trigger — Mireye's `drought_category` (US Drought Monitor) is a
coarse weekly corroborator, stored alongside but not depended on, because
USDM currently shows no active drought polygon anywhere in the metro.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx

NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
HOUSTON_STATION = "USW00012960"  # Houston Bush Intercontinental Airport (IAH)

# Desiccation/re-wetting thresholds for Houston Vertisol clay. A "long dry
# spell" is defined against the station's own climatology, not an arbitrary
# global cutoff: IAH averages ~50 inches (1270mm) of rain a year, so a
# 30-day window under 10mm is a genuine anomaly, not routine Texas weather.
DRY_SPELL_30D_THRESHOLD_MM = 10.0
REWETTING_SINGLE_DAY_MM = 15.0
REWETTING_LOOKBACK_DAYS = 60  # must follow a dry spell to count as "rewetting"


def fetch_daily(start: date, end: date, station: str = HOUSTON_STATION) -> list[dict]:
    with httpx.Client(timeout=60) as client:
        r = client.get(
            NCEI_URL,
            params={
                "dataset": "daily-summaries",
                "stations": station,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dataTypes": "PRCP,TMAX",
                "format": "json",
                "units": "metric",
            },
        )
        r.raise_for_status()
        return r.json()


def compute_series(raw_days: list[dict]) -> list[dict]:
    """Turn raw NCEI rows into a dated series with rolling antecedent
    precipitation sums and a classified trigger state per day."""
    parsed = []
    for row in raw_days:
        d = datetime.strptime(row["DATE"], "%Y-%m-%d").date()
        prcp = float(row["PRCP"]) if row.get("PRCP") not in (None, "") else 0.0
        tmax = float(row["TMAX"]) if row.get("TMAX") not in (None, "") else None
        parsed.append({"date": d, "precip_mm": prcp, "tmax_c": tmax})
    parsed.sort(key=lambda r: r["date"])

    by_date = {r["date"]: r["precip_mm"] for r in parsed}
    out = []
    for r in parsed:
        d = r["date"]
        a30 = sum(by_date.get(d - timedelta(days=i), 0.0) for i in range(30))
        a60 = sum(by_date.get(d - timedelta(days=i), 0.0) for i in range(60))
        a90 = sum(by_date.get(d - timedelta(days=i), 0.0) for i in range(90))

        recent_dry_spell = a30 < DRY_SPELL_30D_THRESHOLD_MM
        big_rain_today = r["precip_mm"] >= REWETTING_SINGLE_DAY_MM
        # rewetting = today's rain is significant AND the preceding window
        # (excluding today) was a dry spell -> the dangerous transition.
        preceding_30 = a30 - r["precip_mm"]
        was_dry = preceding_30 < DRY_SPELL_30D_THRESHOLD_MM

        if big_rain_today and was_dry:
            state = "rewetting"
        elif recent_dry_spell and a60 < a90 * 0.5:
            state = "drying"
        elif recent_dry_spell:
            state = "sustained_dry"
        else:
            state = "stable"

        out.append({
            "date": d.isoformat(),
            "station_id": HOUSTON_STATION,
            "precip_mm": r["precip_mm"],
            "tmax_c": r["tmax_c"],
            "antecedent_30d_mm": round(a30, 1),
            "antecedent_60d_mm": round(a60, 1),
            "antecedent_90d_mm": round(a90, 1),
            "trigger_state": state,
        })
    return out
