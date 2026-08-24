"""Glue: pull NCEI daily precip + the current USDM class, write both into
moisture_history (design spec §5.3). Run daily (new NCEI day) and can be
re-run any time — upserts are idempotent."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from bellwether.ingest import ncei, usdm
from bellwether.memory import dal


def sync(db_path: Path, lookback_days: int = 120) -> int:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    raw = ncei.fetch_daily(start, end)
    series = ncei.compute_series(raw)
    current_dm = usdm.current_usdm_class()

    dal.init_db(db_path)
    with dal.connect(db_path) as con:
        for i, day in enumerate(series):
            is_latest = i == len(series) - 1
            dal.upsert_moisture_day(
                con, day["date"], day["station_id"], day["precip_mm"], day["tmax_c"],
                day["antecedent_30d_mm"], day["antecedent_60d_mm"], day["antecedent_90d_mm"],
                day["trigger_state"], current_dm if is_latest else None,
            )
    return len(series)


if __name__ == "__main__":
    import sys
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/bellwether.db")
    n = sync(db)
    print(f"synced {n} days of moisture history")
