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

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

TOKEN_KINDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# Run durations come out of SQLite's julianday() in days; efficiency reports
# money-per-hour, so durations are converted to hours with this factor.
HOURS_PER_DAY = 24.0


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


# -- attribution of daily_usage rows to agents and projects -------------------


def _run_stats(conn: sqlite3.Connection) -> Tuple[Dict, Dict]:
    """Per (agent_id, date): run duration/count totals and per-project splits.

    A run's date is the date of its start (runs spanning midnight count
    toward the start date — a documented approximation). Duration is
    started_at→completed_at in days (julianday), missing timestamps → 0.
    """
    by_agent_date: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_agent_date_project: Dict[Tuple[str, str], Dict[Optional[str], Dict[str, Any]]] = {}
    rows = conn.execute(
        """
        SELECT r.agent_id,
               substr(COALESCE(r.started_at, r.created_at), 1, 10) AS date,
               i.project_id,
               COUNT(*) AS n,
               SUM(MAX(COALESCE(julianday(r.completed_at) - julianday(r.started_at), 0), 0)) AS dur
        FROM runs r
        LEFT JOIN issues i ON i.id = r.issue_id
        GROUP BY r.agent_id, date, i.project_id
        """
    )
    for row in rows:
        key = (row["agent_id"], row["date"])
        agg = by_agent_date.setdefault(key, {"dur": 0.0, "n": 0})
        agg["dur"] += row["dur"] or 0.0
        agg["n"] += row["n"]
        by_agent_date_project.setdefault(key, {})[row["project_id"]] = {
            "dur": row["dur"] or 0.0, "n": row["n"],
        }
    return by_agent_date, by_agent_date_project


def daily_shares(conn: sqlite3.Connection, date_from: Optional[str] = None,
                 date_to: Optional[str] = None) -> List[Dict[str, Any]]:
    """Split every daily_usage row down to (date, model, agent, project).

    Returns one share per (date, runtime, model, agent, project) with
    fractional token counts and cost, plus estimation flags:
      - ``agent_estimated``: the agent split was ambiguous (shared
        (runtime, model) pair or no matching agent at all);
      - project attribution is always an estimate; ``project_id`` is None
        when the agent had no runs that day to point at a project.
    Cost fields are None for unpriced models (never 0).
    """
    agents = conn.execute(
        "SELECT id, name, model, runtime_id FROM agents"
    ).fetchall()
    pair_agents: Dict[Tuple[str, str], List[str]] = {}
    for a in agents:
        pair_agents.setdefault((a["runtime_id"], a["model"]), []).append(a["id"])

    by_agent_date, by_agent_date_project = _run_stats(conn)

    where, params = _date_clause("date", date_from, date_to)
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
            emit(row, None, None, 1.0, agent_estimated=True)
            continue
        ambiguous = len(candidates) > 1
        durations = {a: by_agent_date.get((a, row["date"]), {}).get("dur", 0.0)
                     for a in candidates}
        counts = {a: by_agent_date.get((a, row["date"]), {}).get("n", 0)
                  for a in candidates}
        agent_weights = _weights(candidates, durations, counts)
        for agent_id, agent_weight in agent_weights.items():
            projects = by_agent_date_project.get((agent_id, row["date"]), {})
            if not projects:
                emit(row, agent_id, None, agent_weight, ambiguous)
                continue
            project_keys = list(projects)
            project_weights = _weights(
                project_keys,
                {p: v["dur"] for p, v in projects.items()},
                {p: v["n"] for p, v in projects.items()},
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
        g["estimated"] = g["estimated"] or s["agent_estimated"]
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
                 project_id: Optional[str] = None) -> Dict[str, Any]:
    """Per-day token/cost series stacked by model, agent or project.

    ``group=model`` without a project filter reads daily_usage directly and
    is exact (matches ``multica runtime usage``). Grouping by agent/project
    or filtering by project goes through attribution and is marked estimated
    where the split was ambiguous.
    """
    if group not in ("model", "agent", "project"):
        raise ValueError(f"unsupported group: {group}")

    if group == "model" and project_id is None:
        where, params = _date_clause("date", date_from, date_to)
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
            unpriced = d.pop("unpriced_rows") > 0
            d["has_unpriced"] = unpriced
            d["estimated"] = False
            d["total_tokens"] = _total(d)
            series.append(d)
        return {"group": group, "estimated": False, "rows": series}

    shares = daily_shares(conn, date_from, date_to)
    if project_id is not None:
        shares = [s for s in shares if s["project_id"] == project_id]
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
        # Any project filter / non-model grouping relies on attribution.
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
                 project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Tokens / cost / credits per agent over the period (attribution-based)."""
    shares = daily_shares(conn, date_from, date_to)
    if project_id is not None:
        shares = [s for s in shares if s["project_id"] == project_id]
    grouped = _sum_shares(shares, lambda s: s["agent_id"])
    agents = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, model, runtime_id FROM agents"
    )}
    runtimes = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM runtimes")}
    run_where, run_params = _date_clause(
        "substr(COALESCE(r.started_at, r.created_at), 1, 10)", date_from, date_to
    )
    if project_id is not None:
        run_where += " AND i.project_id = ?"
        run_params.append(project_id)
    run_counts = {r["agent_id"]: r["n"] for r in conn.execute(
        f"""
        SELECT r.agent_id, COUNT(*) AS n FROM runs r
        LEFT JOIN issues i ON i.id = r.issue_id
        WHERE {run_where} GROUP BY r.agent_id
        """,
        run_params,
    )}
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


def _issue_model_weights(conn: sqlite3.Connection) -> Dict[str, Dict[str, float]]:
    """Per issue: weight of each model, from run durations (fallback counts)."""
    weights: Dict[str, Dict[str, float]] = {}
    for issue_id, models in _issue_model_stats(conn).items():
        keys = list(models)
        weights[issue_id] = _weights(
            keys,
            {m: v["dur"] for m, v in models.items()},
            {m: v["n"] for m, v in models.items()},
        )
    return weights


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
                      credits_per_usd: float = 1.0) -> List[Dict[str, Any]]:
    """Per project: exact tokens, estimated cost/credits, SP, statuses,
    efficiency (tokens per story point over issues with SP and usage)."""
    rates = _pricing_rates(conn)
    model_weights = _issue_model_weights(conn)

    projects = [dict(r) for r in conn.execute(
        "SELECT id, title, status FROM projects ORDER BY title"
    )]
    issues = conn.execute(
        """
        SELECT i.id, i.project_id, i.status, i.story_points,
               u.task_count, u.total_input_tokens, u.total_output_tokens,
               u.total_cache_read_tokens, u.total_cache_write_tokens,
               (u.issue_id IS NOT NULL) AS has_usage
        FROM issues i LEFT JOIN issue_usage u ON u.issue_id = i.id
        WHERE i.is_jira = 0
        """
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
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            if row["story_points"] is not None:
                sp_sum += row["story_points"]
                sp_issues += 1
            if not row["has_usage"]:
                continue
            with_usage += 1
            for kind in TOKEN_KINDS:
                tokens[kind] += row[f"total_{kind}"] or 0
            est = issue_cost_estimate(dict(row), model_weights.get(row["id"], {}), rates)
            if est["attributed"]:
                cost_usd += est["cost_usd"]
                unpriced_tokens += est["unpriced_tokens"]
            else:
                unattributed += 1
            # Efficiency counts only issues with story points > 0 AND usage;
            # issues without SP are excluded, never treated as SP=0.
            if row["story_points"] is not None and row["story_points"] > 0:
                eff_tokens += sum(row[f"total_{k}"] or 0 for k in TOKEN_KINDS)
                eff_sp += row["story_points"]
                eff_issues += 1
        total_tokens = sum(tokens.values())
        out.append({
            "project_id": project["id"],
            "title": project["title"],
            "project_status": project["status"],
            "issues": len(rows),
            "issues_with_usage": with_usage,
            "statuses": statuses,
            **tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "cost_credits": cost_usd * credits_per_usd,
            "cost_estimated": True,
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
                     limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Per-issue efficiency: total tokens ÷ story points, worst first.

    Only issues with story points > 0 and a synced usage row participate;
    issues without story points are excluded from the metric (never SP=0).
    """
    params: List[Any] = []
    where = "i.is_jira = 0 AND i.story_points IS NOT NULL AND i.story_points > 0"
    if project_id is not None:
        where += " AND i.project_id = ?"
        params.append(project_id)
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
        d["issue_id"] = issue_id
        d["agents"] = agents_by_issue.get(issue_id, [])
        d["tokens_per_sp"] = d["total_tokens"] / d["story_points"]
        out.append(d)
    if limit is not None:
        out = out[:limit]
    return out


# -- efficiency (tokens / cost / weighted) ----------------------------------------


def efficiency_breakdown(conn: sqlite3.Connection,
                         project_id: Optional[str] = None) -> Dict[str, Any]:
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

    The per-model rows carry the same three metrics for one model's share of
    the counted issues (story points and tokens split by run duration, cost by
    the model's official rate), sorted cheapest cost-per-SP first so it is
    obvious which model delivers a story point for less.
    """
    rates = _pricing_rates(conn)
    stats = _issue_model_stats(conn)

    where = ("i.is_jira = 0 AND i.story_points IS NOT NULL "
             "AND i.story_points > 0")
    params: List[Any] = []
    if project_id is not None:
        where += " AND i.project_id = ?"
        params.append(project_id)
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
        sp = row["story_points"]
        sp_sum += sp
        for kind in TOKEN_KINDS:
            tokens[kind] += row[f"total_{kind}"] or 0
        issue_models = stats.get(row["id"])
        if not issue_models:
            continue  # usage but no runs → not attributable to a model
        cost_issues += 1
        cost_sp += sp
        keys = list(issue_models)
        weights = _weights(
            keys,
            {m: v["dur"] for m, v in issue_models.items()},
            {m: v["n"] for m, v in issue_models.items()},
        )
        issue_hours = sum(v["dur"] for v in issue_models.values()) * HOURS_PER_DAY
        active_hours += issue_hours
        issue_total = sum(int(row[f"total_{k}"] or 0) for k in TOKEN_KINDS)
        issue_cost = 0.0
        issue_priced = True
        for model, weight in weights.items():
            b = bucket(model)
            for kind in TOKEN_KINDS:
                b["tokens"][kind] += (row[f"total_{kind}"] or 0) * weight
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
                (row["total_input_tokens"] or 0) * weight * rate["input_rate"]
                + (row["total_output_tokens"] or 0) * weight * rate["output_rate"]
                + (row["total_cache_read_tokens"] or 0) * weight * rate["cache_read_rate"]
                + (row["total_cache_write_tokens"] or 0) * weight * rate["cache_write_rate"]
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
            credits_per_usd: float = 1.0) -> Dict[str, Any]:
    """Headline cards. Tokens/cost/credits honor the period filter (and the
    project filter via attribution — flagged estimated). Story points and
    efficiency are project-scope facts from issues/issue_usage; the period
    filter does not apply to them (documented in the README)."""
    if project_id is None:
        where, params = _date_clause("date", date_from, date_to)
        row = conn.execute(
            f"""
            SELECT SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(cost_usd) AS cost_usd,
                   SUM(cost_credits) AS cost_credits,
                   SUM(1 - cost_priced) AS unpriced_rows
            FROM daily_usage WHERE {where}
            """,
            params,
        ).fetchone()
        tokens = {k: row[k] or 0 for k in TOKEN_KINDS}
        cost_usd = row["cost_usd"]
        cost_credits = row["cost_credits"]
        has_unpriced = (row["unpriced_rows"] or 0) > 0
        estimated = False
    else:
        shares = [s for s in daily_shares(conn, date_from, date_to)
                  if s["project_id"] == project_id]
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
    if project_id is not None:
        sp_where, sp_params = "i.is_jira = 0 AND i.project_id = ?", [project_id]
    sp_row = conn.execute(
        f"""
        SELECT SUM(i.story_points) AS sp_sum,
               SUM(CASE WHEN i.story_points IS NOT NULL THEN 1 ELSE 0 END) AS with_sp,
               COUNT(*) AS issues
        FROM issues i WHERE {sp_where}
        """,
        sp_params,
    ).fetchone()
    eff = efficiency_breakdown(conn, project_id)

    last_cycle = conn.execute(
        "SELECT started_at, finished_at, sources_ok, sources_failed "
        "FROM poll_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    unpriced_models = [r["model"] for r in conn.execute(
        "SELECT DISTINCT du.model FROM daily_usage du LEFT JOIN model_pricing mp "
        "ON mp.model = du.model AND mp.unpriced = 0 "
        "WHERE mp.model IS NULL ORDER BY du.model"
    )]

    return {
        **tokens,
        "total_tokens": sum(tokens.values()),
        "cost_usd": cost_usd,
        "cost_credits": cost_credits,
        "has_unpriced": has_unpriced,
        "estimated": estimated,
        "story_points": sp_row["sp_sum"] or 0,
        "issues": sp_row["issues"],
        "issues_with_sp": sp_row["with_sp"] or 0,
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
