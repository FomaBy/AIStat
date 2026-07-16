"""Tenant path safety, owner resolution and legacy data migration."""

import sqlite3

import pytest

from aistat.config import Config
from aistat.db import connect, init_db
from aistat.migrate import migrate_owner_database
from aistat.security import SecurityConfigError, SecurityStore
from conftest import seed_aggregate_fixture


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
