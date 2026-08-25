-- Pluvial-AI memory store. Postgres (Neon). See design spec §5.3 and
-- docs/superpowers/specs/2026-08-24-neon-postgres-port-design.md.
--
-- Mechanical 1:1 translation of schema.sql (SQLite) — no structural
-- changes: same tables, same columns, same relationships. Only types
-- change: INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL, *_json TEXT ->
-- JSONB, soil_usable INTEGER -> BOOLEAN, TEXT timestamps -> TIMESTAMPTZ/DATE,
-- REAL -> DOUBLE PRECISION.
--
-- segments: one row per OSM street segment we've profiled through Mireye.
-- The physical profile is fetched once and cached forever (§9 budget).
CREATE TABLE IF NOT EXISTS segments (
    segment_id      BIGINT PRIMARY KEY,    -- OSM way id
    name            TEXT,
    highway_class   TEXT,
    centroid_lat    DOUBLE PRECISION NOT NULL,
    centroid_lon    DOUBLE PRECISION NOT NULL,
    profile_json    JSONB,                 -- raw Mireye field values, one row per field with source
    soil_usable     BOOLEAN,               -- false when dominant component is Urban land
    profiled_at     TIMESTAMPTZ,           -- timestamp of the Mireye fetch
    mireye_account  TEXT                   -- which sharded account paid for this fetch
);

-- moisture_history: daily antecedent-moisture series (NCEI) plus the coarse
-- weekly USDM corroborator from Mireye. Not per-point: the design's finding
-- is that moisture is a temporal modulator, not a spatial discriminator,
-- inside one metro. It IS per-region, though — address mode can be asked
-- about anywhere in the country — so the key is (region_key, date), where
-- region_key is the nearest NOAA GHCN station id.
CREATE TABLE IF NOT EXISTS moisture_history (
    region_key      TEXT NOT NULL,
    date            DATE NOT NULL,
    station_id      TEXT,
    precip_mm       DOUBLE PRECISION,
    tmax_c          DOUBLE PRECISION,
    antecedent_30d_mm DOUBLE PRECISION,
    antecedent_60d_mm DOUBLE PRECISION,
    antecedent_90d_mm DOUBLE PRECISION,
    trigger_state   TEXT,                  -- drying | sustained_dry | rewetting | stable
    usdm_class      TEXT,                  -- D0-D4, null = no drought polygon (better than D0)
    PRIMARY KEY (region_key, date)
);

-- complaints: cleaned 311 records, snapped to a segment. Mirrors the DuckDB
-- `complaints` table but lives in the memory store so agents can query it
-- alongside verdicts without touching the DuckDB warehouse.
CREATE TABLE IF NOT EXISTS complaints (
    case_number         TEXT PRIMARY KEY,
    segment_id           BIGINT REFERENCES segments(segment_id),
    incident_case_type   TEXT NOT NULL,
    title                TEXT,
    status               TEXT,
    latitude             DOUBLE PRECISION,
    longitude            DOUBLE PRECISION,
    created_at           TIMESTAMPTZ NOT NULL,
    closed_at            TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_complaints_segment ON complaints(segment_id);
CREATE INDEX IF NOT EXISTS idx_complaints_created ON complaints(created_at);

-- verdicts: one row per Adjudicator decision. reasoning/cited_evidence are
-- JSON so every claim carries the Mireye field + value + source that backed
-- it (design spec rule 5: "an answer nobody can check is not a result").
CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id          BIGSERIAL PRIMARY KEY,
    segment_id           BIGINT NOT NULL REFERENCES segments(segment_id),
    case_numbers          JSONB NOT NULL,    -- JSON array of complaint case_numbers this verdict covers
    disposition           TEXT NOT NULL,     -- dispatch | inspect | monitor | close
    priority               TEXT,              -- from the consequence surface, separate from likelihood
    reasoning_json         JSONB NOT NULL,    -- investigator/skeptic claims + adjudicator ruling
    cited_evidence_json    JSONB NOT NULL,    -- [{field, value, source}, ...]
    rejected_counter_argument TEXT,
    invalidation_condition_json JSONB,        -- structured condition that would re-open this verdict
    agent_version          TEXT NOT NULL,     -- guidance version tag, ties to calibration.version
    decided_at             TIMESTAMPTZ NOT NULL,
    reawakened_from        BIGINT REFERENCES verdicts(verdict_id),
    frozen_at              TIMESTAMPTZ        -- set only during backtests: the T this verdict was produced under
);
CREATE INDEX IF NOT EXISTS idx_verdicts_segment ON verdicts(segment_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_cited_evidence ON verdicts USING GIN (cited_evidence_json);
CREATE INDEX IF NOT EXISTS idx_verdicts_reasoning ON verdicts USING GIN (reasoning_json);

-- outcomes: what actually happened after a verdict, used for calibration
-- and eval labelling. label is the escalation/recurrence proxy from spec §8.
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id       BIGSERIAL PRIMARY KEY,
    verdict_id        BIGINT NOT NULL REFERENCES verdicts(verdict_id),
    observed_outcome   TEXT NOT NULL,   -- free text / case reference describing what happened
    label              TEXT NOT NULL,   -- confirmed_failure | no_failure | unknown
    observed_at         TIMESTAMPTZ NOT NULL
);

-- calibration: one row per weekly Calibrator run. guidance_diff is a
-- versioned, human-diffable record of what changed in agent instructions.
CREATE TABLE IF NOT EXISTS calibration (
    version          BIGSERIAL PRIMARY KEY,
    run_at            TIMESTAMPTZ NOT NULL,
    metrics_json       JSONB NOT NULL,   -- precision/recall per (soil_class, symptom_class, trigger_state)
    reporting_bias_json JSONB,           -- per-area reporting-propensity weights (§4.6)
    guidance_diff       TEXT NOT NULL    -- unified-diff-style text of the prompt change
);

-- precedents: materialized view over verdicts+outcomes for fast case-based
-- retrieval, rebuilt after each calibration run. Not a vector store —
-- retrieval keys on the same discrete classes the agents already reason in.
CREATE TABLE IF NOT EXISTS precedents (
    verdict_id        BIGINT PRIMARY KEY REFERENCES verdicts(verdict_id),
    shrink_swell_class TEXT,
    trigger_state       TEXT,
    symptom_class        TEXT,
    disposition           TEXT,
    label                  TEXT
);

-- ---------------------------------------------------------------------------
-- Address mode (docs/superpowers/specs/2026-08-25-address-mode-live-demo-design.md).
-- Deliberately separate from the eval tables above: the backtest, the
-- ablations and the Calibrator all read `verdicts`, and adding nullable FKs
-- to it to carry product state would put 141 recorded verdicts at migration
-- risk for no gain. Product rows live here; eval rows stay where they are.
-- ---------------------------------------------------------------------------

-- locations: one row per address someone asked about. region_key is the
-- resolved nearest NOAA station id, which is what makes moisture history
-- national rather than Houston-only.
CREATE TABLE IF NOT EXISTS locations (
    location_id     BIGSERIAL PRIMARY KEY,
    query_text      TEXT NOT NULL,        -- what the user typed
    label           TEXT,                 -- geocoder's display name
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    region_key      TEXT,                 -- nearest NOAA GHCN station id
    geocoded_at     TIMESTAMPTZ NOT NULL
);

-- location_samples: the 9-point sample plan (1 property, 4 frontage,
-- 4 neighbourhood). One Mireye fetch per row. sample_id is what every
-- spatial claim cites, and what the map binds to.
CREATE TABLE IF NOT EXISTS location_samples (
    sample_id       BIGSERIAL PRIMARY KEY,
    location_id     BIGINT NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
    role            TEXT NOT NULL,        -- property | frontage | neighbourhood
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    profile_json    JSONB,                -- {field: {value, source}}, null until fetched
    soil_usable     BOOLEAN,
    mireye_account  TEXT,
    fetched_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_location_samples_location ON location_samples(location_id);

-- threat_rulings: one adjudicated ruling per (location, threat). Mirrors
-- `verdicts` in shape so the Calibrator's reawakening machinery reads the
-- same invalidation-condition contract, but carries severity rather than a
-- dispatcher disposition.
CREATE TABLE IF NOT EXISTS threat_rulings (
    ruling_id       BIGSERIAL PRIMARY KEY,
    location_id     BIGINT NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
    threat          TEXT NOT NULL,        -- foundation | service_lines | subsidence
    severity        TEXT NOT NULL,        -- high | elevated | low | unresolved
    reasoning_json  JSONB NOT NULL,       -- investigator + skeptic record
    cited_evidence_json JSONB NOT NULL,   -- [{field, value, source, sample_id}, ...]
    rejected_counter_argument TEXT,
    invalidation_condition_json JSONB,
    agent_version   TEXT NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL,
    reawakened_from BIGINT REFERENCES threat_rulings(ruling_id)
);
CREATE INDEX IF NOT EXISTS idx_threat_rulings_location ON threat_rulings(location_id);

-- region_searches: one adaptive traversal. exhausted_budget records whether
-- the search stopped because it converged or because it ran out of credits,
-- so a partial result is always labelled as partial.
CREATE TABLE IF NOT EXISTS region_searches (
    search_id       BIGSERIAL PRIMARY KEY,
    query_text      TEXT NOT NULL,
    objective_json  JSONB NOT NULL,
    bbox            JSONB NOT NULL,       -- {min_lat, min_lon, max_lat, max_lon}
    credit_budget   INTEGER NOT NULL,
    credits_spent   INTEGER NOT NULL DEFAULT 0,
    exhausted_budget BOOLEAN NOT NULL DEFAULT FALSE,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ
);

-- region_cells: doubles as the fetch cache for one search. A subdivided
-- cell's existing sample point is reused rather than refetched, which is
-- where much of the adaptive saving comes from.
CREATE TABLE IF NOT EXISTS region_cells (
    cell_id         BIGSERIAL PRIMARY KEY,
    search_id       BIGINT NOT NULL REFERENCES region_searches(search_id) ON DELETE CASCADE,
    level           INTEGER NOT NULL,
    lat             DOUBLE PRECISION NOT NULL,   -- cell centre, the sampled point
    lon             DOUBLE PRECISION NOT NULL,
    bbox            JSONB NOT NULL,
    profile_json    JSONB,
    soil_usable     BOOLEAN,
    objective_score DOUBLE PRECISION,
    subdivided      BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_region_cells_search ON region_cells(search_id);
