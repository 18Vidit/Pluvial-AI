from datetime import date, timedelta

from pluvial.ingest.ncei import compute_series


def _rows(precip_by_offset: dict[int, float], base: date, n_days: int = 120) -> list[dict]:
    """offset counts days before base; offset=0 is base itself, offset=1 is base-1day, etc."""
    rows = []
    for offset in range(n_days, -1, -1):
        d = base - timedelta(days=offset)
        rows.append({"DATE": d.isoformat(), "PRCP": str(precip_by_offset.get(offset, 0.0)), "TMAX": "30.0"})
    return rows


def test_sustained_dry_when_no_rain_for_60_plus_days():
    base = date(2026, 8, 1)
    rows = _rows({}, base, n_days=90)  # zero rain for the whole window
    series = compute_series(rows)
    last = series[-1]
    assert last["trigger_state"] in ("sustained_dry", "drying")
    assert last["antecedent_30d_mm"] == 0.0


def test_rewetting_after_dry_spell():
    base = date(2026, 8, 1)
    # dry for 89 days, then one big rain event on the last day
    precip = {0: 40.0}  # offset 0 = base date (last row)
    rows = _rows(precip, base, n_days=90)
    series = compute_series(rows)
    last = series[-1]
    assert last["trigger_state"] == "rewetting"


def test_stable_when_rain_is_routine():
    base = date(2026, 8, 1)
    # steady moderate rain every few days keeps the 30d sum well above threshold
    precip = {i: 5.0 for i in range(0, 90, 3)}
    rows = _rows(precip, base, n_days=90)
    series = compute_series(rows)
    last = series[-1]
    assert last["trigger_state"] == "stable"


def test_series_is_chronologically_sorted_and_complete():
    base = date(2026, 8, 1)
    rows = _rows({}, base, n_days=30)
    series = compute_series(rows)
    dates = [s["date"] for s in series]
    assert dates == sorted(dates)
    assert len(series) == 31


# --- freshness guard ----------------------------------------------------------

def test_freshness_window_is_wider_than_noaa_own_publication_lag():
    """NOAA's daily-summaries service publishes several days behind real
    time. A two-day window meant a region that was as current as it could
    possibly be got re-pulled on every query — and a live analysis stalled
    inside that re-pull. Seven days is still far inside the 30/60/90-day
    antecedent windows the trigger state is computed over."""
    from pluvial.ingest.moisture_sync import FRESH_ENOUGH_DAYS

    assert FRESH_ENOUGH_DAYS >= 5
    assert FRESH_ENOUGH_DAYS < 30, "must stay well inside the shortest antecedent window"


def test_a_failing_noaa_does_not_strand_a_paid_analysis(monkeypatch):
    """The moisture trigger state is a corroborator, not the basis of a
    ruling, and the ground has already been paid for by this point."""
    import httpx

    from pluvial import analyze

    def boom(*a, **k):
        raise httpx.ConnectTimeout("NCEI unreachable")

    monkeypatch.setattr(analyze.moisture_sync, "ensure_region", boom)
    plan = analyze.AnalysisPlan(
        location_id=1, query_text="x", label="x", lat=30.0, lon=-97.0,
        region_key="USW00000001", station_name="test", station_distance_m=0.0,
        samples=[], fields=[], quoted_credits=0, quote_raw={},
    )
    assert analyze.ensure_moisture(plan) == -1, "reports the failure rather than raising or hiding it"
