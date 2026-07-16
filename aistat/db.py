"""SQLite schema and connection helpers.

Dimensions: runtimes, agents, projects, issues.
Facts: daily_usage (runtime_id, model, date), issue_usage snapshots,
runs (Multica tasks), runtime_activity (hour-of-day snapshot).
Bookkeeping: sync_state (per-source health), poll_cycles, sync_beats
(single-row change counter driving SSE live updates).

All writes are idempotent upserts keyed on natural primary keys, so the
poller can run any number of times without producing duplicate rows.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS runtimes (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    provider      TEXT,
    status        TEXT,
    device_info   TEXT,
    last_seen_at  TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    synced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    model         TEXT,
    runtime_id    TEXT,
    description   TEXT,
    archived_at   TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    synced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    description   TEXT,
    status        TEXT,
    priority      TEXT,
    issue_count   INTEGER,
    done_count    INTEGER,
    created_at    TEXT,
    updated_at    TEXT,
    synced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id                 TEXT PRIMARY KEY,
    identifier         TEXT,
    number             INTEGER,
    title              TEXT,
    status             TEXT,
    priority           TEXT,
    project_id         TEXT,
    parent_issue_id    TEXT,
    stage              INTEGER,
    assignee_id        TEXT,
    assignee_type      TEXT,
    story_points       REAL,
    estimation_model   TEXT,
    -- Jira provenance: issues imported from the legacy Jira archive carry
    -- jira_* / historical_import markers in their Multica metadata. They were
    -- never executed in Multica (no runs, no usage), so they are excluded from
    -- token/story-point/efficiency statistics. is_jira flags such an issue;
    -- jira_key keeps the original Jira key (e.g. SCRUM-1078) for reference.
    is_jira            INTEGER NOT NULL DEFAULT 0,
    jira_key           TEXT,
    created_at         TEXT,
    updated_at         TEXT,
    synced_at          TEXT NOT NULL,
    -- Details (issue usage + runs) are fetched separately with a per-cycle
    -- budget. `details_synced_for` stores the staleness key at fetch time:
    -- the issue's `updated_at`, extended with the latest run activity
    -- timestamp once the issue has runs (see poller.DETAIL_KEY_EXPR).
    -- Details are refetched when the recomputed key diverges. Comparing
    -- server timestamps to server timestamps avoids local clock skew.
    details_synced_at  TEXT,
    details_synced_for TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project_id);
CREATE INDEX IF NOT EXISTS idx_issues_details_pending
    ON issues(updated_at) WHERE details_synced_for IS NULL;

CREATE TABLE IF NOT EXISTS daily_usage (
    runtime_id          TEXT NOT NULL,
    model               TEXT NOT NULL,
    date                TEXT NOT NULL,
    provider            TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    synced_at           TEXT NOT NULL,
    -- Cost is (re)computed from token counts x model_pricing (stage 2).
    -- cost_usd/cost_credits are NULL when the model is unpriced (never 0),
    -- and cost_priced flags whether an official rate was applied.
    cost_usd            REAL,
    cost_credits        REAL,
    cost_priced         INTEGER NOT NULL DEFAULT 0,
    cost_computed_at    TEXT,
    PRIMARY KEY (runtime_id, model, date)
);

CREATE TABLE IF NOT EXISTS issue_usage (
    issue_id                  TEXT PRIMARY KEY,
    task_count                INTEGER NOT NULL DEFAULT 0,
    total_input_tokens        INTEGER NOT NULL DEFAULT 0,
    total_output_tokens       INTEGER NOT NULL DEFAULT 0,
    total_cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    total_cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    synced_at                 TEXT NOT NULL
);

-- Multica tasks, as returned by `issue runs` and `agent tasks` (same objects).
CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    issue_id       TEXT,
    agent_id       TEXT,
    runtime_id     TEXT,
    kind           TEXT,
    status         TEXT,
    attempt        INTEGER,
    error          TEXT,
    created_at     TEXT,
    dispatched_at  TEXT,
    started_at     TEXT,
    completed_at   TEXT,
    synced_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_issue ON runs(issue_id);
CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_id);

-- Hour-of-day task counts; a rolling snapshot replaced on every cycle.
CREATE TABLE IF NOT EXISTS runtime_activity (
    runtime_id  TEXT NOT NULL,
    hour        INTEGER NOT NULL,
    count       INTEGER NOT NULL,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (runtime_id, hour)
);

-- One row per data source; updated on every attempt. Errors are kept until
-- the next successful attempt of the same source so health always shows the
-- latest failure.
CREATE TABLE IF NOT EXISTS sync_state (
    source           TEXT PRIMARY KEY,
    ok               INTEGER,
    last_attempt_at  TEXT,
    last_success_at  TEXT,
    last_error_at    TEXT,
    last_error       TEXT
);

-- Single-row change counter bumped whenever the poller commits a batch of
-- fresh data (after the live phase and after a full cycle). The SSE stream
-- watches `seq`, so clients refresh as soon as live data lands rather than
-- waiting for the whole cycle.
CREATE TABLE IF NOT EXISTS sync_beats (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    seq    INTEGER NOT NULL,
    at     TEXT NOT NULL,
    phase  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    sources_ok      INTEGER NOT NULL DEFAULT 0,
    sources_failed  INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

-- Official per-1M-token rates, loaded from pricing.json (+ optional override).
-- Rates are NULL for an unpriced model; source_url/captured_at record where
-- and when each rate was taken from the vendor's official pricing page.
CREATE TABLE IF NOT EXISTS model_pricing (
    model                TEXT PRIMARY KEY,
    vendor               TEXT,
    currency             TEXT,
    input_rate           REAL,
    output_rate          REAL,
    cache_read_rate      REAL,
    cache_write_rate     REAL,
    cache_write_1h_rate  REAL,
    unpriced             INTEGER NOT NULL DEFAULT 0,
    source_url           TEXT,
    captured_at          TEXT,
    notes                TEXT,
    loaded_at            TEXT NOT NULL
);
"""

# Columns added to pre-existing tables after their first release. init_db adds
# any that are missing so an already-populated database upgrades in place
# (CREATE TABLE IF NOT EXISTS never alters an existing table).
_ADDED_COLUMNS = {
    "daily_usage": [
        ("cost_usd", "REAL"),
        ("cost_credits", "REAL"),
        ("cost_priced", "INTEGER NOT NULL DEFAULT 0"),
        ("cost_computed_at", "TEXT"),
    ],
    "issues": [
        ("is_jira", "INTEGER NOT NULL DEFAULT 0"),
        ("jira_key", "TEXT"),
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def utcnow_iso() -> str:
    """Current UTC time in the same second-precision Z format Multica uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect_readonly(db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a query-only connection suitable for the hosted snapshot."""
    path = Path(db_path).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
