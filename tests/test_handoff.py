"""Unit tests for the shared token-handoff protocol (FAN-1220).

Covers the worker-channel signature, request validators and the atomic
connection state machine both WSGI contours share via ``aistat.handoff``.
"""

import concurrent.futures
import sqlite3
import threading
import time

import pytest

from aistat import handoff
from aistat.security import SecurityStore

WORKER_SECRET = "unit-worker-" + "w" * 48
TOKEN = "mlt_unit_secret_token_1f2e3d4c5b6a"
NONCE = "a" * 24
NOW = 1_800_000_000


@pytest.fixture
def store(tmp_path):
    return SecurityStore(tmp_path / "security.db")


@pytest.fixture
def user_id(store):
    return store.find_or_create_user_by_identity(
        "google", "subject-1", email="user@example.com"
    )


def submit(store, user_id, token=TOKEN, now=NOW):
    return store.submit_connection(
        user_id, "https://multica.example", "My workspace", token, now=now
    )


# --- signature ---------------------------------------------------------


def test_worker_signature_binds_all_fields():
    body = b'{"acks":[]}'
    signature = handoff.worker_signature(
        WORKER_SECRET, handoff.WORKER_PULL_PATH, NOW, NONCE, body
    )
    assert signature.startswith("v1=")
    timestamp, nonce = handoff.verify_worker_request(
        WORKER_SECRET,
        handoff.WORKER_PULL_PATH,
        str(NOW),
        NONCE,
        signature,
        body,
        300,
        now=NOW,
    )
    assert (timestamp, nonce) == (NOW, NONCE)

    for path, ts, nonce_v, body_v in (
        (handoff.WORKER_ACK_PATH, NOW, NONCE, body),      # other endpoint
        (handoff.WORKER_PULL_PATH, NOW + 1, NONCE, body),  # other timestamp
        (handoff.WORKER_PULL_PATH, NOW, "b" * 24, body),   # other nonce
        (handoff.WORKER_PULL_PATH, NOW, NONCE, b"{}"),     # other body
    ):
        with pytest.raises(ValueError):
            handoff.verify_worker_request(
                WORKER_SECRET, path, str(ts), nonce_v, signature,
                body_v, 300, now=NOW,
            )
    with pytest.raises(ValueError):
        handoff.verify_worker_request(
            "x" * 48, handoff.WORKER_PULL_PATH, str(NOW), NONCE,
            signature, body, 300, now=NOW,
        )


def test_worker_request_timestamp_and_nonce_rules():
    body = b"{}"

    def signed(timestamp, nonce):
        return handoff.worker_signature(
            WORKER_SECRET, handoff.WORKER_PULL_PATH, timestamp, nonce, body
        )

    stale = NOW - 301
    with pytest.raises(ValueError):
        handoff.verify_worker_request(
            WORKER_SECRET, handoff.WORKER_PULL_PATH, str(stale), NONCE,
            signed(stale, NONCE), body, 300, now=NOW,
        )
    with pytest.raises(ValueError):
        handoff.verify_worker_request(
            WORKER_SECRET, handoff.WORKER_PULL_PATH, "soon", NONCE,
            signed(NOW, NONCE), body, 300, now=NOW,
        )
    for bad_nonce in ("short", "!" * 24, "c" * 129, None):
        with pytest.raises(ValueError):
            handoff.verify_worker_request(
                WORKER_SECRET, handoff.WORKER_PULL_PATH, str(NOW), bad_nonce,
                signed(NOW, bad_nonce or ""), body, 300, now=NOW,
            )


def test_nonce_is_single_use_until_purged(store):
    connect = store._connect
    assert handoff.consume_worker_nonce(connect, NONCE, 300, now=NOW)
    assert not handoff.consume_worker_nonce(connect, NONCE, 300, now=NOW)
    # After the retention window the row is purged and the timestamp check
    # alone rejects such an old request.
    later = NOW + 300 * handoff.WORKER_NONCE_TTL_FACTOR + 1
    assert handoff.consume_worker_nonce(connect, NONCE, 300, now=later)


# --- validators --------------------------------------------------------


def test_token_validator():
    assert handoff.validate_connection_token("  " + TOKEN + "\n") == TOKEN
    for bad in (None, 7, "short", "x" * 513, "with space inside", "тайна-key"):
        with pytest.raises(ValueError):
            handoff.validate_connection_token(bad)


def test_server_url_validator():
    assert (
        handoff.validate_server_url("https://multica.example/")
        == "https://multica.example"
    )
    assert (
        handoff.validate_server_url("", "https://fallback.example")
        == "https://fallback.example"
    )
    assert (
        handoff.validate_server_url("http://localhost:8787")
        == "http://localhost:8787"
    )
    for bad in (
        "ftp://multica.example",
        "http://evil.example",
        "http://localhost.evil.example",
        "https://user@evil.example",
        "https:///path-only",
        "https://bad url",
        "https://" + "a" * 520,
    ):
        with pytest.raises(ValueError):
            handoff.validate_server_url(bad)
    with pytest.raises(ValueError):
        handoff.validate_server_url("", None)


def test_workspace_label_validator():
    assert handoff.validate_workspace_label(None) is None
    assert handoff.validate_workspace_label("  ") is None
    assert handoff.validate_workspace_label(" Team A ") == "Team A"
    for bad in ("x" * 129, "line\nbreak", 5):
        with pytest.raises(ValueError):
            handoff.validate_workspace_label(bad)


# --- state machine -----------------------------------------------------


def test_submit_and_status_projection(store, user_id):
    view = submit(store, user_id)
    assert view["status"] == "pending"
    assert view["server_url"] == "https://multica.example"
    assert view["workspace_label"] == "My workspace"
    assert "token" not in view and "lease_id" not in view
    assert store.connection_status(user_id)["status"] == "pending"
    with pytest.raises(ValueError):
        submit(store, user_id + 999)  # unknown user


def test_full_handoff_erases_host_token(store, user_id, tmp_path):
    submit(store, user_id)
    security_db = tmp_path / "security.db"
    assert TOKEN.encode() in security_db.read_bytes()

    state = store.lease_pending_connections(now=NOW)
    assert state["revoked"] == []
    (entry,) = state["pending"]
    assert entry["token"] == TOKEN and entry["token_epoch"] == 1

    ok, status = handoff.ack_connection_stored(
        store._connect, user_id, entry["lease_id"], 1, now=NOW
    )
    assert (ok, status) == (True, "active")
    assert store.connection_status(user_id)["status"] == "active"
    # The token is physically absent from the database file, not just from
    # the row: secure_delete zeroes the freed bytes.
    assert TOKEN.encode() not in security_db.read_bytes()
    # A second ack for the consumed epoch changes nothing.
    ok, reason = handoff.ack_connection_stored(
        store._connect, user_id, entry["lease_id"], 1, now=NOW
    )
    assert (ok, reason) == (False, "stale-epoch")


def test_ack_requires_current_lease_and_epoch(store, user_id):
    submit(store, user_id)
    first = store.lease_pending_connections(now=NOW)["pending"][0]
    second = store.lease_pending_connections(now=NOW)["pending"][0]
    assert first["lease_id"] != second["lease_id"]
    ok, reason = handoff.ack_connection_stored(
        store._connect, user_id, first["lease_id"], 1, now=NOW
    )
    assert (ok, reason) == (False, "stale-lease")
    # An expired lease is refused even when it matches.
    late = NOW + handoff.WORKER_LEASE_SECONDS + 1
    ok, reason = handoff.ack_connection_stored(
        store._connect, user_id, second["lease_id"], 1, now=late
    )
    assert (ok, reason) == (False, "stale-lease")


def test_replace_invalidates_inflight_ack(store, user_id, tmp_path):
    submit(store, user_id, token=TOKEN + "old")
    lease = store.lease_pending_connections(now=NOW)["pending"][0]
    submit(store, user_id, token=TOKEN + "new", now=NOW + 5)
    ok, reason = handoff.ack_connection_stored(
        store._connect, user_id, lease["lease_id"], 1, now=NOW + 6
    )
    assert (ok, reason) == (False, "stale-epoch")
    raw = (tmp_path / "security.db").read_bytes()
    assert (TOKEN + "old").encode() not in raw  # replaced value is erased
    (entry,) = store.lease_pending_connections(now=NOW + 7)["pending"]
    assert entry["token"] == TOKEN + "new" and entry["token_epoch"] == 2
    ok, status = handoff.ack_connection_stored(
        store._connect, user_id, entry["lease_id"], 2, now=NOW + 8
    )
    assert (ok, status) == (True, "active")


def test_revoke_erases_token_and_notifies_worker(store, user_id, tmp_path):
    submit(store, user_id)
    assert store.revoke_connection(user_id, now=NOW)
    assert TOKEN.encode() not in (tmp_path / "security.db").read_bytes()
    state = store.lease_pending_connections(now=NOW)
    assert state["pending"] == []
    assert state["revoked"] == [{"user_id": user_id, "token_epoch": 2}]
    ok, status = handoff.ack_connection_revoked(
        store._connect, user_id, 2, now=NOW
    )
    assert (ok, status) == (True, "revoked")
    assert store.lease_pending_connections(now=NOW)["revoked"] == []
    # Idempotent, and unknown connections are refused.
    assert handoff.ack_connection_revoked(
        store._connect, user_id, 2, now=NOW
    ) == (True, "revoked")
    assert not store.revoke_connection(user_id + 999)


def test_sync_reports_flip_between_active_and_error(store, user_id):
    submit(store, user_id)
    entry = store.lease_pending_connections(now=NOW)["pending"][0]
    handoff.ack_connection_stored(
        store._connect, user_id, entry["lease_id"], 1, now=NOW
    )
    ok, status = handoff.record_connection_sync(
        store._connect, user_id, 1, False, error="CLI failed: exit 1",
        now=NOW + 10,
    )
    assert (ok, status) == (True, "error")
    view = store.connection_status(user_id)
    assert view["status"] == "error"
    assert view["last_sync_error"] == "CLI failed: exit 1"
    ok, status = handoff.record_connection_sync(
        store._connect, user_id, 1, True, now=NOW + 20
    )
    assert (ok, status) == (True, "active")
    view = store.connection_status(user_id)
    assert view["status"] == "active"
    assert view["last_sync_error"] is None
    assert view["last_synced_at"] == NOW + 20
    # Stale epoch and wrong state are refused.
    assert handoff.record_connection_sync(
        store._connect, user_id, 2, True
    ) == (False, "stale-epoch")
    submit(store, user_id, now=NOW + 30)
    assert handoff.record_connection_sync(
        store._connect, user_id, 2, True
    ) == (False, "invalid-state")


def test_submission_throttle_reserves_atomically(store, user_id):
    barrier = threading.Barrier(12)

    def reserve(_):
        barrier.wait()
        return store.reserve_connection_submission(user_id, now=NOW)

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(reserve, range(12)))

    assert results.count(0) == handoff.CONNECTION_MAX_SUBMISSIONS
    assert sum(result > 0 for result in results) == 2
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT window_started, submissions FROM connection_throttle "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["window_started"] == NOW
    assert row["submissions"] == handoff.CONNECTION_MAX_SUBMISSIONS
    assert store.reserve_connection_submission(user_id, now=NOW + 899) == 1
    assert (
        store.reserve_connection_submission(
            user_id, now=NOW + handoff.CONNECTION_WINDOW_SECONDS
        )
        == 0
    )


def test_submission_throttle_is_tenant_scoped(store, user_id):
    other_user_id = store.find_or_create_user_by_identity(
        "google", "subject-2", email="other@example.com"
    )
    for _ in range(handoff.CONNECTION_MAX_SUBMISSIONS):
        assert store.reserve_connection_submission(user_id, now=NOW) == 0
    assert store.reserve_connection_submission(user_id, now=NOW) > 0
    assert store.reserve_connection_submission(other_user_id, now=NOW) == 0


def test_submission_throttle_rolls_back_on_sqlite_error(user_id):
    class FailingConnection:
        def __init__(self):
            self.rollback_count = 0
            self.close_count = 0

        def execute(self, _sql, _parameters=()):
            raise sqlite3.OperationalError("synthetic throttle failure")

        def rollback(self):
            self.rollback_count += 1

        def close(self):
            self.close_count += 1

    connection = FailingConnection()
    with pytest.raises(sqlite3.OperationalError, match="synthetic throttle"):
        handoff.reserve_connection_submission(
            lambda: connection, user_id, now=NOW
        )
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_apply_worker_acks_batch(store, user_id):
    submit(store, user_id)
    entry = store.lease_pending_connections(now=NOW)["pending"][0]
    results = store.apply_worker_acks(
        [
            {
                "user_id": user_id,
                "token_epoch": 1,
                "lease_id": entry["lease_id"],
                "result": "stored",
            },
            {"user_id": user_id, "token_epoch": 1, "result": "sync_ok"},
            {"user_id": 424242, "token_epoch": 1, "result": "revoked"},
            {"user_id": user_id, "result": "unexpected"},
            "not-a-dict",
        ],
        now=NOW,
    )
    assert [r["ok"] for r in results] == [True, True, False, False, False]
    assert results[0]["status"] == "active"
    assert results[2]["reason"] == "unknown-connection"
    assert results[3]["reason"] == "invalid-request"
    with pytest.raises(ValueError):
        store.apply_worker_acks({"not": "a list"})
    with pytest.raises(ValueError):
        store.apply_worker_acks([{}] * (handoff.MAX_ACK_ITEMS + 1))


# --- hardening: official host, pending TTL, readiness, ack-gated revoke -


def test_official_server_url_is_pinned():
    official = "https://multica.ai"
    assert handoff.normalize_official_server_url("", official) == official
    assert handoff.normalize_official_server_url(None, official) == official
    assert (
        handoff.normalize_official_server_url("https://multica.ai/", official)
        == official
    )
    # No alternate host, subdomain suffix, IP, port, credentials or scheme
    # downgrade can point a stored connection anywhere but the official host.
    for bad in (
        "https://evil.example",
        "https://multica.ai.evil.example",
        "http://multica.ai",
        "https://multica.ai:8443",
        "https://user@multica.ai",
        "https://127.0.0.1",
        "ftp://multica.ai",
        7,
    ):
        with pytest.raises(ValueError):
            handoff.normalize_official_server_url(bad, official)
    # A misconfigured official URL is rejected outright (fail closed).
    for bad_official in (
        "http://multica.ai",
        "https://multica.ai/path",
        "https://multica.ai:8443",
        "https://user@multica.ai",
        "",
        "not a url",
    ):
        with pytest.raises(ValueError):
            handoff.canonical_official_server_url(bad_official)


def test_pending_token_expires_into_revocation_pending(store, user_id, tmp_path):
    submit(store, user_id)  # pending at NOW
    security_db = tmp_path / "security.db"
    assert TOKEN.encode() in security_db.read_bytes()
    ttl = handoff.PENDING_TTL_DEFAULT_SECONDS
    later = NOW + ttl + 1
    # A pull past the TTL erases the plaintext and hands it to the worker as a
    # revocation to confirm, instead of leasing a stale token.
    state = store.lease_pending_connections(ttl, now=later)
    assert state["pending"] == []
    assert state["revoked"] == [{"user_id": user_id, "token_epoch": 2}]
    assert TOKEN.encode() not in security_db.read_bytes()
    view = store.connection_status(user_id, ttl, now=later)
    assert view["status"] == "revocation_pending"
    assert view["last_sync_error"] == handoff.PENDING_EXPIRED_REASON


def test_status_read_alone_purges_expired_pending(store, user_id, tmp_path):
    submit(store, user_id)
    ttl = handoff.PENDING_TTL_DEFAULT_SECONDS
    assert (
        store.connection_status(user_id, ttl, now=NOW + 5)["status"] == "pending"
    )
    view = store.connection_status(user_id, ttl, now=NOW + ttl + 1)
    assert view["status"] == "revocation_pending"
    assert TOKEN.encode() not in (tmp_path / "security.db").read_bytes()


def test_worker_readiness_tracks_last_pull(store):
    ttl = handoff.WORKER_READINESS_TTL_DEFAULT_SECONDS
    assert not store.worker_ready(ttl, now=NOW)  # no pull has landed yet
    store.lease_pending_connections(now=NOW)  # a pull records the heartbeat
    assert store.worker_ready(ttl, now=NOW + ttl)
    assert not store.worker_ready(ttl, now=NOW + ttl + 1)  # gone stale


def test_replacement_pending_is_distinct_from_first_connect(store, user_id):
    assert submit(store, user_id)["status"] == "pending"
    assert submit(store, user_id, now=NOW + 1)["status"] == "replacement_pending"


def test_revoked_only_after_worker_delete_ack(store, user_id):
    submit(store, user_id)
    assert store.revoke_connection(user_id, now=NOW)
    # Still pending on the worker side until it confirms the delete.
    assert store.connection_status(user_id)["status"] == "revocation_pending"
    ok, status = handoff.ack_connection_revoked(
        store._connect, user_id, 2, now=NOW
    )
    assert (ok, status) == (True, "revoked")
    assert store.connection_status(user_id)["status"] == "revoked"
    # A stale epoch cannot complete a revocation opened for a newer lifecycle.
    submit(store, user_id, now=NOW + 1)  # reconnect -> epoch 3
    assert handoff.ack_connection_revoked(
        store._connect, user_id, 2, now=NOW + 2
    ) == (False, "stale-epoch")


def test_account_cleanup_leaves_no_active_worker_credential(
    store, user_id, tmp_path
):
    # Model account deletion / credential cleanup: it erases the host token and
    # cannot read `revoked` until the worker acknowledges deleting its copy.
    submit(store, user_id)
    lease = store.lease_pending_connections(now=NOW)["pending"][0]
    handoff.ack_connection_stored(
        store._connect, user_id, lease["lease_id"], 1, now=NOW
    )
    assert store.connection_status(user_id)["status"] == "active"
    assert store.revoke_connection(user_id, now=NOW + 1)  # cleanup primitive
    # Visible fail-closed intermediate state, never left as an active credential.
    assert store.connection_status(user_id)["status"] == "revocation_pending"
    (revoked,) = store.lease_pending_connections(now=NOW + 2)["revoked"]
    ok, status = handoff.ack_connection_revoked(
        store._connect, user_id, revoked["token_epoch"], now=NOW + 3
    )
    assert (ok, status) == (True, "revoked")
    assert store.connection_status(user_id)["status"] == "revoked"
    assert TOKEN.encode() not in (tmp_path / "security.db").read_bytes()
