"""Idempotent upserts of normalized rows into SQLite."""

import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .db import utcnow_iso


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
    """Upsert issues while preserving the details_synced_* bookkeeping."""
    synced_at = synced_at or utcnow_iso()
    count = 0
    for row in rows:
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
    synced_at = synced_at or utcnow_iso()
    count = 0
    for row in rows:
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
                              issue_updated_at: str,
                              synced_at: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE issues SET details_synced_at = ?, details_synced_for = ? WHERE id = ?",
        (synced_at or utcnow_iso(), issue_updated_at, issue_id),
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
