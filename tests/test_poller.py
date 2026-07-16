"""Poller cycle against a stubbed CLI runner: idempotency and error handling."""

from aistat.cli import CliError
from aistat.config import Config
from aistat.poller import CycleResult, Poller
from conftest import load_fixture


def make_runner(fail_sources=()):
    """Stub runner serving the captured fixtures.

    fail_sources: command prefixes (space-joined) that should raise CliError.
    """

    def runner(args):
        key = " ".join(args)
        for prefix in fail_sources:
            if key.startswith(prefix):
                raise CliError(args, "stubbed failure: connection refused")
        if args[:2] == ["runtime", "list"]:
            return load_fixture("runtime_list.json")
        if args[:2] == ["runtime", "usage"]:
            fixture = load_fixture("runtime_usage.json")
            # fixture was captured for one runtime; retarget rows so every
            # runtime id gets plausible data
            for row in fixture:
                row["runtime_id"] = args[2]
            return fixture
        if args[:2] == ["runtime", "activity"]:
            return load_fixture("runtime_activity.json")
        if args[:2] == ["agent", "list"]:
            return load_fixture("agent_list.json")
        if args[:2] == ["agent", "tasks"]:
            return load_fixture("agent_tasks.json")
        if args[:2] == ["project", "list"]:
            return load_fixture("project_list.json")
        if args[:2] == ["issue", "list"]:
            page = load_fixture("issue_list_page.json")
            page["has_more"] = False  # single page per project in tests
            return page
        if args[:2] == ["issue", "usage"]:
            return load_fixture("issue_usage.json")
        if args[:2] == ["issue", "runs"]:
            return load_fixture("issue_runs.json")
        raise AssertionError(f"unexpected CLI call: {key}")

    return runner


def table_counts(conn):
    tables = ["runtimes", "agents", "projects", "issues", "daily_usage",
              "issue_usage", "runs", "runtime_activity"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def test_cycle_ingests_fixture_data(conn):
    poller = Poller(Config(), conn, runner=make_runner())
    result = poller.run_cycle()

    assert result.sources_failed == 0, result.errors
    counts = table_counts(conn)
    assert counts["runtimes"] == 3
    assert counts["agents"] == 5
    assert counts["projects"] == 3
    assert counts["issues"] == 3  # one fixture page shared by all projects
    assert counts["daily_usage"] > 0
    assert counts["issue_usage"] == 3
    assert counts["runs"] > 0

    # story points landed from metadata
    sp = conn.execute(
        "SELECT story_points FROM issues WHERE identifier = 'FAN-1139'"
    ).fetchone()[0]
    assert sp == 8.0


def test_cycle_is_idempotent(conn):
    poller = Poller(Config(), conn, runner=make_runner())
    poller.run_cycle()
    first = table_counts(conn)
    poller.run_cycle()
    poller.run_cycle()
    assert table_counts(conn) == first


def test_detail_sync_skips_already_synced_issues(conn):
    poller = Poller(Config(), conn, runner=make_runner())
    result1 = poller.run_cycle()
    assert result1.detail_synced == 3
    # second cycle: updated_at unchanged -> no detail work
    result2 = poller.run_cycle()
    assert result2.detail_synced == 0
    # simulate a server-side update -> that issue becomes stale again
    conn.execute(
        "UPDATE issues SET updated_at = '2030-01-01T00:00:00Z' "
        "WHERE identifier = 'FAN-1139'"
    )
    conn.commit()
    pending = poller.pending_detail_issues(budget=10)
    assert [row["identifier"] for row in pending] == ["FAN-1139"]


def test_new_run_activity_marks_issue_details_stale(conn):
    """Usage can change without the issue record changing (a new run lands
    on the issue) — the composed staleness key must catch that."""
    poller = Poller(Config(), conn, runner=make_runner())
    poller.run_cycle()
    while poller.pending_detail_issues(budget=10):
        poller.sync_issue_details(CycleResult())  # converge composed keys
    conn.execute(
        "INSERT INTO runs (id, issue_id, agent_id, runtime_id, status, "
        "created_at, completed_at, synced_at) VALUES "
        "('late-run', (SELECT id FROM issues WHERE identifier = 'FAN-1139'), "
        "'agent-x', 'rt-x', 'completed', '2031-01-01T00:00:00Z', "
        "'2031-01-01T00:10:00Z', '2031-01-01T00:10:00Z')"
    )
    conn.commit()
    pending = poller.pending_detail_issues(budget=10)
    assert [row["identifier"] for row in pending] == ["FAN-1139"]


def test_issue_with_active_run_is_refreshed_every_cycle(conn):
    poller = Poller(Config(), conn, runner=make_runner())
    poller.run_cycle()
    while poller.pending_detail_issues(budget=10):
        poller.sync_issue_details(CycleResult())
    conn.execute(
        "INSERT INTO runs (id, issue_id, agent_id, runtime_id, status, "
        "created_at, started_at, synced_at) VALUES "
        "('live-run', (SELECT id FROM issues WHERE identifier = 'FAN-1139'), "
        "'agent-x', 'rt-x', 'running', '2031-01-01T00:00:00Z', "
        "'2031-01-01T00:00:01Z', '2031-01-01T00:00:01Z')"
    )
    conn.commit()
    # While the run is active the issue stays pending on every cycle …
    for _ in range(3):
        pending = poller.pending_detail_issues(budget=10)
        assert [row["identifier"] for row in pending] == ["FAN-1139"]
        poller.sync_issue_details(CycleResult())
    # … and settles once the run reaches a terminal state.
    conn.execute(
        "UPDATE runs SET status = 'completed', "
        "completed_at = '2031-01-01T01:00:00Z' WHERE id = 'live-run'"
    )
    conn.commit()
    poller.sync_issue_details(CycleResult())  # final refresh, stores new key
    assert poller.pending_detail_issues(budget=10) == []


def test_failed_source_recorded_without_breaking_cycle(conn):
    runner = make_runner(fail_sources=("runtime usage",))
    poller = Poller(Config(), conn, runner=runner)
    result = poller.run_cycle()

    # 3 runtimes -> 3 failed usage sources; everything else still ingested
    assert result.sources_failed == 3
    counts = table_counts(conn)
    assert counts["daily_usage"] == 0  # no zeros faked in place of data
    assert counts["agents"] == 5
    assert counts["issues"] == 3

    failing = conn.execute(
        "SELECT source, last_error FROM sync_state WHERE ok = 0"
    ).fetchall()
    assert len(failing) == 3
    assert all("connection refused" in row["last_error"] for row in failing)

    # recovery: next healthy cycle clears the error state
    poller_ok = Poller(Config(), conn, runner=make_runner())
    result2 = poller_ok.run_cycle()
    assert result2.sources_failed == 0
    assert conn.execute("SELECT COUNT(*) FROM sync_state WHERE ok = 0").fetchone()[0] == 0
    assert table_counts(conn)["daily_usage"] > 0


def test_detail_sync_failure_leaves_issue_pending(conn):
    runner = make_runner(fail_sources=("issue usage",))
    poller = Poller(Config(), conn, runner=runner)
    result = poller.run_cycle()
    assert result.detail_failed == 3
    assert "issue_details" in " ".join(result.errors)
    # issues remain pending for retry
    assert len(poller.pending_detail_issues(budget=10)) == 3
    row = conn.execute(
        "SELECT ok, last_error FROM sync_state WHERE source = 'issue_details'"
    ).fetchone()
    assert row["ok"] == 0
    assert "3 of 3" in row["last_error"]


def test_live_beat_lands_before_slow_phase(conn):
    """FAN-1147: SSE clients must be woken as soon as live data (daily usage,
    pricing) is committed — before issue lists and the detail backfill."""
    inner = make_runner()
    seen = {}

    def runner(args):
        if args[:2] == ["issue", "list"] and "beat" not in seen:
            row = conn.execute("SELECT seq, phase FROM sync_beats").fetchone()
            seen["beat"] = (row["seq"], row["phase"]) if row else None
        return inner(args)

    poller = Poller(Config(), conn, runner=runner)
    poller.run_cycle()

    assert seen["beat"] == (1, "live")
    final = conn.execute("SELECT seq, phase FROM sync_beats").fetchone()
    assert (final["seq"], final["phase"]) == (2, "cycle")


def test_detail_backfill_deferred_past_deadline_still_progresses(conn):
    """FAN-1147: a large detail backlog must not stretch the tick — but every
    cycle still syncs at least one issue, so the backlog always drains."""
    poller = Poller(Config(), conn, runner=make_runner(), clock=lambda: 100.0)
    result = poller.run_cycle(deadline=0.0)  # deadline already passed

    assert result.detail_synced == 1
    assert result.detail_deferred == 2
    assert result.sources_failed == 0  # deferral is scheduling, not an error
    assert len(poller.pending_detail_issues(budget=10)) == 2

    poller.run_cycle(deadline=0.0)
    poller.run_cycle(deadline=0.0)
    assert poller.pending_detail_issues(budget=10) == []


def test_detail_backfill_unclamped_without_deadline(conn):
    poller = Poller(Config(), conn, runner=make_runner(), clock=lambda: 100.0)
    result = poller.run_cycle()  # --once path: no deadline
    assert result.detail_synced == 3
    assert result.detail_deferred == 0


def test_upsert_updates_changed_values(conn):
    poller = Poller(Config(), conn, runner=make_runner())
    poller.run_cycle()
    conn.execute("UPDATE daily_usage SET output_tokens = -1")
    conn.commit()
    poller.run_cycle()
    assert conn.execute(
        "SELECT COUNT(*) FROM daily_usage WHERE output_tokens = -1"
    ).fetchone()[0] == 0
