import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import typer
from dotenv import load_dotenv

from pluvial.agents import calibrator
from pluvial.agents.live import process_new_complaints
from pluvial.agents.reawaken import scan_and_reawaken
from pluvial.ingest import houston_311, moisture_sync, osm_pbf, osm_segments
from pluvial.memory import dal
from pluvial.mireye.client import MireyeAccount, MireyeClient
from pluvial.mireye.wrapper import MireyeToolWrapper, RunBudget
from pluvial.mireye.profile_job import run_profiling_shard, select_stratified_segments, shard_by_longitude

load_dotenv()

app = typer.Typer(help="Pluvial-AI: Mireye x Houston 311 water-main-failure agent")

DEFAULT_DUCKDB = Path("../data/pluvial.duckdb")
DEFAULT_DATA_DIR = Path("../data")
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
def sync_complaints(duckdb_path: Path = DEFAULT_DUCKDB, batch_size: int = 5000) -> None:
    """Copy ingested 311 complaints (and their referenced street segments,
    as unprofiled stubs) from the DuckDB warehouse into the Postgres memory
    store, so agents/backtest/reawaken can query them. Never touches a
    segment's Mireye profile if one is already cached."""
    dal.init_db()
    duck = duckdb.connect(str(duckdb_path))
    duck.execute("INSTALL spatial"); duck.execute("LOAD spatial")

    seg_rows = duck.execute(
        """
        SELECT segment_id, name, highway_class,
               (ST_YMin(geom) + ST_YMax(geom)) / 2 AS lat,
               (ST_XMin(geom) + ST_XMax(geom)) / 2 AS lon
        FROM street_segments
        """
    ).fetchall()
    with dal.connect() as con:
        for i in range(0, len(seg_rows), batch_size):
            dal.upsert_segment_stubs_bulk(con, seg_rows[i : i + batch_size])
    typer.echo(f"synced {len(seg_rows)} street segment stubs")

    complaint_rows = duck.execute(
        """
        SELECT case_number, segment_id, incident_case_type, title, status,
               latitude, longitude, CAST(created_at AS VARCHAR), CAST(closed_at AS VARCHAR)
        FROM complaints
        """
    ).fetchall()
    with dal.connect() as con:
        for i in range(0, len(complaint_rows), batch_size):
            dal.upsert_complaints_bulk(con, complaint_rows[i : i + batch_size])
    typer.echo(f"synced {len(complaint_rows)} complaints")


@app.command()
def sync_moisture(lookback_days: int = 120) -> None:
    """Pull NOAA NCEI daily precipitation and the current USDM class into memory."""
    n = moisture_sync.sync(lookback_days)
    typer.echo(f"synced {n} days of moisture history")


@app.command()
def profile_study_area(
    duckdb_path: Path = DEFAULT_DUCKDB,
    n_target: int = 2000,
    monthly_ceiling_per_account: int = 25000,
    batch_size: int = 25,
    idempotency_salt: str = "",
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
        run_profiling_shard(
            account, shard, monthly_ceiling_per_account,
            idempotency_salt=idempotency_salt, batch_size=batch_size,
        )


@app.command()
def process_queue(
    account_label: str = "shard-1",
    run_budget_ceiling: int = 0,
    since: str = None,
    until: str = None,
    max_cases: int = 100,
) -> None:
    """Phase 4's production entrypoint: run the cascade over complaints that
    don't have a verdict yet and write one for each. This is what populates
    GET /queue and gives the Calibrator/reawaken loop something to work
    with — nothing else calls dal.record_verdict except reawaken, which
    needs a verdict to already exist. run_budget_ceiling=0 keeps this
    cache-only (no live Mireye spend) unless you explicitly raise it."""
    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        typer.echo("MIREYE_API_KEY_1 not set", err=True)
        raise typer.Exit(1)
    account = MireyeAccount(label=account_label, api_key=key)
    with dal.connect() as con:
        guidance_version = dal.latest_guidance_version(con)
        new_ids = asyncio.run(
            process_new_complaints(
                con, account, run_budget_ceiling, guidance_version,
                since=since, until=until, max_cases=max_cases,
            )
        )
    typer.echo(f"recorded {len(new_ids)} verdicts: {new_ids}")


@app.command()
def calibrate() -> None:
    """Phase 6: run the weekly Calibrator — outcome harvest, metrics, guidance diff."""
    with dal.connect() as con:
        version = calibrator.run_calibration(con, ESCALATION_CASE_TYPES)
    typer.echo(f"calibration version {version} recorded")


@app.command()
def reawaken(
    account_label: str = "shard-1",
    run_budget_ceiling: int = 200,
    mode: str = typer.Option(
        "triage",
        help="'triage' re-opens 311 verdicts; 'address' re-opens threat rulings whose "
             "moisture condition now holds — the 'watch this address' loop.",
    ),
) -> None:
    """Scan rulings that stated a physical precondition for reconsideration
    and re-argue the ones whose precondition now holds — unprompted."""
    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        typer.echo("MIREYE_API_KEY_1 not set", err=True)
        raise typer.Exit(1)
    if mode not in ("triage", "address"):
        raise typer.BadParameter("mode must be 'triage' or 'address'")
    account = MireyeAccount(label=account_label, api_key=key)
    with dal.connect() as con:
        version = dal.latest_guidance_version(con)
        if mode == "address":
            from pluvial.agents.reawaken import scan_and_reawaken_addresses

            new_ids = asyncio.run(scan_and_reawaken_addresses(con, account, version))
            typer.echo(f"reawakened {len(new_ids)} threat rulings: {new_ids}")
            return
        new_ids = asyncio.run(scan_and_reawaken(con, account, run_budget_ceiling, version))
    typer.echo(f"reawakened {len(new_ids)} verdicts: {new_ids}")


@app.command()
def backtest(
    frozen_at: str = typer.Option(..., help="ISO date/time: only complaints on/before this are fed to the cascade"),
    label_window_days: int = 30,
    run_budget_ceiling: int = 200,
    max_cases: int | None = None,
    ablation: str = typer.Option(None, help="None, 'no_moisture', or 'no_memory' (design spec §8)"),
    mode: str = typer.Option(
        "triage",
        help="'triage' scores the 311 cascade as shipped originally; 'address' feeds the same "
             "cases only their ground physics and scores the service_lines ruling. The gap "
             "between them is what complaint evidence contributes.",
    ),
    account_label: str = "shard-1",
    rescore: Path = typer.Option(
        None,
        help="Path to a prior backtest JSON; re-runs that run's exact cases instead of "
             "the first --max-cases. Use this to compare two runs like for like.",
    ),
) -> None:
    """Phase 5: backtest the cascade against frozen historical complaints,
    scoring precision/recall against the escalation/recurrence proxy label."""
    from pluvial.eval.backtest import run_backtest

    only_cases = None
    if rescore:
        prior = json.loads(rescore.read_text())
        only_cases = [c for r in prior["results"] for c in r["case_numbers"]]
        typer.echo(f"re-scoring {len(only_cases)} cases pinned from {rescore}")

    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        typer.echo("MIREYE_API_KEY_1 not set", err=True)
        raise typer.Exit(1)
    account = MireyeAccount(label=account_label, api_key=key)

    if mode not in ("triage", "address"):
        raise typer.BadParameter("mode must be 'triage' or 'address'")
    if mode == "address" and ablation:
        raise typer.BadParameter("ablations are defined against the triage cascade only")

    with dal.connect() as con:
        if mode == "address":
            from pluvial.eval.address_backtest import run_address_backtest

            result = asyncio.run(run_address_backtest(
                con, account, frozen_at, label_window_days, ESCALATION_CASE_TYPES,
                max_cases=max_cases, only_cases=only_cases,
            ))
        else:
            result = asyncio.run(run_backtest(
                con, account, frozen_at, label_window_days, ESCALATION_CASE_TYPES,
                run_budget_ceiling, max_cases=max_cases, ablation=ablation,
                only_cases=only_cases,
            ))

    summary = {k: v for k, v in result.items() if k != "results"}
    typer.echo(json.dumps(summary, indent=2))

    # A re-score reads its case list from a prior run's file, so it must never
    # write back over that file: doing so destroys the very baseline it is
    # being compared against, and these results are expensive to reproduce.
    tag = "address" if mode == "address" else (ablation or "full")
    stem = f"backtest_{tag}_{frozen_at.replace(':', '-')}"
    if rescore:
        stem = f"backtest_rescore_{tag}_{frozen_at.replace(':', '-')}"
    out_path = DEFAULT_DATA_DIR / f"{stem}.json"
    if rescore and out_path.resolve() == rescore.resolve():
        raise typer.BadParameter("refusing to overwrite the --rescore baseline")
    out_path.write_text(json.dumps(result, indent=2, default=str))
    typer.echo(f"full per-case results (including failures) written to {out_path}")


@app.command()
def negative_control(
    sample_size: int = 200,
    run_full: bool = False,
    profile_limit: int = 25,
    credit_ceiling: int = 1000,
    account_label: str = "shard-1",
) -> None:
    """Phase 5 (design spec §8). Default: sanity-check the premise without
    spending anything — what fraction of a live NYC 311 sample even has
    usable soil data (design research found 3/12; expect a similarly low
    rate, not Houston's 11/12). Pass --run-full to actually profile
    `profile_limit` of those points through Mireye (quoted against
    credit_ceiling first) and run the unmodified cascade over them,
    reporting whether the soil_usable gate ever gets bypassed."""
    from pluvial.eval.negative_control import expected_soil_usable_rate, pull_nyc_sample

    sample = pull_nyc_sample(sample_size)
    typer.echo(f"pulled {len(sample)} NYC water/sewer complaints")

    if not run_full:
        typer.echo(
            "NOTE: sample only, nothing spent. Pass --run-full to profile a subset "
            "through Mireye and run the cascade — see design spec §8."
        )
        return

    from pluvial.eval.negative_control import profile_nyc_sample, run_negative_control

    key = os.environ.get("MIREYE_API_KEY_1")
    if not key:
        typer.echo("MIREYE_API_KEY_1 not set", err=True)
        raise typer.Exit(1)
    account = MireyeAccount(label=account_label, api_key=key)

    subset = sample[:profile_limit]
    profiled = profile_nyc_sample(account, subset, credit_ceiling)
    typer.echo(f"profiled {len(profiled)} NYC points through Mireye")

    usable_rate = expected_soil_usable_rate([p["profile"] for p in profiled])
    typer.echo(f"soil-usable rate: {usable_rate:.0%} (Houston bulk sample: ~14%)")

    with dal.connect() as con:
        guidance_version = dal.latest_guidance_version(con)
        result = asyncio.run(run_negative_control(con, account, profiled, guidance_version))

    typer.echo(json.dumps({k: v for k, v in result.items() if k != "results"}, indent=2))
    out_path = DEFAULT_DATA_DIR / "negative_control_nyc.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    typer.echo(f"full per-case results written to {out_path}")


@app.command()
def analyze_address(
    address: str,
    confirm: bool = typer.Option(False, help="Actually spend the quoted credits. Without it this stops at the quote."),
    threats: str = "foundation,service_lines,subsidence",
    run_budget_ceiling: int = 500,
) -> None:
    """Address mode from the command line: geocode, plan nine sample points,
    quote, and — only with --confirm — fetch live and run the three
    adversarial cascades.

    The same two-step gate the UI uses. Without --confirm this prints the
    plan and the quote and spends nothing, which is also how you check that
    an address geocodes before paying to profile it.
    """
    from pluvial import analyze
    from pluvial.agents.address_cascade import record_rulings, run_all_threats
    from pluvial.agents.context import AddressContext
    from pluvial.mireye.accounts import MireyeClientPool

    dal.init_db()
    with dal.connect() as con, MireyeClientPool() as client:
        # An address that does not geocode is an ordinary outcome of typing
        # one, not a crash. A traceback here buries the one line that matters.
        try:
            plan = analyze.plan(con, address, client)
        except analyze.GeocodeFailed as e:
            typer.echo(f"{e}. Try adding a city and state, or a nearby street number.", err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(plan.as_dict(), indent=2, default=str))

        if not confirm:
            typer.echo(f"\nquoted {plan.quoted_credits} credits, spent 0. Re-run with --confirm to fetch.")
            raise typer.Exit(0)

        analyze.ensure_moisture(plan)
        analyze.fetch_samples(
            con, client, plan,
            on_point=lambda p: typer.echo(
                f"  point {p['sample_id']}: soil_usable={p['soil_usable']} "
                f"{(p['profile'].get('soil_map_unit_name') or {}).get('value')}"
            ),
        )

        samples = dal.location_samples(con, plan.location_id)
        ctx = AddressContext(
            con=con,
            mireye=MireyeToolWrapper(con, client, RunBudget(ceiling=0)),
            run_budget=RunBudget(ceiling=run_budget_ceiling),
            guidance_version=dal.latest_guidance_version(con),
            location=dal.get_location(con, plan.location_id),
            samples=samples,
            region_key=plan.region_key,
        )

        triage_out, results = asyncio.run(
            run_all_threats(ctx, tuple(t.strip() for t in threats.split(",") if t.strip()))
        )
        typer.echo(f"\ntriage: {triage_out.decision} — {triage_out.reason}")
        for threat, (ruling, _, skeptic_out) in results.items():
            cited = [c for c in ruling.decisive_evidence if c.sample_id is not None]
            typer.echo(
                f"\n[{threat}] {ruling.severity.upper()}  "
                f"({len(ruling.decisive_evidence)} claims, {len(cited)} point-anchored, "
                f"vetoed points: {skeptic_out.vetoed_sample_ids})"
            )
            typer.echo(f"  {ruling.explanation}")
            for u in ruling.unknowns:
                typer.echo(f"  unknown: {u}")

        ids = record_rulings(con, plan.location_id, ctx.guidance_version, results)
        con.commit()
        typer.echo(f"\nrecorded rulings {ids} for location {plan.location_id}")


@app.command()
def search_region(
    query: str,
    credit_budget: int = 2500,
    confirm: bool = typer.Option(False, help="Actually run the traversal. Without it, nothing is spent."),
    adjudicate: bool = True,
) -> None:
    """Adaptive regional search: find ground in a metro or county that is
    less likely to move, spending up to a ceiling and no further.

    The ceiling is enforced before each request, so an exhausted budget means
    the request was never sent. Partial results come back labelled as
    partial rather than silently truncated.
    """
    from pluvial.agents.region_search import adjudicate_survivors, run_region_search
    from pluvial.api.events import EventStream
    from pluvial.mireye.accounts import MireyeClientPool

    if not confirm:
        typer.echo(
            f"would search: {query!r} with a ceiling of {credit_budget} credits.\n"
            "Re-run with --confirm to spend."
        )
        raise typer.Exit(0)

    dal.init_db()
    stream = EventStream()

    async def emit(event) -> None:
        payload = event.payload
        if event.type == "cell_scored":
            score = payload["score"]
            typer.echo(
                f"  L{payload['level']} cell {payload['cell_id']} "
                f"({payload['lat']:.4f}, {payload['lon']:.4f}) "
                f"score={'—' if score is None else f'{score:.2f}'} "
                f"[{payload.get('soil_map_unit_name')}] {stream.credits_spent}cr"
            )
        elif event.type == "cell_subdivided":
            typer.echo(f"  subdividing L{payload['level']} cell (score {payload['score']:.2f})")
        elif event.type in ("message", "error"):
            typer.echo(f"[{event.type}] {payload.get('text') or payload.get('message')}")
        elif event.type == "ruling":
            typer.echo(f"    ruling {payload['threat']}: {payload['severity']}")

    async def go() -> None:
        with dal.connect() as con, MireyeClientPool() as client:
            result = await run_region_search(con, client, query, credit_budget, stream, emit)
            if result is None:
                return
            typer.echo(
                f"\nsearched {len(result.scored)} cells across {result.levels_completed} level(s); "
                f"spent {result.credits_spent}/{result.credit_budget} credits; "
                f"exhausted_budget={result.exhausted_budget}"
            )
            for rank, sc in enumerate(result.survivors, 1):
                typer.echo(f"  survivor {rank}: {sc.cell.lat:.4f}, {sc.cell.lon:.4f} score={sc.score:.3f}")
            if adjudicate:
                summary = await adjudicate_survivors(con, client, result, stream, emit)
                typer.echo(f"\nadjudicated: {json.dumps(summary, indent=2, default=str)}")

    asyncio.run(go())


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8811) -> None:
    """Run the FastAPI backend."""
    import uvicorn
    uvicorn.run("pluvial.api.app:app", host=host, port=port, reload=True)


if __name__ == "__main__":
    app()
