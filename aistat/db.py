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

SCHEMA_VERSION = 9

# Serving contract for hosted tenant databases (FAN-1734). The run-attributed
# aggregates introduced with schema v5 physically require ``runs.model``, so
# uploads older than v5 (e.g. a valid v4 snapshot) or unknown future versions
# must be rejected before they can reach aggregate SQL. Schemas v6 (FAN-3306),
# v7 (FAN-3349), and v8 (FAN-3454) only add flow-metrics data, so a v5 snapshot stays
# fully servable: every pre-existing aggregate works unchanged and the flow
# endpoint truthfully reports "no data" instead of failing. Snapshot
# admission, owner migration admission and both WSGI serving surfaces all
# consult this single definition via :func:`schema_admission_error` so the
# surfaces cannot drift. The public host never mutates authenticated snapshot
# bytes; an inadmissible database becomes servable only by running
# :func:`init_db` on the writable source and re-publishing.
MIN_SERVABLE_SCHEMA_VERSION = 5
REQUIRED_SERVABLE_COLUMNS = {"runs": ("model",)}

# Multica's run payload does not carry a model snapshot.  The one documented
# Claude transition must therefore be applied both when upgrading stored runs
# and when a historical run is first received after the transition.  Keep the
# constants here beside the schema migration so the two paths cannot drift.
OPUS_TRANSITION_AGENT_ID = "e2e1c89f-587d-4a2d-bbaa-ce9b5dea908d"
OPUS_TRANSITION_AT = "2026-07-24T21:31:46Z"
OPUS_4_8_MODEL = "claude-opus-4-8"
OPUS_5_MODEL = "claude-opus-5"

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
    -- Flow-metrics fields (FAN-3306), sourced from Multica issue metadata at
    -- ingest. dispatch_lane/dispatch_ready describe routing; the qa_* fields
    -- mirror the durable QA verdict a QA card carries once review finished
    -- (qa_candidate is the immutable candidate SHA or artifact revision the
    -- verdict applies to, qa_for_issue_id links back to the implementation
    -- issue). All are NULL/0 for issues that never carried the metadata.
    dispatch_lane      TEXT,
    dispatch_ready     INTEGER NOT NULL DEFAULT 0,
    qa_verdict         TEXT,
    qa_verdict_at      TEXT,
    qa_candidate       TEXT,
    qa_for_issue_id    TEXT,
    -- Versioned control-plane provenance. These values arrive from issue
    -- metadata and are copied into a first-seen run attribution event; they
    -- never repair or relabel older raw runs.
    attribution_schema_version INTEGER,
    model_revision     TEXT,
    runtime_revision   TEXT,
    prompt_revision    TEXT,
    skills_revision    TEXT,
    harness_revision   TEXT,
    governance_bundle_revision TEXT,
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
    rate_effective_from TEXT,
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
    -- Immutable model snapshot for historical run-level metrics.  Multica
    -- currently omits this field from run payloads, so it is set at ingest.
    model          TEXT,
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

-- Observed issue status transitions (FAN-3306). One row per (issue, status,
-- observation time), written by store.upsert_issues when a freshly synced
-- status differs from the stored one. `initial` marks the first observation
-- of an issue (a collection baseline, not a real transition): cycle-time
-- aggregation only trusts an initial in_progress row when the issue was
-- created after collection began, so pre-existing history is reported as
-- uncovered instead of being backdated to the first sync.
CREATE TABLE IF NOT EXISTS issue_status_events (
    issue_id     TEXT NOT NULL,
    status       TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    initial      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (issue_id, status, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_status_events_status
    ON issue_status_events(status, observed_at);

-- Durable fleet capacity snapshots (FAN-3306), one row per successful poll
-- cycle. `starved_idle` counts eligible delivery agents that were idle while
-- no lane-compatible dispatch_ready card existed — the idle-fleet condition,
-- resolved at snapshot time from live agents/runs/issues. `paused` records a
-- manual workspace pause when one is observable (none is today; the column
-- keeps historical rows honest if a pause signal appears later). The old
-- ``done -> next in_progress`` approximation is deliberately not derivable
-- from this table: intervals without snapshots stay uncovered.
CREATE TABLE IF NOT EXISTS fleet_snapshots (
    at            TEXT PRIMARY KEY,
    eligible      INTEGER NOT NULL,
    idle          INTEGER NOT NULL,
    starved_idle  INTEGER NOT NULL,
    ready_cards   INTEGER NOT NULL,
    paused        INTEGER NOT NULL DEFAULT 0,
    -- Whether `paused` came from the authoritative workspace observation.
    -- A missing observation is not evidence of an active workspace.
    pause_observed INTEGER NOT NULL DEFAULT 0
);

-- Per-lane breakdown of each fleet snapshot (agents attributed to their
-- native lane; ready_cards counted by the card's dispatch_lane).
CREATE TABLE IF NOT EXISTS fleet_snapshot_lanes (
    at            TEXT NOT NULL,
    lane          TEXT NOT NULL,
    eligible      INTEGER NOT NULL,
    idle          INTEGER NOT NULL,
    starved_idle  INTEGER NOT NULL,
    ready_cards   INTEGER NOT NULL,
    PRIMARY KEY (at, lane)
);

-- Immutable provenance captured when a run is first observed. Existing runs
-- are backfilled only as legacy_unknown so the original run rows remain raw.
CREATE TABLE IF NOT EXISTS run_attribution_events (
    run_id                       TEXT PRIMARY KEY,
    issue_id                     TEXT,
    attribution_schema_version   INTEGER,
    provenance_state             TEXT NOT NULL,
    model_revision               TEXT,
    runtime_revision             TEXT,
    prompt_revision              TEXT,
    skills_revision              TEXT,
    harness_revision             TEXT,
    governance_bundle_revision  TEXT,
    observed_at                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_attribution_issue
    ON run_attribution_events(issue_id);

-- A readiness baseline is explicitly marked initial when v8 first sees a
-- card that was already ready. Only later observed transitions measure PM
-- preparation and current waiting time.
CREATE TABLE IF NOT EXISTS issue_readiness_events (
    issue_id     TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    initial      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (issue_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_readiness_events_observed
    ON issue_readiness_events(observed_at);

-- One immutable terminal QA observation per QA issue. A row first seen after
-- collection but already terminal is marked initial by the collector.
CREATE TABLE IF NOT EXISTS qa_lineage_events (
    qa_issue_id            TEXT PRIMARY KEY,
    implementation_issue_id TEXT NOT NULL,
    candidate              TEXT NOT NULL,
    verdict                TEXT NOT NULL,
    verdict_at             TEXT,
    observed_at            TEXT NOT NULL,
    initial                INTEGER NOT NULL DEFAULT 0,
    accepted_candidate     TEXT,
    accepted_story_points  REAL
);
CREATE INDEX IF NOT EXISTS idx_qa_lineage_impl
    ON qa_lineage_events(implementation_issue_id);

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

-- Append-only price revisions.  The first observed rate for a model/date is
-- retained so a later catalog publication cannot rewrite closed history.
CREATE TABLE IF NOT EXISTS model_price_history (
    model                TEXT NOT NULL,
    effective_from       TEXT NOT NULL,
    vendor               TEXT,
    currency             TEXT,
    input_rate           REAL,
    output_rate          REAL,
    cache_read_rate      REAL,
    cache_write_rate     REAL,
    unpriced             INTEGER NOT NULL DEFAULT 0,
    source_url           TEXT,
    captured_at          TEXT,
    loaded_at            TEXT NOT NULL,
    PRIMARY KEY (model, effective_from)
);

-- Sanitized billing reconciliation totals only: no provider export or invoice
-- payload is stored in the application database.
CREATE TABLE IF NOT EXISTS billing_reconciliation (
    provider               TEXT NOT NULL,
    period                 TEXT NOT NULL,
    calculated_usd         REAL NOT NULL,
    actual_usd             REAL NOT NULL,
    variance_ratio         REAL NOT NULL,
    over_threshold         INTEGER NOT NULL,
    diagnostic_emitted_at  TEXT,
    PRIMARY KEY (provider, period)
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
        ("rate_effective_from", "TEXT"),
    ],
    "issues": [
        ("is_jira", "INTEGER NOT NULL DEFAULT 0"),
        ("jira_key", "TEXT"),
        ("dispatch_lane", "TEXT"),
        ("dispatch_ready", "INTEGER NOT NULL DEFAULT 0"),
        ("qa_verdict", "TEXT"),
        ("qa_verdict_at", "TEXT"),
        ("qa_candidate", "TEXT"),
        ("qa_for_issue_id", "TEXT"),
        ("attribution_schema_version", "INTEGER"),
        ("model_revision", "TEXT"),
        ("runtime_revision", "TEXT"),
        ("prompt_revision", "TEXT"),
        ("skills_revision", "TEXT"),
        ("harness_revision", "TEXT"),
        ("governance_bundle_revision", "TEXT"),
    ],
    "runs": [
        ("model", "TEXT"),
    ],
    "fleet_snapshots": [
        ("pause_observed", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def model_snapshot_for_run(agent_id, started_at, dispatched_at, created_at,
                           fallback_model):
    """Return the durable model identity for one Multica run.

    The named agent is the only historical correction with evidence beyond the
    current agent catalog: its first confirmed Opus 5 run started at
    ``OPUS_TRANSITION_AT``.  A missing timestamp remains conservatively Opus
    4.8, rather than allowing today's agent model to relabel unknown history.
    Every other run uses the model observed while it is ingested.
    """
    if agent_id != OPUS_TRANSITION_AGENT_ID:
        return fallback_model
    event_at = started_at or dispatched_at or created_at
    if event_at is not None and event_at >= OPUS_TRANSITION_AT:
        return OPUS_5_MODEL
    return OPUS_4_8_MODEL


def _backfill_run_models(conn: sqlite3.Connection) -> None:
    """Fill legacy ``runs.model`` once without changing existing snapshots."""
    # Apply the evidence-backed cutoff before consulting the current catalog:
    # this agent has both models on 2026-07-24, so a calendar-day migration
    # would merge two real identities.
    conn.execute(
        """
        UPDATE runs
        SET model = CASE
            WHEN COALESCE(started_at, dispatched_at, created_at) >= ? THEN ?
            ELSE ?
        END
        WHERE model IS NULL AND agent_id = ?
        """,
        (OPUS_TRANSITION_AT, OPUS_5_MODEL, OPUS_4_8_MODEL,
         OPUS_TRANSITION_AGENT_ID),
    )
    # The generic legacy case has no transition evidence.  Copy the current
    # catalog once, but only when it actually supplies a non-null model.
    conn.execute(
        """
        UPDATE runs
        SET model = (SELECT a.model FROM agents a WHERE a.id = runs.agent_id)
        WHERE model IS NULL
          AND agent_id IS NOT NULL
          AND agent_id != ?
          AND EXISTS (
              SELECT 1 FROM agents a
              WHERE a.id = runs.agent_id AND a.model IS NOT NULL
          )
        """,
        (OPUS_TRANSITION_AGENT_ID,),
    )


def _backfill_legacy_attribution(conn: sqlite3.Connection) -> None:
    """Mark pre-v8 raw rows unknown without altering their source columns."""
    conn.execute(
        """
        INSERT OR IGNORE INTO run_attribution_events
            (run_id, issue_id, provenance_state, observed_at)
        SELECT id, issue_id, 'legacy_unknown', ? FROM runs
        """,
        (utcnow_iso(),),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO issue_readiness_events
            (issue_id, observed_at, initial)
        SELECT i.id, ?, 1 FROM issues i
        WHERE i.dispatch_ready = 1
          AND NOT EXISTS (
              SELECT 1 FROM issue_readiness_events e WHERE e.issue_id = i.id
          )
        """,
        (utcnow_iso(),),
    )


def schema_admission_error(conn: sqlite3.Connection):
    """Why ``conn``'s database may not be served, or ``None`` when it may.

    Fail-closed: any schema version outside the servable range — older valid
    snapshots such as v4 as well as unknown future versions — or a physically
    missing required column disqualifies the database before any aggregate SQL
    can touch it. Works on a query-only connection; never mutates the database.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < MIN_SERVABLE_SCHEMA_VERSION or version > SCHEMA_VERSION:
        return "unsupported schema version {}; server requires {}-{}".format(
            version, MIN_SERVABLE_SCHEMA_VERSION, SCHEMA_VERSION
        )
    for table in sorted(REQUIRED_SERVABLE_COLUMNS):
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info({})".format(table))
        }
        missing = [
            name
            for name in REQUIRED_SERVABLE_COLUMNS[table]
            if name not in existing
        ]
        if missing:
            return "table {} is missing required columns: {}".format(
                table, ", ".join(missing)
            )
    return None


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
    _backfill_run_models(conn)
    _backfill_legacy_attribution(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
