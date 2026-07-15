"""SQLite schema and connection helpers.

Dimensions: runtimes, agents, projects, issues.
Facts: daily_usage (runtime_id, model, date), issue_usage snapshots,
runs (Multica tasks), runtime_activity (hour-of-day snapshot).
Bookkeeping: sync_state (per-source health), poll_cycles.

All writes are idempotent upserts keyed on natural primary keys, so the
poller can run any number of times without producing duplicate rows.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

SCHEMA_VERSION = 1

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
    created_at         TEXT,
    updated_at         TEXT,
    synced_at          TEXT NOT NULL,
    -- Details (issue usage + runs) are fetched separately with a per-cycle
    -- budget. `details_synced_for` echoes the issue's `updated_at` at fetch
    -- time; details are refetched when the two diverge. Comparing server
    -- timestamps to server timestamps avoids local clock skew.
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

CREATE TABLE IF NOT EXISTS poll_cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    sources_ok      INTEGER NOT NULL DEFAULT 0,
    sources_failed  INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);
"""


def utcnow_iso() -> str:
    """Current UTC time in the same second-precision Z format Multica uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
