# Pluvial-AI

An agent that decides which of Houston's vague 311 water complaints ("brown water," "weird smell," "low pressure") are early symptoms of a main about to fail — given what the clay underneath them is doing right now — and which a crew should be sent to today.

Built for the Mireye × Delhi University Build Brief. Full design rationale, dataset verification, and the physics behind the enrichment are in [`docs/superpowers/specs/2026-08-23-bellwether-design.md`](docs/superpowers/specs/2026-08-23-bellwether-design.md). The implementation plan is in [`docs/superpowers/specs/2026-08-23-bellwether-implementation-plan.md`](docs/superpowers/specs/2026-08-23-bellwether-implementation-plan.md).

The Python package underneath is still named `bellwether` (the agent's internal codename) — every `bellwether.cli` command and `backend/bellwether/` path below refers to that package, not a different project.

## Why Houston

Houston sits on Vertisol clay that moves 1–4 inches through a wet–dry cycle. That movement — not the soil's mere existence — is what breaks pipes, and 311 complaints are the earliest, noisiest signal the city already has. Verified during design: Houston SSURGO returns usable soil data at 11/12 sampled points (vs. NYC's 3/12, where the dominant component is `Urban land` with no drainage or shrink-swell data at all).

## Architecture

```
Ingestor (ETL)  →  Triage  →  Investigator ⇄ Skeptic  →  Adjudicator  →  memory
                                                                            ↑
                                                                      Calibrator (weekly)
```

- **Data**: Mireye (soil, drainage, karst, consequence fields) + Houston 311 CRIS extracts + NOAA NCEI daily precipitation (the moisture trigger) + US Drought Monitor (coarse corroborator).
- **Enrichment**: Soil Movement Potential (spatial) × Movement Trigger State (temporal, requires memory) × Void Formation Likelihood, kept separate from a Consequence surface. No formula combines them — the agents argue over thresholded facts.
- **Memory**: SQLite. Segment dossiers fetched once from Mireye, cached forever. Every verdict carries an invalidation condition; the weekly Calibrator re-opens closed cases when the ground's physical state changes, unprompted.

## Repo layout

```
backend/bellwether/
  ingest/    Houston 311 parser, OSM street snapping, NOAA/USDM moisture sync
  mireye/    field selection, REST client, wrapped agent-facing tool, bulk profiler
  memory/    SQLite schema + typed data access layer
  agents/    Triage/Investigator/Skeptic/Adjudicator, Calibrator, reawakening loop
  eval/      backtest harness, NYC negative control, ablations
  api/       FastAPI (GET /queue, GET /segments/{id})
frontend/    Next.js dispatcher board
```

## Setup

```bash
cd backend
uv sync
cp .env.example .env   # fill in OPENAI_API_KEY, MIREYE_API_KEY_1/2/3

cd ../frontend
npm install
```

`.env` (backend):

```
OPENAI_API_KEY=...
MIREYE_API_KEY_1=...   # sharded accounts, one per team member (design spec §9)
MIREYE_API_KEY_2=...
MIREYE_API_KEY_3=...
```

## Running the pipeline

```bash
cd backend

# 1. Parse Houston 311 extracts (data/raw/houston_311/*.txt) into DuckDB
uv run python -m bellwether.cli ingest-311

# 2. Snap complaints to OSM street segments — needs a local Geofabrik Texas
#    PBF extract (the live Overpass API proved too unreliable for a scripted
#    multi-tile job — see "Current status" below). One-time download:
#    curl -sL -o data/raw/osm/texas-latest.osm.pbf \
#      https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf
uv run python -m bellwether.cli snap-streets

# 3. Pull NOAA moisture history + current drought class (free, no key needed)
uv run python -m bellwether.cli sync-moisture

# 4. Bulk-profile the study area through Mireye (spends credits — quotes first, see §9 budget)
uv run python -m bellwether.cli profile-study-area

# 5. Serve the API
uv run python -m bellwether.cli serve
```

```bash
cd frontend
npm run dev
```

Weekly maintenance (design spec §5.1, Phase 6 — the standing-watch heartbeat):

```bash
uv run python -m bellwether.cli calibrate    # outcome harvest, precision metrics, guidance diff
uv run python -m bellwether.cli reawaken     # re-open closed verdicts whose ground truth changed
```

## Tests

```bash
cd backend
uv run pytest tests/ -v
```

16 tests cover the moisture trigger-state classification, the `Urban land` soil-usability gate, the 311 parser's real-world edge cases (paginated re-inserted headers), and the Calibrator's outcome-harvesting and guidance-drafting logic — all pure-function tests that run without API keys.

## Current status

- **Data pipeline is complete and verified against real data.** All five Houston 311 extracts (2022 through 2026-08, ~1.7GB) are downloaded, byte-verified against the server, and ingested: **395,783 clean, geocoded, deduplicated water/sewer/drainage complaints**, spanning January 2022 through the day this was run. Two real bugs were found and fixed against the live extracts (not caught by synthetic fixtures alone): the export re-inserts its full header line every ~30k rows (a naive parser tries to convert the literal string "Latitude" to a coordinate and crashes), and an early parsing approach silently dropped the first several data rows on every file. Both have regression tests. One data-integrity bug was also caught and corrected: a file originally downloaded as "2026 YTD" was actually Houston's archived "2021 (Jul-Dec)" extract under a URL that doesn't encode the year — the correct current-year source is the "MTD" (month-to-date) file, which is now what's ingested.
- **Street segmentation is complete.** 168,341 Houston-area street segments extracted from a local Geofabrik Texas OSM PBF extract (not the live Overpass API — see below), with **99.5% of complaints (393,940/395,783) successfully snapped** to a segment within 150m.
- **Why a local PBF instead of Overpass:** the shared Overpass API instance proved unreliable for a scripted multi-tile job — it cycled between working normally and refusing every connection within the same session (confirmed via direct `curl` testing, not just our client code), independent of request pacing or backoff strategy. Since street geometry doesn't change week to week, a one-time local extract removes that live dependency entirely rather than working around it. `snap-streets` now uses the PBF path by default; the original Overpass path is kept as `snap-streets-overpass` for environments where the ~715MB download isn't practical.
- Backend and frontend scaffolds build and run end-to-end (verified live: FastAPI `/healthz`, `/queue`, `/segments/{id}` all respond correctly; Next.js dispatcher board renders against the live API).
- NOAA moisture sync verified against live data: Houston's 30-day antecedent rainfall genuinely trended toward a dry spell during development, and the US Drought Monitor genuinely returned no active polygon over the metro — both matching the design research.
- Agent cascade (Triage/Investigator/Skeptic/Adjudicator/Calibrator) is fully wired against the OpenAI Agents SDK and imports cleanly; a live run requires `OPENAI_API_KEY` and populated `MIREYE_API_KEY_*` and hasn't yet been run against real complaints — the remaining step before that is bulk-profiling the study area through Mireye (`profile-study-area`).
- Not yet built: negative-control live run against NYC, ablation runs, the public address-lookup surface, and the SSURGO `corsteel`/`corcon` field request to Mireye.

## What we cannot know

See design spec §10. In short: SSURGO is dominant-component and reconnaissance-scale; `Urban land` means no answer, not low risk. Native soil is a proxy for trench backfill. No pipe material or age without a further GIS join. Moisture is city-wide, not per-segment — it modulates *when*, soil discriminates *where*. Labels are escalation/recurrence proxies, not utility repair records.
