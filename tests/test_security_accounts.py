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


def test_old_sessions_schema_migrates_and_purges_pre_opaque_rows(tmp_path):
    # FAN-1392: an old sessions table (no csrf column) with a live row. The
    # one-time migration adds the column and drops every pre-opaque row, so a
    # cookie carrying that old inner id can never resolve after the upgrade —
    # while fresh opaque sessions then persist across restarts unpurged.
    path = tmp_path / "security.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            "sid_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sessions (sid_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id_hash("legacy-sid"), 7, 100, 10 ** 12),
        )
        conn.commit()
    finally:
        conn.close()

    store = SecurityStore(path)
    conn = sqlite3.connect(str(path))
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        assert "csrf" in columns
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        conn.close()
    assert store.resolve_session("legacy-sid") is None

    sid = store.create_session(7, ttl_seconds=3600, now=1000)
    assert store.resolve_session(sid, now=1001)["user_id"] == 7
    # A second worker start must not re-purge live sessions.
    restarted = SecurityStore(path)
    resolved = restarted.resolve_session(sid, now=1002)
    assert resolved["user_id"] == 7
    assert resolved["csrf"]


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


def _counts(store):
    conn = store._connect()
    try:
        return {
            "users": conn.execute(
                "SELECT COUNT(*) AS n FROM users"
            ).fetchone()["n"],
            "identities": conn.execute(
                "SELECT COUNT(*) AS n FROM oauth_identities"
            ).fetchone()["n"],
            "tenants": conn.execute(
                "SELECT COUNT(*) AS n FROM tenants"
            ).fetchone()["n"],
        }
    finally:
        conn.close()


def test_register_creates_user_identity_and_tenant_atomically(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    result = store.register_or_link_identity(
        "google", "sub-1", email="new@example.com", display_name="New",
        admin_email="owner@example.com", allowed_emails=frozenset(),
        owner_user_id=None, now=100,
    )
    assert result["outcome"] == "created"
    user_id = result["user_id"]
    # exactly one user (ordinary), one identity and one tenant row exist
    assert _counts(store) == {"users": 1, "identities": 1, "tenants": 1}
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        tenant = conn.execute(
            "SELECT user_id FROM tenants WHERE user_id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    assert int(row["is_admin"]) == 0
    assert tenant is not None


def test_register_is_subject_first_and_ignores_email_and_allowlist(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    first = store.register_or_link_identity(
        "google", "sub-1", email="a@example.com", allowed_emails=frozenset(),
        now=100,
    )
    assert first["outcome"] == "created"
    # a later login with a changed email keeps the same account, no new rows
    again = store.register_or_link_identity(
        "google", "sub-1", email="b@example.com",
        allowed_emails=frozenset({"only@else.com"}), now=200,
    )
    assert again == {"user_id": first["user_id"], "outcome": "existing"}
    assert _counts(store) == {"users": 1, "identities": 1, "tenants": 1}


def test_register_ordinary_subject_is_never_elevated_to_owner(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    owner = store.ensure_owner_user("sergey", email="owner@example.com", now=10)
    ordinary = store.register_or_link_identity(
        "google", "sub-1", email="person@example.com",
        admin_email="owner@example.com", allowed_emails=frozenset(),
        owner_user_id=owner, now=100,
    )
    assert ordinary["user_id"] != owner
    # the same subject later presents the admin email: subject-first means it
    # stays its own ordinary account, never merged into or elevated to the owner
    again = store.register_or_link_identity(
        "google", "sub-1", email="owner@example.com",
        admin_email="owner@example.com", allowed_emails=frozenset(),
        owner_user_id=owner, now=200,
    )
    assert again == {"user_id": ordinary["user_id"], "outcome": "existing"}
    conn = store._connect()
    try:
        is_admin = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (ordinary["user_id"],)
        ).fetchone()["is_admin"]
        admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert int(is_admin) == 0
    assert int(admins) == 1


def test_register_links_new_admin_subject_to_owner_without_new_tenant(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    owner = store.ensure_owner_user("sergey", email="owner@example.com", now=10)
    linked = store.register_or_link_identity(
        "google", "owner-sub", email="Owner@Example.com",
        admin_email="owner@example.com", allowed_emails=frozenset(),
        owner_user_id=owner, now=100,
    )
    assert linked == {"user_id": owner, "outcome": "linked_owner"}
    # no ordinary user was created, and no tenant row was minted for the owner
    # by the link (the owner keeps whatever tenant it already had — none here)
    assert _counts(store) == {"users": 1, "identities": 1, "tenants": 0}


def test_register_denies_new_outsider_under_nonempty_allowlist(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    denied = store.register_or_link_identity(
        "google", "sub-out", email="stranger@example.com",
        admin_email="owner@example.com",
        allowed_emails=frozenset({"allowed@example.com"}), now=100,
    )
    assert denied == {"user_id": None, "outcome": "denied"}
    # nothing at all was written for the rejected subject
    assert _counts(store) == {"users": 0, "identities": 0, "tenants": 0}
    # a normalized, allow-listed email does register
    ok = store.register_or_link_identity(
        "google", "sub-in", email="Allowed@Example.com",
        admin_email="owner@example.com",
        allowed_emails=frozenset({"allowed@example.com"}), now=100,
    )
    assert ok["outcome"] == "created"


def test_register_stores_canonical_email_and_matches_allowlist_case_insensitively(
    tmp_path,
):
    store = SecurityStore(tmp_path / "security.db")
    # the policy hands the store an already-canonical (trimmed) address; the
    # store persists it verbatim and its allow-list match is case-insensitive
    result = store.register_or_link_identity(
        "google", "sub-pad", email="New@Example.com",
        admin_email="owner@example.com",
        allowed_emails=frozenset({"new@example.com"}), now=100,
    )
    assert result["outcome"] == "created"
    conn = store._connect()
    try:
        user_email = conn.execute(
            "SELECT email FROM users WHERE id = ?", (result["user_id"],)
        ).fetchone()["email"]
        id_email = conn.execute(
            "SELECT email FROM oauth_identities WHERE subject = ?", ("sub-pad",)
        ).fetchone()["email"]
    finally:
        conn.close()
    assert user_email == "New@Example.com"
    assert id_email == "New@Example.com"


def test_register_concurrent_new_subject_yields_one_account(tmp_path):
    store = SecurityStore(tmp_path / "security.db")

    def register(_index):
        return store.register_or_link_identity(
            "google", "race-sub", email="race@example.com",
            allowed_emails=frozenset(), now=100,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(register, range(8)))

    user_ids = {r["user_id"] for r in results}
    assert len(user_ids) == 1
    # exactly one callback created the account; every other one saw it existing
    outcomes = sorted(r["outcome"] for r in results)
    assert outcomes.count("created") == 1
    assert set(outcomes) <= {"created", "existing"}
    # exactly one user, identity and tenant survived the concurrent callbacks
    assert _counts(store) == {"users": 1, "identities": 1, "tenants": 1}


class _FailingConnection:
    """Wraps a real connection and raises on a chosen statement."""

    def __init__(self, real, fail_fragment):
        self._real = real
        self._fail_fragment = fail_fragment

    def execute(self, sql, *args):
        if self._fail_fragment in sql:
            raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_register_injected_failure_rolls_back_completely(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    real = store._connect()
    failing = _FailingConnection(real, "INSERT INTO tenants")
    # the user and identity inserts run, then the tenant insert fails mid
    # transaction; the whole registration must roll back with no partial rows
    original_connect = store._connect
    store._connect = lambda: failing
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.register_or_link_identity(
                "google", "sub-x", email="x@example.com",
                allowed_emails=frozenset(), now=100,
            )
    finally:
        store._connect = original_connect
    assert _counts(store) == {"users": 0, "identities": 0, "tenants": 0}


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
