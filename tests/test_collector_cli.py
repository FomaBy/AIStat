"""CLI contract tests for the per-user collector entry point."""

import json

import pytest

import aistat.collector as collector


def test_collector_default_command_is_continuous(monkeypatch):
    calls = []

    def fake_watch(config):
        calls.append(config)
        return 23

    monkeypatch.setattr(collector, "watch", fake_watch)

    assert collector.main([]) == 23
    assert len(calls) == 1


def test_collector_once_command_is_one_shot_and_safe_json(monkeypatch, capsys):
    class FakeStore:
        def __init__(self, *args):
            pass

    class FakeCollector:
        def __init__(self, config, store):
            pass

        def collect_once(self):
            return [
                collector.ConnectionOutcome(101, "collected"),
                collector.ConnectionOutcome(
                    202, "failed", "PAT=/tmp/secret arbitrary stderr"
                ),
            ]

    monkeypatch.setattr(collector, "WorkerTokenStore", FakeStore)
    monkeypatch.setattr(collector, "Collector", FakeCollector)

    assert collector.main(["--once"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["collected"] == [101]
    assert summary["failed"] == [
        {"user_id": 202, "detail": collector.CONNECTION_FAILURE}
    ]
    assert "PAT=" not in json.dumps(summary)
    assert "/tmp/secret" not in json.dumps(summary)


def test_collector_watch_flag_is_not_supported():
    with pytest.raises(SystemExit) as exc_info:
        collector.main(["--watch"])
    assert exc_info.value.code == 2


def test_collector_env_file_is_validated_before_collection(monkeypatch, tmp_path):
    env_file = tmp_path / "production.env"
    env_file.write_text("bad line\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setattr(collector, "Config", lambda: (_ for _ in ()).throw(
        AssertionError("Config must not be built after an invalid env file")
    ))

    assert collector.main(["--once", "--env-file", str(env_file)]) == 1
