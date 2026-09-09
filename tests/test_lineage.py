"""FAN-3460: end-to-end lineage traces and pipeline SLOs.

Every fixture is a *replay*: synthetic Multica issue payloads are pushed
through the real ingest path (``normalize.normalize_issue`` +
``store.upsert_issues``) one poll cycle at a time, so the tests cover the
collector and the aggregation together. The five replays required by the
acceptance criteria are ``replay_success``, ``replay_rework``,
``replay_failed_qa``, ``replay_stale_deploy`` and ``replay_missing_lineage``.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import aistat.server as server_module
from aistat import lineage, normalize, store
from aistat.config import Config
from aistat.db import connect, init_db

NOW = datetime(2026, 8, 24, 12, 0, 0)


def ts(**delta):
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "lineage.db")
    init_db(connection)
    yield connection
    connection.close()


# -- replay helpers ------------------------------------------------------------


def issue_payload(issue_id, identifier, created_at=None, status="in_progress",
                  project_id="p1", lane="dev_medium", **metadata):
    return {
        "id": issue_id,
        "identifier": identifier,
        "status": status,
        "project_id": project_id,
        "created_at": created_at or ts(days=10),
        "updated_at": ts(days=1),
        "metadata": dict({"dispatch_lane": lane}, **metadata),
    }


def sync(conn, payloads, at):
    """One poll cycle: normalize and upsert exactly like the collector does."""
    store.upsert_issues(
        conn, [normalize.normalize_issue(p) for p in payloads], synced_at=at
    )
    conn.commit()


def add_run(conn, run_id, issue_id, created_at, completed_at=None):
    conn.execute(
        "INSERT OR REPLACE INTO runs "
        "(id, issue_id, status, created_at, dispatched_at, completed_at, synced_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, issue_id, "completed", created_at, created_at, completed_at,
         ts()),
    )
    conn.commit()


def add_ready(conn, issue_id, observed_at, initial=0):
    conn.execute(
        "INSERT OR REPLACE INTO issue_readiness_events "
        "(issue_id, observed_at, initial) VALUES (?, ?, ?)",
        (issue_id, observed_at, initial),
    )
    conn.commit()


def replay_success(conn):
    """Ready -> dispatched -> candidate -> QA PASSED -> integrated -> released."""
    add_ready(conn, "impl-1", ts(days=9))
    add_run(conn, "run-1", "impl-1", ts(days=9, minutes=-30), ts(days=8))
    sync(conn, [issue_payload(
        "impl-1", "FAN-1001", status="in_review",
        candidate_sha="a" * 40, qa_issue_id="qa-1",
        post_qa_integration_required=True,
    )], ts(days=8))
    sync(conn, [issue_payload(
        "qa-1", "FAN-1002", status="done", qa_for_issue_id="impl-1",
        qa_candidate_sha="a" * 40, qa_verdict="PASSED",
        qa_verdict_at=ts(days=7),
    )], ts(days=7))
    sync(conn, [issue_payload(
        "devops-1", "FAN-1003", status="done",
        implementation_issue_id="impl-1", integration_outcome="INTEGRATED",
        integration_sha="b" * 40, external_ci_status="success",
    )], ts(days=6))
    sync(conn, [issue_payload(
        "impl-1", "FAN-1001", status="done", candidate_sha="a" * 40,
        qa_issue_id="qa-1", post_qa_integration_required=True,
        release_version="0.4.0",
    )], ts(days=5))


def replay_rework(conn):
    """First QA attempt FAILED, second candidate PASSED and integrated."""
    add_ready(conn, "impl-2", ts(days=9))
    add_run(conn, "run-2", "impl-2", ts(days=9), ts(days=8))
    sync(conn, [issue_payload("impl-2", "FAN-2001", candidate_sha="c" * 40,
                              qa_issue_id="qa-2a",
                              post_qa_integration_required=True)],
         ts(days=8))
    sync(conn, [issue_payload("qa-2a", "FAN-2002", status="done",
                              qa_for_issue_id="impl-2",
                              qa_candidate_sha="c" * 40, qa_verdict="FAILED",
                              qa_verdict_at=ts(days=8))], ts(days=8))
    sync(conn, [issue_payload("impl-2", "FAN-2001", candidate_sha="d" * 40,
                              qa_issue_id="qa-2b",
                              post_qa_integration_required=True)],
         ts(days=7))
    sync(conn, [issue_payload("qa-2b", "FAN-2003", status="done",
                              qa_for_issue_id="impl-2",
                              qa_candidate_sha="d" * 40, qa_verdict="PASSED",
                              qa_verdict_at=ts(days=6))], ts(days=6))
    sync(conn, [issue_payload("devops-2", "FAN-2004", status="done",
                              implementation_issue_id="impl-2",
                              integration_outcome="INTEGRATED",
                              integration_sha="e" * 40,
                              external_ci_status="success")], ts(days=5))


def replay_failed_qa(conn):
    """QA FAILED and never passed: the integration gate stays closed."""
    add_ready(conn, "impl-3", ts(days=9))
    add_run(conn, "run-3", "impl-3", ts(days=9), ts(days=8))
    sync(conn, [issue_payload("impl-3", "FAN-3001", candidate_sha="f" * 40,
                              qa_issue_id="qa-3",
                              post_qa_integration_required=True)],
         ts(days=8))
    sync(conn, [issue_payload("qa-3", "FAN-3002", status="done",
                              qa_for_issue_id="impl-3",
                              qa_candidate_sha="f" * 40, qa_verdict="FAILED",
                              qa_verdict_at=ts(days=7))], ts(days=7))


def replay_stale_deploy(conn):
    """Integration observed at one SHA, metadata later restated at another."""
    add_ready(conn, "impl-4", ts(days=9))
    add_run(conn, "run-4", "impl-4", ts(days=9), ts(days=8))
    sync(conn, [issue_payload("impl-4", "FAN-4001", candidate_sha="1" * 40,
                              qa_issue_id="qa-4",
                              post_qa_integration_required=True)],
         ts(days=8))
    sync(conn, [issue_payload("qa-4", "FAN-4002", status="done",
                              qa_for_issue_id="impl-4",
                              qa_candidate_sha="1" * 40, qa_verdict="PASSED",
                              qa_verdict_at=ts(days=7))], ts(days=7))
    sync(conn, [issue_payload("impl-4", "FAN-4001", candidate_sha="1" * 40,
                              qa_issue_id="qa-4",
                              post_qa_integration_required=True,
                              integration_outcome="INTEGRATED",
                              integration_sha="2" * 40,
                              external_ci_status="failure")], ts(days=6))
    # A later cycle restates the integration SHA on the same card.
    sync(conn, [issue_payload("impl-4", "FAN-4001", candidate_sha="1" * 40,
                              qa_issue_id="qa-4",
                              post_qa_integration_required=True,
                              integration_outcome="INTEGRATED",
                              integration_sha="3" * 40,
                              external_ci_status="failure")], ts(days=2))


def replay_missing_lineage(conn):
    """A card that names a QA child which was never observed terminal."""
    add_ready(conn, "impl-5", ts(days=9))
    sync(conn, [issue_payload("impl-5", "FAN-5001", status="in_review",
                              qa_issue_id="qa-5",
                              post_qa_integration_required=True)],
         ts(days=8))


def stage(result, name):
    return next(s for s in result["stages"] if s["stage"] == name)


# -- trace ---------------------------------------------------------------------


def test_success_replay_reconstructs_the_whole_chain(conn):
    replay_success(conn)
    result = lineage.trace(conn, "impl-1")

    assert result["found"] is True
    assert result["correlation_id"] == "impl-1"
    assert result["complete"] is True
    assert result["gaps"] == []
    assert [s["status"] for s in result["stages"]] == ["observed"] * 6
    assert stage(result, "run")["run_ids"] == ["run-1"]
    assert stage(result, "candidate")["candidate_sha"] == "a" * 40
    assert stage(result, "qa")["accepted_candidate"] == "a" * 40
    integration = stage(result, "integration")
    assert integration["integration_issue_id"] == "devops-1"
    assert integration["integration_sha"] == "b" * 40
    assert integration["ci_status"] == "success"
    assert stage(result, "release")["release_version"] == "0.4.0"


def test_trace_resolves_from_a_qa_child_to_the_same_chain(conn):
    replay_success(conn)
    from_root = lineage.trace(conn, "impl-1")
    from_child = lineage.trace(conn, "qa-1")

    assert from_child["correlation_id"] == "impl-1"
    assert from_child["requested_issue_id"] == "qa-1"
    assert from_child["requested_is_root"] is False
    assert from_child["stages"] == from_root["stages"]


def test_trace_accepts_the_human_identifier(conn):
    replay_success(conn)
    assert lineage.trace(conn, "fan-1001")["correlation_id"] == "impl-1"
    assert lineage.trace(conn, "FAN-9999")["found"] is False


def test_rework_replay_keeps_both_qa_attempts_in_order(conn):
    replay_rework(conn)
    result = lineage.trace(conn, "impl-2")

    attempts = stage(result, "qa")["attempts"]
    assert [(a["identifier"], a["verdict"], a["candidate"]) for a in attempts] == [
        ("FAN-2002", "FAILED", "c" * 40),
        ("FAN-2003", "PASSED", "d" * 40),
    ]
    assert stage(result, "candidate")["reviewed_candidates"] == ["c" * 40,
                                                                 "d" * 40]
    assert stage(result, "qa")["accepted_candidate"] == "d" * 40
    assert stage(result, "integration")["status"] == "observed"


def test_failed_qa_replay_does_not_report_a_missing_integration(conn):
    replay_failed_qa(conn)
    result = lineage.trace(conn, "impl-3")

    assert stage(result, "qa")["accepted_candidate"] is None
    # The gate is closed on purpose: an absent integration is not a gap.
    assert stage(result, "integration")["status"] == "not_expected"
    assert stage(result, "release")["status"] == "not_expected"
    assert result["gaps"] == []


def test_stale_deploy_replay_reports_both_shas_without_choosing(conn):
    replay_stale_deploy(conn)
    result = lineage.trace(conn, "impl-4")

    integration = stage(result, "integration")
    assert integration["status"] == "stale"
    assert integration["integration_sha"] == "2" * 40         # first observed
    assert integration["mirrored_integration_sha"] == "3" * 40  # current metadata
    assert "integration" in result["gaps"]
    assert result["complete"] is False


def test_missing_lineage_replay_marks_the_absent_links(conn):
    replay_missing_lineage(conn)
    result = lineage.trace(conn, "impl-5")

    assert stage(result, "run")["status"] == "missing"
    assert stage(result, "candidate")["status"] == "missing"
    assert stage(result, "qa")["status"] == "missing"
    assert stage(result, "qa")["expected_qa_issue_id"] == "qa-5"
    assert sorted(result["gaps"]) == ["candidate", "qa", "run"]
    assert result["complete"] is False


def test_candidate_that_no_qa_event_reviewed_is_stale(conn):
    replay_success(conn)
    conn.execute("UPDATE issues SET candidate_sha = ? WHERE id = 'impl-1'",
                 ("9" * 40,))
    conn.commit()
    result = lineage.trace(conn, "impl-1")

    candidate = stage(result, "candidate")
    assert candidate["status"] == "stale"
    assert candidate["candidate_sha"] == "9" * 40
    assert candidate["reviewed_candidates"] == ["a" * 40]


def test_trace_requires_a_usable_correlation_id(conn):
    with pytest.raises(ValueError):
        lineage.trace(conn, "  ")
    with pytest.raises(ValueError):
        lineage.trace(conn, "F" * 200)


def test_trace_on_a_legacy_snapshot_reports_unsupported_schema(tmp_path):
    connection = connect(tmp_path / "legacy.db")
    init_db(connection)
    connection.execute("DROP TABLE lineage_stage_events")
    connection.commit()
    result = lineage.trace(connection, "impl-1")
    assert result == {"trace": "impl-1", "schema_supported": False,
                      "found": False, "stages": [], "gaps": [],
                      "complete": False}
    connection.close()


def test_collector_keeps_the_first_integration_observation_immutable(conn):
    replay_stale_deploy(conn)
    rows = conn.execute(
        "SELECT stage, outcome, reference, initial FROM lineage_stage_events "
        "WHERE implementation_issue_id = 'impl-4'"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("integration", "INTEGRATED", "2" * 40, 0),
    ]


def test_collector_is_idempotent_across_repeated_cycles(conn):
    replay_success(conn)
    before = [tuple(r) for r in conn.execute(
        "SELECT * FROM lineage_stage_events ORDER BY stage")]
    replay_success(conn)
    after = [tuple(r) for r in conn.execute(
        "SELECT * FROM lineage_stage_events ORDER BY stage")]
    assert after == before
    assert len(after) == 2


def test_unknown_outcome_words_are_not_guessed_into_a_stage(conn):
    sync(conn, [issue_payload("impl-6", "FAN-6001",
                              integration_outcome="in flight",
                              integration_sha="7" * 40)], ts(days=3))
    assert conn.execute(
        "SELECT COUNT(*) FROM lineage_stage_events").fetchone()[0] == 0
    assert conn.execute(
        "SELECT integration_outcome FROM issues WHERE id = 'impl-6'"
    ).fetchone()[0] is None


# -- SLOs ----------------------------------------------------------------------


def slo_by_id(payload, slo_id):
    return next(s for s in payload["slos"] if s["id"] == slo_id)


def test_every_slo_declares_window_threshold_owner_and_budget(conn):
    payload = lineage.slo(conn, days=30, now=NOW)
    assert [s["id"] for s in payload["slos"]] == [
        "pm_readiness_latency", "dispatch_latency",
        "production_data_freshness", "ci_green", "release_gate",
    ]
    for entry in payload["slos"]:
        assert entry["window_days"] == 30
        assert entry["owner"]
        assert 0.0 < entry["objective"] < 1.0
        assert "numerator" in entry and "denominator" in entry
        assert "budget_remaining" in entry
        assert "threshold_seconds" in entry


def test_empty_window_is_unmeasured_rather_than_a_breach(conn):
    payload = lineage.slo(conn, days=7, now=NOW)
    for entry in payload["slos"]:
        if entry["id"] == "production_data_freshness":
            continue
        assert entry["measured"] is False
        assert entry["ratio"] is None
        assert entry["breached"] is False
    # No data has ever been collected, so served data is stale right now: that
    # condition alerts on its own, without inventing a measured ratio.
    assert [a["dedupe_key"] for a in payload["alerts"]] == [
        "production_data_freshness|7d|stale_data",
    ]


def test_pm_readiness_latency_counts_only_observed_transitions(conn):
    sync(conn, [issue_payload("impl-a", "FAN-7001", created_at=ts(days=6)),
                issue_payload("impl-b", "FAN-7002", created_at=ts(days=6)),
                issue_payload("impl-c", "FAN-7003", created_at=ts(days=6))],
         ts(days=6))
    add_ready(conn, "impl-a", ts(days=5, hours=23))   # 1 h  -> met
    add_ready(conn, "impl-b", ts(days=1))             # 5 d  -> breach
    add_ready(conn, "impl-c", ts(days=2), initial=1)  # baseline -> excluded

    entry = slo_by_id(lineage.slo(conn, days=30, now=NOW),
                      "pm_readiness_latency")
    assert (entry["numerator"], entry["denominator"]) == (1, 2)
    assert entry["ratio"] == 0.5
    assert entry["breached"] is True
    assert [s["identifier"] for s in entry["subjects"]] == ["FAN-7002"]
    assert entry["subjects"][0]["elapsed_seconds"] == 5 * 86400


def test_dispatch_latency_censors_a_card_still_inside_its_threshold(conn):
    sync(conn, [issue_payload("impl-d", "FAN-7101"),
                issue_payload("impl-e", "FAN-7102")], ts(days=6))
    add_ready(conn, "impl-d", ts(days=5))
    add_run(conn, "run-d", "impl-d", ts(days=5, minutes=-10))
    add_ready(conn, "impl-e", ts(minutes=10))  # ready 10 min ago, no run yet

    entry = slo_by_id(lineage.slo(conn, days=30, now=NOW), "dispatch_latency")
    assert (entry["numerator"], entry["denominator"]) == (1, 1)
    assert entry["excluded"] == 1
    assert entry["breached"] is False


def test_dispatch_latency_counts_a_long_undispatched_card_as_a_breach(conn):
    sync(conn, [issue_payload("impl-f", "FAN-7201")], ts(days=6))
    add_ready(conn, "impl-f", ts(days=3))

    entry = slo_by_id(lineage.slo(conn, days=30, now=NOW), "dispatch_latency")
    assert (entry["numerator"], entry["denominator"]) == (0, 1)
    assert entry["breached"] is True
    assert entry["subjects"][0]["identifier"] == "FAN-7201"
    assert entry["subjects"][0]["run_id"] is None


def test_production_data_freshness_reports_stale_served_data(conn):
    for started, failed in ((ts(days=1), 0), (ts(hours=12), 0),
                            (ts(hours=6), 1)):
        conn.execute(
            "INSERT INTO poll_cycles (started_at, finished_at, sources_ok, "
            "sources_failed) VALUES (?, ?, ?, ?)", (started, started, 3, failed))
    conn.execute(
        "INSERT INTO sync_state (source, ok, last_success_at) VALUES (?, ?, ?)",
        ("issues", 1, ts(hours=6)))
    conn.commit()

    entry = slo_by_id(lineage.slo(conn, days=30, now=NOW),
                      "production_data_freshness")
    assert (entry["numerator"], entry["denominator"]) == (2, 3)
    assert entry["data_age_seconds"] == 6 * 3600
    assert entry["data_stale"] is True
    assert entry["breached"] is True


def test_ci_green_lists_the_exact_red_integration(conn):
    replay_success(conn)       # ci success
    replay_stale_deploy(conn)  # ci failure

    entry = slo_by_id(lineage.slo(conn, days=30, now=NOW), "ci_green")
    assert (entry["numerator"], entry["denominator"]) == (1, 2)
    assert entry["breached"] is True
    assert entry["subjects"] == [{
        "issue_id": "impl-4", "identifier": "FAN-4001",
        "ci_status": "failure", "integration_sha": "2" * 40,
    }]


def test_release_gate_flags_a_blocked_candidate_with_its_sha(conn):
    replay_success(conn)  # integrated one day after PASSED
    sync(conn, [issue_payload("impl-7", "FAN-7301",
                              post_qa_integration_required=True,
                              qa_issue_id="qa-7")], ts(days=8))
    sync(conn, [issue_payload("qa-7", "FAN-7302", status="done",
                              qa_for_issue_id="impl-7",
                              qa_candidate_sha="8" * 40, qa_verdict="PASSED",
                              qa_verdict_at=ts(days=6))], ts(days=6))

    entry = slo_by_id(lineage.slo(conn, days=30, now=NOW), "release_gate")
    assert (entry["numerator"], entry["denominator"]) == (1, 2)
    assert entry["breached"] is True
    assert entry["subjects"][0]["identifier"] == "FAN-7301"
    assert entry["subjects"][0]["candidate"] == "8" * 40
    assert entry["subjects"][0]["elapsed_seconds"] == 6 * 86400


def test_breach_alerts_are_dedupe_ready_and_stable(conn):
    replay_stale_deploy(conn)
    first = lineage.slo(conn, days=30, now=NOW)["alerts"]
    second = lineage.slo(conn, days=30, now=NOW)["alerts"]

    keys = [alert["dedupe_key"] for alert in first]
    assert keys == [alert["dedupe_key"] for alert in second]
    assert len(keys) == len(set(keys))
    assert "ci_green|30d|breach" in keys
    ci = next(a for a in first if a["slo"] == "ci_green")
    assert ci["owner"] == "devops"
    assert ci["subjects"][0]["integration_sha"] == "2" * 40


def test_slo_rejects_an_unsupported_window(conn):
    with pytest.raises(ValueError):
        lineage.slo(conn, days=14, now=NOW)


def test_slo_on_a_legacy_snapshot_reports_unsupported_schema(tmp_path):
    connection = connect(tmp_path / "legacy.db")
    init_db(connection)
    connection.execute("DROP TABLE lineage_stage_events")
    connection.commit()
    payload = lineage.slo(connection, days=30, now=NOW)
    assert payload["schema_supported"] is False
    assert slo_by_id(payload, "ci_green")["measured"] is False
    connection.close()


# -- API surface ---------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    config = Config()
    config.db_path = tmp_path / "api.db"
    connection = connect(config.db_path)
    init_db(connection)
    replay_success(connection)
    with TestClient(server_module.create_app(config)) as client:
        yield client
    connection.close()


def test_api_lineage_returns_the_chain(api):
    payload = api.get("/api/lineage", params={"trace": "FAN-1001"}).json()
    assert payload["correlation_id"] == "impl-1"
    assert payload["complete"] is True


def test_api_lineage_requires_a_trace(api):
    assert api.get("/api/lineage").status_code == 422


def test_api_slo_validates_the_window(api):
    assert api.get("/api/slo", params={"days": "30"}).status_code == 200
    assert api.get("/api/slo", params={"days": "14"}).status_code == 422


def test_api_payloads_carry_no_issue_titles(api):
    body = api.get("/api/lineage", params={"trace": "impl-1"}).text
    assert "title" not in body
