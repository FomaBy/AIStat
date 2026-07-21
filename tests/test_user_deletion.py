"""Safe user + data deletion and its tenant-isolation guarantees (FAN-1183).

FAN-1222 proved no request input can cross the tenant boundary of a *live*
account. This suite proves the other half of user data control: deleting a user
erases every host-side trace of exactly that user — account, identities,
sessions, tenant registry row, connection state and the on-disk tenant database
— while leaving every other tenant untouched and readable, and it does so
safely:

* an outstanding "connect your Multica" credential is driven through the
  existing two-party revocation so the trusted worker deletes its own copy;
* removing the tenant registry row makes the purge durable — a late background
  publish for the deleted user is rejected, never resurrecting its data;
* the single admin/owner account can never be deleted through this path.

Only synthetic users, secrets and databases are used.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from aistat.config import Config
from aistat.db import connect, init_db
from aistat.security import (
    SecurityConfigError,
    SecurityStore,
    _main,
    purge_user,
    remove_tenant_databases,
)
from aistat.tenant import tenant_db_path

# Flask-contour fixtures/helpers reused verbatim so deletion is exercised
# against the exact same app construction the isolation proof uses.
from conftest import seed_aggregate_fixture
from test_wsgi import (
    INGEST_SECRET,
    public_app,
    login as flask_login,
    snapshot_signature,
)
from aistat.snapshot import create_compressed_snapshot
from test_tenant_isolation import (
    SENTINEL_B,
    _flask_authorize_session,
    _flask_make_tenant_b,
    _seed_tenant_db,
)


# --------------------------------------------------------------------------- #
# Store-level unit tests
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path):
    return SecurityStore(tmp_path / "security.db")


def _make_tenant(store, tenants_dir, subject, email, title, now=100):
    """Register an ordinary user with a tenant row and a seeded tenant DB."""
    user_id = store.find_or_create_user_by_identity(
        "google", subject, email=email, now=now
    )
    store.ensure_tenant(user_id, now=now)
    _seed_tenant_db(
        Path(tenant_db_path(tenants_dir, user_id)),
        title=title,
        project_id="P-" + subject,
    )
    return user_id


def _count(store, table, column, value):
    conn = sqlite3.connect(str(store.path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM {} WHERE {} = ?".format(table, column),
            (value,),
        ).fetchone()[0]
    finally:
        conn.close()


def _connection(store, user_id):
    conn = sqlite3.connect(str(store.path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, token, token_epoch FROM connections "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _make_active_connection(store, user_id, now=200):
    """Submit a token and drive it to ``active`` through the worker acks."""
    store.submit_connection(user_id, "", None, "tokentoken", now=now)
    leased = store.lease_pending_connections(now=now)
    pending = [p for p in leased["pending"] if p["user_id"] == user_id]
    assert pending, "expected the submitted token to be leasable"
    store.apply_worker_acks(
        [
            {
                "user_id": user_id,
                "token_epoch": pending[0]["token_epoch"],
                "lease_id": pending[0]["lease_id"],
                "result": "stored",
            }
        ],
        now=now,
    )
    assert _connection(store, user_id)["status"] == "active"


def test_purge_user_removes_only_target_across_all_stores(store, tmp_path):
    tenants = tmp_path / "tenants"
    a_id = _make_tenant(store, tenants, "alice", "alice@example.com", "AliceData")
    b_id = _make_tenant(store, tenants, "bob", "bob@example.com", "BobData")
    # Give A extra per-user state in every table a purge must clear.
    store.create_session(a_id, 3600, now=200)
    assert store.reserve_connection_submission(a_id, now=200) == 0
    a_path = Path(tenant_db_path(tenants, a_id))
    b_path = Path(tenant_db_path(tenants, b_id))
    assert a_path.is_file() and b_path.is_file()

    config = Config()
    config.security_db_path = store.path
    config.tenants_dir = tenants
    result = purge_user(config, a_id)
    assert result["existed"] is True
    assert result["connection_teardown"] == "none"
    assert str(a_path) in result["files_removed"]

    # Every host-side store for A is empty.
    for table, column in (
        ("users", "id"),
        ("oauth_identities", "user_id"),
        ("sessions", "user_id"),
        ("tenants", "user_id"),
        ("connection_throttle", "user_id"),
    ):
        assert _count(store, table, column, a_id) == 0, table
    assert not a_path.exists()

    # B is entirely intact — rows and data file.
    for table, column in (
        ("users", "id"),
        ("oauth_identities", "user_id"),
        ("tenants", "user_id"),
    ):
        assert _count(store, table, column, b_id) == 1, table
    assert b_path.is_file()
    assert store.get_tenant(b_id) is not None


def test_delete_user_revokes_active_connection_for_worker_teardown(store):
    a_id = store.find_or_create_user_by_identity(
        "google", "alice", email="alice@example.com", now=100
    )
    store.ensure_tenant(a_id, now=100)
    _make_active_connection(store, a_id)

    result = store.delete_user(a_id)
    # The account is gone but the connection survives as a token-free tombstone
    # so the worker still learns it must delete its own copy.
    assert result["connection_teardown"] == "pending"
    assert _count(store, "users", "id", a_id) == 0
    row = _connection(store, a_id)
    assert row is not None
    assert row["status"] == "revocation_pending"
    assert row["token"] is None

    # The worker's delete-ack advances it to the terminal ``revoked``.
    ok = store.apply_worker_acks(
        [{"user_id": a_id, "token_epoch": row["token_epoch"], "result": "revoked"}]
    )
    assert ok[0]["ok"] is True
    assert _connection(store, a_id)["status"] == "revoked"


def test_delete_user_drops_already_revoked_connection_row(store):
    a_id = store.find_or_create_user_by_identity(
        "google", "alice", email="alice@example.com", now=100
    )
    store.ensure_tenant(a_id, now=100)
    _make_active_connection(store, a_id)
    store.revoke_connection(a_id)
    epoch = _connection(store, a_id)["token_epoch"]
    store.apply_worker_acks(
        [{"user_id": a_id, "token_epoch": epoch, "result": "revoked"}]
    )
    assert _connection(store, a_id)["status"] == "revoked"

    result = store.delete_user(a_id)
    assert result["connection_teardown"] == "revoked"
    assert _connection(store, a_id) is None


def test_delete_user_refuses_owner_account(store, tmp_path):
    owner_id = store.ensure_owner_user("sergey", email="owner@example.com")
    store.ensure_tenant(owner_id, now=100)
    tenants = tmp_path / "tenants"
    _seed_tenant_db(Path(tenant_db_path(tenants, owner_id)), title="OwnerData")

    with pytest.raises(SecurityConfigError):
        store.delete_user(owner_id)

    # Nothing was touched.
    assert _count(store, "users", "id", owner_id) == 1
    assert store.get_tenant(owner_id) is not None
    assert Path(tenant_db_path(tenants, owner_id)).is_file()


def test_deleted_user_session_dies_and_identity_reregisters_fresh(store):
    a_id = store.find_or_create_user_by_identity(
        "google", "alice", email="alice@example.com", now=100
    )
    store.ensure_tenant(a_id, now=100)
    sid = store.create_session(a_id, 3600, now=200)
    assert store.resolve_session(sid, now=200)["user_id"] == a_id

    store.delete_user(a_id)

    # The live session no longer resolves to any tenant.
    assert store.resolve_session(sid, now=200) is None
    # The same external identity now maps to a brand-new, empty account.
    new_id = store.find_or_create_user_by_identity(
        "google", "alice", email="alice@example.com", now=300
    )
    assert new_id != a_id
    assert store.get_tenant(new_id) is None


def test_delete_missing_user_is_a_safe_noop(store, tmp_path):
    tenants = tmp_path / "tenants"
    b_id = _make_tenant(store, tenants, "bob", "bob@example.com", "BobData")
    result = store.delete_user(b_id + 9999)
    assert result["existed"] is False
    assert result["connection_teardown"] == "none"
    # The unrelated tenant is untouched.
    assert store.get_tenant(b_id) is not None
    assert Path(tenant_db_path(tenants, b_id)).is_file()


def test_remove_tenant_databases_clears_backup_and_sidecars(tmp_path):
    tenants = tmp_path / "tenants"
    tenants.mkdir()
    base = Path(tenant_db_path(tenants, 42))
    base.write_bytes(b"db")
    Path(str(base) + "-wal").write_bytes(b"wal")
    Path(str(base) + "-shm").write_bytes(b"shm")
    Path(str(base) + ".previous").write_bytes(b"prev")

    removed = remove_tenant_databases(tenants, 42)

    assert set(removed) == {
        str(base),
        str(base) + "-wal",
        str(base) + "-shm",
        str(base) + ".previous",
    }
    assert not any(p.exists() for p in tenants.iterdir())
    # Rejects a hostile id before touching the filesystem.
    with pytest.raises(ValueError):
        remove_tenant_databases(tenants, "../escape")


def test_cli_delete_user_purges_and_refuses_owner(tmp_path, monkeypatch, capsys):
    sec = tmp_path / "security.db"
    tenants = tmp_path / "tenants"
    monkeypatch.setenv("AISTAT_SECURITY_DB_PATH", str(sec))
    monkeypatch.setenv("AISTAT_TENANTS_DIR", str(tenants))
    store = SecurityStore(sec)
    owner_id = store.ensure_owner_user("sergey", email="owner@example.com")
    store.ensure_tenant(owner_id, now=100)
    a_id = _make_tenant(store, tenants, "alice", "alice@example.com", "AliceData")

    # The owner account is refused fail-closed with a non-zero exit and no
    # change to its data.
    assert _main(["delete-user", "--user-id", str(owner_id), "--yes"]) == 1
    assert store.get_tenant(owner_id) is not None

    # An ordinary user is purged end to end.
    assert _main(["delete-user", "--user-id", str(a_id), "--yes"]) == 0
    assert store.get_tenant(a_id) is None
    assert not Path(tenant_db_path(tenants, a_id)).exists()
    assert '"existed": true' in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Flask contour: a purged tenant is denied and cannot be resurrected
# --------------------------------------------------------------------------- #


def test_flask_deleted_tenant_is_denied_and_cannot_be_resurrected(
    public_app, tmp_path
):
    """AC: after deletion a user reaches no data (direct, session or background
    publish), while every other tenant stays isolated and readable."""
    app, config = public_app
    b_id = _flask_make_tenant_b(config)

    client_b = app.test_client()
    _flask_authorize_session(client_b, config, b_id, "bob@example.com")
    meta_b = client_b.get("/api/meta", base_url="https://localhost")
    assert meta_b.status_code == 200
    assert {p["title"] for p in meta_b.get_json()["projects"]} == {SENTINEL_B}

    store = SecurityStore(config.security_db_path)
    result = store.delete_user(b_id)
    assert result["existed"] is True
    assert remove_tenant_databases(config.tenants_dir, b_id)

    # B's still-cached session cookie now authorises nothing.
    after = client_b.get("/api/meta", base_url="https://localhost")
    assert after.status_code == 401

    # The tenant is gone, so a signed background publish for B is rejected and
    # never recreates its database.
    assert store.get_tenant(b_id) is None
    source = tmp_path / "resurrect.db"
    conn = connect(source)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()
    payload = create_compressed_snapshot(source)
    ts = int(time.time())
    publisher = app.test_client()
    resp = publisher.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Tenant": str(b_id),
            "X-AIStat-Timestamp": str(ts),
            "X-AIStat-Signature": snapshot_signature(INGEST_SECRET, b_id, ts, payload),
        },
    )
    assert resp.status_code == 401
    assert not config.tenant_db_path(b_id).exists()

    # The owner tenant is untouched by B's deletion.
    owner = app.test_client()
    flask_login(owner)
    owner_meta = owner.get("/api/meta", base_url="https://localhost")
    assert owner_meta.status_code == 200
    assert {p["title"] for p in owner_meta.get_json()["projects"]} == {
        "Alpha",
        "Beta",
    }
