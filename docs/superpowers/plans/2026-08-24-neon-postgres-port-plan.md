# Implementation plan: SQLite → Neon Postgres port

Spec: [`docs/superpowers/specs/2026-08-24-neon-postgres-port-design.md`](../specs/2026-08-24-neon-postgres-port-design.md)

Goal restated: swap the memory store from SQLite to Neon Postgres with zero
behavior change, and in the process pull the 56 raw-SQL calls currently
scattered outside `dal.py` into it. Verified by reproducing the existing
eval-report numbers exactly.

Each phase should leave the repo in a working, testable state. Do not start
a phase until the previous one's checks pass.

---

## Phase 0 — Prerequisites

1. Get the Neon connection string (`DATABASE_URL`, `postgresql://...`) from
   the user; confirm plan tier against the ~150–250MB estimate.
2. Add `psycopg[binary]>=3.1` to `backend/pyproject.toml` dependencies; `uv sync`.
3. Add `DATABASE_URL=` to `backend/.env.example`; document it in the README's
   `.env` section alongside the existing `OPENAI_API_KEY`/`MIREYE_API_KEY_*` block.

**Check:** `uv run python -c "import psycopg"` succeeds; `psycopg.connect(os.environ["DATABASE_URL"])` opens and closes cleanly against the empty Neon database.

---

## Phase 1 — Postgres schema

1. Write `backend/pluvial/memory/schema_postgres.sql`, translating
   `schema.sql` table-by-table per the spec's type-mapping table
   (`BIGSERIAL`, `JSONB`, `BOOLEAN`, `TIMESTAMPTZ`/`DATE`, `DOUBLE PRECISION`).
   Keep table and column names byte-identical to the SQLite schema — no
   renames, this phase is purely mechanical.
2. Add the two `JSONB` GIN indexes on `verdicts.cited_evidence_json` and
   `verdicts.reasoning_json`.
3. Keep `schema.sql` (SQLite) in place, untouched, for now — do not delete
   until Phase 6.

**Check:** run the new DDL against Neon by hand once; confirm all 7 tables and both indexes exist (`\dt`, `\di` or an equivalent query) and that re-running it is idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).

---

## Phase 2 — Port `dal.py`

This is the core of the port. One file, done as one unit since every
function follows the same mechanical transform.

1. `sqlite3` → `psycopg` imports; `Iterator[sqlite3.Connection]` type hints
   → `Iterator[psycopg.Connection]`.
2. `init_db()`: either drop it (schema is applied once via Phase 1's DDL
   file, not per-connection) or repoint it at `schema_postgres.sql` if
   call sites (`api/app.py`'s startup hook, tests) still expect an
   `init_db` entrypoint — check call sites before deciding; prefer keeping
   the function name so call sites don't change.
3. `connect()`: `psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)`.
   Drop `PRAGMA foreign_keys = ON` (Postgres FKs are always enforced). Drop
   `check_same_thread=False` (no Postgres equivalent needed).
4. Every function: `json.dumps(...)` on write and `json.loads(...)` on read
   for the six `*_json` columns is removed — pass/receive the dict or list
   directly, since `JSONB` round-trips natively through psycopg.
5. `soil_usable`: drop `int(soil_usable)` casts on write; pass the `bool`
   directly.
6. `cur.lastrowid` (used in `record_verdict`, `record_calibration`) has no
   psycopg3 equivalent — switch those two `INSERT` statements to
   `INSERT ... RETURNING verdict_id` / `RETURNING version` and read the
   returned id via `cur.fetchone()`.
7. Leave all query *logic* (WHERE clauses, ORDER BY, the `ON CONFLICT`
   upsert shapes) unchanged — only syntax/type touches move.

**Check:** none yet — `dal.py` has no direct tests; verified in Phase 5 once call sites also compile.

---

## Phase 3 — Pull scattered raw SQL into `dal.py`

For each of the 9 files with direct `con.execute` calls against the memory
store, extract each query into a new named `dal.py` function (following the
existing naming pattern, e.g. `dal.recent_verdicts_for_segment`,
`dal.latest_calibration_history`), then replace the call site with the new
function call. Do this file by file so each is independently reviewable:

1. `pluvial/agents/tools.py` — `dossier_lookup`'s inline verdict query → `dal.recent_verdicts(con, segment_id, limit=10)`.
2. `pluvial/agents/context.py` — check for any direct SQL (currently none observed beyond holding the connection; confirm during implementation).
3. `pluvial/agents/guidance.py` — the `calibration` history query → `dal.calibration_history(con)`.
4. `pluvial/agents/calibrator.py` — its 11 calls.
5. `pluvial/agents/reawaken.py` — its 4 calls.
6. `pluvial/mireye/wrapper.py` — its 2 calls.
7. `pluvial/eval/backtest.py` — its 8 calls.
8. `pluvial/eval/negative_control.py` — its 3 calls.
9. `pluvial/api/app.py` — its 25 calls (the biggest single file; expect several new `dal.py` functions here — e.g. `dal.queue_cards()`, `dal.segment_with_history()`).

In every extracted function, apply the same `json.dumps`/`json.loads`
removal and `dict_row` assumption as Phase 2.

**Check:** `grep -rn "\.execute(" backend/pluvial --include=*.py | grep -v pluvial/memory/dal.py | grep -v pluvial/ingest | grep -v pluvial/cli.py` returns nothing against the memory store (the `pluvial/ingest`/`cli.py` DuckDB calls are expected and excluded, per the spec's non-goals).

---

## Phase 4 — Migration script

1. Write `backend/pluvial/cli.py migrate-to-neon` (new Typer command) or
   `backend/scripts/migrate_to_neon.py`:
   - Open both connections (SQLite source, Postgres dest).
   - Apply Phase 1's DDL to Postgres if not already applied.
   - For each of the 7 tables, in dependency order (`segments` and
     `moisture_history` first, then `complaints`, then `verdicts`, then
     `outcomes`/`calibration`/`precedents`): stream rows in batches
     (~1000–5000 rows), converting `*_json` TEXT → `json.loads(...)` dict
     and `soil_usable` int → bool in flight, insert via `executemany` or
     `COPY`.
   - After each table, compare `SELECT COUNT(*)` between source and dest;
     raise loudly on mismatch.
2. Run it against the real `data/pluvial.db` (168,341 segments, 395,783
   complaints, plus verdicts/moisture/calibration/precedents/outcomes) into
   Neon.

**Check:** row counts match for all 7 tables; spot-check a handful of `verdicts` rows by `verdict_id` for JSONB field equality against the original parsed JSON.

---

## Phase 5 — Test and eval verification

1. Update `backend/tests/` fixtures/connection setup to point at a test
   Postgres database (or the same Neon instance with a scoped test
   schema/prefix — decide based on what's cheapest against Neon's free
   tier; a local Postgres via Docker for tests is also acceptable if Neon
   branching isn't set up yet) instead of an in-memory/temp SQLite file.
   No test *logic* should change — these are pure-function tests per the
   README.
2. `uv run pytest tests/ -v` — all 16 tests pass.
3. `uv run python -m pluvial.cli backtest` (n=50) against Neon; diff results
   against [`docs/eval-report-2026-08-24.md`](../../eval-report-2026-08-24.md).
   Must match exactly (78.6% precision / 30.6% recall headline, `soil_usable`
   gate at 0% false claims on NYC, memory-ablation recall halving).
4. `uv run python -m pluvial.cli` negative-control (n=10) against Neon; same check.
5. `uv run python -m pluvial.cli reawaken` against Neon; confirm the same 21
   verdicts reopen (3 flipping `monitor`/`close` → `inspect`).
6. `uv run python -m pluvial.cli serve`; smoke-test `/healthz`, `/queue`,
   `/segments/{id}`, `POST /reprofile/{id}`, `/lookup?address=` against
   live Neon data, comparing shape/values to a pre-migration run against
   SQLite (capture a baseline response for each endpoint before starting
   this phase, for the diff).

**Check:** all of the above pass with output matching the pre-migration baseline.

---

## Phase 6 — Cutover

1. Flip the default: `DATABASE_URL` becomes required (fail fast with a
   clear error if unset), replacing `DB_PATH`/`data/pluvial.db` as the
   memory store used by `cli.py`, `api/app.py`, and all agents.
2. Update README: `.env` section gains `DATABASE_URL`; setup instructions
   note Postgres/Neon replaces the local SQLite file for the memory store
   (DuckDB staging instructions unchanged).
3. Leave `data/pluvial.db` on disk (do not delete) and leave
   `schema.sql`/`init_db`'s SQLite path importable but unused, as the
   rollback artifact called out in the spec. Remove only if the user
   explicitly asks for cleanup in a later session.

**Check:** fresh clone + `uv sync` + `.env` with `DATABASE_URL` set + `uv run python -m pluvial.cli serve` works with no SQLite file present at all.

---

## Explicitly deferred (per spec's non-goals / out-of-scope)

No new tables, no connection pooling, no auth/credits/chat — those are
specs B (auth + credit wallet), C (chat orchestrator agent), D (chat UI),
E (public deployment), each requiring their own brainstorming pass before
implementation.
