"""Idempotent upserts of normalized rows into SQLite."""

import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .db import model_snapshot_for_run, utcnow_iso


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    key_columns: List[str],
    row: Dict[str, Any],
    synced_at: str,
) -> None:
    row = dict(row)
    row["synced_at"] = synced_at
    columns = list(row.keys())
    placeholders = ", ".join(["?"] * len(columns))
    updates = ", ".join(
        f"{col} = excluded.{col}" for col in columns if col not in key_columns
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(key_columns)}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[col] for col in columns])


def upsert_runtimes(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]],
                    synced_at: Optional[str] = None) -> int:
    synced_at = synced_at or utcnow_iso()
    count = 0
    for row in rows:
        _upsert(conn, "runtimes", ["id"], row, synced_at)
        count += 1
    return count


def upsert_agents(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]],
                  synced_at: Optional[str] = None) -> int:
    synced_at = synced_at or utcnow_iso()
    count = 0
    for row in rows:
        _upsert(conn, "agents", ["id"], row, synced_at)
        count += 1
    return count


def upsert_projects(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]],
                    synced_at: Optional[str] = None) -> int:
    synced_at = synced_at or utcnow_iso()
    count = 0
    for row in rows:
        _upsert(conn, "projects", ["id"], row, synced_at)
        count += 1
    return count


def upsert_issues(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]],
                  synced_at: Optional[str] = None) -> int:
    """Upsert issues while preserving the details_synced_* bookkeeping.

    Also records issue_status_events (FAN-3306): the first observation of an
    issue is written with initial=1 (a collection baseline, not a real
    transition), and every subsequently observed status change with initial=0.
    Re-upserting an unchanged status writes nothing, so repeated cycles stay
    idempotent.
    """
    synced_at = synced_at or utcnow_iso()
    count = 0
    for row in rows:
        status = row.get("status")
        if status:
            existing = conn.execute(
                "SELECT status FROM issues WHERE id = ?", (row["id"],)
            ).fetchone()
            if existing is None or existing["status"] != status:
                conn.execute(
                    "INSERT OR IGNORE INTO issue_status_events "
                    "(issue_id, status, observed_at, initial) VALUES (?, ?, ?, ?)",
                    (row["id"], status, synced_at, 1 if existing is None else 0),
                )
        _upsert(conn, "issues", ["id"], row, synced_at)
        count += 1
    return count


def upsert_daily_usage(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]],
                       synced_at: Optional[str] = None) -> int:
    synced_at = synced_at or utcnow_iso()
    count = 0
    for row in rows:
        _upsert(conn, "daily_usage", ["runtime_id", "model", "date"], row, synced_at)
        count += 1
    return count


def upsert_issue_usage(conn: sqlite3.Connection, row: Dict[str, Any],
                       synced_at: Optional[str] = None) -> None:
    _upsert(conn, "issue_usage", ["issue_id"], row, synced_at or utcnow_iso())


def upsert_runs(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]],
                synced_at: Optional[str] = None) -> int:
    """Upsert runs while preserving their first recorded model snapshot.

    A later agent-catalog refresh must not rewrite an old run after that agent
    changes model.  When Multica supplies no model (its current contract), use
    the contemporaneous catalog, including the documented Opus transition.
    """
    synced_at = synced_at or utcnow_iso()
    count = 0
    for row in rows:
        row = dict(row)
        existing = conn.execute(
            "SELECT model FROM runs WHERE id = ?", (row["id"],)
        ).fetchone()
        if existing is not None and existing["model"] is not None:
            # Stored snapshots are immutable, even if a later payload happens
            # to include a different current model.
            row["model"] = existing["model"]
        elif row.get("model") is None:
            agent_model = None
            agent_id = row.get("agent_id")
            if agent_id:
                agent = conn.execute(
                    "SELECT model FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                agent_model = agent["model"] if agent is not None else None
            row["model"] = model_snapshot_for_run(
                agent_id,
                row.get("started_at"),
                row.get("dispatched_at"),
                row.get("created_at"),
                agent_model,
            )
        _upsert(conn, "runs", ["id"], row, synced_at)
        count += 1
    return count


def replace_runtime_activity(conn: sqlite3.Connection, runtime_id: str,
                             rows: Iterable[Dict[str, Any]],
                             synced_at: Optional[str] = None) -> int:
    """The activity endpoint returns a rolling hour-of-day snapshot, so the
    previous snapshot for the runtime is replaced wholesale."""
    synced_at = synced_at or utcnow_iso()
    conn.execute("DELETE FROM runtime_activity WHERE runtime_id = ?", (runtime_id,))
    count = 0
    for row in rows:
        _upsert(conn, "runtime_activity", ["runtime_id", "hour"], row, synced_at)
        count += 1
    return count


def mark_issue_details_synced(conn: sqlite3.Connection, issue_id: str,
                              detail_key: str,
                              synced_at: Optional[str] = None) -> None:
    """Record the staleness key (issue updated_at [+ last run activity])
    the details were fetched for; see poller.DETAIL_KEY_EXPR."""
    conn.execute(
        "UPDATE issues SET details_synced_at = ?, details_synced_for = ? WHERE id = ?",
        (synced_at or utcnow_iso(), detail_key, issue_id),
    )


def parse_dispatch_profile(description: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse the machine-readable dispatch profile an agent description carries.

    Delivery agents in this workspace describe themselves as
    ``dispatch_profile=...; role=implementation; native_lane=dev_high;
    borrow_lanes=dev_medium,dev_low; ...``. Returns {"role", "native_lane",
    "lanes"} (lanes = native + borrow set) for an eligible delivery agent
    (role implementation/qa/devops with a native lane), else None — RETIRED
    and non-delivery agents carry no dispatch_profile and are not part of the
    fleet.
    """
    if not description or "dispatch_profile=" not in description:
        return None
    fields = {}
    for part in description.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            fields[key.strip()] = value.strip()
    role = fields.get("role")
    native_lane = fields.get("native_lane")
    if role not in ("implementation", "qa", "devops") or not native_lane:
        return None
    lanes = {native_lane}
    for lane in (fields.get("borrow_lanes") or "").split(","):
        lane = lane.strip()
        if lane and lane != "none":
            lanes.add(lane)
    return {"role": role, "native_lane": native_lane, "lanes": lanes}


# Non-terminal run statuses: an agent with such a run is busy right now.
ACTIVE_RUN_STATUSES = ("pending", "dispatched", "running")

# Issue statuses under which a dispatch_ready card is still waiting for an
# executor (mirrors the dispatcher's queue: once dispatched it is in_progress).
READY_CARD_STATUSES = ("todo", "backlog")


def record_fleet_snapshot(conn: sqlite3.Connection, at: Optional[str] = None,
                          paused: Optional[bool] = None) -> Dict[str, int]:
    """Record one durable fleet capacity snapshot (FAN-3306).

    Resolved entirely from freshly synced local tables (agents, runs, issues)
    — the caller is responsible for only invoking this when those inputs
    synced successfully this cycle, so a degraded cycle never fabricates
    capacity state. An idle agent is *starved* when no dispatch_ready card in
    a lane it serves (native + borrow) exists; ready cards without a
    dispatch_lane are counted but match no agent.
    """
    at = at or utcnow_iso()
    busy_ids = {
        row["agent_id"] for row in conn.execute(
            "SELECT DISTINCT agent_id FROM runs WHERE status IN (?, ?, ?) "
            "AND agent_id IS NOT NULL", ACTIVE_RUN_STATUSES,
        )
    }
    ready_by_lane: Dict[str, int] = {}
    for row in conn.execute(
        "SELECT COALESCE(dispatch_lane, 'unknown') AS lane, COUNT(*) AS n "
        "FROM issues WHERE dispatch_ready = 1 AND is_jira = 0 "
        "AND status IN (?, ?) GROUP BY lane", READY_CARD_STATUSES,
    ):
        ready_by_lane[row["lane"]] = row["n"]

    lane_rollup: Dict[str, Dict[str, int]] = {}

    def lane_bucket(lane: str) -> Dict[str, int]:
        if lane not in lane_rollup:
            lane_rollup[lane] = {
                "eligible": 0, "idle": 0, "starved_idle": 0, "ready_cards": 0,
            }
        return lane_rollup[lane]

    eligible = idle = starved = 0
    for row in conn.execute(
        "SELECT id, description FROM agents WHERE archived_at IS NULL"
    ):
        profile = parse_dispatch_profile(row["description"])
        if profile is None:
            continue
        eligible += 1
        bucket = lane_bucket(profile["native_lane"])
        bucket["eligible"] += 1
        if row["id"] in busy_ids:
            continue
        idle += 1
        bucket["idle"] += 1
        if not any(ready_by_lane.get(lane) for lane in profile["lanes"]):
            starved += 1
            bucket["starved_idle"] += 1

    ready_total = sum(ready_by_lane.values())
    for lane, n in ready_by_lane.items():
        lane_bucket(lane)["ready_cards"] = n

    conn.execute(
        "INSERT OR REPLACE INTO fleet_snapshots "
        "(at, eligible, idle, starved_idle, ready_cards, paused, "
        "pause_observed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (at, eligible, idle, starved, ready_total, 1 if paused else 0,
         1 if paused is not None else 0),
    )
    conn.execute("DELETE FROM fleet_snapshot_lanes WHERE at = ?", (at,))
    for lane, bucket in lane_rollup.items():
        conn.execute(
            "INSERT INTO fleet_snapshot_lanes "
            "(at, lane, eligible, idle, starved_idle, ready_cards) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (at, lane, bucket["eligible"], bucket["idle"],
             bucket["starved_idle"], bucket["ready_cards"]),
        )
    return {
        "eligible": eligible, "idle": idle, "starved_idle": starved,
        "ready_cards": ready_total,
    }


def record_beat(conn: sqlite3.Connection, phase: str,
                at: Optional[str] = None) -> None:
    """Bump the single-row counter that tells the SSE stream "data changed".

    phase: 'live' after the live phase (daily usage, pricing, dimensions),
    'cycle' after a full poll cycle.
    """
    conn.execute(
        """
        INSERT INTO sync_beats (id, seq, at, phase) VALUES (1, 1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            seq = sync_beats.seq + 1, at = excluded.at, phase = excluded.phase
        """,
        (at or utcnow_iso(), phase),
    )


def record_source_attempt(conn: sqlite3.Connection, source: str, ok: bool,
                          error: Optional[str] = None,
                          at: Optional[str] = None) -> None:
    at = at or utcnow_iso()
    if ok:
        conn.execute(
            """
            INSERT INTO sync_state (source, ok, last_attempt_at, last_success_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                ok = 1, last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at
            """,
            (source, at, at),
        )
    else:
        conn.execute(
            """
            INSERT INTO sync_state (source, ok, last_attempt_at, last_error_at, last_error)
            VALUES (?, 0, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                ok = 0, last_attempt_at = excluded.last_attempt_at,
                last_error_at = excluded.last_error_at,
                last_error = excluded.last_error
            """,
            (source, at, at, error or "unknown error"),
        )
