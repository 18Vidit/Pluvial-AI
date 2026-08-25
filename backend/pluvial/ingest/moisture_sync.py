"""Glue: pull NCEI daily precip + the current USDM class, write both into
moisture_history (design spec §5.3). Run daily (new NCEI day) and can be
re-run any time — upserts are idempotent."""
from __future__ import annotations

from datetime import date, timedelta

from pluvial.ingest import ncei, usdm
from pluvial.memory import dal


def sync(
    lookback_days: int = 120,
    station: str = ncei.HOUSTON_STATION,
    lat: float | None = None,
    lon: float | None = None,
    init: bool = True,
) -> int:
    """Pull one station's series into moisture_history under region_key=station.

    lat/lon are only used for the USDM point query; they default to the
    Houston representative point, which is right for the original study area
    and wrong for anywhere else, so address mode passes the real coordinate.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    raw = ncei.fetch_daily(start, end, station=station)
    series = ncei.compute_series(raw, station=station)
    current_dm = (
        usdm.current_usdm_class(lat, lon) if lat is not None and lon is not None
        else usdm.current_usdm_class()
    )

    if init:
        dal.init_db()
    with dal.connect() as con:
        for i, day in enumerate(series):
            is_latest = i == len(series) - 1
            dal.upsert_moisture_day(
                con, day["date"], day["station_id"], day["precip_mm"], day["tmax_c"],
                day["antecedent_30d_mm"], day["antecedent_60d_mm"], day["antecedent_90d_mm"],
                day["trigger_state"], current_dm if is_latest else None,
                region_key=station,
            )
    return len(series)


def ensure_region(station: str, lat: float, lon: float, lookback_days: int = 120) -> int:
    """Sync this station only if the store has nothing recent for it.

    Address mode calls this per query. Without the guard, every analysis of
    every address in the same metro would re-pull 120 days from NOAA for a
    series that has not changed since this morning.
    """
    with dal.connect() as con:
        latest = dal.current_trigger_state(con, region_key=station)
    if latest is not None and (date.today() - latest["date"]).days <= 2:
        return 0
    return sync(lookback_days, station=station, lat=lat, lon=lon, init=False)


if __name__ == "__main__":
    n = sync()
    print(f"synced {n} days of moisture history")
