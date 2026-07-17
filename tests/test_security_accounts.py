"""Unit tests for the multi-user account model in SecurityStore."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from aistat.security import (
    OAUTH_STATE_TTL_SECONDS,
    SecurityConfigError,
    SecurityStore,
    session_id_hash,
)


def test_link_identity_to_owner_is_idempotent(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    owner = store.ensure_owner_user("sergey", email="owner@example.com", now=10)
    first = store.link_identity_to_owner(
        "google", "sub-1", owner, email="owner@example.com", now=100
    )
    again = store.link_identity_to_owner(
        "google", "sub-1", owner, email="owner@example.com", now=200
    )
    assert first == owner == again
    # no extra user was created — only the owner exists
    conn = store._connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    finally:
        conn.close()
    assert count == 1


def test_link_identity_to_owner_rejects_subject_owned_elsewhere(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    owner = store.ensure_owner_user("sergey", email="owner@example.com", now=10)
    other = store.find_or_create_user_by_identity("google", "sub-2", now=20)
    assert other != owner
    with pytest.raises(SecurityConfigError):
        store.link_identity_to_owner("google", "sub-2", owner, now=100)


def test_link_identity_to_owner_requires_admin_owner(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    non_admin = store.find_or_create_user_by_identity("google", "sub-3", now=20)
    with pytest.raises(SecurityConfigError):
        store.link_identity_to_owner("google", "sub-4", non_admin, now=100)


def test_find_or_create_user_is_idempotent_per_identity(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    first = store.find_or_create_user_by_identity(
        "google", "sub-1", email="a@example.com", display_name="Alice", now=100
    )
    again = store.find_or_create_user_by_identity(
        "google", "sub-1", email="a@example.com", display_name="Alice", now=200
    )
    assert first == again


def test_distinct_identities_map_to_distinct_users(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    alice = store.find_or_create_user_by_identity("google", "sub-1", now=100)
    bob = store.find_or_create_user_by_identity("google", "sub-2", now=100)
    assert alice != bob
    # the same subject on another provider is a separate identity/user
    cross = store.find_or_create_user_by_identity("yandex", "sub-1", now=100)
    assert cross not in (alice, bob)


def test_oauth_state_is_single_use(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    store.put_oauth_state(
        "state-1", "google", next_url="/api/meta", client_hash="hash-1", now=100
    )
    taken = store.take_oauth_state("state-1", now=110)
    assert taken == {
        "provider": "google",
        "next_url": "/api/meta",
        "client_hash": "hash-1",
    }
    # a second consumption of the same state is rejected
    assert store.take_oauth_state("state-1", now=111) is None


def test_oauth_state_unknown_is_rejected(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    assert store.take_oauth_state("never-issued", now=100) is None


def test_oauth_state_expires(tmp_path):
    path = tmp_path / "security.db"
    store = SecurityStore(path)
    store.put_oauth_state("state-x", "google", next_url="/", now=100)
    expired_at = 100 + OAUTH_STATE_TTL_SECONDS + 1
    assert store.take_oauth_state("state-x", now=expired_at) is None
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM oauth_state WHERE state = 'state-x'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_old_oauth_state_schema_is_migrated_fail_closed(tmp_path):
    path = tmp_path / "security.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE oauth_state ("
            "state TEXT PRIMARY KEY, provider TEXT NOT NULL, "
            "next_url TEXT, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO oauth_state "
            "(state, provider, next_url, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-state", "google", "/", 100),
        )
        conn.commit()
    finally:
        conn.close()

    store = SecurityStore(path)
    conn = sqlite3.connect(str(path))
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(oauth_state)")
        }
        assert "client_hash" in columns
    finally:
        conn.close()
    assert store.take_oauth_state("legacy-state", now=101) == {
        "provider": "google",
        "next_url": "/",
        "client_hash": None,
    }


def test_old_schema_migration_is_safe_under_concurrent_startup(tmp_path):
    path = tmp_path / "security.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE oauth_state ("
            "state TEXT PRIMARY KEY, provider TEXT NOT NULL, "
            "next_url TEXT, created_at INTEGER NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _index: SecurityStore(path), range(32)))

    conn = sqlite3.connect(str(path))
    try:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(oauth_state)")
        ]
        assert columns.count("client_hash") == 1
    finally:
        conn.close()


def test_account_model_does_not_disturb_throttle_or_ingest(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    user_id = store.find_or_create_user_by_identity("google", "sub", now=100)
    store.ensure_tenant(user_id, now=100)
    store.put_oauth_state("s", "google", now=100)
    # pre-existing throttle and ingest replay state keep working
    assert store.record_login_failure("client", now=100) == 0
    assert store.login_retry_after("client", now=100) == 0
    assert store.record_tenant_snapshot(user_id, 500, "a" * 64) is True
    assert store.record_tenant_snapshot(user_id, 500, "a" * 64) is False


def test_session_lifecycle_revocation_and_expiry(tmp_path):
    path = tmp_path / "security.db"
    store = SecurityStore(path)
    first = store.create_session(7, ttl_seconds=100, now=1000)
    second = store.create_session(7, ttl_seconds=100, now=1000)
    assert first != second
    assert store.session_is_active(first, now=1050)
    assert store.session_is_active(second, now=1050)

    # revocation kills exactly the targeted session
    store.revoke_session(first)
    assert not store.session_is_active(first, now=1050)
    assert store.session_is_active(second, now=1050)

    # expiry fails closed even while the row still exists
    assert not store.session_is_active(second, now=1100)

    # unknown, empty and non-string ids fail closed without errors
    assert not store.session_is_active("never-issued", now=1050)
    assert not store.session_is_active(None, now=1050)
    assert not store.session_is_active(12345, now=1050)
    store.revoke_session(None)
    store.revoke_session(12345)


def test_sessions_store_only_hashes_and_purge_expired_rows(tmp_path):
    path = tmp_path / "security.db"
    store = SecurityStore(path)
    stale = store.create_session(7, ttl_seconds=100, now=1000)
    fresh = store.create_session(7, ttl_seconds=100, now=1200)
    conn = sqlite3.connect(str(path))
    try:
        rows = [
            row[0]
            for row in conn.execute("SELECT sid_hash FROM sessions")
        ]
    finally:
        conn.close()
    # the stale row was purged by the later create_session transaction
    assert rows == [session_id_hash(fresh)]
    assert stale not in rows
    assert fresh not in rows
