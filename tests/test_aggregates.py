"""Aggregation tests on the hand-checkable fixture DB (see conftest).

Every expected number below is derivable by hand from seed_aggregate_fixture:
daily rows 1M + 3M + 0.5M + 0.2M tokens; the shared (R2, m-shared) row splits
A2/A3 by run durations 3h/2h → 0.6/0.4; A3's day is 50/50 Alpha/Beta.
"""

import pytest

from aistat import aggregates as ag
from conftest import seed_model_less_fixture


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


def test_hour_range_uses_dated_run_intervals_without_renormalizing(agg_conn):
    """A partial day retains only the run-time share of that day's tokens.

    On 2026-01-01 the shared runtime has five dated active hours: A2 has
    10–11 and 12–14, A3 has 12–13 and 14–15. Selecting 10–11 therefore
    receives A2's 1/5 share (600k) rather than all 3M shared-model tokens.
    A1's single 10–11 run owns its entire 1M Claude daily row.
    """
    filters = ag.make_filters("2026-01-01T10:00Z", "2026-01-01T11:00Z")
    result = ag.daily_series(agg_conn, group="model", filters=filters)
    by_model = {r["key"]: r for r in result["rows"]}
    assert result["estimated"] is True
    assert by_model["m-claude"]["total_tokens"] == 1_000_000
    assert by_model["m-shared"]["total_tokens"] == 600_000
    assert by_model["m-shared"]["cost_usd"] == pytest.approx(1.2)
    assert sum(r["total_tokens"] for r in result["rows"]) == 1_600_000


def test_hour_range_combines_agent_model_and_project_filters(agg_conn):
    filters = ag.make_filters(
        "2026-01-01T10:00Z", "2026-01-01T11:00Z",
        project_ids=["P1"], agent_ids=["A2"], models=["m-shared"],
    )
    summary = ag.summary(agg_conn, filters=filters)
    assert summary["estimated"] is True
    assert summary["total_tokens"] == 600_000
    assert summary["cost_usd"] == pytest.approx(1.2)
    assert summary["story_points"] == pytest.approx(2.5)

    no_match = ag.make_filters(
        "2026-01-01T10:00Z", "2026-01-01T11:00Z",
        project_ids=["P2"], agent_ids=["A2"], models=["m-shared"],
    )
    assert ag.summary(agg_conn, filters=no_match)["total_tokens"] == 0


def test_hour_range_excludes_runs_without_complete_dated_interval(agg_conn):
    now = "2026-01-02T00:00:00Z"
    agg_conn.executescript(f"""
    INSERT INTO agents (id, name, model, runtime_id, synced_at) VALUES
      ('A5', 'Incomplete', 'm-claude', 'R1', '{now}');
    INSERT INTO runs (id, issue_id, agent_id, runtime_id, status,
                      started_at, completed_at, synced_at) VALUES
      ('run-incomplete', 'I1', 'A5', 'R1', 'completed',
       '2026-01-01T10:00:00Z', NULL, '{now}');
    """)
    agg_conn.commit()
    filters = ag.make_filters(
        "2026-01-01T10:00Z", "2026-01-01T11:00Z", agent_ids=["A5"]
    )
    assert ag.summary(agg_conn, filters=filters)["total_tokens"] == 0


def test_task_views_apply_dated_run_filter_share(agg_conn):
    filters = ag.make_filters(
        "2026-01-01T10:00Z", "2026-01-01T11:00Z", agent_ids=["A2"]
    )
    projects = {p["title"]: p for p in ag.projects_overview(agg_conn, filters=filters)}
    # I1 has two equal one-hour runs (A1 and A2), so A2's selected hour owns
    # half of its cumulative task usage and SP. I2 starts at noon and is out.
    assert projects["Alpha"]["total_tokens"] == pytest.approx(750)
    assert projects["Alpha"]["story_points"] == pytest.approx(2.5)
    issue = ag.issue_efficiency(agg_conn, filters=filters)
    assert issue[0]["identifier"] == "T-1"
    assert issue[0]["estimated"] is True
    assert issue[0]["total_tokens"] == 750
    assert issue[0]["story_points"] == pytest.approx(2.5)


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


def test_daily_rows_carry_stable_typed_id(agg_conn):
    # FAN-1237: every daily row exposes a stable identity ``id`` separate from
    # the display ``key``, so the frontend can color a series by typed identity
    # instead of its position. For models identity == label; for agents and
    # projects it is the stable id, and the unattributed bucket is id=None.
    models = ag.daily_series(agg_conn, group="model")["rows"]
    assert all(r["id"] == r["key"] for r in models)
    assert {r["id"] for r in models} == {"m-claude", "m-shared", "m-mystery"}

    agents = ag.daily_series(agg_conn, group="agent")["rows"]
    by_id = {r["id"]: r for r in agents}
    assert by_id["A1"]["key"] == "Solo Claude"   # stable id kept apart from name
    assert None in by_id                          # unattributed bucket
    assert by_id[None]["key"] == "(не атрибутировано)"

    projects = ag.daily_series(agg_conn, group="project")["rows"]
    labels = {r["id"]: r["key"] for r in projects}
    assert labels["P1"] == "Alpha" and labels["P2"] == "Beta"


def test_daily_project_filter(agg_conn):
    result = ag.daily_series(agg_conn, group="model", project_id="P2")
    assert result["estimated"] is True
    assert sum(r["total_tokens"] for r in result["rows"]) == 600_000


def test_daily_unique_agent_filter_is_exact(agg_conn):
    # FAN-1253: A1 alone owns (R1, m-claude), so a model series under an A1
    # filter is an exact attribution — the top-level flag must follow the rows.
    result = ag.daily_series(
        agg_conn, group="model", filters=ag.make_filters(agent_ids=["A1"])
    )
    assert result["estimated"] is False
    assert all(r["estimated"] is False for r in result["rows"])
    assert sum(r["total_tokens"] for r in result["rows"]) == 1_000_000

    # A shared-pair agent filter still splits by duration → estimated.
    shared = ag.daily_series(
        agg_conn, group="model", filters=ag.make_filters(agent_ids=["A2"])
    )
    assert shared["estimated"] is True


def test_daily_empty_partial_hour_keeps_estimated(agg_conn):
    # FAN-1253 re-QA: a partial-hour window with no overlapping runs yields no
    # rows, yet the slice is still an estimate — an empty result must not erase
    # the headline flag (summary reports the same slice as estimated).
    filters = ag.make_filters(
        date_from="2026-01-01T16:00Z", date_to="2026-01-01T17:00Z"
    )
    result = ag.daily_series(agg_conn, group="model", filters=filters)
    assert result["rows"] == []
    assert result["estimated"] is True
    assert ag.summary(agg_conn, filters=filters)["estimated"] is True


def _sql_ts(value):
    return "NULL" if value is None else f"'{value}'"


def _seed_incomplete_run(conn, started_at, completed_at):
    """A6 uniquely owns (R5, m-solo) and works issue I6 (project P1) on
    2026-01-05 with a single run whose timestamps are incomplete.

    R5/m-solo carries 100 tokens that day, so a date-only slice has no
    complete dated interval to allocate and must fall back to the run count —
    which still points at agent A6 and project P1 (FAN-1254).
    """
    now = "2026-01-05T00:00:00Z"
    conn.executescript(f"""
    INSERT INTO runtimes (id, name, provider, status, synced_at) VALUES
      ('R5', 'Solo RT', 'claude', 'online', '{now}');
    INSERT INTO agents (id, name, model, runtime_id, synced_at) VALUES
      ('A6', 'Legacy Solo', 'm-solo', 'R5', '{now}');
    INSERT INTO issues (id, identifier, title, status, project_id, story_points,
                        updated_at, synced_at) VALUES
      ('I6', 'T-6', 'incomplete run', 'done', 'P1', NULL, '{now}', '{now}');
    INSERT INTO daily_usage (runtime_id, model, date, input_tokens, output_tokens,
                             cache_read_tokens, cache_write_tokens,
                             cost_usd, cost_credits, cost_priced, synced_at) VALUES
      ('R5', 'm-solo', '2026-01-05', 100, 0, 0, 0, NULL, NULL, 0, '{now}');
    INSERT INTO runs (id, issue_id, agent_id, runtime_id, status,
                      started_at, completed_at, synced_at) VALUES
      ('run-legacy', 'I6', 'A6', 'R5', 'completed',
       {_sql_ts(started_at)}, {_sql_ts(completed_at)}, '{now}');
    """)
    conn.commit()


@pytest.mark.parametrize(
    "started_at, completed_at",
    [
        ("2026-01-05T10:00:00Z", None),   # missing end
        (None, "2026-01-05T10:00:00Z"),   # missing start
    ],
)
def test_date_only_incomplete_run_keeps_project_attribution(
    agg_conn, started_at, completed_at
):
    # FAN-1254: a date-only slice by agent keeps a run with an incomplete
    # timestamp; adding the project filter must not drop the same 100 tokens.
    # Before the fix the run carried a count but no selected duration, so the
    # fallback emitted project_id=None and the project filter erased it.
    _seed_incomplete_run(agg_conn, started_at, completed_at)

    agent_only = ag.make_filters(agent_ids=["A6"])
    combined = ag.make_filters(project_ids=["P1"], agent_ids=["A6"])
    assert ag.summary(agg_conn, filters=agent_only)["total_tokens"] == 100
    assert ag.summary(agg_conn, filters=combined)["total_tokens"] == 100
    p1_agents = {a["agent_id"]: a for a in ag.agent_totals(agg_conn, project_id="P1")}
    assert p1_agents["A6"]["total_tokens"] == 100

    a6 = [s for s in ag.daily_shares(agg_conn, filters=agent_only)
          if s["agent_id"] == "A6"]
    assert len(a6) == 1
    assert a6[0]["project_id"] == "P1"
    assert sum(a6[0][k] for k in ag.TOKEN_KINDS) == 100


def test_partial_hour_ignores_incomplete_run_without_inventing_duration(agg_conn):
    # FAN-1254: the count fallback is date-only. A partial-hour window must
    # still exclude a run with no complete dated interval rather than invent a
    # duration for it.
    _seed_incomplete_run(agg_conn, "2026-01-05T10:00:00Z", None)
    hour = ag.make_filters(
        "2026-01-05T10:00Z", "2026-01-05T11:00Z", agent_ids=["A6"]
    )
    assert ag.summary(agg_conn, filters=hour)["total_tokens"] == 0
    assert [s for s in ag.daily_shares(agg_conn, filters=hour)
            if s["agent_id"] == "A6"] == []


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


def test_agent_totals_project_filter_marks_unique_agent_estimated(agg_conn):
    # FAN-1253 re-QA: a project filter keeps only the project-attributed share
    # of each daily row, so even a unique-pair agent row is an estimate — the
    # marker must not be dropped just because the split happens to be 100%.
    filtered = {a["name"]: a for a in ag.agent_totals(agg_conn, project_id="P1")}
    assert filtered["Solo Claude"]["total_tokens"] == 1_000_000
    assert filtered["Solo Claude"]["estimated"] is True
    # Without the project axis the same unique agent stays exact.
    plain = {a["name"]: a for a in ag.agent_totals(agg_conn)}
    assert plain["Solo Claude"]["estimated"] is False


def test_agent_run_counts_follow_half_open_filtered_intervals(agg_conn):
    # A2's normal runs are 10:00-11:00 and 12:00-14:00. The first extra run
    # starts exactly at the upper bound and must not leak into the 10:00 hour;
    # the second crosses into the selected date and proves date-only ranges
    # use actual interval overlap rather than the run start date.
    now = "2026-01-02T00:00:00Z"
    agg_conn.executescript(f"""
    INSERT INTO runs (id, issue_id, agent_id, runtime_id, status,
                      started_at, completed_at, synced_at) VALUES
      ('run-boundary', 'I1', 'A2', 'R2', 'completed',
       '2026-01-01T11:00:00Z', '2026-01-01T12:00:00Z', '{now}'),
      ('run-cross-date', 'I1', 'A2', 'R2', 'completed',
       '2025-12-31T23:30:00Z', '2026-01-01T00:30:00Z', '{now}');
    """)
    agg_conn.commit()

    hour = ag.make_filters(
        "2026-01-01T10:00Z", "2026-01-01T11:00Z",
        project_ids=["P1"], agent_ids=["A2"], models=["m-shared"],
    )
    hourly_agents = {a["agent_id"]: a for a in ag.agent_totals(agg_conn, filters=hour)}
    assert hourly_agents["A2"]["runs"] == 1

    day = ag.make_filters("2026-01-01", "2026-01-01", agent_ids=["A2"])
    daily_agents = {a["agent_id"]: a for a in ag.agent_totals(agg_conn, filters=day)}
    assert daily_agents["A2"]["runs"] == 4


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


def test_projects_overview_filtered_cost_uses_matching_run_models(agg_conn):
    """FAN-1251: a run filter must price project cost from the matching runs'
    models — the same run-overlap set that feeds tokens, SP and
    model-efficiency — never the issue's lifetime model weights.

    I1 ran one hour each on A1/m-claude and A2/m-shared. Inside 10:00-11:00
    (which excludes I2's noon runs) each cut prices only its own run, so
    /api/projects and /api/model-efficiency report the same cost.
    """
    def alpha(**kw):
        f = ag.make_filters("2026-01-01T10:00Z", "2026-01-01T11:00Z", **kw)
        row = {p["title"]: p
               for p in ag.projects_overview(agg_conn, filters=f)}["Alpha"]
        return row, f

    # Canonical repro: only A2/m-shared selected → 750 tokens priced entirely
    # as m-shared ($0.002). The bug blended in the excluded m-claude run's
    # lifetime weight and returned $0.00125.
    row, f = alpha(project_ids=["P1"], agent_ids=["A2"], models=["m-shared"])
    assert row["total_tokens"] == pytest.approx(750)
    assert row["story_points"] == pytest.approx(2.5)
    assert row["cost_usd"] == pytest.approx(0.002)
    assert row["unpriced_tokens"] == 0
    assert row["cost_unattributed_issues"] == 0
    assert ag.efficiency_breakdown(agg_conn, filters=f)["cost_usd"] == pytest.approx(0.002)

    # The other agent's model priced alone: 500 input × m-claude $1/M =
    # $0.0005 — the weight follows the matching run, not the 50/50 lifetime.
    row, f = alpha(agent_ids=["A1"])
    assert row["cost_usd"] == pytest.approx(0.0005)
    assert ag.efficiency_breakdown(agg_conn, filters=f)["cost_usd"] == pytest.approx(0.0005)

    # Repeated agent dimension selecting both runs rebuilds the exact 50/50
    # split → I1's lifetime cost, still agreeing across the two views.
    row, f = alpha(agent_ids=["A1", "A2"])
    assert row["cost_usd"] == pytest.approx(0.0025)
    assert ag.efficiency_breakdown(agg_conn, filters=f)["cost_usd"] == pytest.approx(0.0025)


def test_projects_overview_partial_hour_overlap_scales_filtered_cost(agg_conn):
    # FAN-1251 partial-hour: 30 min of A2's hour on I1 → share 0.25, priced as
    # m-shared only → $0.001, identical in projects and model-efficiency.
    f = ag.make_filters("2026-01-01T10:00Z", "2026-01-01T10:30Z",
                        project_ids=["P1"], agent_ids=["A2"], models=["m-shared"])
    alpha = {p["title"]: p
             for p in ag.projects_overview(agg_conn, filters=f)}["Alpha"]
    assert alpha["total_tokens"] == pytest.approx(375)
    assert alpha["cost_usd"] == pytest.approx(0.001)
    assert ag.efficiency_breakdown(agg_conn, filters=f)["cost_usd"] == pytest.approx(0.001)


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


# -- chartable token efficiency -------------------------------------------------


def test_efficiency_chart_breakdown_allocates_agents_models_and_time(agg_conn):
    charts = ag.efficiency_chart_breakdown(agg_conn)
    agents = {row["key"]: row for row in charts["agents"]}
    models = {row["key"]: row for row in charts["models"]}

    # I1 ran for one hour on A1 and A2 simultaneously: each gets half of
    # its 1,500 tokens and 5 SP, with the same 300 tokens/SP efficiency.
    for key in ("A1", "A2"):
        assert agents[key]["total_tokens"] == 750
        assert agents[key]["story_points"] == pytest.approx(2.5)
        assert agents[key]["tokens_per_sp"] == pytest.approx(300)
        assert agents[key]["estimated"] is True
    for key in ("m-claude", "m-shared"):
        assert models[key]["total_tokens"] == 750
        assert models[key]["story_points"] == pytest.approx(2.5)
        assert models[key]["tokens_per_sp"] == pytest.approx(300)

    assert charts["time"]["granularity"] == "day"
    assert charts["time"]["rows"] == [{
        "key": "2026-01-01", "label": "2026-01-01",
        "total_tokens": 1500, "story_points": 5.0, "issues": 1,
        "tokens_per_sp": 300.0, "estimated": True,
    }]


def test_efficiency_chart_breakdown_uses_hour_buckets_and_fixed_denominator(agg_conn):
    filters = ag.make_filters("2026-01-01T10:00Z", "2026-01-01T10:30Z")
    charts = ag.efficiency_chart_breakdown(agg_conn, filters=filters)
    assert charts["time"]["granularity"] == "hour"
    row = charts["time"]["rows"][0]
    # The selected half-hour is half of each one-hour run and one quarter of
    # I1's two-run duration, never re-expanded to the whole task.
    assert row["key"] == "2026-01-01T10:00Z"
    assert row["total_tokens"] == 750
    assert row["story_points"] == pytest.approx(2.5)
    assert row["tokens_per_sp"] == pytest.approx(300)
    assert {item["key"] for item in charts["agents"]} == {"A1", "A2"}


def test_efficiency_chart_breakdown_uses_days_for_long_windows(agg_conn):
    charts = ag.efficiency_chart_breakdown(
        agg_conn, filters=ag.make_filters("2026-01-01", "2026-01-04")
    )
    assert charts["time"]["granularity"] == "day"
    assert [row["key"] for row in charts["time"]["rows"]] == [
        "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
    ]
    assert charts["time"]["rows"][0]["tokens_per_sp"] == pytest.approx(300)
    assert all(row["tokens_per_sp"] is None for row in charts["time"]["rows"][1:])


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


def test_summary_estimation_metadata_per_field(agg_conn):
    # Unfiltered: everything is exact — no false ≈ on task-level values.
    s = ag.summary(agg_conn)
    assert s["estimated"] is False
    assert s["sp_estimated"] is False
    assert s["efficiency_estimated"] is False

    # Model-only (FAN-1241 repro): whole-day model tokens stay exact, but SP
    # and tokens/SP are run-duration attributions and carry their own flags.
    m = ag.summary(agg_conn, filters=ag.make_filters(models=["m-shared"]))
    assert m["estimated"] is False
    assert m["sp_estimated"] is True
    assert m["efficiency_estimated"] is True
    assert m["total_tokens"] == 3_000_000
    assert m["story_points"] == pytest.approx(2.5)  # I1: 5 SP × 1h/2h shared
    assert m["tokens_per_sp"] == pytest.approx(300.0)

    # Agent-only and (whole-day) period-only allocate SP the same way.
    a = ag.summary(agg_conn, filters=ag.make_filters(agent_ids=["A2"]))
    assert a["sp_estimated"] is True and a["efficiency_estimated"] is True
    p = ag.summary(agg_conn, date_from="2026-01-01", date_to="2026-01-01")
    assert p["estimated"] is False
    assert p["sp_estimated"] is True and p["efficiency_estimated"] is True

    # Project-only: SP belong to issues directly — exact even though the
    # token attribution is estimated.
    pr = ag.summary(agg_conn, project_id="P1")
    assert pr["estimated"] is True
    assert pr["sp_estimated"] is False
    assert pr["efficiency_estimated"] is False


def test_summary_unique_agent_filter_keeps_tokens_exact(agg_conn):
    # FAN-1253: A1 alone owns (R1, m-claude); filtering by A1 is an exact token
    # attribution, so the token card must not be flagged estimated even though
    # task-level SP/efficiency stay run-duration estimates.
    a1 = ag.summary(agg_conn, filters=ag.make_filters(agent_ids=["A1"]))
    assert a1["estimated"] is False
    assert a1["total_tokens"] == 1_000_000
    assert a1["cost_usd"] == pytest.approx(1.0)
    assert a1["sp_estimated"] is True and a1["efficiency_estimated"] is True

    # A shared pair stays estimated, and adding a project axis is never exact.
    a2 = ag.summary(agg_conn, filters=ag.make_filters(agent_ids=["A2"]))
    assert a2["estimated"] is True
    a1_p1 = ag.summary(
        agg_conn, filters=ag.make_filters(agent_ids=["A1"], project_ids=["P1"])
    )
    assert a1_p1["estimated"] is True
    assert a1_p1["total_tokens"] == 1_000_000


def test_issue_efficiency_model_filter_marks_estimated(agg_conn):
    rows = ag.issue_efficiency(agg_conn, filters=ag.make_filters(models=["m-shared"]))
    assert [r["identifier"] for r in rows] == ["T-1"]
    assert rows[0]["estimated"] is True
    assert rows[0]["story_points"] == pytest.approx(2.5)
    assert rows[0]["tokens_per_sp"] == pytest.approx(300.0)
    # Unfiltered rows are exact task-level facts — never marked estimated.
    assert all(r["estimated"] is False for r in ag.issue_efficiency(agg_conn))


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


def test_efficiency_breakdown_filters_use_one_run_overlap_set(agg_conn):
    # FAN-1244 repro. agent=A2 must count only A2's one-hour m-shared run on
    # I1: tokens 750, SP 2.5, cost (500 in × $2 + 250 out × $4)/1e6 = 0.002,
    # and active hours equal that selected hour — never lifetime × share.
    eff = ag.efficiency_breakdown(agg_conn, filters=ag.make_filters(agent_ids=["A2"]))
    assert [m["model"] for m in eff["models"]] == ["m-shared"]  # no m-claude
    assert eff["cost_story_points"] == pytest.approx(2.5)
    assert eff["cost_usd"] == pytest.approx(0.002)
    assert eff["active_hours"] == pytest.approx(1.0)
    assert eff["cost_per_sp"] == pytest.approx(0.0008)
    assert eff["weighted_efficiency"] == pytest.approx(0.0008)

    # Combined half-hour: 30 selected minutes of A2 → share 0.25 for tokens
    # and SP, but hours are the actual 0.5h overlap, applied exactly once.
    combined = ag.efficiency_breakdown(agg_conn, filters=ag.make_filters(
        "2026-01-01T10:00Z", "2026-01-01T10:30Z", ["P1"], ["A2"], ["m-shared"]
    ))
    assert [m["model"] for m in combined["models"]] == ["m-shared"]
    assert combined["cost_story_points"] == pytest.approx(1.25)
    assert combined["cost_usd"] == pytest.approx(0.001)
    assert combined["active_hours"] == pytest.approx(0.5)
    assert combined["cost_per_sp"] == pytest.approx(0.0008)
    assert combined["weighted_efficiency"] == pytest.approx(0.0016)

    # Model-only and time-only cuts use the same single selection.
    model_only = ag.efficiency_breakdown(
        agg_conn, filters=ag.make_filters(models=["m-shared"])
    )
    assert [m["model"] for m in model_only["models"]] == ["m-shared"]
    assert model_only["active_hours"] == pytest.approx(1.0)
    assert model_only["weighted_efficiency"] == pytest.approx(0.0008)
    time_only = ag.efficiency_breakdown(agg_conn, filters=ag.make_filters(
        "2026-01-01T10:00Z", "2026-01-01T10:30Z"
    ))
    assert [m["model"] for m in time_only["models"]] == ["m-claude", "m-shared"]
    assert time_only["cost_usd"] == pytest.approx(0.00125)
    assert time_only["active_hours"] == pytest.approx(1.0)
    assert time_only["weighted_efficiency"] == pytest.approx(0.0005)


def test_efficiency_breakdown_asymmetric_overlap_by_model(agg_conn):
    # Models cover different parts of the window: m-claude 09:00–11:00 (1h of
    # 2h inside 10:00–11:30) vs m-shared 11:00–11:30 (its whole 0.5h inside).
    # Hours must be those real overlaps and the weights 2/3 vs 1/3 — lifetime
    # durations (2h vs 0.5h → 0.8/0.2) would attribute cost incorrectly.
    now = "2026-01-03T00:00:00Z"
    agg_conn.executescript(f"""
    INSERT INTO issues (id, identifier, title, status, project_id, story_points,
                        updated_at, synced_at) VALUES
      ('I7', 'T-7', 'asymmetric overlap', 'done', 'P1', 3, '{now}', '{now}');
    INSERT INTO issue_usage (issue_id, task_count, total_input_tokens,
                             total_output_tokens, total_cache_read_tokens,
                             total_cache_write_tokens, synced_at) VALUES
      ('I7', 1, 1000, 500, 0, 0, '{now}');
    INSERT INTO runs (id, issue_id, agent_id, runtime_id, status,
                      started_at, completed_at, synced_at) VALUES
      ('run7', 'I7', 'A1', 'R1', 'completed',
       '2026-01-03T09:00:00Z', '2026-01-03T11:00:00Z', '{now}'),
      ('run8', 'I7', 'A2', 'R2', 'completed',
       '2026-01-03T11:00:00Z', '2026-01-03T11:30:00Z', '{now}');
    """)
    agg_conn.commit()
    eff = ag.efficiency_breakdown(agg_conn, filters=ag.make_filters(
        "2026-01-03T10:00Z", "2026-01-03T11:30Z"
    ))
    # Selected 1.5h of the 2.5h total → share 0.6: SP 1.8, tokens 900.
    assert eff["cost_story_points"] == pytest.approx(1.8)
    assert eff["active_hours"] == pytest.approx(1.5)
    by_model = {m["model"]: m for m in eff["models"]}
    assert by_model["m-claude"]["active_hours"] == pytest.approx(1.0)
    assert by_model["m-shared"]["active_hours"] == pytest.approx(0.5)
    assert by_model["m-claude"]["story_points"] == pytest.approx(1.2)   # 1.8 × 2/3
    assert by_model["m-shared"]["story_points"] == pytest.approx(0.6)   # 1.8 × 1/3
    # m-claude: 600 in × 2/3 × $1/M; m-shared: (200 in × $2 + 100 out × $4)/1e6.
    assert by_model["m-claude"]["cost_usd"] == pytest.approx(0.0004)
    assert by_model["m-shared"]["cost_usd"] == pytest.approx(0.0008)
    assert eff["cost_usd"] == pytest.approx(0.0012)
    assert eff["weighted_efficiency"] == pytest.approx(0.0012 / 1.5 / 1.8)


def test_efficiency_breakdown_keeps_model_less_share(agg_conn):
    # FAN-1247: a selected run without model metadata keeps its own share as
    # an unpriced None-model row instead of being priced as a known model.
    seed_model_less_fixture(agg_conn)
    mixed = ag.efficiency_breakdown(agg_conn, filters=ag.make_filters(
        "2026-01-04", "2026-01-04", ["P3"]
    ))
    assert [m["model"] for m in mixed["models"]] == ["m-claude", None]
    by_model = {m["model"]: m for m in mixed["models"]}
    assert by_model["m-claude"]["total_tokens"] == 500
    assert by_model["m-claude"]["story_points"] == pytest.approx(2.0)
    assert by_model["m-claude"]["active_hours"] == pytest.approx(1.0)
    assert by_model["m-claude"]["cost_usd"] == pytest.approx(0.0005)
    assert by_model[None]["cost_usd"] is None
    assert by_model[None]["has_unpriced"] is True
    assert by_model[None]["active_hours"] == pytest.approx(1.0)
    assert mixed["cost_usd"] == pytest.approx(0.0005)
    assert mixed["active_hours"] == pytest.approx(2.0)
    assert mixed["cost_per_sp"] == pytest.approx(0.000125)
    assert mixed["unpriced_tokens"] == 500
    assert mixed["has_unpriced"] is True
    # A partially unpriced issue never yields a fully-priced weighted metric.
    assert mixed["weighted_efficiency"] is None

    # Half-covering window: both shares shrink together, hours applied once.
    partial = ag.efficiency_breakdown(agg_conn, filters=ag.make_filters(
        "2026-01-04T10:30Z", "2026-01-04T11:30Z", ["P3"]
    ))
    assert partial["active_hours"] == pytest.approx(1.0)
    assert partial["cost_usd"] == pytest.approx(0.00025)
    assert partial["unpriced_tokens"] == 250
    assert partial["has_unpriced"] is True

    # All-model-less selection: unpriced share survives, no invented money.
    null_only = ag.efficiency_breakdown(
        agg_conn, filters=ag.make_filters(agent_ids=["A5"])
    )
    assert [m["model"] for m in null_only["models"]] == [None]
    assert null_only["cost_per_sp"] is None
    assert null_only["weighted_efficiency"] is None
    assert null_only["active_hours"] == pytest.approx(1.0)
    assert null_only["unpriced_tokens"] == 500
    assert null_only["has_unpriced"] is True

    # Project-only (lifetime stats) agrees with the covering filtered window.
    exact = ag.efficiency_breakdown(agg_conn, project_id="P3")
    assert [m["model"] for m in exact["models"]] == ["m-claude", None]
    assert exact["cost_usd"] == pytest.approx(0.0005)
    assert exact["active_hours"] == pytest.approx(2.0)
    assert exact["cost_per_sp"] == pytest.approx(0.000125)
    assert exact["unpriced_tokens"] == 500
    assert exact["weighted_efficiency"] is None

    # Summary inherits the same fail-safe fields.
    s = ag.summary(agg_conn, filters=ag.make_filters(
        "2026-01-04", "2026-01-04", ["P3"]
    ))
    assert s["cost_per_sp"] == pytest.approx(0.000125)
    assert s["weighted_efficiency"] is None
    assert s["efficiency_hours"] == pytest.approx(2.0)
    assert s["efficiency_has_unpriced"] is True


def test_summary_agent_filter_uses_filtered_cost_and_hours(agg_conn):
    s = ag.summary(agg_conn, filters=ag.make_filters(agent_ids=["A2"]))
    assert s["cost_per_sp"] == pytest.approx(0.0008)
    assert s["weighted_efficiency"] == pytest.approx(0.0008)
    assert s["efficiency_hours"] == pytest.approx(1.0)
    assert s["efficiency_cost_usd"] == pytest.approx(0.002)
    assert s["efficiency_cost_sp"] == pytest.approx(2.5)


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
