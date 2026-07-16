"""Aggregation tests on the hand-checkable fixture DB (see conftest).

Every expected number below is derivable by hand from seed_aggregate_fixture:
daily rows 1M + 3M + 0.5M + 0.2M tokens; the shared (R2, m-shared) row splits
A2/A3 by run durations 3h/2h → 0.6/0.4; A3's day is 50/50 Alpha/Beta.
"""

import pytest

from aistat import aggregates as ag


# -- weights helper ------------------------------------------------------------


def test_weights_prefer_durations():
    w = ag._weights(["a", "b"], {"a": 3.0, "b": 1.0}, {"a": 1, "b": 99})
    assert w == {"a": 0.75, "b": 0.25}


def test_weights_fall_back_to_counts_then_equal():
    assert ag._weights(["a", "b"], {}, {"a": 1, "b": 3}) == {"a": 0.25, "b": 0.75}
    assert ag._weights(["a", "b"], {}, {}) == {"a": 0.5, "b": 0.5}


# -- daily series ----------------------------------------------------------------


def test_daily_by_model_is_exact(agg_conn):
    result = ag.daily_series(agg_conn, group="model")
    assert result["estimated"] is False
    by_key = {(r["date"], r["key"]): r for r in result["rows"]}
    assert by_key[("2026-01-01", "m-claude")]["total_tokens"] == 1_000_000
    assert by_key[("2026-01-01", "m-shared")]["cost_usd"] == 6.0
    assert by_key[("2026-01-02", "m-mystery")]["has_unpriced"] is True
    total = sum(r["total_tokens"] for r in result["rows"])
    assert total == 4_700_000


def test_daily_by_model_respects_period(agg_conn):
    result = ag.daily_series(agg_conn, group="model",
                             date_from="2026-01-02", date_to="2026-01-02")
    assert {r["date"] for r in result["rows"]} == {"2026-01-02"}
    assert sum(r["total_tokens"] for r in result["rows"]) == 700_000


def test_daily_by_agent_splits_shared_pair_by_duration(agg_conn):
    result = ag.daily_series(agg_conn, group="agent")
    assert result["estimated"] is True
    day1 = {r["key"]: r for r in result["rows"] if r["date"] == "2026-01-01"}
    assert day1["Solo Claude"]["total_tokens"] == 1_000_000
    assert day1["Solo Claude"]["estimated"] is False
    # A2 3h vs A3 2h on the shared pair → 60% / 40% of 3M tokens and $6.
    assert day1["Dev Shared"]["total_tokens"] == 1_800_000
    assert day1["Dev Shared"]["cost_usd"] == pytest.approx(3.6)
    assert day1["Dev Shared"]["estimated"] is True
    assert day1["QA Shared"]["total_tokens"] == 1_200_000
    # Unmapped (runtime, model) pairs land in the unattributed bucket.
    day2 = {r["key"]: r for r in result["rows"] if r["date"] == "2026-01-02"}
    assert day2["(не атрибутировано)"]["total_tokens"] == 700_000
    assert day2["(не атрибутировано)"]["has_unpriced"] is True


def test_daily_by_project_attributes_by_runs(agg_conn):
    result = ag.daily_series(agg_conn, group="project")
    day1 = {r["key"]: r for r in result["rows"] if r["date"] == "2026-01-01"}
    # Alpha: A1 1M + A2 1.8M + A3 0.6M; Beta: A3 0.6M.
    assert day1["Alpha"]["total_tokens"] == 3_400_000
    assert day1["Beta"]["total_tokens"] == 600_000


def test_daily_project_filter(agg_conn):
    result = ag.daily_series(agg_conn, group="model", project_id="P2")
    assert result["estimated"] is True
    assert sum(r["total_tokens"] for r in result["rows"]) == 600_000


def test_daily_rejects_unknown_group(agg_conn):
    with pytest.raises(ValueError):
        ag.daily_series(agg_conn, group="runtime")


def test_shares_conserve_totals(agg_conn):
    shares = ag.daily_shares(agg_conn)
    total = sum(sum(s[k] for k in ag.TOKEN_KINDS) for s in shares)
    assert total == pytest.approx(4_700_000)
    cost = sum(s["cost_usd"] or 0 for s in shares)
    assert cost == pytest.approx(7.5)


# -- agents -----------------------------------------------------------------------


def test_agent_totals(agg_conn):
    agents = {a["name"]: a for a in ag.agent_totals(agg_conn)}
    assert agents["Dev Shared"]["total_tokens"] == 1_800_000
    assert agents["Dev Shared"]["estimated"] is True
    assert agents["Dev Shared"]["runs"] == 2
    assert agents["Solo Claude"]["estimated"] is False
    assert agents["Solo Claude"]["cost_usd"] == pytest.approx(1.0)
    unattributed = agents["(не атрибутировано)"]
    assert unattributed["total_tokens"] == 700_000
    assert unattributed["has_unpriced"] is True


def test_agent_totals_project_filter(agg_conn):
    agents = {a["name"]: a for a in ag.agent_totals(agg_conn, project_id="P2")}
    # Only A3 worked on Beta: half of her 1.2M share.
    assert agents["QA Shared"]["total_tokens"] == 600_000
    assert "Dev Shared" not in agents


# -- projects and efficiency --------------------------------------------------------


def test_projects_overview_tokens_sp_statuses(agg_conn):
    projects = {p["title"]: p for p in ag.projects_overview(agg_conn)}
    alpha, beta = projects["Alpha"], projects["Beta"]
    assert alpha["total_tokens"] == 3500          # exact issue_usage sums
    assert alpha["statuses"] == {"done": 2}
    assert alpha["story_points"] == 5
    assert beta["total_tokens"] == 400
    assert beta["issues"] == 3 and beta["issues_with_usage"] == 2
    assert beta["story_points"] == 2              # SP=0 adds nothing, I4 adds 2


def test_projects_overview_cost_estimate(agg_conn):
    projects = {p["title"]: p for p in ag.projects_overview(agg_conn,
                                                            credits_per_usd=2.0)}
    # I1 ran 50/50 on m-claude/m-shared:
    #   0.5*(1000*1.0 + 500*0)/1e6 + 0.5*(1000*2.0 + 500*4.0)/1e6 = 0.0025
    # I2 is 100% m-shared: 2000*2.0/1e6 = 0.004
    assert projects["Alpha"]["cost_usd"] == pytest.approx(0.0065)
    assert projects["Alpha"]["cost_credits"] == pytest.approx(0.013)
    assert projects["Alpha"]["cost_estimated"] is True
    # I3 is 100% m-shared: 300*2.0/1e6; I5 has usage but no runs.
    assert projects["Beta"]["cost_usd"] == pytest.approx(0.0006)
    assert projects["Beta"]["cost_unattributed_issues"] == 1


def test_project_efficiency_excludes_missing_and_zero_sp(agg_conn):
    projects = {p["title"]: p for p in ag.projects_overview(agg_conn)}
    # Alpha: only I1 qualifies (I2 has no SP) → 1500 / 5.
    assert projects["Alpha"]["tokens_per_sp"] == pytest.approx(300.0)
    assert projects["Alpha"]["efficiency_issues"] == 1
    # Beta: I3 has SP=0 (excluded), I4 has SP but no usage → no metric, not 0.
    assert projects["Beta"]["tokens_per_sp"] is None
    assert projects["Beta"]["efficiency_issues"] == 0


def test_issue_efficiency_list(agg_conn):
    issues = ag.issue_efficiency(agg_conn)
    assert [i["identifier"] for i in issues] == ["T-1"]
    only = issues[0]
    assert only["tokens_per_sp"] == pytest.approx(300.0)
    assert only["agents"] == ["Dev Shared", "Solo Claude"]
    assert ag.issue_efficiency(agg_conn, project_id="P2") == []


# -- summary ---------------------------------------------------------------------


def test_summary_unfiltered(agg_conn):
    s = ag.summary(agg_conn)
    assert s["total_tokens"] == 4_700_000
    assert s["cost_usd"] == pytest.approx(7.5)     # NULL cost is not counted as 0
    assert s["has_unpriced"] is True
    assert s["unpriced_models"] == ["m-mystery"]
    assert s["estimated"] is False
    assert s["story_points"] == 7                  # 5 + 0 + 2
    assert s["issues"] == 5 and s["issues_with_sp"] == 3
    assert s["tokens_per_sp"] == pytest.approx(300.0)
    assert s["last_cycle"]["finished_at"] == "2026-01-02T00:00:30Z"


def test_summary_project_filter_is_estimated(agg_conn):
    s = ag.summary(agg_conn, project_id="P1")
    assert s["estimated"] is True
    assert s["total_tokens"] == 3_400_000
    # Alpha's cost share: $1 (A1) + $3.6 (A2) + $1.2 (half of A3's $2.4).
    assert s["cost_usd"] == pytest.approx(5.8)
    assert s["story_points"] == 5
    assert s["tokens_per_sp"] == pytest.approx(300.0)

    beta = ag.summary(agg_conn, project_id="P2")
    assert beta["total_tokens"] == 600_000
    assert beta["tokens_per_sp"] is None           # no SP>0 issue with usage


def test_summary_period_filter(agg_conn):
    s = ag.summary(agg_conn, date_from="2026-01-01", date_to="2026-01-01")
    assert s["total_tokens"] == 4_000_000
    assert s["has_unpriced"] is False


# -- efficiency breakdown (tokens / cost / weighted) -----------------------------


def test_efficiency_breakdown_tokens_cost_weighted(agg_conn):
    # Only I1 counts (SP>0 + usage). It ran on m-claude (A1) and m-shared (A2)
    # 1h each → 50/50. Tokens 1000 in / 500 out = 1500.
    #   cost = 0.5*(1000*1.0)/1e6 + 0.5*(1000*2.0 + 500*4.0)/1e6 = 0.0025 USD
    #   active hours = 1h + 1h = 2h
    eff = ag.efficiency_breakdown(agg_conn)
    assert eff["tokens_per_sp"] == pytest.approx(300.0)      # 1500 / 5
    assert eff["cost_usd"] == pytest.approx(0.0025)
    assert eff["cost_per_sp"] == pytest.approx(0.0005)       # 0.0025 / 5
    assert eff["active_hours"] == pytest.approx(2.0)
    # weighted = SP-weighted mean of cost/hours/sp = (0.0025/2) / 5
    assert eff["weighted_efficiency"] == pytest.approx(0.00025)
    assert eff["has_unpriced"] is False
    assert eff["cost_issues"] == 1


def test_efficiency_breakdown_per_model_cheapest_first(agg_conn):
    models = ag.efficiency_breakdown(agg_conn)["models"]
    assert [m["model"] for m in models] == ["m-claude", "m-shared"]  # cheaper first
    by_model = {m["model"]: m for m in models}
    # Each took half of I1: 2.5 SP, 750 tokens, 1h.
    assert by_model["m-claude"]["story_points"] == pytest.approx(2.5)
    assert by_model["m-claude"]["cost_per_sp"] == pytest.approx(0.0002)   # 0.0005/2.5
    assert by_model["m-claude"]["weighted_efficiency"] == pytest.approx(0.0002)
    assert by_model["m-shared"]["cost_per_sp"] == pytest.approx(0.0008)   # 0.002/2.5
    assert by_model["m-shared"]["tokens_per_sp"] == pytest.approx(300.0)


def test_efficiency_breakdown_project_filter(agg_conn):
    # P2 has no issue with SP>0 and usage → nothing to measure.
    empty = ag.efficiency_breakdown(agg_conn, project_id="P2")
    assert empty["models"] == []
    assert empty["cost_per_sp"] is None
    assert empty["weighted_efficiency"] is None
    assert empty["tokens_per_sp"] is None
    # P1 carries all of I1.
    assert ag.efficiency_breakdown(agg_conn, project_id="P1")["cost_per_sp"] == pytest.approx(0.0005)


def test_efficiency_breakdown_flags_unpriced_model(agg_conn):
    # An issue whose only run used an unpriced model surfaces the model with
    # no cost (never $0) and flips the unpriced flag; priced rows stay intact.
    now = "2026-01-02T00:00:00Z"
    agg_conn.executescript(f"""
    INSERT INTO agents (id, name, model, runtime_id, synced_at) VALUES
      ('A4', 'Mystery', 'm-mystery', 'R4', '{now}');
    INSERT INTO issues (id, identifier, title, status, project_id, story_points,
                        updated_at, synced_at) VALUES
      ('I6', 'T-6', 'unpriced model', 'done', 'P1', 4, '{now}', '{now}');
    INSERT INTO issue_usage (issue_id, task_count, total_input_tokens,
                             total_output_tokens, total_cache_read_tokens,
                             total_cache_write_tokens, synced_at) VALUES
      ('I6', 1, 900, 0, 0, 0, '{now}');
    INSERT INTO runs (id, issue_id, agent_id, runtime_id, status,
                      started_at, completed_at, synced_at) VALUES
      ('run6', 'I6', 'A4', 'R4', 'completed',
       '2026-01-01T10:00:00Z', '2026-01-01T11:00:00Z', '{now}');
    """)
    agg_conn.commit()
    eff = ag.efficiency_breakdown(agg_conn)
    assert eff["has_unpriced"] is True
    by_model = {m["model"]: m for m in eff["models"]}
    mystery = by_model["m-mystery"]
    assert mystery["cost_usd"] is None
    assert mystery["cost_per_sp"] is None
    assert mystery["weighted_efficiency"] is None
    assert mystery["has_unpriced"] is True
    # Priced cost is unchanged; cost_per_sp now spreads it over 5 + 4 SP.
    assert eff["cost_usd"] == pytest.approx(0.0025)
    assert eff["cost_per_sp"] == pytest.approx(0.0025 / 9)
    # Weighted efficiency ignores the unpriced (not fully priced) issue.
    assert eff["weighted_efficiency"] == pytest.approx(0.00025)


def test_summary_adds_cost_and_weighted_efficiency(agg_conn):
    s = ag.summary(agg_conn)
    assert s["cost_per_sp"] == pytest.approx(0.0005)
    assert s["weighted_efficiency"] == pytest.approx(0.00025)
    assert s["efficiency_hours"] == pytest.approx(2.0)
    assert s["efficiency_has_unpriced"] is False
    beta = ag.summary(agg_conn, project_id="P2")
    assert beta["cost_per_sp"] is None
    assert beta["weighted_efficiency"] is None


# -- Jira exclusion --------------------------------------------------------------


def _insert_jira_issue(conn):
    """A legacy Jira-imported issue in project Alpha: SP=8 and a usage row.
    If it were counted it would dominate every issue-based statistic."""
    now = "2026-01-02T00:00:00Z"
    conn.executescript(f"""
    INSERT INTO issues (id, identifier, title, status, project_id, story_points,
                        is_jira, jira_key, updated_at, synced_at) VALUES
      ('J1', 'FAN-999', 'legacy jira', 'done', 'P1', 8, 1, 'SCRUM-1078', '{now}', '{now}');
    INSERT INTO issue_usage (issue_id, task_count, total_input_tokens,
                             total_output_tokens, total_cache_read_tokens,
                             total_cache_write_tokens, synced_at) VALUES
      ('J1', 5, 999999, 0, 0, 0, '{now}');
    """)
    conn.commit()


def test_summary_excludes_jira_issues(agg_conn):
    before = ag.summary(agg_conn)
    _insert_jira_issue(agg_conn)
    after = ag.summary(agg_conn)
    # The Jira issue's SP=8 and 999999 tokens must not move any headline.
    assert after["issues"] == before["issues"]
    assert after["story_points"] == before["story_points"]
    assert after["issues_with_sp"] == before["issues_with_sp"]
    assert after["tokens_per_sp"] == before["tokens_per_sp"]


def test_projects_overview_excludes_jira_issues(agg_conn):
    _insert_jira_issue(agg_conn)
    alpha = {p["title"]: p for p in ag.projects_overview(agg_conn)}["Alpha"]
    assert alpha["issues"] == 2            # I1, I2 only — J1 excluded
    assert alpha["story_points"] == 5      # J1's 8 SP not added
    assert alpha["total_tokens"] == 3500   # J1's usage not added


def test_issue_efficiency_excludes_jira_issues(agg_conn):
    _insert_jira_issue(agg_conn)
    # Despite J1 having both SP and (huge) usage, it stays out of the metric.
    issues = ag.issue_efficiency(agg_conn)
    assert [i["identifier"] for i in issues] == ["T-1"]
