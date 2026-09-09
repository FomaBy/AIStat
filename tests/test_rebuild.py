"""Deterministic Multica recovery replay on isolated captured inputs."""

import copy
import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from aistat.config import Config
from aistat.rebuild import (
    RebuildError,
    capture_inputs,
    rebuild,
    rebuild_self_test,
    verify_rebuild,
)
from conftest import load_fixture


SOURCE = {
    "base_sha": "b631e38e668d6138cbf4495a3e0e650505438465",
    "base_ref": "origin/dev",
    "base_tree": "50f0e191d00bf3cb8353c794592049967c417be5",
    "captured_at": "2026-08-24T22:00:00Z",
}


def fixture_runner(args):
    """Return complete recorded CLI rows for every command the replay needs."""
    if args[:2] == ["runtime", "list"]:
        return load_fixture("runtime_list.json")
    if args[:2] == ["runtime", "usage"]:
        rows = copy.deepcopy(load_fixture("runtime_usage.json"))
        for row in rows:
            row["runtime_id"] = args[2]
        return rows
    if args[:2] == ["runtime", "activity"]:
        return load_fixture("runtime_activity.json")
    if args[:2] == ["agent", "list"]:
        return load_fixture("agent_list.json")
    if args[:2] == ["agent", "tasks"]:
        return load_fixture("agent_tasks.json")
    if args[:2] == ["project", "list"]:
        return load_fixture("project_list.json")
    if args[:2] == ["workspace", "get"]:
        return {"settings": {}}
    if args[:2] == ["issue", "list"]:
        page = copy.deepcopy(load_fixture("issue_list_page.json"))
        page["has_more"] = False
        return page
    if args[:2] == ["issue", "usage"]:
        return load_fixture("issue_usage.json")
    if args[:2] == ["issue", "runs"]:
        return load_fixture("issue_runs.json")
    raise AssertionError("unexpected CLI command: %s" % " ".join(args))


def capture_fixture(tmp_path):
    input_dir = tmp_path / "input"
    capture_inputs(
        input_dir,
        config=Config(),
        runner=fixture_runner,
        source=SOURCE,
    )
    return input_dir


def manifest(path):
    with (Path(path) / "output-manifest.json").open(encoding="utf-8") as stream:
        return json.load(stream)


def write_canonical(path, value):
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    Path(path).write_text(raw + "\n", encoding="utf-8")
    return (hashlib.sha256((raw + "\n").encode("utf-8")).hexdigest(), len(raw) + 1)


def test_rebuild_replays_complete_frozen_input_deterministically(tmp_path):
    """Changing any replay behavior must change the verified output manifest."""
    input_dir = capture_fixture(tmp_path)

    first = rebuild(input_dir, tmp_path / "first")
    second = rebuild(input_dir, tmp_path / "second")

    assert first == second
    assert manifest(tmp_path / "first") == manifest(tmp_path / "second")
    assert verify_rebuild(input_dir, tmp_path / "first") == first
    assert first["source"] == SOURCE
    assert first["daily_usage"] == {
        "range": {"first": "2026-07-12", "last": "2026-07-15"},
        "row_count": 30,
        "rows_sha256": "30eb450ed8eeb4d87f951709cfba189daf75f1c7742bbc0bc890e6e1ffcdf159",
        "totals": {
            "input_tokens": 1481781,
            "output_tokens": 10540908,
            "cache_read_tokens": 1512792609,
            "cache_write_tokens": 47813598,
        },
        "watermark": "2026-07-15",
    }


def test_rebuild_rejects_tampered_input_before_creating_output(tmp_path):
    """The source hash is the recovery boundary, not an advisory warning."""
    input_dir = capture_fixture(tmp_path)
    response = next((input_dir / "responses").glob("*.json"))
    response.write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "output"

    with pytest.raises(RebuildError, match="hash"):
        rebuild(input_dir, output_dir)

    assert not output_dir.exists()


def test_rebuild_rejects_a_manifest_valid_duplicate_usage_row(tmp_path):
    """A rewritten manifest cannot turn duplicated source counters into one row."""
    input_dir = capture_fixture(tmp_path)
    with (input_dir / "input-manifest.json").open(encoding="utf-8") as stream:
        input_manifest = json.load(stream)
    entry = next(
        item
        for item in input_manifest["responses"]
        if item["command"][:2] == ["runtime", "usage"]
    )
    response_path = input_dir / entry["path"]
    rows = json.loads(response_path.read_text(encoding="utf-8"))
    rows.append(copy.deepcopy(rows[0]))
    entry["sha256"], entry["size_bytes"] = write_canonical(response_path, rows)
    write_canonical(input_dir / "input-manifest.json", input_manifest)

    with pytest.raises(RebuildError, match="duplicate"):
        rebuild(input_dir, tmp_path / "output")


def test_rebuild_rejects_an_unresolved_input_directory_diff(tmp_path):
    """An apparently harmless extra directory makes a pinned input unsafe."""
    input_dir = capture_fixture(tmp_path)
    (input_dir / "unexpected").mkdir()

    with pytest.raises(RebuildError, match="unknown or missing"):
        rebuild(input_dir, tmp_path / "output")


def test_verify_rebuild_rejects_a_counter_decrease_after_replay(tmp_path):
    """The output manifest protects every per-runtime/model/day counter."""
    input_dir = capture_fixture(tmp_path)
    output_dir = tmp_path / "output"
    rebuild(input_dir, output_dir)
    conn = sqlite3.connect(str(output_dir / "aistat.db"))
    try:
        conn.execute(
            "UPDATE daily_usage SET input_tokens = input_tokens - 1 "
            "WHERE runtime_id = (SELECT runtime_id FROM daily_usage LIMIT 1)"
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()

    with pytest.raises(RebuildError, match="daily usage counters differ"):
        verify_rebuild(input_dir, output_dir)


def test_rebuild_self_test_uses_only_copies_and_restores_rollback_target(tmp_path):
    """The cutover probe must leave the original isolated target byte-identical."""
    input_dir = capture_fixture(tmp_path)
    evidence = rebuild_self_test(input_dir, tmp_path / "recovery")

    assert evidence["ok"] is True
    assert evidence["cutover"]["rolled_back"] is True
    assert evidence["backup_restore"]["ok"] is True


def test_recovery_inputs_and_outputs_are_owner_only(tmp_path):
    """Captured Multica telemetry must not become group- or world-readable."""
    input_dir = capture_fixture(tmp_path)
    output_dir = tmp_path / "output"
    rebuild(input_dir, output_dir)

    for root in (input_dir, output_dir):
        for path in root.rglob("*"):
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, path


def test_capture_freezes_an_optional_pricing_override(tmp_path):
    """Replay must not depend on an override file outside the pinned input."""
    override = tmp_path / "override.json"
    write_canonical(
        override,
        {
            "models": {
                "claude-fable-5": {
                    "input": 11.0,
                    "output": 22.0,
                    "cache_read": 1.1,
                    "cache_write": 2.2,
                }
            }
        },
    )
    input_dir = tmp_path / "input"
    capture_inputs(
        input_dir,
        config=Config(pricing_overrides_path=override),
        runner=fixture_runner,
        source=SOURCE,
    )
    override.write_text("not JSON", encoding="utf-8")

    with (input_dir / "input-manifest.json").open(encoding="utf-8") as stream:
        pricing = json.load(stream)["pricing"]
    assert pricing["overrides"]["path"] == "pricing-overrides.json"
    assert rebuild(input_dir, tmp_path / "output")["source"] == SOURCE


def test_rebuild_reuses_captured_poller_settings(tmp_path):
    """A later environment cannot change the frozen command stream or costs."""
    override = tmp_path / "override.json"
    write_canonical(
        override,
        {
            "models": {
                "claude-fable-5": {
                    "input": 1.0,
                    "output": 2.0,
                    "cache_read": 0.1,
                    "cache_write": 1.25,
                }
            }
        },
    )
    input_dir = tmp_path / "input"
    capture_inputs(
        input_dir,
        config=Config(
            issue_page_limit=7,
            credits_per_usd=3.5,
            pricing_overrides_path=override,
        ),
        runner=fixture_runner,
        source=SOURCE,
    )

    output_dir = tmp_path / "output"
    rebuild(input_dir, output_dir)

    with (input_dir / "input-manifest.json").open(encoding="utf-8") as stream:
        assert json.load(stream)["poller"] == {
            "credits_per_usd": 3.5,
            "issue_page_limit": 7,
        }
    conn = sqlite3.connect(str(output_dir / "aistat.db"))
    try:
        cost_usd, cost_credits = conn.execute(
            "SELECT cost_usd, cost_credits FROM daily_usage "
            "WHERE model = 'claude-fable-5' AND cost_usd IS NOT NULL LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert cost_credits == pytest.approx(cost_usd * 3.5)


def test_capture_rejects_duplicate_usage_before_publishing_input(tmp_path):
    """A capture must not leave evidence that its offline replay will reject."""
    def duplicate_usage_runner(args):
        rows = fixture_runner(args)
        if args[:2] == ["runtime", "usage"]:
            rows.append(copy.deepcopy(rows[0]))
        return rows

    input_dir = tmp_path / "input"
    with pytest.raises(RebuildError, match="duplicate"):
        capture_inputs(
            input_dir,
            config=Config(),
            runner=duplicate_usage_runner,
            source=SOURCE,
        )
    assert not input_dir.exists()


def test_capture_rejects_fractional_usage_before_publishing_input(tmp_path):
    """Token counters are integral facts, never silently truncated values."""
    def fractional_usage_runner(args):
        rows = fixture_runner(args)
        if args[:2] == ["runtime", "usage"]:
            rows[0]["input_tokens"] = 1.5
        return rows

    input_dir = tmp_path / "input"
    with pytest.raises(RebuildError, match="integral"):
        capture_inputs(
            input_dir,
            config=Config(),
            runner=fractional_usage_runner,
            source=SOURCE,
        )
    assert not input_dir.exists()


def test_capture_rejects_non_utc_provenance_timestamp(tmp_path):
    """A replay timestamp must be an exact UTC fact, not arbitrary text."""
    source = dict(SOURCE, captured_at="tomorrow")
    input_dir = tmp_path / "input"

    with pytest.raises(RebuildError, match="UTC timestamp"):
        capture_inputs(
            input_dir,
            config=Config(),
            runner=fixture_runner,
            source=source,
        )

    assert not input_dir.exists()


def test_capture_rejects_noncanonical_utc_provenance_timestamp(tmp_path):
    """UTC provenance has fixed RFC3339 widths, not parser-specific variants."""
    source = dict(SOURCE, captured_at="2026-8-5T1:2:3Z")
    input_dir = tmp_path / "input"

    with pytest.raises(RebuildError, match="UTC timestamp"):
        capture_inputs(
            input_dir,
            config=Config(),
            runner=fixture_runner,
            source=source,
        )

    assert not input_dir.exists()
