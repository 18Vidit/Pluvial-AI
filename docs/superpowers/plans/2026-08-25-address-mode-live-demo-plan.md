# Implementation plan: address mode, live map-anchored demo

Spec: [`docs/superpowers/specs/2026-08-25-address-mode-live-demo-design.md`](../specs/2026-08-25-address-mode-live-demo-design.md)

Goal restated: enter any US address, watch Mireye fetched live, watch three
pairs of agents argue over it with every claim anchored to a point on a
map, then interrogate the result in chat, including an adaptive regional
search. Target: a 2-3 minute demo in which nothing is precomputed.

Each phase leaves the repo working. Do not start a phase until the previous
one's checks pass. Phases 1-5 are the demo spine; 6-7 are depth.

Verified before writing this plan: `agents` SDK is 0.22.0 and exposes
`Runner.run_streamed`, so the streaming path in Phase 3 is available and
does not need a custom wrapper around `Runner.run`.

---

## Phase 0 — Prerequisites

1. Confirm with the organizers whether the **parcel fields** are hard out of
   scope. The spec assumes yes and draws no boundaries. This is the only
   open external question and it does not block anything below.
2. Confirm the **real Mireye credit ceiling**. Regional search is ~4,600
   credits per query and recurs. At the brief's stated 25,000/account,
   about five searches exhaust one account. If the ceiling is real, Phase 6
   should be cut rather than shipped and demoed repeatedly.
3. Add deps: `maplibre-gl` to `frontend/package.json`.

**Check:** `npm install` succeeds; a `DATABASE_URL`-backed `uv run pytest tests/ -q` still passes 22.

---

## Phase 1 — Schema and sampling geometry

1. Append to `pluvial/memory/schema_postgres.sql`: `locations`,
   `location_samples`, `threat_rulings`, `region_searches`, `region_cells`
   per the spec. `segments`/`complaints`/`verdicts`/`outcomes`/
   `calibration`/`precedents` are untouched.
2. Repin `moisture_history` to `(region_key, date)`. This is the one
   destructive change: write a small migration that backfills the existing
   117 rows with the Houston station id (`USW00012960`) as `region_key`
   before altering the key.
3. New `pluvial/geo/sample_plan.py` — pure functions, no I/O:
   - `build_sample_plan(lat, lon) -> list[SamplePoint]`: 1 property, 4
     frontage at 30m N/S/E/W, 4 neighbourhood at 150m diagonals (9 points).
   - `offset_m(lat, lon, dx_m, dy_m)` for the metre-to-degree conversion.
4. New `pluvial/ingest/stations.py` — `nearest_station(lat, lon)` resolving
   a NOAA station id from a bundled station list, giving `region_key`.
5. `dal.py` functions for the new tables, following existing naming.

**Check:** DDL applies idempotently; `moisture_history` retains 117 rows all
carrying the Houston `region_key`; `build_sample_plan` unit-tested for point
count and approximate spacing (pure function, no network).

---

## Phase 2 — Address-mode cascade

1. `agents/models.py`:
   - `CitedClaim` gains `sample_id: int | None` (nullable: `moisture_history`
     is citywide and has no point).
   - `SkepticOutput` gains `vetoed_sample_ids: list[int]`.
   - New `Threat = Literal["foundation", "service_lines", "subsidence"]` and
     `Severity = Literal["high", "elevated", "low", "unresolved"]`.
   - New `ThreatRuling` mirroring `Verdict` but carrying `threat` and
     `severity` instead of `disposition`/`priority`.
2. `agents/tools.py`: `mireye_profile` returns per-point values keyed by
   `sample_id` rather than one blended profile. `neighbourhood_complaints`
   is dropped from the address-mode toolset (no 311 in the product).
3. `agents/cascade.py`: rewrite the four role prompts.
   - Reframe from "one new 311 complaint" to "a location and one threat".
   - **Remove the Houston/Lake Charles naming.** State the physics
     generally: expansive clay is not a Texas phenomenon, and a reviewer in
     Denver will notice.
   - Investigator loses complaint clustering; Skeptic loses hydrant
     flushing; both keep the Honesty Gate and `usgs_gage`.
   - Adjudicator returns `ThreatRuling`; `unresolved` is mandatory (never
     `low`) when every soil-derived claim for that threat was vetoed.
4. New `agents/address_cascade.py`: `run_address_cascade(ctx, location,
   samples, threat)` for one threat, and `run_all_threats(...)` running the
   three concurrently via `asyncio.gather` after one shared Triage.

**Check:** a scripted end-to-end run against one real address produces three
`ThreatRuling`s with at least one `sample_id`-bearing claim each; a known
Urban-land address yields `unresolved` (not `low`) on `foundation`.

---

## Phase 3 — Streaming

1. New `pluvial/api/events.py` defining the SSE event envelope:
   `{type, lane, payload, credits_spent}` with types `sample_planned`,
   `quote`, `point_profiled`, `tool_call`, `claim`, `veto`, `ruling`,
   `cell_scored`, `cell_subdivided`, `done`, `error`.
2. Convert `address_cascade` to use `Runner.run_streamed`, translating SDK
   stream events into the envelope. Tool-call events are what keep the UI
   moving during agent latency and are the main reason for this phase.
3. Merge three concurrent lanes into one `asyncio.Queue` consumed by the
   endpoint, each event tagged with its `lane`.
4. `POST /analyze/plan` (geocode, sample plan, quote — **spends nothing**)
   and `POST /analyze/run` (confirm, fetch, cascades, SSE) in
   `pluvial/api/app.py`. `POST /cascade/run` is removed; it is the
   `ceiling=0` path that made Mireye invisible.

**Check:** `curl -N` against `/analyze/run` shows interleaved events from
all three lanes with a monotonically rising `credits_spent`, and a
`tool_call` event arrives within a few seconds of the run starting (proving
the UI will not sit idle).

---

## Phase 4 — Map with evidence binding

1. `frontend/src/components/GroundMap.tsx`: MapLibre GL, free raster
   tiles, no API key.
2. Sample-point layer with four states: pending, fetched, cited, vetoed.
3. Property marker, frontage cross, consequence rings drawn from
   `nearest_school_distance_m` / `nearest_hospital_distance_m` (distances,
   not coordinates — rings are honest, pins would fabricate positions).
4. `frontend/src/lib/stream.ts`: `EventSource` client mapping the envelope
   to state.
5. Three streaming lanes beside the map. A `claim` carrying a `sample_id`
   pulses that point; a `veto` greys it. This binding is the centrepiece.
6. Click a point to see its raw Mireye values **with sources**.

**Check:** a live run against a real address paints 9 points, streams
claims into three lanes, and visibly pulses the cited point. Verify in the
browser preview and capture a screenshot.

---

## Phase 5 — Conversational follow-up

1. New `pluvial/agents/orchestrator.py` with five tools:
   `explain_ruling`, `explain_veto`, `compare_samples`, `analyze_location`,
   `sample_point`.
2. `analyze_location` and `sample_point` emit `quote` and **must not**
   spend until a confirm arrives — the agent proposes, only the user
   confirms, mirroring `MireyeToolWrapper`.
3. Prompt constraint: answer only from fetched evidence, or fetch more.
   Never speculate about ground with no data. State plainly that national
   aggregate and parcel-level inverse questions are out of scope.
4. `POST /chat` reusing the Phase 3 envelope, so a chat-driven fetch drives
   the map identically to the address box. Thread state in-process, keyed
   by session id, no persistence.
5. Chat composer below the lanes, active once rulings land.

**Check:** "why did you veto that?" returns the Skeptic's reasoning with the
correct point highlighted; "what about 50m north?" produces a quote,
spends nothing until confirmed, then adds a point to the map.

---

## Phase 6 — Regional search

Cut this phase if Phase 0's credit check comes back at the brief's stated
ceiling.

1. New `pluvial/geo/explorer.py`, pure functions: `score_cell(profile,
   objective)`, `select_for_subdivision(cells, objective)` on *promising*
   and *uncertain* (neighbour disagreement), `subdivide(cell)`.
2. New `pluvial/agents/region_search.py`: agent parses query into objective
   and bbox, then the deterministic loop runs up to 3 levels, reusing
   existing corner points from `region_cells` rather than refetching.
3. Budget enforcement via `RunBudget`. On exhaustion, return partial
   results with `exhausted_budget=True` — never silently overspend, never
   silently truncate.
4. Full three-cascade adjudication on the **top 3 survivors only**.
5. `search_region` registered as the sixth chat tool.
6. Map layer: cells as rectangles shaded by objective score, subdividing
   live.

**Check:** a search with a deliberately small ceiling returns
`exhausted_budget=True` and partial results; a normal metro search stays
under ~5,000 credits and produces finer cells over contested ground than
uniform terrain. Scoring and subdivision rules tested as pure functions
without any fetch.

---

## Phase 7 — Address-mode evaluation

1. `backtest --mode address`: for each pinned Houston case, feed only the
   location's ground physics (no complaint text, no clustering) and score
   `high|elevated` on `service_lines` against the same escalation labels.
   Reuses the existing `--rescore` flag for identical case selection.
2. Report beside the triage-mode number; the gap quantifies what complaint
   evidence contributes.

**Check:** runs on the pinned 50 and produces a scored result. Expect a
lower number than triage mode — the `no_memory` ablation already showed
removing prior-verdict access roughly halves recall. Report it honestly
either way.

---

## Outstanding, carried forward and not solved here

- **The unexplained eval shift.** The re-score of the pinned 50 gave
  75.0%/16.7% against the published 78.6%/30.6%, with `inspect` 14→8.
  Labels and Mireye profiles were verified byte-identical and the
  contamination hypothesis was tested and disproved. No accuracy number
  should be published until this is resolved.
- **`dossier_lookup` does not filter to `frozen_at`**, so the backtest can
  see some post-cutoff verdicts. Pre-existing, not introduced by the port.
- **Deployment.** Deferred by the user, but the brief's first handin item
  is the product "running, reachable, usable by someone who did not build
  it". This remains an open gap against the handin criteria.
