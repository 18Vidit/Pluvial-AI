-- Bellwether memory store. SQLite. See design spec §5.3.
--
-- segments: one row per OSM street segment we've profiled through Mireye.
-- The physical profile is fetched once and cached forever (§9 budget).
CREATE TABLE IF NOT EXISTS segments (
    segment_id      INTEGER PRIMARY KEY,   -- OSM way id
    name            TEXT,
    highway_class   TEXT,
    centroid_lat    REAL NOT NULL,
    centroid_lon    REAL NOT NULL,
    profile_json    TEXT,                  -- raw Mireye field values, one row per field with source
    soil_usable     INTEGER,               -- 0/1: false when dominant component is Urban land
    profiled_at     TEXT,                  -- ISO timestamp of the Mireye fetch
    mireye_account  TEXT                   -- which sharded account paid for this fetch
);

-- moisture_history: city-wide daily antecedent-moisture series (NCEI) plus
-- the coarse weekly USDM corroborator from Mireye. Not per-segment: the
-- design's finding is that moisture is a temporal modulator, not a spatial
-- discriminator, inside one metro.
CREATE TABLE IF NOT EXISTS moisture_history (
    date            TEXT PRIMARY KEY,      -- YYYY-MM-DD
    station_id      TEXT,
    precip_mm       REAL,
    tmax_c          REAL,
    antecedent_30d_mm REAL,
    antecedent_60d_mm REAL,
    antecedent_90d_mm REAL,
    trigger_state   TEXT,                  -- drying | sustained_dry | rewetting | stable
    usdm_class      TEXT                   -- D0-D4, null = no drought polygon (better than D0)
);

-- complaints: cleaned 311 records, snapped to a segment. Mirrors the DuckDB
-- `complaints` table but lives in the memory store so agents can query it
-- alongside verdicts without touching the DuckDB warehouse.
CREATE TABLE IF NOT EXISTS complaints (
    case_number         TEXT PRIMARY KEY,
    segment_id           INTEGER REFERENCES segments(segment_id),
    incident_case_type   TEXT NOT NULL,
    title                TEXT,
    status               TEXT,
    latitude             REAL,
    longitude            REAL,
    created_at           TEXT NOT NULL,
    closed_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_complaints_segment ON complaints(segment_id);
CREATE INDEX IF NOT EXISTS idx_complaints_created ON complaints(created_at);

-- verdicts: one row per Adjudicator decision. reasoning/cited_evidence are
-- JSON so every claim carries the Mireye field + value + source that backed
-- it (design spec rule 5: "an answer nobody can check is not a result").
CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id           INTEGER NOT NULL REFERENCES segments(segment_id),
    case_numbers          TEXT NOT NULL,     -- JSON array of complaint case_numbers this verdict covers
    disposition           TEXT NOT NULL,     -- dispatch | inspect | monitor | close
    priority               TEXT,              -- from the consequence surface, separate from likelihood
    reasoning_json         TEXT NOT NULL,     -- investigator/skeptic claims + adjudicator ruling
    cited_evidence_json    TEXT NOT NULL,     -- [{field, value, source}, ...]
    rejected_counter_argument TEXT,
    invalidation_condition_json TEXT,         -- structured condition that would re-open this verdict
    agent_version          TEXT NOT NULL,     -- guidance version tag, ties to calibration.version
    decided_at             TEXT NOT NULL,
    reawakened_from        INTEGER REFERENCES verdicts(verdict_id),
    frozen_at              TEXT               -- set only during backtests: the T this verdict was produced under
);
CREATE INDEX IF NOT EXISTS idx_verdicts_segment ON verdicts(segment_id);

-- outcomes: what actually happened after a verdict, used for calibration
-- and eval labelling. label is the escalation/recurrence proxy from spec §8.
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id        INTEGER NOT NULL REFERENCES verdicts(verdict_id),
    observed_outcome   TEXT NOT NULL,   -- free text / case reference describing what happened
    label              TEXT NOT NULL,   -- confirmed_failure | no_failure | unknown
    observed_at         TEXT NOT NULL
);

-- calibration: one row per weekly Calibrator run. guidance_diff is a
-- versioned, human-diffable record of what changed in agent instructions.
CREATE TABLE IF NOT EXISTS calibration (
    version          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at            TEXT NOT NULL,
    metrics_json       TEXT NOT NULL,   -- precision/recall per (soil_class, symptom_class, trigger_state)
    reporting_bias_json TEXT,           -- per-area reporting-propensity weights (§4.6)
    guidance_diff       TEXT NOT NULL   -- unified-diff-style text of the prompt change
);

-- precedents: materialized view over verdicts+outcomes for fast case-based
-- retrieval, rebuilt after each calibration run. Not a vector store —
-- retrieval keys on the same discrete classes the agents already reason in.
CREATE TABLE IF NOT EXISTS precedents (
    verdict_id        INTEGER PRIMARY KEY REFERENCES verdicts(verdict_id),
    shrink_swell_class TEXT,
    trigger_state       TEXT,
    symptom_class        TEXT,
    disposition           TEXT,
    label                  TEXT
);
