"""FAN-3306: flow metrics — cycle time, rework rate, idle-fleet share.

Covers the metric contracts end to end on hand-checkable fixtures: exact
median/nearest-rank p90, candidate de-duplication, FAILED/INCONCLUSIVE rework,
lane compatibility of capacity snapshots, pause exclusion, sparse/absent
coverage, idempotent migrations and legacy v5 startup, and the 100k-event
aggregation performance bound (AC6).
"""

import time
from datetime import datetime, timedelta

from aistat import flow_metrics, store
from aistat.db import (
    SCHEMA_VERSION,
    connect,
    init_db,
    schema_admission_error,
)

NOW = datetime(2026, 8, 24, 12, 0, 0)


def ts(**delta):
    """UTC timestamp `delta` before NOW, e.g. ts(days=1, hours=2)."""
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_issue(conn, issue_id, status="todo", project_id="p1", lane="dev_high",
              created_at=None, is_jira=0, dispatch_ready=0, story_points=None,
              **qa):
    conn.execute(
        """
        INSERT OR REPLACE INTO issues
            (id, status, project_id, dispatch_lane, dispatch_ready, is_jira,
             story_points, created_at, qa_verdict, qa_verdict_at, qa_candidate,
             qa_for_issue_id, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (issue_id, status, project_id, lane, dispatch_ready, is_jira,
         story_points, created_at or ts(days=80), qa.get("qa_verdict"),
         qa.get("qa_verdict_at"), qa.get("qa_candidate"),
         qa.get("qa_for_issue_id"), ts()),
    )


def add_event(conn, issue_id, status, observed_at, initial=0):
    conn.execute(
        "INSERT INTO issue_status_events (issue_id, status, observed_at, initial) "
        "VALUES (?, ?, ?, ?)",
        (issue_id, status, observed_at, initial),
    )


def add_ready_event(conn, issue_id, observed_at, initial=0):
    conn.execute(
        "INSERT INTO issue_readiness_events (issue_id, observed_at, initial) "
        "VALUES (?, ?, ?)", (issue_id, observed_at, initial),
    )


def add_lineage_event(conn, qa_issue_id, implementation_issue_id, candidate,
                      verdict, verdict_at, accepted_story_points=None,
                      initial=0):
    conn.execute(
        """
        INSERT INTO qa_lineage_events
            (qa_issue_id, implementation_issue_id, candidate, verdict, verdict_at,
             observed_at, initial, accepted_candidate, accepted_story_points)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (qa_issue_id, implementation_issue_id, candidate, verdict, verdict_at,
         verdict_at or ts(), initial,
         candidate if verdict == "PASSED" else None, accepted_story_points),
    )


def add_lifecycle(conn, issue_id, started_at, done_at, **issue_kwargs):
    """One measured issue: in_progress at `started_at`, done at `done_at`."""
    add_issue(conn, issue_id, status="done", **issue_kwargs)
    add_event(conn, issue_id, "in_progress", started_at)
    if done_at is not None:
        add_event(conn, issue_id, "done", done_at)


def add_snapshot(conn, at, starved=0, paused=0, pause_observed=1, idle=0,
                 eligible=1, ready=0, lane="dev_high"):
    conn.execute(
        "INSERT OR REPLACE INTO fleet_snapshots "
        "(at, eligible, idle, starved_idle, ready_cards, paused, "
        "pause_observed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (at, eligible, idle, starved, ready, paused, pause_observed),
    )
    conn.execute(
        "INSERT OR REPLACE INTO fleet_snapshot_lanes "
        "(at, lane, eligible, idle, starved_idle, ready_cards) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (at, lane, eligible, idle, starved, ready),
    )


# -- status event collection (store.upsert_issues) ---------------------------


def test_upsert_records_initial_and_transition_events_idempotently(conn):
    row = {"id": "i1", "status": "todo", "is_jira": 0,
           "updated_at": "2026-08-01T00:00:00Z"}
    store.upsert_issues(conn, [row], synced_at="2026-08-01T00:00:00Z")
    store.upsert_issues(conn, [row], synced_at="2026-08-01T00:01:00Z")  # no-op
    row["status"] = "in_progress"
    store.upsert_issues(conn, [row], synced_at="2026-08-01T00:02:00Z")
    events = conn.execute(
        "SELECT status, observed_at, initial FROM issue_status_events "
        "ORDER BY observed_at"
    ).fetchall()
    assert [tuple(e) for e in events] == [
        ("todo", "2026-08-01T00:00:00Z", 1),
        ("in_progress", "2026-08-01T00:02:00Z", 0),
    ]


def test_run_attribution_is_frozen_and_incomplete_values_are_unknown(conn):
    observed_at = ts(days=2)
    complete = {
        "id": "impl", "status": "todo", "story_points": 5,
        "attribution_schema_version": 1,
        "model_revision": "model@v1", "runtime_revision": "runtime@v1",
        "prompt_revision": "prompt@v1", "skills_revision": "skills@v1",
        "harness_revision": "harness@v1",
        "governance_bundle_revision": "bundle@v1",
    }
    store.upsert_issues(conn, [complete], synced_at=observed_at)
    store.upsert_runs(conn, [{"id": "run-1", "issue_id": "impl"}],
                      synced_at=observed_at)
    complete["prompt_revision"] = "prompt@v2"
    store.upsert_issues(conn, [complete], synced_at=ts(days=1))
    store.upsert_runs(conn, [{"id": "run-1", "issue_id": "impl"}],
                      synced_at=ts(days=1))

    incomplete = {"id": "incomplete", "status": "todo"}
    store.upsert_issues(conn, [incomplete], synced_at=observed_at)
    store.upsert_runs(conn, [{"id": "run-2", "issue_id": "incomplete"}],
                      synced_at=observed_at)

    rows = conn.execute(
        "SELECT run_id, prompt_revision, provenance_state "
        "FROM run_attribution_events ORDER BY run_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("run-1", "prompt@v1", "observed"),
        ("run-2", None, "unknown"),
    ]


def test_terminal_qa_lineage_snapshots_acceptance_once(conn):
    store.upsert_issues(conn, [{"id": "impl", "status": "in_review",
                                "story_points": 5}], synced_at=ts(days=2))
    verdict = {
        "id": "qa", "status": "done", "qa_for_issue_id": "impl",
        "qa_candidate": "sha-a", "qa_verdict": "PASSED",
        "qa_verdict_at": ts(days=1),
    }
    store.upsert_issues(conn, [verdict], synced_at=ts(days=1))
    verdict["qa_candidate"] = "sha-b"
    store.upsert_issues(conn, [verdict], synced_at=ts())

    saved = conn.execute(
        "SELECT candidate, verdict, accepted_candidate, accepted_story_points "
        "FROM qa_lineage_events"
    ).fetchone()
    assert tuple(saved) == ("sha-a", "PASSED", "sha-a", 5.0)


def test_ready_transition_is_append_only_and_not_an_initial_baseline(conn):
    row = {"id": "i1", "status": "todo", "dispatch_ready": 0}
    store.upsert_issues(conn, [row], synced_at=ts(days=2))
    row["dispatch_ready"] = 1
    store.upsert_issues(conn, [row], synced_at=ts(days=1))
    store.upsert_issues(conn, [row], synced_at=ts())

    rows = conn.execute(
        "SELECT observed_at, initial FROM issue_readiness_events"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(ts(days=1), 0)]


def test_frontier_reports_ready_pm_p95_and_waiting_age(conn):
    for hours in range(1, 21):
        issue_id = "ready-{}".format(hours)
        add_issue(conn, issue_id, status="todo", dispatch_ready=1,
                  story_points=1, created_at=ts(days=1, hours=hours))
        add_ready_event(conn, issue_id, ts(days=1))
    conn.commit()

    frontier = flow_metrics.flow(conn, days=7, now=NOW)["frontier"]

    assert frontier == {
        "ready": 20,
        "ready_story_points": 20.0,
        "pm_p95_seconds": 19 * 3600.0,
        "pm_measured": 20,
        "waiting_median_seconds": 86400.0,
        "waiting_p95_seconds": 86400.0,
        "waiting": 20,
    }


def test_lineage_metrics_use_immutable_events_and_explicit_denominators(conn):
    add_issue(conn, "impl-a", story_points=5)
    add_issue(conn, "impl-b", story_points=3)
    add_issue(conn, "impl-c")
    add_lineage_event(conn, "qa-a", "impl-a", "sha-a", "PASSED", ts(days=2),
                      accepted_story_points=5)
    add_lineage_event(conn, "qa-b1", "impl-b", "sha-b", "FAILED", ts(days=4))
    add_lineage_event(conn, "qa-b2", "impl-b", "sha-c", "PASSED", ts(days=1),
                      accepted_story_points=3)
    add_lineage_event(conn, "qa-c", "impl-c", "sha-d", "INCONCLUSIVE", None)
    conn.executemany(
        """
        INSERT INTO run_attribution_events
            (run_id, issue_id, provenance_state, observed_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("run-a", "impl-a", "observed", ts(days=2)),
            ("run-b", "impl-b", "unknown", ts(days=1)),
            ("run-legacy", "impl-c", "legacy_unknown", ts(days=1)),
        ],
    )
    conn.commit()

    lineage = flow_metrics.flow(conn, days=7, now=NOW)["lineage"]

    assert lineage == {
        "attempts": 2,
        "rework": 1,
        "accepted_candidates": 2,
        "accepted_story_points": 8.0,
        "first_pass_rate": 0.5,
        "first_passed": 1,
        "first_pass_denominator": 2,
        "unwindowed": 1,
        "unknown": 1,
        "legacy_unknown": 1,
    }


# -- cycle time ---------------------------------------------------------------


def test_cycle_time_exact_median_and_nearest_rank_p90(conn):
    """10 issues with durations 1..10 hours: median = (5+6)/2 = 5.5 h,
    nearest-rank p90 = value at rank ceil(0.9*10)=9 = 9 h."""
    for hours in range(1, 11):
        add_lifecycle(conn, "i%d" % hours,
                      ts(days=2, hours=hours), ts(days=2))
    conn.commit()
    out = flow_metrics.flow(conn, days=7, now=NOW)
    cycle = out["cycle_time"]
    assert cycle["measured"] == cycle["done_total"] == 10
    assert cycle["median_seconds"] == 5.5 * 3600
    assert cycle["p90_seconds"] == 9 * 3600
    assert cycle["groups"] == [{
        "project_id": "p1", "lane": "dev_high", "count": 10,
        "median_seconds": 5.5 * 3600, "p90_seconds": 9 * 3600,
    }]


def test_cycle_time_excludes_cancelled_censored_and_unobserved_start(conn):
    # measured: 2h
    add_lifecycle(conn, "ok", ts(days=1, hours=2), ts(days=1))
    # done in window but the in_progress transition was never observed
    add_issue(conn, "nostart", status="done")
    add_event(conn, "nostart", "done", ts(days=1))
    # cancelled card: excluded from percentiles, counted
    add_issue(conn, "cxl", status="cancelled")
    add_event(conn, "cxl", "in_progress", ts(days=3))
    add_event(conn, "cxl", "cancelled", ts(days=2))
    # censored: started, still open
    add_issue(conn, "open", status="in_progress")
    add_event(conn, "open", "in_progress", ts(days=4))
    conn.commit()
    cycle = flow_metrics.flow(conn, days=7, now=NOW)["cycle_time"]
    assert cycle["measured"] == 1
    assert cycle["median_seconds"] == 2 * 3600
    assert cycle["done_total"] == 2
    assert cycle["excluded_no_start"] == 1
    assert cycle["cancelled"] == 1
    assert cycle["open_censored"] == 1


def test_cycle_time_initial_event_trusted_only_after_collection_start(conn):
    """Collection starts with the baseline observation of `old` (initial=1).
    `old` predates collection, so its baseline in_progress is untrusted;
    `young` was created after collection began, so its initial event counts."""
    add_issue(conn, "old", status="done", created_at=ts(days=60))
    add_event(conn, "old", "in_progress", ts(days=5), initial=1)
    add_event(conn, "old", "done", ts(days=1))
    add_issue(conn, "young", status="done", created_at=ts(days=4))
    add_event(conn, "young", "in_progress", ts(days=3), initial=1)
    add_event(conn, "young", "done", ts(days=2))
    conn.commit()
    cycle = flow_metrics.flow(conn, days=7, now=NOW)["cycle_time"]
    assert cycle["done_total"] == 2
    assert cycle["measured"] == 1  # only `young`
    assert cycle["median_seconds"] == 24 * 3600.0
    assert cycle["excluded_no_start"] == 1  # `old`


def test_cycle_time_reopened_card_keeps_first_done(conn):
    add_lifecycle(conn, "re", ts(days=5), ts(days=4))
    add_event(conn, "re", "in_progress", ts(days=3))  # reopened
    add_event(conn, "re", "done", ts(days=1))
    conn.commit()
    cycle = flow_metrics.flow(conn, days=7, now=NOW)["cycle_time"]
    assert cycle["measured"] == 1
    assert cycle["median_seconds"] == 24 * 3600.0  # first done only


def test_cycle_time_unknown_lane_reported_and_filters_apply(conn):
    add_lifecycle(conn, "known", ts(days=1, hours=1), ts(days=1))
    add_lifecycle(conn, "legacy", ts(days=1, hours=3), ts(days=1),
                  lane=None, project_id="p2")
    conn.commit()
    out = flow_metrics.flow(conn, days=7, now=NOW)
    assert {g["lane"] for g in out["cycle_time"]["groups"]} == \
        {"dev_high", "unknown"}
    filtered = flow_metrics.flow(conn, days=7, now=NOW, lanes=["unknown"])
    assert filtered["cycle_time"]["measured"] == 1
    assert filtered["cycle_time"]["median_seconds"] == 3 * 3600
    by_project = flow_metrics.flow(conn, days=7, now=NOW, project_ids=["p1"])
    assert by_project["cycle_time"]["measured"] == 1
    assert by_project["cycle_time"]["median_seconds"] == 1 * 3600


# -- rework rate --------------------------------------------------------------


def qa_card(conn, card_id, verdict, candidate, impl="impl1", at=None):
    add_issue(conn, card_id, status="done", qa_verdict=verdict,
              qa_verdict_at=at, qa_candidate=candidate, qa_for_issue_id=impl)


def test_rework_rate_counts_failed_and_inconclusive_candidates(conn):
    add_issue(conn, "impl1", status="done")
    qa_card(conn, "q1", "FAILED", "sha-a", at=ts(days=3))
    qa_card(conn, "q2", "PASSED", "sha-b", at=ts(days=2))
    qa_card(conn, "q3", "INCONCLUSIVE", "sha-c", at=ts(days=1))
    conn.commit()
    rework = flow_metrics.flow(conn, days=7, now=NOW)["rework"]
    assert rework["reworked"] == 2
    assert rework["candidates"] == 3
    assert rework["rate"] == 2 / 3


def test_rework_retries_of_same_candidate_count_once(conn):
    """Same SHA reviewed twice: FAILED then FAILED = one reworked candidate;
    another SHA INCONCLUSIVE then PASSED on retry = not reworked."""
    add_issue(conn, "impl1", status="done")
    qa_card(conn, "q1", "FAILED", "sha-a", at=ts(days=5))
    qa_card(conn, "q2", "FAILED", "sha-a", at=ts(days=4))
    qa_card(conn, "q3", "INCONCLUSIVE", "sha-b", at=ts(days=3))
    qa_card(conn, "q4", "PASSED", "sha-b", at=ts(days=2))
    conn.commit()
    rework = flow_metrics.flow(conn, days=7, now=NOW)["rework"]
    assert rework["candidates"] == 2
    assert rework["reworked"] == 1
    assert rework["rate"] == 0.5


def test_rework_same_sha_for_different_impl_issues_are_distinct(conn):
    add_issue(conn, "implA", status="done")
    add_issue(conn, "implB", status="done")
    qa_card(conn, "q1", "FAILED", "sha-x", impl="implA", at=ts(days=2))
    qa_card(conn, "q2", "PASSED", "sha-x", impl="implB", at=ts(days=1))
    conn.commit()
    rework = flow_metrics.flow(conn, days=7, now=NOW)["rework"]
    assert rework["candidates"] == 2
    assert rework["reworked"] == 1


def test_rework_verdicts_without_time_go_to_coverage_not_windows(conn):
    add_issue(conn, "impl1", status="done")
    qa_card(conn, "q1", "FAILED", "sha-a", at=None)
    qa_card(conn, "q2", "PASSED", "sha-b", at=ts(days=1))
    conn.commit()
    rework = flow_metrics.flow(conn, days=7, now=NOW)["rework"]
    assert rework["unwindowed"] == 1
    assert rework["candidates"] == 1
    assert rework["rate"] == 0.0


def test_rework_weekly_buckets_use_verdict_utc_monday(conn):
    """NOW is Monday 2026-08-24; a verdict 1 day earlier (Sunday 23rd) falls
    in the week of Monday the 17th, one at NOW in the week of the 24th."""
    add_issue(conn, "impl1", status="done")
    qa_card(conn, "q1", "FAILED", "sha-a", at=ts(days=1))
    qa_card(conn, "q2", "PASSED", "sha-b", at=ts(days=0))
    conn.commit()
    weekly = flow_metrics.flow(conn, days=7, now=NOW)["rework"]["weekly"]
    assert weekly == [
        {"week_start": "2026-08-17", "reworked": 1, "candidates": 1},
        {"week_start": "2026-08-24", "reworked": 0, "candidates": 1},
    ]


# -- idle-fleet share ---------------------------------------------------------


def test_idle_share_time_weighted_with_pause_and_gap_exclusion(conn):
    """Chain: 60s starved + 60s not + 60s paused + a >900s gap + 60s starved.
    covered = 180s minus 60s paused = 120s... precisely: intervals
    [t0,t1]=60 starved, [t1,t2]=60 idle-free, [t2,t3]=60 paused, [t3,t4]=gap
    (1000s), [t4,t5]=60 starved. covered = 60+60+60 = 180, idle = 60+60 = 120,
    paused = 60, gap = 1000."""
    t = NOW - timedelta(hours=1)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    add_snapshot(conn, fmt(t), starved=1)
    add_snapshot(conn, fmt(t + timedelta(seconds=60)), starved=0)
    add_snapshot(conn, fmt(t + timedelta(seconds=120)), starved=1, paused=1)
    add_snapshot(conn, fmt(t + timedelta(seconds=180)), starved=1)
    add_snapshot(conn, fmt(t + timedelta(seconds=1180)), starved=1)
    add_snapshot(conn, fmt(t + timedelta(seconds=1240)), starved=0)
    conn.commit()
    idle = flow_metrics.flow(conn, days=7, now=NOW)["idle"]
    assert idle["covered_seconds"] == 180
    assert idle["idle_seconds"] == 120
    assert idle["paused_seconds"] == 60
    assert idle["gap_seconds"] == 1000
    assert idle["share"] == 120 / 180


def test_idle_share_lane_filter_uses_lane_rows(conn):
    t = NOW - timedelta(minutes=10)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, starved in enumerate((1, 1, 0)):
        at = fmt(t + timedelta(seconds=60 * i))
        add_snapshot(conn, at, starved=0, lane="dev_high")
        conn.execute(
            "INSERT OR REPLACE INTO fleet_snapshot_lanes "
            "(at, lane, eligible, idle, starved_idle, ready_cards) "
            "VALUES (?, 'qa_low', 1, 1, ?, 0)", (at, starved),
        )
    conn.commit()
    unfiltered = flow_metrics.flow(conn, days=7, now=NOW)["idle"]
    assert unfiltered["share"] == 0.0  # workspace rows never starved
    lane = flow_metrics.flow(conn, days=7, now=NOW, lanes=["qa_low"])["idle"]
    assert lane["share"] == 1.0  # both counted intervals starved in qa_low


def test_idle_share_project_filter_is_flagged_workspace_wide(conn):
    idle = flow_metrics.flow(conn, days=7, now=NOW,
                             project_ids=["p1"])["idle"]
    assert idle["workspace_wide"] is True
    assert idle["project_filter_ignored"] is True


# -- capacity snapshot collection (store.record_fleet_snapshot) ---------------


def seed_agent(conn, agent_id, lane="dev_high", borrow="none",
               role="implementation", archived=None):
    conn.execute(
        "INSERT INTO agents (id, description, archived_at, synced_at) "
        "VALUES (?, ?, ?, 'x')",
        (agent_id,
         "dispatch_profile=delivery_v2; role={}; native_lane={}; "
         "borrow_lanes={}; provider=claude".format(role, lane, borrow),
         archived),
    )


def test_snapshot_lane_compatibility_and_eligibility(conn):
    """Fleet: dev idle without work (starved), qa idle with a compatible ready
    card (not starved), one busy agent, one borrower fed via borrow lane, plus
    an archived and a RETIRED agent that are not eligible at all."""
    seed_agent(conn, "dev")                             # idle, no dev card
    seed_agent(conn, "qa", lane="qa_high", role="qa")   # idle, qa card ready
    seed_agent(conn, "busy", lane="dev_low")            # has a running run
    seed_agent(conn, "borrower", lane="dev_medium", borrow="qa_high")
    seed_agent(conn, "gone", archived="2026-08-01T00:00:00Z")
    conn.execute(
        "INSERT INTO agents (id, description, synced_at) VALUES "
        "('retired', 'RETIRED from delivery routing', 'x')")
    conn.execute(
        "INSERT INTO runs (id, agent_id, status, synced_at) "
        "VALUES ('r1', 'busy', 'running', 'x')")
    add_issue(conn, "ready-qa", status="todo", lane="qa_high",
              dispatch_ready=1)
    add_issue(conn, "not-ready", status="in_progress", lane="dev_high",
              dispatch_ready=1)  # already dispatched: not a waiting card
    conn.commit()
    result = store.record_fleet_snapshot(conn, at="2026-08-24T00:00:00Z")
    assert result == {"eligible": 4, "idle": 3, "starved_idle": 1,
                      "ready_cards": 1}
    lanes = {
        row["lane"]: dict(row) for row in conn.execute(
            "SELECT * FROM fleet_snapshot_lanes")
    }
    assert lanes["dev_high"]["starved_idle"] == 1     # `dev` has no card
    assert lanes["qa_high"]["starved_idle"] == 0      # `qa` has one
    assert lanes["qa_high"]["ready_cards"] == 1
    assert lanes["dev_medium"]["starved_idle"] == 0   # borrows qa_high
    assert "dev_low" in lanes and lanes["dev_low"]["idle"] == 0


# -- coverage, sparse data, legacy startup ------------------------------------


def test_empty_database_reports_no_coverage_and_nulls(conn):
    out = flow_metrics.flow(conn, days=30, now=NOW)
    assert out["coverage"] == {"events_start": None, "snapshots_start": None}
    assert out["cycle_time"]["median_seconds"] is None
    assert out["rework"]["rate"] is None
    assert out["idle"]["share"] is None
    assert out["schema_supported"] is True


def test_legacy_v5_database_serves_with_flow_reported_unsupported(tmp_path):
    """A v5 tenant snapshot (no flow tables/columns) must stay admissible and
    yield truthful N/A flow data instead of an error (legacy DB startup)."""
    path = tmp_path / "v5.db"
    conn = connect(path)
    init_db(conn)
    conn.executescript(
        """
        DROP TABLE issue_status_events;
        DROP TABLE fleet_snapshots;
        DROP TABLE fleet_snapshot_lanes;
        ALTER TABLE issues DROP COLUMN dispatch_lane;
        ALTER TABLE issues DROP COLUMN dispatch_ready;
        ALTER TABLE issues DROP COLUMN qa_verdict;
        ALTER TABLE issues DROP COLUMN qa_verdict_at;
        ALTER TABLE issues DROP COLUMN qa_candidate;
        ALTER TABLE issues DROP COLUMN qa_for_issue_id;
        PRAGMA user_version = 5;
        """
    )
    conn.commit()
    assert schema_admission_error(conn) is None
    out = flow_metrics.flow(conn, days=30, now=NOW)
    assert out["schema_supported"] is False
    assert out["cycle_time"]["median_seconds"] is None
    assert out["rework"]["rate"] is None
    assert out["idle"]["share"] is None
    conn.close()


def test_migration_v5_to_v7_is_idempotent_and_preserves_data(tmp_path):
    """init_db upgrades a populated v5 database in place; a second run is a
    no-op, and rolling user_version back to 5 stays servable (rollback path:
    the additive tables are ignored by pre-v6 code)."""
    path = tmp_path / "owner.db"
    conn = connect(path)
    init_db(conn)
    conn.executescript(
        """
        DROP TABLE issue_status_events;
        DROP TABLE fleet_snapshots;
        DROP TABLE fleet_snapshot_lanes;
        ALTER TABLE issues DROP COLUMN dispatch_lane;
        ALTER TABLE issues DROP COLUMN dispatch_ready;
        ALTER TABLE issues DROP COLUMN qa_verdict;
        ALTER TABLE issues DROP COLUMN qa_verdict_at;
        ALTER TABLE issues DROP COLUMN qa_candidate;
        ALTER TABLE issues DROP COLUMN qa_for_issue_id;
        INSERT INTO issues (id, status, is_jira, synced_at)
            VALUES ('kept', 'done', 0, 'x');
        PRAGMA user_version = 5;
        """
    )
    conn.commit()

    init_db(conn)  # upgrade
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert schema_admission_error(conn) is None
    kept = conn.execute("SELECT status FROM issues WHERE id='kept'").fetchone()
    assert kept["status"] == "done"
    columns = {r[1] for r in conn.execute("PRAGMA table_info(issues)")}
    assert {"dispatch_lane", "qa_verdict", "qa_candidate"} <= columns

    # A v6 database already has fleet rows, but cannot claim that its old
    # paused=0 values were observed resumes. The additive v7 marker defaults
    # to unavailable and survives a second initialization.
    conn.execute("ALTER TABLE fleet_snapshots DROP COLUMN pause_observed")
    conn.execute("PRAGMA user_version = 6")
    init_db(conn)
    snapshot_columns = {
        r[1] for r in conn.execute("PRAGMA table_info(fleet_snapshots)")
    }
    assert "pause_observed" in snapshot_columns

    init_db(conn)  # idempotent second run
    assert schema_admission_error(conn) is None

    conn.execute("PRAGMA user_version = 5")  # rollback evidence
    assert schema_admission_error(conn) is None
    conn.close()


def test_v7_to_v8_migration_keeps_raw_runs_and_marks_legacy_unknown(tmp_path):
    path = tmp_path / "v7.db"
    conn = connect(path)
    init_db(conn)
    conn.execute(
        "INSERT INTO runs (id, issue_id, status, synced_at) VALUES (?, ?, ?, ?)",
        ("legacy-run", "legacy-issue", "completed", "2026-08-20T00:00:00Z"),
    )
    conn.commit()
    before = [tuple(row) for row in conn.execute("SELECT * FROM runs")]
    conn.execute("PRAGMA user_version = 7")
    conn.commit()

    init_db(conn)

    assert SCHEMA_VERSION == 8
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert [tuple(row) for row in conn.execute("SELECT * FROM runs")] == before
    attribution = conn.execute(
        "SELECT run_id, provenance_state, model_revision, prompt_revision "
        "FROM run_attribution_events"
    ).fetchall()
    assert [tuple(row) for row in attribution] == [
        ("legacy-run", "legacy_unknown", None, None),
    ]

    init_db(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM run_attribution_events"
    ).fetchone()[0] == 1
    conn.close()


# -- performance (AC6) --------------------------------------------------------


def test_100k_event_aggregation_under_one_second(conn):
    """Deterministic fixture: 50 000 issues x 2 events = 100 000 status events
    (plus QA verdicts and snapshots); one flow() call must finish < 1 s and
    produce the exact expected counts (no per-issue queries)."""
    base = NOW - timedelta(days=89)
    issues, events = [], []
    for i in range(50000):
        started = base + timedelta(minutes=i)
        done = started + timedelta(hours=1)
        issues.append(("i%d" % i, "done", "p%d" % (i % 3),
                       "dev_high" if i % 2 else "dev_low", 0, 0,
                       started.strftime("%Y-%m-%dT%H:%M:%SZ"), "x"))
        events.append(("i%d" % i, "in_progress",
                       started.strftime("%Y-%m-%dT%H:%M:%SZ"), 0))
        events.append(("i%d" % i, "done",
                       done.strftime("%Y-%m-%dT%H:%M:%SZ"), 0))
    conn.executemany(
        "INSERT INTO issues (id, status, project_id, dispatch_lane, "
        "dispatch_ready, is_jira, created_at, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", issues)
    conn.executemany(
        "INSERT INTO issue_status_events (issue_id, status, observed_at, "
        "initial) VALUES (?, ?, ?, ?)", events)
    conn.commit()

    started_clock = time.perf_counter()
    out = flow_metrics.flow(conn, days=90, now=NOW)
    elapsed = time.perf_counter() - started_clock
    assert out["cycle_time"]["measured"] == 50000
    assert out["cycle_time"]["median_seconds"] == 3600.0
    assert elapsed < 1.0, "aggregation took %.3fs" % elapsed
