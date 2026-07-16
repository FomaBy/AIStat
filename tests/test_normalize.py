"""Parsing of real CLI JSON fixtures (captured from the live workspace)."""

import pytest

from aistat import normalize
from conftest import load_fixture


def test_normalize_runtimes_fixture():
    rows = [normalize.normalize_runtime(r) for r in load_fixture("runtime_list.json")]
    assert len(rows) >= 3
    assert all(r["id"] for r in rows)
    providers = {r["provider"] for r in rows}
    assert "claude" in providers and "codex" in providers


def test_normalize_agents_fixture():
    rows = [normalize.normalize_agent(a) for a in load_fixture("agent_list.json")]
    assert len(rows) >= 5
    by_name = {r["name"]: r for r in rows}
    assert by_name["Fable"]["model"] == "claude-fable-5"
    assert by_name["Fable"]["runtime_id"]


def test_normalize_projects_fixture():
    rows = [normalize.normalize_project(p) for p in load_fixture("project_list.json")]
    assert len(rows) >= 3
    titles = {r["title"] for r in rows}
    assert "AIStat" in titles


def test_normalize_daily_usage_fixture():
    rows = [normalize.normalize_daily_usage(u) for u in load_fixture("runtime_usage.json")]
    assert rows
    for row in rows:
        assert row["date"].count("-") == 2
        assert isinstance(row["input_tokens"], int)
        assert isinstance(row["cache_read_tokens"], int)
    assert any(row["output_tokens"] > 0 for row in rows)


def test_normalize_issues_fixture_with_story_points():
    page = load_fixture("issue_list_page.json")
    rows = [normalize.normalize_issue(i) for i in page["issues"]]
    assert rows
    # every AIStat sub-issue in the fixture carries story_points metadata
    assert all(isinstance(r["story_points"], float) for r in rows)
    assert all(r["estimation_model"] for r in rows)


def test_story_points_from_metadata_precedes_label():
    issue = {
        "id": "x",
        "updated_at": "2026-07-15T00:00:00Z",
        "metadata": {"story_points": 5},
        "labels": [{"name": "SP:8"}],
    }
    assert normalize.extract_story_points(issue) == 5.0


def test_story_points_label_fallback():
    issue = {
        "id": "x",
        "updated_at": "2026-07-15T00:00:00Z",
        "metadata": {},
        "labels": [{"name": "other"}, {"name": "SP:13"}],
    }
    assert normalize.extract_story_points(issue) == 13.0


def test_story_points_absent():
    issue = {"id": "x", "updated_at": "2026-07-15T00:00:00Z"}
    assert normalize.extract_story_points(issue) is None


def test_normalize_issue_detects_jira_origin():
    issue = {
        "id": "j1",
        "updated_at": "2026-07-15T00:00:00Z",
        "metadata": {
            "jira_key": "SCRUM-1078",
            "jira_url": "https://fantasydisk.atlassian.net/browse/SCRUM-1078",
            "historical_import": "true",
            "story_points": 5,
        },
    }
    row = normalize.normalize_issue(issue)
    assert row["is_jira"] == 1
    assert row["jira_key"] == "SCRUM-1078"


def test_normalize_issue_native_multica_task_not_jira():
    # A native task, even one that lives in the Jira Archive project, has no
    # jira_* / historical_import markers and must NOT be flagged.
    issue = {
        "id": "n1",
        "updated_at": "2026-07-15T00:00:00Z",
        "metadata": {"story_points": 3, "estimation_model": "CUE"},
    }
    row = normalize.normalize_issue(issue)
    assert row["is_jira"] == 0
    assert row["jira_key"] is None


def test_is_jira_issue_markers():
    assert normalize.is_jira_issue({"metadata": {"jira_key": "SCRUM-1"}})
    assert normalize.is_jira_issue({"metadata": {"jira_url": "https://x/browse/SCRUM-1"}})
    assert normalize.is_jira_issue({"metadata": {"historical_import": "true"}})
    assert not normalize.is_jira_issue({"metadata": {"historical_import": "false"}})
    assert not normalize.is_jira_issue({"metadata": {"story_points": 5}})
    assert not normalize.is_jira_issue({})


def test_normalize_issues_fixture_are_native():
    page = load_fixture("issue_list_page.json")
    rows = [normalize.normalize_issue(i) for i in page["issues"]]
    assert all(r["is_jira"] == 0 for r in rows)
    assert all(r["jira_key"] is None for r in rows)


def test_normalize_issue_usage_fixture():
    row = normalize.normalize_issue_usage("parent-id", load_fixture("issue_usage.json"))
    assert row["issue_id"] == "parent-id"
    assert row["task_count"] == 4
    assert row["total_output_tokens"] > 0


def test_normalize_runs_fixture():
    rows = [normalize.normalize_run(r) for r in load_fixture("issue_runs.json")]
    assert rows
    assert all(r["id"] for r in rows)
    assert all(r["agent_id"] for r in rows)


def test_normalize_agent_tasks_fixture():
    rows = [normalize.normalize_run(r) for r in load_fixture("agent_tasks.json")]
    assert rows
    assert all(r["id"] for r in rows)


def test_normalize_activity_fixture():
    rows = normalize.normalize_activity("rt-1", load_fixture("runtime_activity.json"))
    assert rows
    assert all(0 <= r["hour"] <= 23 for r in rows)
    assert all(r["runtime_id"] == "rt-1" for r in rows)


def test_missing_required_key_raises():
    with pytest.raises(normalize.NormalizationError):
        normalize.normalize_daily_usage({"model": "m", "date": "2026-01-01"})
    with pytest.raises(normalize.NormalizationError):
        normalize.normalize_issue({"id": "no-updated-at"})
    with pytest.raises(normalize.NormalizationError):
        normalize.normalize_issue_usage("x", {"task_count": "not-a-number"})
