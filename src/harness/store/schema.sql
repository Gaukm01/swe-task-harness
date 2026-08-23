-- swe-task-harness storage schema.
--
-- Applied idempotently on every connection: plain CREATE ... IF NOT EXISTS, no
-- migration framework. Six tables, inspectable with `sqlite3 harness.db`.
--
-- All timestamps are ISO-8601 UTC strings. Ids are ULIDs, so ORDER BY on an id
-- column is chronological and no separate sequence is needed.

PRAGMA foreign_keys = ON;

-- Every CLI call, written BEFORE any work begins and updated on exit, so a
-- crash or a kill -9 still leaves a row showing what was attempted.
CREATE TABLE IF NOT EXISTS invocations (
    id            TEXT PRIMARY KEY,
    argv          TEXT NOT NULL,           -- JSON array, as received
    cwd           TEXT NOT NULL,
    harness_version TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,                    -- NULL means the process never finished
    exit_code     INTEGER                  -- NULL means the same
);

CREATE INDEX IF NOT EXISTS idx_invocations_started ON invocations (started_at DESC);

-- One row per task_id seen. bundle_digest is the most recent one observed;
-- the immutable per-run digest lives on the run.
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    bundle_path   TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    language      TEXT NOT NULL,
    framework     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    invocation_id TEXT REFERENCES invocations (id),
    task_id       TEXT NOT NULL REFERENCES tasks (task_id),
    bundle_digest TEXT NOT NULL,
    image_digest  TEXT,
    cache_key     TEXT,
    solver_kind   TEXT NOT NULL,
    solver_model  TEXT,
    phase_reached TEXT NOT NULL,           -- base | guarded | gold | solve | scored
    outcome       TEXT,                    -- resolved | resolved_suspect | unresolved | inconclusive
    gaming_flags  TEXT NOT NULL DEFAULT '[]',   -- JSON array
    turns         INTEGER,
    cost_usd      REAL,
    timings_ms    TEXT NOT NULL DEFAULT '{}',   -- JSON object
    started_at    TEXT NOT NULL,
    ended_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs (task_id, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_outcome ON runs (outcome);

-- One row per (run, phase, test). The same test appears twice for a graded run:
-- once from the baseline attempt and once from the post-solution attempt.
CREATE TABLE IF NOT EXISTS test_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    phase         TEXT NOT NULL,
    test_id       TEXT NOT NULL,
    bucket        TEXT NOT NULL,           -- f2p | p2p
    status        TEXT NOT NULL,           -- passed | failed | error | collection_error
                                           -- | not_found | timeout | infra_error
    duration_ms   INTEGER,
    message       TEXT,
    UNIQUE (run_id, phase, test_id)
);

CREATE INDEX IF NOT EXISTS idx_test_results_run ON test_results (run_id, phase);

-- Agent turns and notable harness events, in order.
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT REFERENCES runs (run_id) ON DELETE CASCADE,
    invocation_id TEXT REFERENCES invocations (id),
    seq           INTEGER NOT NULL,
    at            TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- tool_call | tool_result | refusal | phase | error
    payload       TEXT NOT NULL DEFAULT '{}'    -- JSON object, results truncated
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, seq);

CREATE TABLE IF NOT EXISTS artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,           -- solution_diff | junit | report_json | report_html | cassette
    path          TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (run_id, kind, path)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts (run_id);
