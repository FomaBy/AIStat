"""Tenant path safety, owner resolution and legacy data migration."""

import os
import sqlite3

import pytest

from aistat.config import Config
from aistat.db import connect, init_db
from aistat import migrate
from aistat.migrate import (
    MigrationError,
    REQUIRED_TABLES,
    migrate_owner_database,
)
from aistat.security import SecurityConfigError, SecurityStore
from conftest import seed_aggregate_fixture


class _NoBackupConnection:
    """Mimics a Python 3.6 sqlite3 connection: no Connection.backup()."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        if name == "backup":
            raise AttributeError("backup")
        return getattr(self._real, name)


class _NoBackupSqlite:
    """sqlite3 stand-in whose connections lack backup(), as on Python 3.6."""

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, *args, **kwargs):
        return _NoBackupConnection(sqlite3.connect(*args, **kwargs))


def _table_counts(conn):
    return {
        table: conn.execute(
            "SELECT COUNT(*) FROM {}".format(table)
        ).fetchone()[0]
        for table in sorted(REQUIRED_TABLES)
    }


def tenant_config(tmp_path):
    config = Config()
    config.db_path = tmp_path / "aistat.db"
    config.security_db_path = tmp_path / "security.db"
    config.tenants_dir = tmp_path / "tenants"
    config.auth_username = "sergey"
    config.admin_email = "owner@example.com"
    return config


def test_tenant_path_accepts_only_internal_positive_integer(tmp_path):
    config = tenant_config(tmp_path)
    assert config.tenant_db_path(12) == tmp_path / "tenants" / "12.db"
    for value in ("../12", "12/other", "01", "+1", 0, -1, True, None):
        with pytest.raises(ValueError):
            config.tenant_db_path(value)


def test_owner_resolution_is_idempotent_and_adopts_configured_email(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    oauth_user = store.find_or_create_user_by_identity(
        "google", "owner-sub", email="owner@example.com", now=10
    )
    assert store.ensure_owner_user(
        "sergey", "OWNER@example.com", now=20
    ) == oauth_user
    assert store.ensure_owner_user(
        "sergey", "owner@example.com", now=30
    ) == oauth_user


def test_owner_resolution_rejects_multiple_admins(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    conn = sqlite3.connect(str(tmp_path / "security.db"))
    conn.execute(
        "INSERT INTO users (created_at, display_name, is_admin) "
        "VALUES (1, 'one', 1), (1, 'two', 1)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(SecurityConfigError):
        store.ensure_owner_user("sergey")


def test_owner_migration_preserves_wal_data_and_is_idempotent(tmp_path):
    config = tenant_config(tmp_path)
    source = connect(config.db_path)
    init_db(source)
    seed_aggregate_fixture(source)
    source.execute(
        "UPDATE daily_usage SET input_tokens = input_tokens + 123 "
        "WHERE runtime_id = 'R1'"
    )
    source.commit()

    first = migrate_owner_database(config, now=1000)
    source.close()
    target = config.tenant_db_path(first["owner_user_id"])
    assert first["migrated"] is True
    assert target.is_file()
    assert not config.db_path.exists()
    assert config.db_path.with_name("aistat.db.migrated").is_file()

    conn = sqlite3.connect(str(target))
    total = conn.execute(
        "SELECT SUM(input_tokens + output_tokens + cache_read_tokens + "
        "cache_write_tokens) FROM daily_usage"
    ).fetchone()[0]
    conn.close()
    assert total == 4_700_123

    second = migrate_owner_database(config, now=2000)
    assert second["owner_user_id"] == first["owner_user_id"]
    assert second["sha256"] == first["sha256"]
    assert second["migrated"] is False
    tenant = SecurityStore(config.security_db_path).get_tenant(
        first["owner_user_id"]
    )
    assert tenant["last_snapshot_sha256"] == first["sha256"]


def test_owner_migration_python36_fallback_handles_wal_database(
    tmp_path, monkeypatch
):
    config = tenant_config(tmp_path)
    source = connect(config.db_path)
    init_db(source)
    seed_aggregate_fixture(source)
    source.execute(
        "UPDATE daily_usage SET input_tokens = input_tokens + 123 "
        "WHERE runtime_id = 'R1'"
    )
    source.commit()
    assert source.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert config.db_path.with_name("aistat.db-wal").is_file()
    expected_counts = _table_counts(source)

    monkeypatch.setattr(migrate, "sqlite3", _NoBackupSqlite())
    first = migrate_owner_database(config, now=1000)
    source.close()

    target = config.tenant_db_path(first["owner_user_id"])
    assert first["migrated"] is True
    assert [p.name for p in config.tenants_dir.iterdir()] == [target.name]
    assert not config.db_path.exists()
    assert config.db_path.with_name("aistat.db.migrated").is_file()
    assert not target.with_name(target.name + "-wal").exists()
    assert not target.with_name(target.name + "-shm").exists()

    # The copy must be a standalone database readable without the source
    # WAL sidecars, exactly how _validate_database opens it.
    conn = sqlite3.connect("file:{}?mode=ro".format(target), uri=True)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert _table_counts(conn) == expected_counts
    total = conn.execute(
        "SELECT SUM(input_tokens + output_tokens + cache_read_tokens + "
        "cache_write_tokens) FROM daily_usage"
    ).fetchone()[0]
    conn.close()
    assert total == 4_700_123

    second = migrate_owner_database(config, now=2000)
    assert second["owner_user_id"] == first["owner_user_id"]
    assert second["sha256"] == first["sha256"]
    assert second["migrated"] is False


def test_owner_migration_fallback_failure_preserves_source(
    tmp_path, monkeypatch
):
    config = tenant_config(tmp_path)
    source = connect(config.db_path)
    init_db(source)
    seed_aggregate_fixture(source)
    source.commit()
    source.close()

    monkeypatch.setattr(migrate, "sqlite3", _NoBackupSqlite())
    real_copy2 = migrate.shutil.copy2

    def broken_copy(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(migrate.shutil, "copy2", broken_copy)
    with pytest.raises(OSError):
        migrate_owner_database(config, now=1000)

    assert config.db_path.is_file()
    assert not config.db_path.with_name("aistat.db.migrated").exists()
    assert list(config.tenants_dir.iterdir()) == []
    conn = sqlite3.connect(str(config.db_path))
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()

    monkeypatch.setattr(migrate.shutil, "copy2", real_copy2)
    result = migrate_owner_database(config, now=2000)
    assert result["migrated"] is True
    target = config.tenant_db_path(result["owner_user_id"])
    conn = sqlite3.connect("file:{}?mode=ro".format(target), uri=True)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()


@pytest.mark.parametrize(
    "failure_mode, expected_error",
    [
        ("no_write", OSError),
        ("partial_write", OSError),
        ("silent_corruption", MigrationError),
    ],
)
def test_owner_migration_archive_failure_rolls_back(
    tmp_path, monkeypatch, failure_mode, expected_error
):
    config = tenant_config(tmp_path)
    source = connect(config.db_path)
    init_db(source)
    seed_aggregate_fixture(source)
    source.commit()
    expected_counts = _table_counts(source)

    monkeypatch.setattr(migrate, "sqlite3", _NoBackupSqlite())
    real_copy2 = migrate.shutil.copy2

    def archive_copy(src, dst):
        if not os.path.basename(dst).startswith(".aistat-owner-archive-"):
            return real_copy2(src, dst)
        if failure_mode == "no_write":
            raise OSError("archive write failed")
        with open(dst, "wb") as fh:
            fh.write(b"partial archive bytes")
        if failure_mode == "partial_write":
            raise OSError("disk full while writing archive")
        return dst

    monkeypatch.setattr(migrate.shutil, "copy2", archive_copy)
    with pytest.raises(expected_error):
        migrate_owner_database(config, now=1000)

    # The source database, its WAL sidecars and the data are untouched.
    assert config.db_path.is_file()
    assert config.db_path.with_name("aistat.db-wal").is_file()
    conn = sqlite3.connect(str(config.db_path))
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert _table_counts(conn) == expected_counts
    conn.close()

    # No tenant DB was installed and no final, partial or temp archive
    # is left behind.
    assert list(config.tenants_dir.iterdir()) == []
    assert not config.db_path.with_name("aistat.db.migrated").exists()
    assert [
        p.name
        for p in tmp_path.iterdir()
        if p.name.startswith(".aistat-owner-")
    ] == []

    # The security transaction rolled back: no registered tenant.
    sec = sqlite3.connect(str(config.security_db_path))
    has_tenants_table = sec.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'tenants'"
    ).fetchone()[0]
    if has_tenants_table:
        assert sec.execute(
            "SELECT COUNT(*) FROM tenants"
        ).fetchone()[0] == 0
    sec.close()

    # Retry after the failure is fixed migrates the same data.
    monkeypatch.setattr(migrate.shutil, "copy2", real_copy2)
    result = migrate_owner_database(config, now=2000)
    source.close()
    assert result["migrated"] is True
    target = config.tenant_db_path(result["owner_user_id"])
    assert config.db_path.with_name("aistat.db.migrated").is_file()
    assert not config.db_path.exists()
    conn = sqlite3.connect("file:{}?mode=ro".format(target), uri=True)
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert _table_counts(conn) == expected_counts
    conn.close()
    tenant = SecurityStore(config.security_db_path).get_tenant(
        result["owner_user_id"]
    )
    assert tenant["last_snapshot_sha256"] == result["sha256"]
