"""Health snapshot shape and error surfacing."""

from aistat.config import Config
from aistat.health import snapshot
from aistat.poller import Poller
from test_poller import make_runner


def test_snapshot_on_empty_db(conn):
    snap = snapshot(conn)
    assert snap["status"] == "empty"
    assert snap["row_counts"]["issues"] == 0
    assert snap["last_cycle"] is None
    assert snap["failing_sources"] == []


def test_snapshot_after_healthy_cycle(conn):
    Poller(Config(), conn, runner=make_runner()).run_cycle()
    snap = snapshot(conn)
    assert snap["status"] == "ok"
    assert snap["row_counts"]["agents"] == 5
    assert snap["last_cycle"]["sources_failed"] == 0
    assert snap["daily_usage_span"]["distinct_days"] >= 1
    assert snap["issues_pending_details"] == 0


def test_snapshot_surfaces_failures(conn):
    Poller(Config(), conn, runner=make_runner(fail_sources=("agent tasks",))).run_cycle()
    snap = snapshot(conn)
    assert snap["status"] == "degraded"
    assert len(snap["failing_sources"]) == 5  # one per agent
    assert all("stubbed failure" in s["last_error"] for s in snap["failing_sources"])
