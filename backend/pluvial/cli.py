import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import typer
from dotenv import load_dotenv

from bellwether.agents import calibrator
from bellwether.agents.reawaken import scan_and_reawaken
from bellwether.ingest import houston_311, moisture_sync, osm_pbf, osm_segments
from bellwether.memory import dal
from bellwether.mireye.client import MireyeAccount
from bellwether.mireye.profile_job import run_profiling_shard, select_stratified_segments, shard_by_longitude

load_dotenv()

app = typer.Typer(help="Bellwether: Mireye x Houston 311 water-main-failure agent")

DEFAULT_DUCKDB = Path("../data/bellwether.duckdb")
DEFAULT_SQLITE = Path("../data/bellwether.db")
DEFAULT_RAW = Path("../data/raw/houston_311")
DEFAULT_OSM_CACHE = Path("../data/raw/osm_streets_houston.json")
DEFAULT_OSM_PBF = Path("../data/raw/osm/texas-latest.osm.pbf")

ESCALATION_CASE_TYPES = ["Major Water Leak", "Water Main Valve"]


@app.command()
def ingest_311(db: Path = DEFAULT_DUCKDB, raw_dir: Path = DEFAULT_RAW) -> None:
    """Parse Houston 311 extracts into DuckDB and build the clean complaints table."""
    houston_311.ingest_all(db, raw_dir)


@app.command()
def snap_streets(db: Path = DEFAULT_DUCKDB, pbf: Path = DEFAULT_OSM_PBF) -> None:
    """Extract Houston-area street geometry from a local OSM PBF extract and
    snap complaints to the nearest segment.

    Uses a local Geofabrik Texas extract rather than the live Overpass API —
    that shared instance proved unreliable in practice (cycled between
    working and refusing connections mid-run). Download it once:
      curl -sL -o data/raw/osm/texas-latest.osm.pbf \\
        https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf
    """
    osm_pbf.run(db, pbf)


@app.command()
def snap_streets_overpass(db: Path = DEFAULT_DUCKDB, cache: Path = DEFAULT_OSM_CACHE) -> None:
    """Legacy path: fetch street geometry from the live Overpass API instead
    of a local PBF extract. Kept for environments where downloading the
    715MB Texas PBF isn't practical, but expect it to be flaky — see
    snap-streets' docstring for what happened when this was the default."""
    osm_segments.run(db, cache)


@app.command()
def sync_moisture(db: Path = DEFAULT_SQLITE, lookback_days: int = 120) -> None:
    """Pull NOAA NCEI daily precipitation and the current USDM class into memory."""
    n = moisture_sync.sync(db, lookback_days)
    typer.echo(f"synced {n} days of moisture history")


@app.command()
def profile_study_area(
    duckdb_path: Path = DEFAULT_DUCKDB,
    sqlite_path: Path = DEFAULT_SQLITE,
    n_target: int = 2000,
    monthly_ceiling_per_account: int = 25000,
) -> None:
    """Phase 2: select a stratified sample of segments and bulk-fetch their
    Mireye profile, sharded across up to 3 accounts by geography."""
    duck = duckdb.connect(str(duckdb_path))
    duck.execute("INSTALL spatial"); duck.execute("LOAD spatial")

    segments = select_stratified_segments(duck, n_target=n_target)
    typer.echo(f"selected {len(segments)} segments with complaint history")

    keys = [os.environ.get(f"MIREYE_API_KEY_{i}") for i in (1, 2, 3)]
    accounts = [MireyeAccount(label=f"shard-{i+1}", api_key=k) for i, k in enumerate(keys) if k]
    if not accounts:
        typer.echo("no MIREYE_API_KEY_1/2/3 set in .env — aborting before spending anything", err=True)
        raise typer.Exit(1)

    shards = shard_by_longitude(segments, len(accounts))
    for account, shard in zip(accounts, shards):
        typer.echo(f"[{account.label}] {len(shard)} segments")
        run_profiling_shard(sqlite_path, account, shard, monthly_ceiling_per_account)


@app.command()
def calibrate(db: Path = DEFAULT_SQLITE) -> None:
    """Phase 6: run the weekly Calibrator — outcome harvest, metrics, guidance diff."""
    with dal.connect(db) as con:
        version = calibrator.run_calibration(con, ESCALATION_CASE_TYPES)
    typer.echo(f"calibration version {version} recorded")


@app.command()
def reawaken(
    db: Path = DEFAULT_SQLITE,
    account_label: str = "shard-1",
    run_budget_ceiling: int = 200,
) -> None:
    """Phase 6: scan closed/monitored verdicts and re-open the ones whose
    invalidation condition now holds."""
    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        typer.echo("MIREYE_API_KEY_1 not set", err=True)
        raise typer.Exit(1)
    account = MireyeAccount(label=account_label, api_key=key)
    with dal.connect(db) as con:
        version = dal.latest_guidance_version(con)
        new_ids = asyncio.run(scan_and_reawaken(con, account, run_budget_ceiling, version))
    typer.echo(f"reawakened {len(new_ids)} verdicts: {new_ids}")


@app.command()
def backtest(
    db: Path = DEFAULT_SQLITE,
    frozen_at: str = typer.Option(..., help="ISO date/time: only complaints on/before this are fed to the cascade"),
    label_window_days: int = 30,
    run_budget_ceiling: int = 200,
    max_cases: int | None = None,
    ablation: str = typer.Option(None, help="None, 'no_moisture', or 'no_memory' (design spec §8)"),
    account_label: str = "shard-1",
) -> None:
    """Phase 5: backtest the cascade against frozen historical complaints,
    scoring precision/recall against the escalation/recurrence proxy label."""
    from bellwether.eval.backtest import run_backtest

    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        typer.echo("MIREYE_API_KEY_1 not set", err=True)
        raise typer.Exit(1)
    account = MireyeAccount(label=account_label, api_key=key)

    with dal.connect(db) as con:
        result = asyncio.run(run_backtest(
            con, account, frozen_at, label_window_days, ESCALATION_CASE_TYPES,
            run_budget_ceiling, max_cases=max_cases, ablation=ablation,
        ))

    summary = {k: v for k, v in result.items() if k != "results"}
    typer.echo(json.dumps(summary, indent=2))
    out_path = db.parent / f"backtest_{ablation or 'full'}_{frozen_at.replace(':', '-')}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    typer.echo(f"full per-case results (including failures) written to {out_path}")


@app.command()
def negative_control(sample_size: int = 200) -> None:
    """Phase 5: sanity-check the NYC negative control's premise before
    spending anything — what fraction of a live NYC 311 sample even has
    usable soil data (design research found 3/12; expect a similarly low
    rate, not Houston's 11/12)."""
    from bellwether.eval.negative_control import expected_soil_usable_rate, pull_nyc_sample

    sample = pull_nyc_sample(sample_size)
    typer.echo(f"pulled {len(sample)} NYC water/sewer complaints")
    typer.echo(
        "NOTE: this only pulls the complaint sample. Running the full negative "
        "control (profiling these points through Mireye and running the cascade) "
        "is a separate step — see design spec §8 and eval/negative_control.py."
    )


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8811) -> None:
    """Run the FastAPI backend."""
    import uvicorn
    uvicorn.run("bellwether.api.app:app", host=host, port=port, reload=True)


if __name__ == "__main__":
    app()
