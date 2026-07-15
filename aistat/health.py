"""Health / inspection snapshot of the local database.

Shows table row counts, last poll cycle, per-source sync state (including
the latest error of every failing source) and the detail-sync backlog.

CLI: python -m aistat.health [--db PATH]
HTTP: GET /health (see aistat.server)
"""

import argparse
import json
import sqlite3
from typing import Any, Dict, List, Optional

from .config import Config
from .db import connect, utcnow_iso

TABLES = [
    "runtimes",
    "agents",
    "projects",
    "issues",
    "daily_usage",
    "issue_usage",
    "runs",
    "runtime_activity",
    "poll_cycles",
]


def snapshot(conn: sqlite3.Connection, db_path: Optional[str] = None) -> Dict[str, Any]:
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES
    }

    last_cycle_row = conn.execute(
        "SELECT started_at, finished_at, sources_ok, sources_failed, notes "
        "FROM poll_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_cycle = dict(last_cycle_row) if last_cycle_row else None

    sources: List[Dict[str, Any]] = [
        dict(row)
        for row in conn.execute(
            "SELECT source, ok, last_attempt_at, last_success_at, last_error_at, last_error "
            "FROM sync_state ORDER BY source"
        )
    ]
    failing = [s for s in sources if not s["ok"]]

    usage_span = conn.execute(
        "SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM daily_usage"
    ).fetchone()

    pending_details = conn.execute(
        "SELECT COUNT(*) FROM issues "
        "WHERE details_synced_for IS NULL OR details_synced_for != updated_at"
    ).fetchone()[0]

    return {
        "generated_at": utcnow_iso(),
        "db_path": db_path,
        "status": "degraded" if failing else ("empty" if not last_cycle else "ok"),
        "row_counts": counts,
        "last_cycle": last_cycle,
        "daily_usage_span": {
            "first_date": usage_span[0],
            "last_date": usage_span[1],
            "distinct_days": usage_span[2],
        },
        "issues_pending_details": pending_details,
        "failing_sources": failing,
        "sources": sources,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AIStat health snapshot")
    parser.add_argument("--db", default=None, help="SQLite database path")
    args = parser.parse_args(argv)

    config = Config()
    db_path = args.db or str(config.db_path)
    conn = connect(db_path)
    try:
        print(json.dumps(snapshot(conn, db_path=db_path), indent=2, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
