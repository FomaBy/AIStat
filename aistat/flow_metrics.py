"""Flow metrics aggregates (FAN-3306): cycle time, rework rate, idle fleet.

Read-only aggregates over the durable flow rows the poller collects:
``issue_status_events`` (observed status transitions), the ``qa_*`` issue
columns (terminal QA verdicts mirrored from Multica metadata) and
``fleet_snapshots``/``fleet_snapshot_lanes`` (per-cycle capacity snapshots).

Truthfulness contract: every value is computed only from rows the collection
actually observed. History that predates collection is reported through
coverage counters and start timestamps — never estimated, interpolated or
backdated. A database without the flow schema (a legacy v5 tenant snapshot)
yields ``schema_supported: false`` with empty coverage, not an error.

Deterministic edge behavior (also documented in docs/flow-metrics.md):

- Cycle time = first trusted ``in_progress`` observation to the FIRST ``done``
  observation; a card reopened after done keeps its first done. An ``initial``
  in_progress event (collection baseline) is trusted only when the issue was
  created after collection began. Cancelled cards and still-open (censored)
  cards are excluded from percentiles but counted in coverage.
- Rework: a candidate is one distinct (implementation issue, candidate SHA or
  artifact revision) pair; QA retries of the same candidate count once. A
  candidate is reworked when it has terminal FAILED/INCONCLUSIVE verdicts and
  never a PASSED. Candidates whose verdict carries no ``qa_verdict_at`` cannot
  be placed in a window and are reported as ``unwindowed``.
- Idle fleet: consecutive snapshot pairs closer than ``SNAPSHOT_GAP_SECONDS``
  form observed intervals; wider pairs are coverage gaps. Paused or
  pause-unavailable intervals are excluded from both numerator and denominator.
  The interval counts as idle
  when at least one eligible delivery agent was idle with no lane-compatible
  ``dispatch_ready`` card (``starved_idle > 0``). Agents are workspace-scoped,
  so a project filter does not narrow this metric (``workspace_wide: true``).
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

WINDOW_DAYS = (7, 30, 90)
REWORK_VERDICTS = ("FAILED", "INCONCLUSIVE")
UNKNOWN_LANE = "unknown"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Consecutive snapshots further apart than this are a collection gap (poller
# down), not an observed interval. 900 s = 20x the default 45 s poll cadence.
SNAPSHOT_GAP_SECONDS = 900


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse Multica's UTC second-precision timestamp without Python 3.7+."""
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1]
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _fmt_ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _has_columns(conn: sqlite3.Connection, table: str,
                 columns: Sequence[str]) -> bool:
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info({})".format(table))
    }
    return all(column in existing for column in columns)


def _median(values: List[float]) -> Optional[float]:
    """Standard median over a sorted list (mean of middle pair when even)."""
    n = len(values)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _p90_nearest_rank(values: List[float]) -> Optional[float]:
    """Nearest-rank 90th percentile over a sorted list (1-indexed ceil)."""
    n = len(values)
    if n == 0:
        return None
    rank = -(-9 * n // 10)  # ceil(0.9 * n) without float error
    return values[rank - 1]


def _in_clause(column: str, values: Sequence[str],
               params: List[Any]) -> str:
    if not values:
        return ""
    params.extend(values)
    return " AND {} IN ({})".format(column, ", ".join(["?"] * len(values)))


def validate_days(value: Any) -> int:
    """Coerce the ``days`` request parameter; raises ValueError when invalid."""
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError("days must be one of {}".format(
            ", ".join(str(d) for d in WINDOW_DAYS)))
    if days not in WINDOW_DAYS:
        raise ValueError("days must be one of {}".format(
            ", ".join(str(d) for d in WINDOW_DAYS)))
    return days


# -- cycle time ---------------------------------------------------------------


def _cycle_time(conn: sqlite3.Connection, win_from: datetime, now: datetime,
                project_ids: Sequence[str], lanes: Sequence[str],
                events_start: Optional[str]) -> Dict[str, Any]:
    empty = {
        "median_seconds": None, "p90_seconds": None, "measured": 0,
        "done_total": 0, "excluded_no_start": 0, "cancelled": 0,
        "open_censored": 0, "groups": [],
    }
    if events_start is None:
        return empty

    params = [events_start]  # type: List[Any]
    filter_sql = ""
    filter_sql += _in_clause("i.project_id", project_ids, params)
    filter_sql += _in_clause(
        "COALESCE(i.dispatch_lane, '{}')".format(UNKNOWN_LANE), lanes, params)
    rows = conn.execute(
        """
        WITH starts AS (
            SELECT e.issue_id, MIN(e.observed_at) AS started_at
            FROM issue_status_events e
            JOIN issues si ON si.id = e.issue_id
            WHERE e.status = 'in_progress'
              AND (e.initial = 0
                   OR (si.created_at IS NOT NULL AND si.created_at >= ?))
            GROUP BY e.issue_id
        ),
        firsts AS (
            SELECT issue_id, status, MIN(observed_at) AS at
            FROM issue_status_events
            WHERE status IN ('done', 'cancelled')
            GROUP BY issue_id, status
        )
        SELECT i.id, i.project_id,
               COALESCE(i.dispatch_lane, '{unknown}') AS lane,
               i.status AS current_status,
               s.started_at AS started_at,
               d.at AS done_at,
               c.at AS cancelled_at,
               CASE WHEN s.started_at IS NOT NULL AND d.at IS NOT NULL
                    THEN CAST(ROUND(
                        (julianday(d.at) - julianday(s.started_at)) * 86400.0
                    ) AS INTEGER)
               END AS duration_seconds
        FROM issues i
        LEFT JOIN starts s ON s.issue_id = i.id
        LEFT JOIN firsts d ON d.issue_id = i.id AND d.status = 'done'
        LEFT JOIN firsts c ON c.issue_id = i.id AND c.status = 'cancelled'
        WHERE i.is_jira = 0
          AND (s.started_at IS NOT NULL OR d.at IS NOT NULL){filters}
        """.format(unknown=UNKNOWN_LANE, filters=filter_sql),
        params,
    ).fetchall()

    win_from_s, now_s = _fmt_ts(win_from), _fmt_ts(now)
    durations = []  # type: List[float]
    groups = {}  # type: Dict[Tuple[Optional[str], str], List[float]]
    done_total = excluded_no_start = cancelled = censored = 0
    for row in rows:
        done_at = row["done_at"]
        if done_at is not None and win_from_s <= done_at <= now_s:
            if row["current_status"] == "cancelled":
                # done then cancelled: treat as cancelled, not delivered
                cancelled += 1
                continue
            done_total += 1
            if row["duration_seconds"] is None:
                excluded_no_start += 1
                continue
            seconds = float(max(0, row["duration_seconds"]))
            durations.append(seconds)
            groups.setdefault((row["project_id"], row["lane"]), []).append(seconds)
        elif done_at is None:
            cancelled_at = row["cancelled_at"]
            if row["current_status"] == "cancelled" or cancelled_at is not None:
                if cancelled_at is not None and win_from_s <= cancelled_at <= now_s:
                    cancelled += 1
            elif row["started_at"] is not None:
                censored += 1

    durations.sort()
    group_rows = []
    for (project_id, lane), values in sorted(
            groups.items(), key=lambda item: (item[0][0] or "", item[0][1])):
        values.sort()
        group_rows.append({
            "project_id": project_id,
            "lane": lane,
            "count": len(values),
            "median_seconds": _median(values),
            "p90_seconds": _p90_nearest_rank(values),
        })
    return {
        "median_seconds": _median(durations),
        "p90_seconds": _p90_nearest_rank(durations),
        "measured": len(durations),
        "done_total": done_total,
        "excluded_no_start": excluded_no_start,
        "cancelled": cancelled,
        "open_censored": censored,
        "groups": group_rows,
    }


# -- rework rate --------------------------------------------------------------


def _rework(conn: sqlite3.Connection, win_from: datetime, now: datetime,
            project_ids: Sequence[str],
            lanes: Sequence[str]) -> Dict[str, Any]:
    params = []  # type: List[Any]
    filter_sql = ""
    filter_sql += _in_clause(
        "COALESCE(impl.project_id, q.project_id)", project_ids, params)
    filter_sql += _in_clause(
        "COALESCE(impl.dispatch_lane, '{}')".format(UNKNOWN_LANE), lanes, params)
    rows = conn.execute(
        """
        SELECT COALESCE(q.qa_for_issue_id, '') AS impl_id,
               q.qa_candidate AS candidate,
               q.qa_verdict AS verdict,
               q.qa_verdict_at AS verdict_at
        FROM issues q
        LEFT JOIN issues impl ON impl.id = q.qa_for_issue_id
        WHERE q.is_jira = 0
          AND q.qa_verdict IS NOT NULL
          AND q.qa_candidate IS NOT NULL{filters}
        """.format(filters=filter_sql),
        params,
    ).fetchall()

    candidates = {}  # type: Dict[Tuple[str, str], Dict[str, Any]]
    for row in rows:
        key = (row["impl_id"], row["candidate"])
        entry = candidates.setdefault(
            key, {"passed": False, "reworked": False, "at": None})
        if row["verdict"] == "PASSED":
            entry["passed"] = True
        elif row["verdict"] in REWORK_VERDICTS:
            entry["reworked"] = True
        at = _parse_ts(row["verdict_at"])
        if at is not None and (entry["at"] is None or at < entry["at"]):
            entry["at"] = at

    reworked = denominator = unwindowed = 0
    weekly = {}  # type: Dict[str, Dict[str, int]]
    for entry in candidates.values():
        is_rework = entry["reworked"] and not entry["passed"]
        at = entry["at"]
        if at is None:
            unwindowed += 1
            continue
        if not (win_from <= at <= now):
            continue
        denominator += 1
        week_start = (at - timedelta(days=at.weekday())).strftime("%Y-%m-%d")
        bucket = weekly.setdefault(
            week_start, {"reworked": 0, "candidates": 0})
        bucket["candidates"] += 1
        if is_rework:
            reworked += 1
            bucket["reworked"] += 1

    return {
        "rate": (reworked / denominator) if denominator else None,
        "reworked": reworked,
        "candidates": denominator,
        "unwindowed": unwindowed,
        "weekly": [
            {"week_start": week, "reworked": bucket["reworked"],
             "candidates": bucket["candidates"]}
            for week, bucket in sorted(weekly.items())
        ],
    }


# -- idle-fleet share ---------------------------------------------------------


def _idle(conn: sqlite3.Connection, win_from: datetime, now: datetime,
          lanes: Sequence[str], project_filtered: bool) -> Dict[str, Any]:
    fetch_from = _fmt_ts(win_from - timedelta(seconds=SNAPSHOT_GAP_SECONDS))
    now_s = _fmt_ts(now)
    if lanes:
        params = list(lanes)  # type: List[Any]
        params.extend([fetch_from, now_s])
        rows = conn.execute(
            """
            SELECT l.at AS at, SUM(l.starved_idle) AS starved, s.paused AS paused,
                   s.pause_observed AS pause_observed
            FROM fleet_snapshot_lanes l
            JOIN fleet_snapshots s ON s.at = l.at
            WHERE l.lane IN ({lanes}) AND l.at >= ? AND l.at <= ?
            GROUP BY l.at, s.paused, s.pause_observed
            ORDER BY l.at
            """.format(lanes=", ".join(["?"] * len(lanes))),
            params,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT at, starved_idle AS starved, paused, pause_observed "
            "FROM fleet_snapshots "
            "WHERE at >= ? AND at <= ? ORDER BY at",
            (fetch_from, now_s),
        ).fetchall()

    idle_seconds = covered = paused_seconds = unavailable_pause_seconds = gap_seconds = 0.0
    previous = None  # type: Optional[Tuple[datetime, int, int, int]]
    for row in rows:
        at = _parse_ts(row["at"])
        if at is None:
            continue
        if previous is not None:
            prev_at, prev_starved, prev_paused, prev_pause_observed = previous
            span = (at - prev_at).total_seconds()
            # clip the interval to the requested window
            left = max(prev_at, win_from)
            right = min(at, now)
            overlap = max(0.0, (right - left).total_seconds())
            if span > SNAPSHOT_GAP_SECONDS:
                gap_seconds += overlap
            elif overlap > 0:
                if not prev_pause_observed:
                    unavailable_pause_seconds += overlap
                elif prev_paused:
                    paused_seconds += overlap
                else:
                    covered += overlap
                    if prev_starved > 0:
                        idle_seconds += overlap
        previous = (at, row["starved"] or 0, row["paused"] or 0,
                    row["pause_observed"] or 0)

    window_seconds = (now - win_from).total_seconds()
    return {
        "share": (idle_seconds / covered) if covered > 0 else None,
        "idle_seconds": round(idle_seconds),
        "covered_seconds": round(covered),
        "paused_seconds": round(paused_seconds),
        "unavailable_pause_seconds": round(unavailable_pause_seconds),
        "gap_seconds": round(gap_seconds),
        "coverage_pct": (
            round(100.0 * covered / window_seconds, 1)
            if window_seconds > 0 else 0.0
        ),
        "snapshots": len(rows),
        "workspace_wide": True,
        "project_filter_ignored": bool(project_filtered),
    }


# -- entry point --------------------------------------------------------------


def flow(conn: sqlite3.Connection, days: int = 30,
         project_ids: Sequence[str] = (), lanes: Sequence[str] = (),
         now: Optional[datetime] = None) -> Dict[str, Any]:
    """The /api/flow payload: three flow metrics for one rolling UTC window.

    ``now`` is injectable for deterministic tests; ``days`` must be one of
    WINDOW_DAYS (routes validate via :func:`validate_days`).
    """
    if days not in WINDOW_DAYS:
        raise ValueError("days must be one of {}".format(
            ", ".join(str(d) for d in WINDOW_DAYS)))
    now = now or datetime.utcnow().replace(microsecond=0)
    win_from = now - timedelta(days=days)
    project_ids = tuple(project_ids)
    lanes = tuple(lanes)

    has_events = (_table_exists(conn, "issue_status_events")
                  and _has_columns(conn, "issues", ("dispatch_lane",)))
    has_qa = _has_columns(
        conn, "issues",
        ("qa_verdict", "qa_verdict_at", "qa_candidate", "qa_for_issue_id"))
    has_snapshots = (_table_exists(conn, "fleet_snapshots")
                     and _table_exists(conn, "fleet_snapshot_lanes"))

    events_start = None
    if has_events:
        row = conn.execute(
            "SELECT MIN(observed_at) FROM issue_status_events").fetchone()
        events_start = row[0]
    snapshots_start = None
    if has_snapshots:
        row = conn.execute("SELECT MIN(at) FROM fleet_snapshots").fetchone()
        snapshots_start = row[0]

    observed_lanes = set()
    if has_events:
        observed_lanes.update(
            row[0] for row in conn.execute(
                "SELECT DISTINCT dispatch_lane FROM issues "
                "WHERE is_jira = 0 AND dispatch_lane IS NOT NULL")
        )
    if has_snapshots:
        observed_lanes.update(
            row[0] for row in conn.execute(
                "SELECT DISTINCT lane FROM fleet_snapshot_lanes")
        )

    if has_events:
        cycle = _cycle_time(conn, win_from, now, project_ids, lanes,
                            events_start)
    else:
        cycle = _cycle_time(conn, win_from, now, project_ids, lanes, None)
    if has_qa:
        rework = _rework(conn, win_from, now, project_ids, lanes)
    else:
        rework = {"rate": None, "reworked": 0, "candidates": 0,
                  "unwindowed": 0, "weekly": []}
    if has_snapshots:
        idle = _idle(conn, win_from, now, lanes, bool(project_ids))
    else:
        idle = {"share": None, "idle_seconds": 0, "covered_seconds": 0,
                "paused_seconds": 0, "unavailable_pause_seconds": 0,
                "gap_seconds": 0, "coverage_pct": 0.0,
                "snapshots": 0, "workspace_wide": True,
                "project_filter_ignored": bool(project_ids)}

    return {
        "days": days,
        "now": _fmt_ts(now),
        "window_from": _fmt_ts(win_from),
        "schema_supported": has_events or has_qa or has_snapshots,
        "coverage": {
            "events_start": events_start,
            "snapshots_start": snapshots_start,
        },
        "lanes": sorted(observed_lanes),
        "cycle_time": cycle,
        "rework": rework,
        "idle": idle,
    }
