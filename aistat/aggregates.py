"""Aggregate queries for the dashboard API (stage 3).

Sources of truth and their precision:

- Tokens by day / by model come straight from ``daily_usage`` — exact, they
  match ``multica runtime usage`` one to one.
- Tokens per issue / per project come from ``issue_usage`` — exact, they
  match ``multica issue usage``.
- Any split of a daily row across *agents* or *projects* is an attribution:
  Multica has no per-task token series, so a (runtime, model, date) row is
  divided between the agents mapped to that (runtime, model) pair. A unique
  mapping is attributed as-is; when several agents share the pair (Codex Dev
  Sol / QA Codex Sol) the row is split by their run durations that day
  (fallback: run counts, then an equal split) and flagged ``estimated``.
  Project attribution of daily rows uses the same run weights and is always
  flagged estimated — one agent can serve several projects in one day.
- Cost of a project/issue is estimated by pricing the issue's exact token
  counts with the model(s) of the agents that ran it (weighted by run
  durations). Tokens attributed to an unpriced/unknown model are surfaced,
  never priced as 0.

Efficiency (tokens per story point, lower is better) only counts issues that
have BOTH story points > 0 and a synced usage row; issues without story
points are excluded from the metric entirely rather than treated as 0.
"""

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

TOKEN_KINDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# Run durations come out of SQLite's julianday() in days; efficiency reports
# money-per-hour, so durations are converted to hours with this factor.
HOURS_PER_DAY = 24.0
EFFICIENCY_HOURLY_MAX_HOURS = 48.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?$")


def _filter_values(values: Optional[List[str]]) -> Tuple[str, ...]:
    """Return distinct, non-empty query values in their original order."""
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    out = []
    for value in values:
        value = str(value).strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def _parse_bound(value: Optional[str], name: str, is_end: bool) -> Tuple[Optional[datetime], bool]:
    """Parse the public UTC filter format.

    Date-only ``to`` remains backwards compatible with the old inclusive day
    filter: it becomes the start of the following day. Datetimes are a
    half-open interval, so ``from=10:00&to=11:00`` selects one hour.
    """
    if value is None or not str(value).strip():
        return None, False
    raw = str(value).strip()
    try:
        if _DATE_RE.match(raw):
            result = datetime.strptime(raw, "%Y-%m-%d")
            return (result + timedelta(days=1) if is_end else result), False
        if _DATETIME_RE.match(raw):
            raw = raw[:-1] if raw.endswith("Z") else raw
            fmt = "%Y-%m-%dT%H:%M:%S" if len(raw) == 19 else "%Y-%m-%dT%H:%M"
            return datetime.strptime(raw, fmt), True
    except ValueError:
        pass
    raise ValueError(
        "%s must be YYYY-MM-DD or UTC YYYY-MM-DDTHH:MM[:SS]Z" % name
    )


def make_filters(date_from: Optional[str] = None, date_to: Optional[str] = None,
                 project_ids: Optional[List[str]] = None,
                 agent_ids: Optional[List[str]] = None,
                 models: Optional[List[str]] = None) -> Dict[str, Any]:
    """Validate and normalize filters shared by every HTTP implementation.

    Token facts are supplied by Multica per day. A range that cuts through a
    day is therefore allocated by the real dated task-run intervals and is
    explicitly returned as an estimate by affected aggregates.
    """
    start, start_has_time = _parse_bound(date_from, "from", False)
    end, end_has_time = _parse_bound(date_to, "to", True)
    if start is not None and end is not None and start >= end:
        raise ValueError("from must be earlier than to")
    return {
        "from": start,
        "to": end,
        "projects": _filter_values(project_ids),
        "agents": _filter_values(agent_ids),
        "models": _filter_values(models),
        "time_estimated": (
            (start_has_time and start is not None and start.time().isoformat() != "00:00:00")
            or (end_has_time and end is not None and end.time().isoformat() != "00:00:00")
        ),
    }


def _coerce_filters(date_from: Optional[str] = None, date_to: Optional[str] = None,
                    project_id: Optional[str] = None,
                    filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if filters is not None:
        return filters
    return make_filters(date_from, date_to, [project_id] if project_id else None)


def _filter_dates(filters: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Inclusive SQLite date bounds that cover a half-open datetime window."""
    start, end = filters["from"], filters["to"]
    date_from = start.date().isoformat() if start is not None else None
    date_to = (end - timedelta(microseconds=1)).date().isoformat() if end is not None else None
    return date_from, date_to


def _day_fraction(date: str, filters: Dict[str, Any]) -> float:
    """Part of a UTC day inside the selected half-open time interval."""
    day_start = datetime.strptime(date, "%Y-%m-%d")
    day_end = day_start + timedelta(days=1)
    start = max(day_start, filters["from"]) if filters["from"] else day_start
    end = min(day_end, filters["to"]) if filters["to"] else day_end
    return max(0.0, (end - start).total_seconds() / (HOURS_PER_DAY * 3600.0))


def _where_values(column: str, values: Tuple[str, ...],
                  params: List[str]) -> str:
    if not values:
        return ""
    params.extend(values)
    return " AND %s IN (%s)" % (column, ", ".join("?" for _ in values))


def _total(row: Dict[str, Any]) -> int:
    return sum(int(row[k] or 0) for k in TOKEN_KINDS)


def _date_clause(column: str, date_from: Optional[str], date_to: Optional[str]
                 ) -> Tuple[str, List[str]]:
    parts, params = [], []
    if date_from:
        parts.append(f"{column} >= ?")
        params.append(date_from)
    if date_to:
        parts.append(f"{column} <= ?")
        params.append(date_to)
    return (" AND ".join(parts) or "1=1"), params


def _weights(keys: List[str], durations: Dict[str, float],
             counts: Dict[str, int]) -> Dict[str, float]:
    """Normalized weights over ``keys``: run durations, else run counts,
    else an equal split. Always sums to 1.0 for a non-empty key list."""
    total_dur = sum(durations.get(k, 0.0) for k in keys)
    if total_dur > 0:
        return {k: durations.get(k, 0.0) / total_dur for k in keys}
    total_cnt = sum(counts.get(k, 0) for k in keys)
    if total_cnt > 0:
        return {k: counts.get(k, 0) / total_cnt for k in keys}
    return {k: 1.0 / len(keys) for k in keys}


def _run_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse Multica's UTC run timestamp without requiring Python 3.7+."""
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1]
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _overlap_seconds(start: datetime, end: datetime,
                     lower: datetime, upper: datetime) -> float:
    """Duration of two half-open UTC intervals in seconds."""
    left, right = max(start, lower), min(end, upper)
    return max(0.0, (right - left).total_seconds())


def _run_intervals(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    """Dated task intervals grouped by agent.

    ``runs`` is the durable record of when a task was actually worked on.
    Rows with missing/invalid timestamps remain available for date-only legacy
    attribution but are never invented into a partial-hour allocation.
    """
    intervals: Dict[str, List[Dict[str, Any]]] = {}
    for row in conn.execute(
        """
        SELECT r.agent_id, i.project_id, r.started_at, r.completed_at
        FROM runs r LEFT JOIN issues i ON i.id = r.issue_id
        WHERE r.agent_id IS NOT NULL
        """
    ):
        item = dict(row)
        item["start"] = _run_datetime(item.pop("started_at"))
        item["end"] = _run_datetime(item.pop("completed_at"))
        intervals.setdefault(item["agent_id"], []).append(item)
    return intervals


def _run_filter_selection(conn: sqlite3.Connection,
                          filters: Dict[str, Any]
                          ) -> Tuple[bool, Dict[str, Dict[str, Any]]]:
    """One filtered run-overlap set per issue for every run-level metric.

    Issue usage is cumulative, not a timestamped token stream. We therefore
    allocate it over the issue's *dated* run intervals: each run is matched
    against the agent and model filters and intersected with the time window
    exactly once, and that single selection feeds the issue's token/SP share
    (``fraction``), its model membership and the per-model active durations —
    so cost and hours can never disagree with the share (FAN-1244). The
    denominator is always every valid interval for the issue, so selecting a
    short hour never inflates to the whole task. Runs without a complete
    dated interval are never invented into the selection, and an issue with
    no matching overlap is omitted entirely.

    Returns ``(needs_runs, selection)`` where ``selection[issue_id]`` is::

        {"fraction": selected / total run seconds (0..1],
         "models": {model: {"dur": selected overlap in days, "n": run count}}}

    ``models`` may carry a ``None`` key: the share of matching runs whose
    agent has no model (or that have no agent). Consumers treat it as an
    unpriced model, so that share is surfaced in ``unpriced_tokens`` /
    ``has_unpriced`` rather than silently priced as a known model.
    """
    needs_runs = bool(
        filters["agents"] or filters["models"]
        or filters["from"] is not None or filters["to"] is not None
    )
    if not needs_runs:
        return False, {}
    lower = filters["from"] or datetime.min
    upper = filters["to"] or datetime.max
    totals: Dict[str, float] = {}
    selected: Dict[str, float] = {}
    models: Dict[str, Dict[str, Dict[str, float]]] = {}
    for row in conn.execute(
        """
        SELECT r.issue_id, r.agent_id, a.model, r.started_at, r.completed_at
        FROM runs r LEFT JOIN agents a ON a.id = r.agent_id
        WHERE r.issue_id IS NOT NULL
        """
    ):
        start, end = _run_datetime(row["started_at"]), _run_datetime(row["completed_at"])
        if start is None or end is None or end <= start:
            continue
        issue_id = row["issue_id"]
        totals[issue_id] = totals.get(issue_id, 0.0) + (end - start).total_seconds()
        if filters["agents"] and row["agent_id"] not in filters["agents"]:
            continue
        if filters["models"] and row["model"] not in filters["models"]:
            continue
        overlap = _overlap_seconds(start, end, lower, upper)
        if overlap <= 0:
            continue
        selected[issue_id] = selected.get(issue_id, 0.0) + overlap
        # A matching run without model metadata keeps its own share under the
        # None key — the same representation _issue_model_stats() uses for a
        # model-less agent — so the unknown part stays unpriced instead of
        # being renormalized onto the known models (FAN-1247).
        model = models.setdefault(issue_id, {}).setdefault(
            row["model"], {"dur": 0.0, "n": 0}
        )
        model["dur"] += overlap / (HOURS_PER_DAY * 3600.0)
        model["n"] += 1
    out: Dict[str, Dict[str, Any]] = {}
    for issue_id, total in totals.items():
        share = selected.get(issue_id, 0.0)
        if total > 0 and share > 0:
            out[issue_id] = {
                "fraction": min(1.0, share / total),
                "models": models.get(issue_id, {}),
            }
    return True, out


def _run_filter_fractions(conn: sqlite3.Connection,
                          filters: Dict[str, Any]) -> Tuple[bool, Dict[str, float]]:
    """Each issue's token/SP share covered by run-level filters, derived from
    :func:`_run_filter_selection` so every consumer applies the same filtered
    run set exactly once."""
    needs_runs, selection = _run_filter_selection(conn, filters)
    return needs_runs, {
        issue_id: entry["fraction"] for issue_id, entry in selection.items()
    }


# -- attribution of daily_usage rows to agents and projects -------------------


def daily_shares(conn: sqlite3.Connection, date_from: Optional[str] = None,
                 date_to: Optional[str] = None,
                 filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Split every daily_usage row down to (date, model, agent, project).

    Returns one share per (date, runtime, model, agent, project) with
    fractional token counts and cost, plus estimation flags:
      - ``agent_estimated``: the agent split was ambiguous (shared
        (runtime, model) pair or no matching agent at all);
      - project attribution is always an estimate; ``project_id`` is None
        when the agent had no runs that day to point at a project.
    Cost fields are None for unpriced models (never 0).
    """
    filters = _coerce_filters(date_from, date_to, filters=filters)
    date_from, date_to = _filter_dates(filters)
    agents = conn.execute(
        "SELECT id, name, model, runtime_id FROM agents"
    ).fetchall()
    pair_agents: Dict[Tuple[str, str], List[str]] = {}
    for a in agents:
        pair_agents.setdefault((a["runtime_id"], a["model"]), []).append(a["id"])

    intervals = _run_intervals(conn)

    where, params = _date_clause("date", date_from, date_to)
    where += _where_values("model", filters["models"], params)
    usage = conn.execute(
        f"SELECT * FROM daily_usage WHERE {where}", params
    ).fetchall()

    shares: List[Dict[str, Any]] = []

    def emit(row: sqlite3.Row, agent_id: Optional[str], project_id: Optional[str],
             weight: float, agent_estimated: bool) -> None:
        if weight <= 0:
            return
        share: Dict[str, Any] = {
            "date": row["date"],
            "runtime_id": row["runtime_id"],
            "model": row["model"],
            "agent_id": agent_id,
            "project_id": project_id,
            "weight": weight,
            "agent_estimated": agent_estimated,
            "estimated": agent_estimated or filters["time_estimated"],
        }
        for kind in TOKEN_KINDS:
            share[kind] = (row[kind] or 0) * weight
        share["cost_usd"] = None if row["cost_usd"] is None else row["cost_usd"] * weight
        share["cost_credits"] = (
            None if row["cost_credits"] is None else row["cost_credits"] * weight
        )
        share["cost_priced"] = bool(row["cost_priced"])
        shares.append(share)

    for row in usage:
        candidates = pair_agents.get((row["runtime_id"], row["model"]), [])
        if not candidates:
            # Without an agent run there is no dated interval to allocate a
            # partial day. The full-date dashboard keeps the legacy
            # unattributed bucket, but a clock-range must not invent one.
            if not filters["time_estimated"]:
                emit(row, None, None, 1.0, agent_estimated=True)
            continue
        ambiguous = len(candidates) > 1
        day_start = datetime.strptime(row["date"], "%Y-%m-%d")
        day_end = day_start + timedelta(days=1)
        selected_start = max(day_start, filters["from"]) if filters["from"] else day_start
        selected_end = min(day_end, filters["to"]) if filters["to"] else day_end

        # Allocate a daily row by actual dated task intervals. The
        # denominator always stays the entire day: selecting 10:00–11:00
        # intentionally shows only the corresponding fraction of that day's
        # tokens, rather than re-normalising the selected hour to 100%.
        full_seconds = 0.0
        selected: Dict[Tuple[str, Optional[str]], float] = {}
        counts: Dict[Tuple[str, Optional[str]], int] = {}
        for agent_id in candidates:
            for item in intervals.get(agent_id, []):
                start, end = item["start"], item["end"]
                key = (agent_id, item["project_id"])
                counts[key] = counts.get(key, 0) + 1
                if start is None or end is None or end <= start:
                    continue
                full = _overlap_seconds(start, end, day_start, day_end)
                if full <= 0:
                    continue
                full_seconds += full
                partial = _overlap_seconds(start, end, selected_start, selected_end)
                if partial > 0:
                    selected[key] = selected.get(key, 0.0) + partial

        if filters["time_estimated"]:
            if full_seconds <= 0:
                continue
            for (agent_id, project_id), seconds in selected.items():
                if filters["agents"] and agent_id not in filters["agents"]:
                    continue
                if filters["projects"] and project_id not in filters["projects"]:
                    continue
                emit(row, agent_id, project_id, seconds / full_seconds, ambiguous)
            continue

        # Date-only filtering keeps the earlier duration/count/equal fallback
        # so historical days without complete run timestamps remain visible.
        durations = {a: 0.0 for a in candidates}
        agent_counts = {a: 0 for a in candidates}
        by_project: Dict[str, Dict[Optional[str], Dict[str, float]]] = {}
        for (agent_id, project_id), seconds in selected.items():
            durations[agent_id] += seconds
            agent_counts[agent_id] += counts.get((agent_id, project_id), 0)
            project = by_project.setdefault(agent_id, {}).setdefault(
                project_id, {"dur": 0.0, "n": 0}
            )
            project["dur"] += seconds
            project["n"] += counts.get((agent_id, project_id), 0)
        # Rows entirely outside the selected date window have no SQL match;
        # a zero-duration day falls back to the known candidate agents.
        agent_weights = _weights(candidates, durations, agent_counts)
        for agent_id, agent_weight in agent_weights.items():
            projects = by_project.get(agent_id, {})
            if not projects:
                emit(row, agent_id, None, agent_weight, ambiguous)
                continue
            project_keys = list(projects)
            project_weights = _weights(
                project_keys,
                {p: v["dur"] for p, v in projects.items()},
                {p: int(v["n"]) for p, v in projects.items()},
            )
            for project_id, project_weight in project_weights.items():
                emit(row, agent_id, project_id, agent_weight * project_weight,
                     ambiguous)
    return shares


def _sum_shares(shares: List[Dict[str, Any]], key_fn) -> List[Dict[str, Any]]:
    """Group shares, summing tokens and cost. A None cost on a share whose
    model is unpriced marks the group ``has_unpriced`` instead of adding 0."""
    groups: Dict[Any, Dict[str, Any]] = {}
    for s in shares:
        key = key_fn(s)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "input_tokens": 0.0, "output_tokens": 0.0,
                "cache_read_tokens": 0.0, "cache_write_tokens": 0.0,
                "cost_usd": 0.0, "cost_credits": 0.0,
                "has_unpriced": False, "estimated": False, "_key": key,
            }
        for kind in TOKEN_KINDS:
            g[kind] += s[kind]
        if s["cost_usd"] is None:
            g["has_unpriced"] = True
        else:
            g["cost_usd"] += s["cost_usd"]
            g["cost_credits"] += s["cost_credits"] or 0.0
        g["estimated"] = g["estimated"] or s["estimated"]
    out = []
    for g in groups.values():
        for kind in TOKEN_KINDS:
            g[kind] = round(g[kind])
        g["total_tokens"] = sum(g[k] for k in TOKEN_KINDS)
        out.append(g)
    return out


# -- daily series --------------------------------------------------------------


def daily_series(conn: sqlite3.Connection, group: str = "model",
                 date_from: Optional[str] = None, date_to: Optional[str] = None,
                 project_id: Optional[str] = None,
                 filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Per-day token/cost series stacked by model, agent or project.

    ``group=model`` without a project filter reads daily_usage directly and
    is exact (matches ``multica runtime usage``). Grouping by agent/project
    or filtering by project goes through attribution and is marked estimated
    where the split was ambiguous.
    """
    if group not in ("model", "agent", "project"):
        raise ValueError(f"unsupported group: {group}")
    filters = _coerce_filters(date_from, date_to, project_id, filters)
    date_from, date_to = _filter_dates(filters)

    if (group == "model" and not filters["projects"] and not filters["agents"]
            and not filters["time_estimated"]):
        where, params = _date_clause("date", date_from, date_to)
        where += _where_values("model", filters["models"], params)
        rows = conn.execute(
            f"""
            SELECT date, model AS key,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(cost_usd) AS cost_usd,
                   SUM(cost_credits) AS cost_credits,
                   SUM(1 - cost_priced) AS unpriced_rows
            FROM daily_usage WHERE {where}
            GROUP BY date, model ORDER BY date, model
            """,
            params,
        ).fetchall()
        series = []
        for r in rows:
            d = dict(r)
            fraction = _day_fraction(d["date"], filters)
            if fraction <= 0:
                continue
            for kind in TOKEN_KINDS:
                d[kind] = round((d[kind] or 0) * fraction)
            if d["cost_usd"] is not None:
                d["cost_usd"] *= fraction
            if d["cost_credits"] is not None:
                d["cost_credits"] *= fraction
            unpriced = d.pop("unpriced_rows") > 0
            d["has_unpriced"] = unpriced
            d["estimated"] = filters["time_estimated"]
            d["total_tokens"] = _total(d)
            series.append(d)
        return {
            "group": group, "estimated": filters["time_estimated"], "rows": series,
        }

    shares = daily_shares(conn, filters=filters)
    if filters["projects"]:
        shares = [s for s in shares if s["project_id"] in filters["projects"]]
    if filters["agents"]:
        shares = [s for s in shares if s["agent_id"] in filters["agents"]]
    if group == "model":
        key_fn = lambda s: (s["date"], s["model"])  # noqa: E731
    elif group == "agent":
        key_fn = lambda s: (s["date"], s["agent_id"])  # noqa: E731
    else:
        key_fn = lambda s: (s["date"], s["project_id"])  # noqa: E731
    grouped = _sum_shares(shares, key_fn)
    names = _display_names(conn)
    rows = []
    for g in sorted(grouped, key=lambda g: (g["_key"][0], g["_key"][1] or "")):
        date, key = g.pop("_key")
        g["date"] = date
        g["key"] = _label(group, key, names)
        rows.append(g)
    return {
        "group": group,
        # Any dimension attribution or a partial-day range is an estimate.
        "estimated": True,
        "rows": rows,
    }


def _display_names(conn: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    return {
        "agent": {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents")},
        "project": {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM projects")},
    }


def _label(group: str, key: Optional[str], names: Dict[str, Dict[str, str]]) -> str:
    if group == "model":
        return key or "unknown"
    if key is None:
        return "(не атрибутировано)"
    return names.get(group, {}).get(key, key)


# -- agents ---------------------------------------------------------------------


def agent_totals(conn: sqlite3.Connection, date_from: Optional[str] = None,
                 date_to: Optional[str] = None,
                 project_id: Optional[str] = None,
                 filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Tokens / cost / credits per agent over the period (attribution-based)."""
    filters = _coerce_filters(date_from, date_to, project_id, filters)
    shares = daily_shares(conn, filters=filters)
    if filters["projects"]:
        shares = [s for s in shares if s["project_id"] in filters["projects"]]
    if filters["agents"]:
        shares = [s for s in shares if s["agent_id"] in filters["agents"]]
    grouped = _sum_shares(shares, lambda s: s["agent_id"])
    agents = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, model, runtime_id FROM agents"
    )}
    runtimes = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM runtimes")}
    run_where, run_params = "1=1", []
    if filters["projects"]:
        run_where += _where_values("i.project_id", filters["projects"], run_params)
    if filters["agents"]:
        run_where += _where_values("r.agent_id", filters["agents"], run_params)
    if filters["models"]:
        run_where += _where_values("a.model", filters["models"], run_params)
    run_counts: Dict[str, int] = {}
    has_time_window = filters["from"] is not None or filters["to"] is not None
    lower = filters["from"] or datetime.min
    upper = filters["to"] or datetime.max
    for row in conn.execute(
        f"""
        SELECT r.agent_id, r.started_at, r.completed_at FROM runs r
        LEFT JOIN issues i ON i.id = r.issue_id
        LEFT JOIN agents a ON a.id = r.agent_id
        WHERE {run_where}
        """,
        run_params,
    ):
        if has_time_window:
            start = _run_datetime(row["started_at"])
            end = _run_datetime(row["completed_at"])
            # A filtered run count is defined by the same half-open interval
            # semantics as token attribution. Incomplete timestamps cannot be
            # placed in a selected window without inventing a duration.
            if (start is None or end is None or end <= start
                    or _overlap_seconds(start, end, lower, upper) <= 0):
                continue
        agent_id = row["agent_id"]
        run_counts[agent_id] = run_counts.get(agent_id, 0) + 1
    out = []
    for g in grouped:
        agent_id = g.pop("_key")
        agent = agents.get(agent_id)
        g["agent_id"] = agent_id
        g["name"] = agent["name"] if agent else "(не атрибутировано)"
        g["model"] = agent["model"] if agent else None
        g["runtime"] = runtimes.get(agent["runtime_id"]) if agent else None
        g["runs"] = run_counts.get(agent_id, 0)
        out.append(g)
    out.sort(key=lambda g: -g["total_tokens"])
    return out


# -- issue / project cost estimation ---------------------------------------------


def _pricing_rates(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    return {
        r["model"]: dict(r)
        for r in conn.execute(
            "SELECT model, input_rate, output_rate, cache_read_rate, "
            "cache_write_rate, unpriced FROM model_pricing"
        )
    }


def _issue_model_stats(conn: sqlite3.Connection) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Per issue: ``{model: {'dur': run-duration-in-days, 'n': run-count}}``.

    Duration is started_at→completed_at in days (julianday), missing
    timestamps → 0. Used both to weight a model's share of an issue and to
    report the active hours it spent on it.
    """
    stats: Dict[str, Dict[str, Dict[str, float]]] = {}
    rows = conn.execute(
        """
        SELECT r.issue_id, a.model,
               COUNT(*) AS n,
               SUM(MAX(COALESCE(julianday(r.completed_at) - julianday(r.started_at), 0), 0)) AS dur
        FROM runs r JOIN agents a ON a.id = r.agent_id
        GROUP BY r.issue_id, a.model
        """
    )
    for row in rows:
        stats.setdefault(row["issue_id"], {})[row["model"]] = {
            "dur": row["dur"] or 0.0, "n": row["n"],
        }
    return stats


def _model_weights(models: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """Normalized model weights for one issue from its ``{model: {dur, n}}``
    stats, using run durations with a count fallback (:func:`_weights`)."""
    return _weights(
        list(models),
        {m: v["dur"] for m, v in models.items()},
        {m: v["n"] for m, v in models.items()},
    )


def _issue_model_weights(conn: sqlite3.Connection) -> Dict[str, Dict[str, float]]:
    """Per issue: weight of each model, from run durations (fallback counts)."""
    return {
        issue_id: _model_weights(models)
        for issue_id, models in _issue_model_stats(conn).items()
    }


def issue_cost_estimate(usage: Dict[str, Any], model_weights: Dict[str, float],
                        rates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Estimated USD cost of one issue's exact token counts.

    Token counts are split across the models that ran the issue and priced
    with each model's official rates. Tokens landing on an unpriced/unknown
    model are reported in ``unpriced_tokens`` and NOT priced as 0. An issue
    with usage but no runs cannot be attributed: cost None, ``attributed``
    False.
    """
    if not model_weights:
        return {"cost_usd": None, "attributed": False, "unpriced_tokens": 0}
    usd = 0.0
    unpriced_tokens = 0.0
    total = sum(int(usage[f"total_{k}"] or 0) for k in TOKEN_KINDS)
    for model, weight in model_weights.items():
        rate = rates.get(model)
        if rate is None or rate["unpriced"]:
            unpriced_tokens += total * weight
            continue
        usd += (
            (usage["total_input_tokens"] or 0) * weight * rate["input_rate"]
            + (usage["total_output_tokens"] or 0) * weight * rate["output_rate"]
            + (usage["total_cache_read_tokens"] or 0) * weight * rate["cache_read_rate"]
            + (usage["total_cache_write_tokens"] or 0) * weight * rate["cache_write_rate"]
        ) / 1_000_000
    return {
        "cost_usd": usd,
        "attributed": True,
        "unpriced_tokens": round(unpriced_tokens),
    }


def projects_overview(conn: sqlite3.Connection,
                      credits_per_usd: float = 1.0,
                      filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Per project: exact tokens, estimated cost/credits, SP, statuses,
    efficiency (tokens per story point over issues with SP and usage)."""
    rates = _pricing_rates(conn)
    filters = _coerce_filters(filters=filters)
    needs_run_filter, selection = _run_filter_selection(conn, filters)
    # Unfiltered cost prices each issue's exact tokens with its lifetime model
    # weights; a run filter instead prices only the *matching* runs' models,
    # taken from the same run-overlap set that supplies the token/SP share, so
    # cost can never disagree with tokens for the selected slice (FAN-1251).
    model_weights = {} if needs_run_filter else _issue_model_weights(conn)
    project_where, project_params = "1=1", []
    project_where += _where_values("id", filters["projects"], project_params)
    projects = [dict(r) for r in conn.execute(
        "SELECT id, title, status FROM projects WHERE %s ORDER BY title" % project_where,
        project_params,
    )]
    issue_where, issue_params = "i.is_jira = 0", []
    issue_where += _where_values("i.project_id", filters["projects"], issue_params)
    issues = conn.execute(
        """
        SELECT i.id, i.project_id, i.status, i.story_points,
               u.task_count, u.total_input_tokens, u.total_output_tokens,
               u.total_cache_read_tokens, u.total_cache_write_tokens,
               (u.issue_id IS NOT NULL) AS has_usage
        FROM issues i LEFT JOIN issue_usage u ON u.issue_id = i.id
        WHERE %s
        """ % issue_where,
        issue_params,
    ).fetchall()

    by_project: Dict[str, List[sqlite3.Row]] = {}
    for row in issues:
        by_project.setdefault(row["project_id"], []).append(row)

    out = []
    for project in projects:
        rows = by_project.get(project["id"], [])
        tokens = {k: 0 for k in TOKEN_KINDS}
        cost_usd = 0.0
        unpriced_tokens = 0
        unattributed = 0
        with_usage = 0
        statuses: Dict[str, int] = {}
        sp_sum = 0.0
        sp_issues = 0
        eff_tokens = 0
        eff_sp = 0.0
        eff_issues = 0
        for row in rows:
            if needs_run_filter:
                entry = selection.get(row["id"])
                if entry is None:
                    continue
                factor = entry["fraction"]
                weights = _model_weights(entry["models"])
            else:
                factor = 1.0
                weights = model_weights.get(row["id"], {})
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            if row["story_points"] is not None:
                sp_sum += row["story_points"] * factor
                sp_issues += 1
            if not row["has_usage"]:
                continue
            with_usage += 1
            usage = dict(row)
            for kind in TOKEN_KINDS:
                usage[f"total_{kind}"] = (row[f"total_{kind}"] or 0) * factor
                tokens[kind] += usage[f"total_{kind}"]
            est = issue_cost_estimate(usage, weights, rates)
            if est["attributed"]:
                cost_usd += est["cost_usd"]
                unpriced_tokens += est["unpriced_tokens"]
            else:
                unattributed += 1
            # Efficiency counts only issues with story points > 0 AND usage;
            # issues without SP are excluded, never treated as SP=0.
            if row["story_points"] is not None and row["story_points"] > 0:
                eff_tokens += sum(usage[f"total_{k}"] for k in TOKEN_KINDS)
                eff_sp += row["story_points"] * factor
                eff_issues += 1
        total_tokens = sum(tokens.values())
        out.append({
            "project_id": project["id"],
            "title": project["title"],
            "project_status": project["status"],
            "issues": sum(
                1 for row in rows
                if not needs_run_filter or row["id"] in selection
            ),
            "issues_with_usage": with_usage,
            "statuses": statuses,
            **tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "cost_credits": cost_usd * credits_per_usd,
            "cost_estimated": True,
            "estimated": needs_run_filter,
            "cost_unattributed_issues": unattributed,
            "unpriced_tokens": unpriced_tokens,
            "story_points": sp_sum,
            "issues_with_sp": sp_issues,
            "efficiency_tokens": eff_tokens,
            "efficiency_sp": eff_sp,
            "efficiency_issues": eff_issues,
            "tokens_per_sp": (eff_tokens / eff_sp) if eff_sp > 0 else None,
        })
    out.sort(key=lambda p: -p["total_tokens"])
    return out


def issue_efficiency(conn: sqlite3.Connection, project_id: Optional[str] = None,
                     limit: Optional[int] = None,
                     filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Per-issue efficiency: total tokens ÷ story points, worst first.

    Only issues with story points > 0 and a synced usage row participate;
    issues without story points are excluded from the metric (never SP=0).
    """
    filters = _coerce_filters(project_id=project_id, filters=filters)
    needs_run_filter, fractions = _run_filter_fractions(conn, filters)
    params: List[Any] = []
    where = "i.is_jira = 0 AND i.story_points IS NOT NULL AND i.story_points > 0"
    where += _where_values("i.project_id", filters["projects"], params)
    rows = conn.execute(
        f"""
        SELECT i.id, i.identifier, i.title, i.status, i.story_points,
               i.project_id, p.title AS project,
               u.total_input_tokens + u.total_output_tokens
                 + u.total_cache_read_tokens + u.total_cache_write_tokens AS total_tokens,
               u.task_count
        FROM issues i
        JOIN issue_usage u ON u.issue_id = i.id
        LEFT JOIN projects p ON p.id = i.project_id
        WHERE {where}
        ORDER BY ((u.total_input_tokens + u.total_output_tokens
                   + u.total_cache_read_tokens + u.total_cache_write_tokens)
                  / i.story_points) DESC
        """,
        params,
    ).fetchall()
    agents_by_issue: Dict[str, List[str]] = {}
    for r in conn.execute(
        "SELECT DISTINCT r.issue_id, a.name FROM runs r "
        "JOIN agents a ON a.id = r.agent_id ORDER BY a.name"
    ):
        agents_by_issue.setdefault(r["issue_id"], []).append(r["name"])
    out = []
    for r in rows:
        d = dict(r)
        issue_id = d.pop("id")
        factor = fractions.get(issue_id, 0.0) if needs_run_filter else 1.0
        if factor <= 0:
            continue
        d["issue_id"] = issue_id
        d["agents"] = agents_by_issue.get(issue_id, [])
        d["total_tokens"] = round(d["total_tokens"] * factor)
        d["story_points"] *= factor
        d["estimated"] = needs_run_filter
        d["tokens_per_sp"] = d["total_tokens"] / d["story_points"]
        out.append(d)
    if limit is not None:
        out = out[:limit]
    return out


# -- chartable token efficiency -------------------------------------------------


def _bucket_start(value: datetime, granularity: str) -> datetime:
    """Return the UTC hour/day bucket containing ``value``."""
    if granularity == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket_end(start: datetime, granularity: str) -> datetime:
    return start + timedelta(hours=1 if granularity == "hour" else 24)


def _bucket_key(start: datetime, granularity: str) -> str:
    if granularity == "hour":
        return start.strftime("%Y-%m-%dT%H:00Z")
    return start.date().isoformat()


def efficiency_chart_breakdown(
        conn: sqlite3.Connection,
        project_id: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Token efficiency cuts for the dashboard charts.

    ``issue_usage`` contains an exact cumulative token total, while story
    points belong to the issue rather than an individual agent/model/run.  To
    show either fact in a run-level cut, both are allocated by a valid dated
    run's duration divided by *all* valid dated run durations of that issue.
    A dimension or time filter selects shares from that fixed denominator; it
    never renormalizes a short interval to a whole issue.  This is necessarily
    an estimate, and issues without usable dated runs are omitted rather than
    assigned invented agent/model/time values.

    Time uses UTC hour buckets only for an explicitly bounded window of at
    most 48 hours.  All other selections use day buckets, keeping ordinary
    7/14/30/90-day dashboard ranges readable.
    """
    filters = _coerce_filters(project_id=project_id, filters=filters)
    params: List[Any] = []
    where = ("i.is_jira = 0 AND i.story_points IS NOT NULL "
             "AND i.story_points > 0")
    where += _where_values("i.project_id", filters["projects"], params)
    rows = conn.execute(
        f"""
        SELECT i.id AS issue_id, i.story_points,
               u.total_input_tokens, u.total_output_tokens,
               u.total_cache_read_tokens, u.total_cache_write_tokens,
               r.agent_id, a.name AS agent_name, a.model,
               r.started_at, r.completed_at
        FROM issues i
        JOIN issue_usage u ON u.issue_id = i.id
        JOIN runs r ON r.issue_id = i.id
        LEFT JOIN agents a ON a.id = r.agent_id
        WHERE {where}
        """,
        params,
    ).fetchall()

    by_issue: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        start = _run_datetime(row["started_at"])
        end = _run_datetime(row["completed_at"])
        if start is None or end is None or end <= start:
            continue
        issue = by_issue.setdefault(row["issue_id"], {
            "story_points": float(row["story_points"]),
            "tokens": {
                kind: float(row[f"total_{kind}"] or 0) for kind in TOKEN_KINDS
            },
            "runs": [],
        })
        issue["runs"].append({
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "model": row["model"],
            "start": start,
            "end": end,
        })

    def empty_bucket(key: str, label: str) -> Dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "total_tokens": 0.0,
            "story_points": 0.0,
            "issues": set(),
        }

    agents: Dict[str, Dict[str, Any]] = {}
    models: Dict[str, Dict[str, Any]] = {}
    time_segments: List[Dict[str, Any]] = []
    lower = filters["from"] or datetime.min
    upper = filters["to"] or datetime.max

    for issue_id, issue in by_issue.items():
        total_seconds = sum(
            (run["end"] - run["start"]).total_seconds() for run in issue["runs"]
        )
        if total_seconds <= 0:
            continue
        total_tokens = sum(issue["tokens"].values())
        for run in issue["runs"]:
            matches_agent = (
                not filters["agents"] or run["agent_id"] in filters["agents"]
            )
            matches_model = (
                not filters["models"] or run["model"] in filters["models"]
            )
            if not (matches_agent and matches_model):
                continue
            start, end = max(run["start"], lower), min(run["end"], upper)
            seconds = _overlap_seconds(run["start"], run["end"], lower, upper)
            if seconds <= 0:
                continue
            share = seconds / total_seconds
            contribution = {
                "issue_id": issue_id,
                "start": start,
                "end": end,
                "total_tokens": total_tokens * share,
                "story_points": issue["story_points"] * share,
            }
            time_segments.append(contribution)

            if run["agent_id"] and run["agent_name"]:
                bucket = agents.setdefault(
                    run["agent_id"], empty_bucket(run["agent_id"], run["agent_name"])
                )
                bucket["total_tokens"] += contribution["total_tokens"]
                bucket["story_points"] += contribution["story_points"]
                bucket["issues"].add(issue_id)
            if run["model"]:
                bucket = models.setdefault(
                    run["model"], empty_bucket(run["model"], run["model"])
                )
                bucket["total_tokens"] += contribution["total_tokens"]
                bucket["story_points"] += contribution["story_points"]
                bucket["issues"].add(issue_id)

    bounded_short_window = (
        filters["from"] is not None and filters["to"] is not None
        and (filters["to"] - filters["from"]).total_seconds()
        <= EFFICIENCY_HOURLY_MAX_HOURS * 3600.0
    )
    granularity = "hour" if bounded_short_window else "day"
    time: Dict[str, Dict[str, Any]] = {}
    if time_segments:
        window_start = filters["from"] or min(s["start"] for s in time_segments)
        window_end = filters["to"] or max(s["end"] for s in time_segments)
        current = _bucket_start(window_start, granularity)
        while current < window_end:
            key = _bucket_key(current, granularity)
            time[key] = empty_bucket(key, key)
            current = _bucket_end(current, granularity)
        for segment in time_segments:
            cursor = _bucket_start(segment["start"], granularity)
            while cursor < segment["end"]:
                bucket_end = _bucket_end(cursor, granularity)
                overlap = _overlap_seconds(
                    segment["start"], segment["end"], cursor, bucket_end
                )
                if overlap > 0:
                    fraction = overlap / (segment["end"] - segment["start"]).total_seconds()
                    bucket = time[_bucket_key(cursor, granularity)]
                    bucket["total_tokens"] += segment["total_tokens"] * fraction
                    bucket["story_points"] += segment["story_points"] * fraction
                    bucket["issues"].add(segment["issue_id"])
                cursor = bucket_end

    def output(rows: Dict[str, Dict[str, Any]], sort_key) -> List[Dict[str, Any]]:
        out = []
        for row in rows.values():
            total = row["total_tokens"]
            sp = row["story_points"]
            out.append({
                "key": row["key"],
                "label": row["label"],
                "total_tokens": round(total),
                "story_points": sp,
                "issues": len(row["issues"]),
                "tokens_per_sp": (total / sp) if sp > 0 else None,
                "estimated": True,
            })
        out.sort(key=sort_key)
        return out

    by_efficiency = lambda row: (
        row["tokens_per_sp"] is None,
        row["tokens_per_sp"] if row["tokens_per_sp"] is not None else 0.0,
        row["label"],
    )
    return {
        "metric": "tokens_per_sp",
        "estimated": True,
        "agents": output(agents, by_efficiency),
        "models": output(models, by_efficiency),
        "time": {
            "granularity": granularity,
            "rows": output(time, lambda row: row["key"]),
        },
    }


# -- efficiency (tokens / cost / weighted) ----------------------------------------


def efficiency_breakdown(conn: sqlite3.Connection,
                         project_id: Optional[str] = None,
                         filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Three efficiency metrics over the issues that count toward efficiency
    (is_jira = 0, story points > 0, a synced usage row, optional project),
    plus a per-model breakdown. Formulas and rationale:
    ``docs/metrics-efficiency.md``.

    - Token efficiency (``tokens_per_sp``, lower is better) — exact tokens ÷
      story points — spans every counted issue.
    - Cost efficiency (``cost_per_sp``, USD ÷ SP) and weighted efficiency span
      the subset that also has runs, so the exact token counts can be split
      across the models that ran the issue and priced. Tokens landing on an
      unpriced/unknown model are surfaced in ``unpriced_tokens`` and flagged,
      never priced as 0.
    - Weighted efficiency (``weighted_efficiency``, USD per active hour per SP,
      lower is better) is the SP-weighted mean of each fully priced issue's
      cost ÷ active-hours ÷ story-points; algebraically ``Σ(cost_i / hours_i)
      ÷ Σ sp_i``. It is scale-invariant, so models/projects/periods compare
      fairly (see the doc for the derivation).

    An agent/model/time selection uses one filtered run-overlap set for
    everything (FAN-1244): tokens and SP take the issue's selected duration
    share, model membership and weights come from the *matching* runs only
    (an excluded agent's model never appears), and active hours are the
    actual selected overlaps — the filter share is never applied to them a
    second time. Unfiltered and project-only calls keep the exact lifetime
    run stats.

    The per-model rows carry the same three metrics for one model's share of
    the counted issues (story points and tokens split by run duration, cost by
    the model's official rate), sorted cheapest cost-per-SP first so it is
    obvious which model delivers a story point for less.
    """
    filters = _coerce_filters(project_id=project_id, filters=filters)
    needs_run_filter, selection = _run_filter_selection(conn, filters)
    rates = _pricing_rates(conn)
    stats = {} if needs_run_filter else _issue_model_stats(conn)

    where = ("i.is_jira = 0 AND i.story_points IS NOT NULL "
             "AND i.story_points > 0")
    params: List[Any] = []
    where += _where_values("i.project_id", filters["projects"], params)
    rows = conn.execute(
        f"""
        SELECT i.id, i.story_points,
               u.total_input_tokens, u.total_output_tokens,
               u.total_cache_read_tokens, u.total_cache_write_tokens
        FROM issues i JOIN issue_usage u ON u.issue_id = i.id
        WHERE {where}
        """,
        params,
    ).fetchall()

    tokens = {k: 0 for k in TOKEN_KINDS}
    sp_sum = 0.0
    cost_usd = 0.0
    cost_sp = 0.0
    active_hours = 0.0
    unpriced_tokens = 0.0
    has_unpriced = False
    any_priced = False
    cost_issues = 0
    weighted_num = 0.0   # Σ cost_i / hours_i over fully priced issues
    weighted_den = 0.0   # Σ sp_i over those same issues
    models: Dict[str, Dict[str, Any]] = {}

    def bucket(model: str) -> Dict[str, Any]:
        return models.setdefault(model, {
            "tokens": {k: 0.0 for k in TOKEN_KINDS},
            "story_points": 0.0, "cost_usd": 0.0, "active_hours": 0.0,
            "weighted_num": 0.0, "weighted_den": 0.0, "priced": True,
        })

    for row in rows:
        if needs_run_filter:
            entry = selection.get(row["id"])
            if entry is None:
                continue
            factor = entry["fraction"]
            issue_models = entry["models"]
        else:
            factor = 1.0
            issue_models = stats.get(row["id"])
        sp = row["story_points"] * factor
        sp_sum += sp
        for kind in TOKEN_KINDS:
            tokens[kind] += (row[f"total_{kind}"] or 0) * factor
        if not issue_models:
            continue  # usage but no (matching) runs → not attributable to a model
        cost_issues += 1
        cost_sp += sp
        keys = list(issue_models)
        weights = _weights(
            keys,
            {m: v["dur"] for m, v in issue_models.items()},
            {m: v["n"] for m, v in issue_models.items()},
        )
        # ``dur`` is already the selected overlap when a run filter applies,
        # so the filter share must not scale the hours a second time.
        issue_hours = sum(v["dur"] for v in issue_models.values()) * HOURS_PER_DAY
        active_hours += issue_hours
        issue_total = sum((row[f"total_{k}"] or 0) * factor for k in TOKEN_KINDS)
        issue_cost = 0.0
        issue_priced = True
        for model, weight in weights.items():
            b = bucket(model)
            for kind in TOKEN_KINDS:
                b["tokens"][kind] += (row[f"total_{kind}"] or 0) * factor * weight
            b["story_points"] += sp * weight
            model_hours = issue_models[model]["dur"] * HOURS_PER_DAY
            b["active_hours"] += model_hours
            rate = rates.get(model)
            if rate is None or rate["unpriced"]:
                unpriced_tokens += issue_total * weight
                has_unpriced = True
                issue_priced = False
                b["priced"] = False
                continue
            model_cost = (
                (row["total_input_tokens"] or 0) * factor * weight * rate["input_rate"]
                + (row["total_output_tokens"] or 0) * factor * weight * rate["output_rate"]
                + (row["total_cache_read_tokens"] or 0) * factor * weight * rate["cache_read_rate"]
                + (row["total_cache_write_tokens"] or 0) * factor * weight * rate["cache_write_rate"]
            ) / 1_000_000
            issue_cost += model_cost
            any_priced = True
            b["cost_usd"] += model_cost
            if model_hours > 0:
                b["weighted_num"] += model_cost / model_hours
                b["weighted_den"] += sp * weight
        cost_usd += issue_cost
        # Weighted efficiency only over fully priced issues, so cost_i is whole.
        if issue_priced and issue_hours > 0 and sp > 0:
            weighted_num += issue_cost / issue_hours
            weighted_den += sp

    model_rows = []
    for model, b in models.items():
        priced = b["priced"]
        m_tokens = {k: round(v) for k, v in b["tokens"].items()}
        m_total = sum(m_tokens.values())
        m_sp = b["story_points"]
        model_rows.append({
            "model": model,
            **m_tokens,
            "total_tokens": m_total,
            "story_points": m_sp,
            "active_hours": b["active_hours"],
            "cost_usd": b["cost_usd"] if priced else None,
            "tokens_per_sp": (m_total / m_sp) if m_sp > 0 else None,
            "cost_per_sp": (b["cost_usd"] / m_sp) if (priced and m_sp > 0) else None,
            "weighted_efficiency": (
                b["weighted_num"] / b["weighted_den"]
                if (priced and b["weighted_den"] > 0) else None
            ),
            "has_unpriced": not priced,
        })
    # Cheapest cost per story point first; unpriced models sink to the bottom.
    model_rows.sort(key=lambda m: (m["cost_per_sp"] is None, m["cost_per_sp"] or 0.0))

    total_tokens = sum(tokens.values())
    return {
        "estimated": True,
        **tokens,
        "total_tokens": total_tokens,
        "story_points": sp_sum,
        "cost_story_points": cost_sp,
        "cost_usd": cost_usd,
        "active_hours": active_hours,
        "unpriced_tokens": round(unpriced_tokens),
        "has_unpriced": has_unpriced,
        "cost_issues": cost_issues,
        "tokens_per_sp": (total_tokens / sp_sum) if sp_sum > 0 else None,
        "cost_per_sp": (cost_usd / cost_sp) if (cost_sp > 0 and any_priced) else None,
        "weighted_efficiency": (
            weighted_num / weighted_den if weighted_den > 0 else None
        ),
        "models": model_rows,
    }


# -- summary + meta ---------------------------------------------------------------


def summary(conn: sqlite3.Connection, date_from: Optional[str] = None,
            date_to: Optional[str] = None, project_id: Optional[str] = None,
            credits_per_usd: float = 1.0,
            filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Headline cards for the selected dimensions and run-time window.

    Whole-day model totals remain exact. Agent/project and partial-day ranges
    are duration-weighted estimates over the dated task runs; the same run
    share is applied to task SP and efficiency inputs.

    ``estimated`` covers only the token/cost cards; a model-only or date-only
    selection keeps them exact. Task-level facts have their own precision:
    any agent/model/date selection allocates SP and efficiency inputs by run
    duration shares, reported as ``sp_estimated`` / ``efficiency_estimated``.
    A project-only selection keeps them exact (issues belong to projects
    directly) even though the token attribution above is estimated.
    """
    filters = _coerce_filters(date_from, date_to, project_id, filters)
    date_from, date_to = _filter_dates(filters)
    if (not filters["projects"] and not filters["agents"]
            and not filters["time_estimated"]):
        where, params = _date_clause("date", date_from, date_to)
        where += _where_values("model", filters["models"], params)
        tokens = {k: 0 for k in TOKEN_KINDS}
        cost_usd = cost_credits = 0.0
        has_unpriced = False
        usage_rows = conn.execute(
            f"SELECT * FROM daily_usage WHERE {where}", params
        ).fetchall()
        for usage in usage_rows:
            fraction = _day_fraction(usage["date"], filters)
            for kind in TOKEN_KINDS:
                tokens[kind] += round((usage[kind] or 0) * fraction)
            if usage["cost_usd"] is None:
                has_unpriced = True
            else:
                cost_usd += usage["cost_usd"] * fraction
                cost_credits += (usage["cost_credits"] or 0.0) * fraction
        estimated = filters["time_estimated"]
    else:
        shares = daily_shares(conn, filters=filters)
        if filters["projects"]:
            shares = [s for s in shares if s["project_id"] in filters["projects"]]
        if filters["agents"]:
            shares = [s for s in shares if s["agent_id"] in filters["agents"]]
        agg = _sum_shares(shares, lambda s: "all")
        if agg:
            g = agg[0]
            tokens = {k: g[k] for k in TOKEN_KINDS}
            cost_usd, cost_credits = g["cost_usd"], g["cost_credits"]
            has_unpriced = g["has_unpriced"]
        else:
            tokens = {k: 0 for k in TOKEN_KINDS}
            cost_usd = cost_credits = 0.0
            has_unpriced = False
        estimated = True

    # Jira-imported issues never ran in Multica; exclude them from the
    # story-point and efficiency facts (tokens already exclude them, as Jira
    # issues have no runs/usage).
    sp_where, sp_params = "i.is_jira = 0", []
    if filters["projects"]:
        sp_where += _where_values("i.project_id", filters["projects"], sp_params)
    needs_run_filter, fractions = _run_filter_fractions(conn, filters)
    if needs_run_filter:
        sp_sum = 0.0
        with_sp = 0
        issue_count = 0
        for issue in conn.execute(
            "SELECT i.id, i.story_points FROM issues i WHERE %s" % sp_where,
            sp_params,
        ):
            factor = fractions.get(issue["id"], 0.0)
            if factor <= 0:
                continue
            issue_count += 1
            if issue["story_points"] is not None:
                sp_sum += issue["story_points"] * factor
                with_sp += 1
    else:
        sp_row = conn.execute(
            f"""
            SELECT SUM(i.story_points) AS sp_sum,
                   SUM(CASE WHEN i.story_points IS NOT NULL THEN 1 ELSE 0 END) AS with_sp,
                   COUNT(*) AS issues
            FROM issues i WHERE {sp_where}
            """,
            sp_params,
        ).fetchone()
        sp_sum = sp_row["sp_sum"] or 0
        with_sp = sp_row["with_sp"] or 0
        issue_count = sp_row["issues"]
    # Task-level values use the same duration fraction when the selection
    # contains a run dimension or a date/time range.
    eff = efficiency_breakdown(conn, filters=filters)

    last_cycle = conn.execute(
        "SELECT started_at, finished_at, sources_ok, sources_failed "
        "FROM poll_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    unpriced_where, unpriced_params = "mp.model IS NULL", []
    unpriced_where += _where_values("du.model", filters["models"], unpriced_params)
    unpriced_models = [r["model"] for r in conn.execute(
        "SELECT DISTINCT du.model FROM daily_usage du LEFT JOIN model_pricing mp "
        "ON mp.model = du.model AND mp.unpriced = 0 "
        "WHERE %s ORDER BY du.model" % unpriced_where,
        unpriced_params,
    )]

    return {
        **tokens,
        "total_tokens": sum(tokens.values()),
        "cost_usd": cost_usd,
        "cost_credits": cost_credits,
        "has_unpriced": has_unpriced,
        "estimated": estimated,
        "sp_estimated": needs_run_filter,
        "efficiency_estimated": needs_run_filter,
        "story_points": sp_sum,
        "issues": issue_count,
        "issues_with_sp": with_sp,
        "efficiency_tokens": eff["total_tokens"],
        "efficiency_sp": eff["story_points"],
        "tokens_per_sp": eff["tokens_per_sp"],
        "cost_per_sp": eff["cost_per_sp"],
        "weighted_efficiency": eff["weighted_efficiency"],
        "efficiency_cost_usd": eff["cost_usd"],
        "efficiency_cost_sp": eff["cost_story_points"],
        "efficiency_hours": eff["active_hours"],
        "efficiency_has_unpriced": eff["has_unpriced"],
        "unpriced_models": unpriced_models,
        "last_cycle": dict(last_cycle) if last_cycle else None,
    }


def meta(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Dropdown/filter data for the frontend."""
    span = conn.execute("SELECT MIN(date), MAX(date) FROM daily_usage").fetchone()
    return {
        "projects": [dict(r) for r in conn.execute(
            "SELECT id, title, status FROM projects ORDER BY title"
        )],
        "agents": [dict(r) for r in conn.execute(
            "SELECT id, name, model, runtime_id FROM agents ORDER BY name"
        )],
        "models": [r[0] for r in conn.execute(
            "SELECT DISTINCT model FROM daily_usage ORDER BY model"
        )],
        "date_span": {"first": span[0], "last": span[1]},
    }
