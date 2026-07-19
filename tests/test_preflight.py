"""Preflight validation for the trusted local runtime (FAN-1404)."""

import os

import pytest

from aistat.config import Config
from aistat import preflight


def valid_config(tmp_path):
    config = Config()
    config.publish_tenant_id = 7
    config.publish_url = "https://aistat.app/api/ingest/snapshot"
    config.worker_sync_url = "https://aistat.app"
    config.ingest_secret = "i" * 40
    config.worker_secret = "w" * 40
    config.session_secret = "s" * 40
    config.publish_interval_seconds = 300
    config.worker_pull_interval_seconds = 300
    config.worker_collect_interval_seconds = 300
    config.allow_insecure_publish = False
    config.worker_key_path = tmp_path / "key" / "worker.key"
    config.worker_store_path = tmp_path / "store" / "connections.db"
    return config


def verdict(report, name):
    return next(c for c in report.checks if c.name == name)


def test_valid_config_passes(tmp_path):
    report = preflight.run_preflight(valid_config(tmp_path), check_imports=False)
    assert report.ok, report.render()
    assert report.failures == []


def test_missing_tenant_id_fails(tmp_path):
    config = valid_config(tmp_path)
    config.publish_tenant_id = None
    report = preflight.run_preflight(config, check_imports=False)
    assert not report.ok
    assert not verdict(report, "tenant_id").ok


def test_non_https_publish_url_fails(tmp_path):
    config = valid_config(tmp_path)
    config.publish_url = "http://aistat.app/api/ingest/snapshot"
    report = preflight.run_preflight(config, check_imports=False)
    assert not verdict(report, "AISTAT_PUBLISH_URL").ok


def test_non_https_worker_url_fails(tmp_path):
    config = valid_config(tmp_path)
    config.worker_sync_url = "http://aistat.app"
    report = preflight.run_preflight(config, check_imports=False)
    assert not verdict(report, "AISTAT_WORKER_SYNC_URL").ok


def test_insecure_flag_allows_http(tmp_path):
    config = valid_config(tmp_path)
    config.allow_insecure_publish = True
    config.publish_url = "http://localhost:9000/ingest"
    config.worker_sync_url = "http://localhost:9000"
    report = preflight.run_preflight(config, check_imports=False)
    assert report.ok, report.render()


def test_short_ingest_secret_fails(tmp_path):
    config = valid_config(tmp_path)
    config.ingest_secret = "short"
    report = preflight.run_preflight(config, check_imports=False)
    assert not verdict(report, "ingest_secret").ok


@pytest.mark.parametrize(
    "session_secret",
    [None, "", "s" * 31],
    ids=["missing", "empty", "31-bytes"],
)
def test_invalid_session_secret_fails(tmp_path, session_secret):
    config = valid_config(tmp_path)
    config.session_secret = session_secret
    report = preflight.run_preflight(config, check_imports=False)
    assert not report.ok
    assert not verdict(report, "session_secret").ok


def test_session_secret_exactly_32_bytes_passes(tmp_path):
    config = valid_config(tmp_path)
    config.session_secret = "s" * 32
    report = preflight.run_preflight(config, check_imports=False)
    assert report.ok, report.render()
    assert verdict(report, "session_secret").ok


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("session_secret", "ingest_secret"),
        ("session_secret", "worker_secret"),
        ("ingest_secret", "worker_secret"),
    ],
    ids=["session-ingest", "session-worker", "ingest-worker"],
)
def test_reused_secret_pair_fails_independence(tmp_path, left, right):
    config = valid_config(tmp_path)
    setattr(config, right, getattr(config, left))
    report = preflight.run_preflight(config, check_imports=False)
    assert not verdict(report, "secret_independence").ok


@pytest.mark.parametrize("field", [
    "publish_interval_seconds",
    "worker_pull_interval_seconds",
    "worker_collect_interval_seconds",
])
def test_sub_minute_interval_fails(tmp_path, field):
    config = valid_config(tmp_path)
    setattr(config, field, 30)
    report = preflight.run_preflight(config, check_imports=False)
    assert not report.ok
    assert any(not c.ok and "interval" in c.name for c in report.failures)


def test_key_inside_store_dir_fails(tmp_path):
    config = valid_config(tmp_path)
    shared = tmp_path / "shared"
    config.worker_key_path = shared / "worker.key"
    config.worker_store_path = shared / "connections.db"
    report = preflight.run_preflight(config, check_imports=False)
    assert not verdict(report, "worker_key_location").ok


def test_world_readable_key_file_fails(tmp_path):
    config = valid_config(tmp_path)
    config.worker_key_path.parent.mkdir(parents=True)
    config.worker_key_path.parent.chmod(0o700)
    config.worker_key_path.write_bytes(b"k" * 44)
    config.worker_key_path.chmod(0o644)
    report = preflight.run_preflight(config, check_imports=False)
    assert not verdict(report, "worker_key_perms").ok


def test_group_readable_key_dir_fails(tmp_path):
    config = valid_config(tmp_path)
    config.worker_key_path.parent.mkdir(parents=True)
    config.worker_key_path.parent.chmod(0o750)
    report = preflight.run_preflight(config, check_imports=False)
    assert not verdict(report, "worker_key_dir_perms").ok


def test_env_file_missing_is_ok(tmp_path):
    check = preflight.check_env_file(tmp_path / "absent.env")
    assert check.ok


def test_env_file_owner_only_is_ok(tmp_path):
    env = tmp_path / "production.env"
    env.write_text("AISTAT_TENANT_ID=1\n")
    env.chmod(0o600)
    assert preflight.check_env_file(env).ok


def test_env_file_group_readable_fails(tmp_path):
    env = tmp_path / "production.env"
    env.write_text("AISTAT_INGEST_SECRET=x\n")
    env.chmod(0o640)
    assert not preflight.check_env_file(env).ok


def test_env_file_symlink_rejected(tmp_path):
    target = tmp_path / "real.env"
    target.write_text("AISTAT_TENANT_ID=1\n")
    target.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(target)
    assert not preflight.check_env_file(link).ok


def test_import_checks_pass_for_real_modules(tmp_path):
    report = preflight.run_preflight(valid_config(tmp_path), check_imports=True)
    import_checks = [c for c in report.checks if c.name.startswith("import:")]
    assert len(import_checks) == 4
    assert all(c.ok for c in import_checks)
    assert verdict(report, "dependency:cryptography").ok


def test_render_never_contains_secret_values(tmp_path):
    config = valid_config(tmp_path)
    text = preflight.run_preflight(config, check_imports=False).render()
    assert config.ingest_secret not in text
    assert config.worker_secret not in text
    assert config.session_secret not in text


def test_invalid_session_secret_render_never_contains_value(tmp_path):
    config = valid_config(tmp_path)
    config.session_secret = "q" * 31
    text = preflight.run_preflight(config, check_imports=False).render()
    assert config.session_secret not in text
    assert "FAIL session_secret" in text
    assert "AISTAT_SESSION_SECRET must contain at least 32 bytes" in text


def test_cli_exit_code(tmp_path, monkeypatch):
    # With no runtime config in the environment the CLI must fail closed.
    for key in list(os.environ):
        if key.startswith("AISTAT_"):
            monkeypatch.delenv(key, raising=False)
    assert preflight.main(["--no-imports"]) == 1
