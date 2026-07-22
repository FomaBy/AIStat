"""Unit tests for security helpers, snapshot validation and the publisher."""

import gzip
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from aistat.config import Config
from aistat.db import SCHEMA_VERSION, connect, init_db
from aistat.publish import PublishError, publish_once, publish_snapshot
from aistat.security import (
    SecurityStore,
    safe_next_url,
    snapshot_signature,
    verify_snapshot_signature,
)
from aistat.snapshot import (
    SnapshotError,
    cleanup_orphan_snapshot_sidecars,
    create_compressed_snapshot,
    daily_usage_max_date,
    install_compressed_snapshot,
    snapshot_is_fresh_enough,
)
import aistat.snapshot as snapshot_module
from conftest import seed_aggregate_fixture

SECRET = "publisher-" + "p" * 48
TENANT_ID = 7


def seeded_db(path):
    conn = connect(path)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()


def test_safe_next_url_rejects_browser_normalized_external_redirects():
    assert safe_next_url("/api/meta?x=1") == "/api/meta?x=1"
    assert safe_next_url("https://evil.example/") == "/"
    assert safe_next_url("//evil.example/") == "/"
    assert safe_next_url("//[evil.example/") == "/"
    assert safe_next_url("https://[evil.example/") == "/"
    assert safe_next_url(r"/\evil.example/") == "/"
    assert safe_next_url("/api\r\nX-Injected: yes") == "/"
    assert safe_next_url("/api\x00meta") == "/"
    assert safe_next_url("relative") == "/"


def test_snapshot_signature_age_and_body_binding():
    now = int(time.time())
    body = b"payload"
    signature = snapshot_signature(SECRET, TENANT_ID, now, body)
    assert verify_snapshot_signature(
        SECRET, TENANT_ID, str(now), signature, body, 300, now=now
    ) == now
    with pytest.raises(ValueError):
        verify_snapshot_signature(
            SECRET, TENANT_ID, str(now), signature, b"changed", 300, now=now
        )
    with pytest.raises(ValueError):
        verify_snapshot_signature(
            SECRET,
            TENANT_ID,
            str(now - 301),
            signature,
            body,
            300,
            now=now,
        )
    with pytest.raises(ValueError):
        verify_snapshot_signature(
            SECRET, TENANT_ID + 1, str(now), signature, body, 300, now=now
        )


def test_security_store_persists_throttle_and_replay_state(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    for attempt in range(4):
        assert store.record_login_failure("client", now=100 + attempt) == 0
    assert store.record_login_failure("client", now=104) == 900
    assert store.login_retry_after("client", now=105) == 899
    store.clear_login_failures("client")
    assert store.login_retry_after("client", now=105) == 0

    alice = store.find_or_create_user_by_identity("google", "alice", now=100)
    bob = store.find_or_create_user_by_identity("google", "bob", now=100)
    store.ensure_tenant(alice, now=100)
    store.ensure_tenant(bob, now=100)
    assert store.ingest_timestamp_is_fresh(alice, 1000) is True
    assert store.record_tenant_snapshot(alice, 1000, "a" * 64) is True
    assert store.ingest_timestamp_is_fresh(alice, 1000) is False
    assert store.ingest_timestamp_is_fresh(alice, 999) is False
    assert store.ingest_timestamp_is_fresh(bob, 999) is True
    assert store.record_tenant_snapshot(bob, 999, "b" * 64) is True
    assert store.record_tenant_snapshot(alice, 1001, "c" * 64) is True


def _usage_db(path, dates):
    """A minimal SQLite file with a ``daily_usage`` table holding ``dates``."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE daily_usage (date TEXT)")
        conn.executemany(
            "INSERT INTO daily_usage (date) VALUES (?)", [(d,) for d in dates]
        )
        conn.commit()
    finally:
        conn.close()


def _monotonic_usage_db(path, rows):
    """Create the production-shaped subset used by the freshness helper."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE daily_usage ("
            "runtime_id TEXT NOT NULL, model TEXT NOT NULL, date TEXT NOT NULL, "
            "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
            "cache_read_tokens INTEGER NOT NULL, "
            "cache_write_tokens INTEGER NOT NULL, "
            "PRIMARY KEY (runtime_id, model, date))"
        )
        conn.executemany(
            "INSERT INTO daily_usage VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()


def test_daily_usage_max_date_reads_latest(tmp_path):
    path = tmp_path / "usage.db"
    _usage_db(path, ["2026-07-12", "2026-07-19", "2026-07-15"])
    assert daily_usage_max_date(path) == "2026-07-19"


def test_daily_usage_max_date_is_none_for_missing_empty_or_tableless(tmp_path):
    assert daily_usage_max_date(tmp_path / "absent.db") is None
    empty = tmp_path / "empty.db"
    _usage_db(empty, [])
    assert daily_usage_max_date(empty) is None
    tableless = tmp_path / "tableless.db"
    conn = sqlite3.connect(str(tableless))
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    assert daily_usage_max_date(tableless) is None


def test_snapshot_is_fresh_enough_blocks_backwards_usage(tmp_path):
    # FAN-1442: a stale/degraded snapshot must never move a tenant's usage back.
    fresh = tmp_path / "fresh.db"
    _monotonic_usage_db(
        fresh, [("R1", "m1", "2026-07-22", 10, 20, 30, 40)]
    )
    stale = tmp_path / "stale.db"
    _monotonic_usage_db(
        stale, [("R1", "m1", "2026-07-19", 10, 20, 30, 40)]
    )
    same = tmp_path / "same.db"
    _monotonic_usage_db(
        same, [("R1", "m1", "2026-07-22", 10, 20, 30, 40)]
    )
    empty = tmp_path / "degraded.db"
    _monotonic_usage_db(empty, [])
    empty_target = tmp_path / "empty-target.db"
    _monotonic_usage_db(empty_target, [])
    missing = tmp_path / "missing.db"

    # Strictly older data over newer -> rejected.
    assert snapshot_is_fresh_enough(stale, fresh) is False
    # An empty/degraded snapshot over real data -> rejected (no data loss).
    assert snapshot_is_fresh_enough(empty, fresh) is False
    # Same latest day -> accepted (re-publish / newer within-day is fine).
    assert snapshot_is_fresh_enough(same, fresh) is True
    # Newer data over older -> accepted.
    assert snapshot_is_fresh_enough(fresh, stale) is True
    # First ingest / valid empty target accepts a valid (even empty) snapshot.
    assert snapshot_is_fresh_enough(stale, missing) is True
    assert snapshot_is_fresh_enough(empty, missing) is True
    assert snapshot_is_fresh_enough(stale, empty_target) is True


@pytest.mark.parametrize("counter_index", range(4))
def test_snapshot_is_fresh_enough_rejects_each_lower_same_day_counter(
    tmp_path, counter_index
):
    target = tmp_path / "target.db"
    incoming = tmp_path / "incoming.db"
    counters = [10, 20, 30, 40]
    lower = list(counters)
    lower[counter_index] -= 1
    _monotonic_usage_db(
        target, [("R1", "m1", "2026-07-22") + tuple(counters)]
    )
    _monotonic_usage_db(
        incoming, [("R1", "m1", "2026-07-22") + tuple(lower)]
    )

    assert snapshot_is_fresh_enough(incoming, target) is False


def test_snapshot_is_fresh_enough_same_day_row_matrix(tmp_path):
    target_rows = [
        ("R1", "m1", "2026-07-22", 10, 20, 30, 40),
        ("R2", "m2", "2026-07-22", 50, 60, 70, 80),
    ]
    target = tmp_path / "target.db"
    _monotonic_usage_db(target, target_rows)

    missing = tmp_path / "missing-row.db"
    _monotonic_usage_db(missing, target_rows[:1])
    assert snapshot_is_fresh_enough(missing, target) is False

    equal = tmp_path / "equal.db"
    _monotonic_usage_db(equal, target_rows)
    assert snapshot_is_fresh_enough(equal, target) is True

    growth = tmp_path / "growth.db"
    _monotonic_usage_db(
        growth,
        [
            ("R1", "m1", "2026-07-22", 11, 22, 33, 44),
            ("R2", "m2", "2026-07-22", 55, 66, 77, 88),
        ],
    )
    assert snapshot_is_fresh_enough(growth, target) is True

    new_row = tmp_path / "new-row.db"
    _monotonic_usage_db(
        new_row,
        target_rows
        + [("R3", "m3", "2026-07-22", 1, 2, 3, 4)],
    )
    assert snapshot_is_fresh_enough(new_row, target) is True

    mixed = tmp_path / "mixed.db"
    _monotonic_usage_db(
        mixed,
        [
            ("R1", "m1", "2026-07-22", 11, 21, 31, 41),
            ("R2", "m2", "2026-07-22", 49, 60, 70, 80),
        ],
    )
    assert snapshot_is_fresh_enough(mixed, target) is False


def test_snapshot_is_fresh_enough_ignores_derived_and_sync_fields(tmp_path):
    target = tmp_path / "target.db"
    incoming = tmp_path / "incoming.db"
    for path, provider, cost, credits, priced, synced_at in (
        (target, "codex", 10.0, 20.0, 1, "2026-07-22T10:00:00Z"),
        (incoming, "other", None, None, 0, "2026-07-22T09:00:00Z"),
    ):
        conn = connect(path)
        init_db(conn)
        conn.execute(
            "INSERT INTO daily_usage (runtime_id, model, date, provider, "
            "input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, synced_at, cost_usd, cost_credits, "
            "cost_priced) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "R1",
                "m1",
                "2026-07-22",
                provider,
                10,
                20,
                30,
                40,
                synced_at,
                cost,
                credits,
                priced,
            ),
        )
        conn.commit()
        conn.close()

    assert snapshot_is_fresh_enough(incoming, target) is True


def test_snapshot_is_fresh_enough_fails_closed_on_read_errors(tmp_path):
    valid = tmp_path / "valid.db"
    _monotonic_usage_db(
        valid, [("R1", "m1", "2026-07-22", 10, 20, 30, 40)]
    )
    invalid = tmp_path / "invalid.db"
    invalid.write_bytes(b"not sqlite")
    tableless = tmp_path / "tableless.db"
    conn = sqlite3.connect(str(tableless))
    conn.execute("CREATE TABLE other (value INTEGER)")
    conn.commit()
    conn.close()

    assert snapshot_is_fresh_enough(invalid, valid) is False
    assert snapshot_is_fresh_enough(valid, invalid) is False
    assert snapshot_is_fresh_enough(valid, tableless) is False


def test_snapshot_round_trip_and_size_limit(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    seeded_db(source)
    payload = create_compressed_snapshot(source)
    info = install_compressed_snapshot(payload, target, 64 * 1024 * 1024)
    assert info.schema_version == SCHEMA_VERSION
    assert info.size_bytes == target.stat().st_size

    with pytest.raises(SnapshotError):
        install_compressed_snapshot(payload, tmp_path / "small.db", 100)


def test_snapshot_cleanup_does_not_leak_sidecars(tmp_path):
    source = tmp_path / "source.db"
    seeded_db(source)
    stale_sidecars = [
        tmp_path / ".aistat-snapshot-stale.db-wal",
        tmp_path / ".aistat-snapshot-stale.db-shm",
    ]
    for sidecar in stale_sidecars:
        sidecar.write_bytes(b"stale")
    expected = gzip.decompress(create_compressed_snapshot(source))
    assert all(not sidecar.exists() for sidecar in stale_sidecars)

    for _ in range(100):
        assert gzip.decompress(create_compressed_snapshot(source)) == expected

    assert not list(tmp_path.glob(".aistat-snapshot-*.db"))
    assert not list(tmp_path.glob(".aistat-snapshot-*.db-wal"))
    assert not list(tmp_path.glob(".aistat-snapshot-*.db-shm"))


def test_orphan_sidecar_cleanup_skips_owned_files_and_symlinks(
    tmp_path, monkeypatch
):
    orphan = tmp_path / ".aistat-snapshot-orphan.db"
    orphan_sidecar = Path(str(orphan) + "-shm")
    orphan_sidecar.write_bytes(b"orphan-sidecar")

    owned = tmp_path / ".aistat-snapshot-owned.db"
    owned_sidecar = Path(str(owned) + "-shm")
    owned_sidecar.write_bytes(b"owned-sidecar")

    target = tmp_path / "outside.txt"
    target.write_bytes(b"outside")
    symlink = tmp_path / ".aistat-snapshot-link.db-shm"
    symlink.symlink_to(target)

    monkeypatch.setattr(
        snapshot_module,
        "_path_has_open_owner",
        lambda path: path.name.startswith(".aistat-snapshot-owned"),
    )

    assert cleanup_orphan_snapshot_sidecars(tmp_path) == 1
    assert not orphan_sidecar.exists()
    assert owned_sidecar.exists()
    assert symlink.is_symlink()
    assert target.read_bytes() == b"outside"


def test_snapshot_backup_failure_cleans_sidecars(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    seeded_db(source)
    real_connect = snapshot_module.sqlite3.connect
    created = []
    real_temp_path = snapshot_module._temp_path

    def temp_path(parent, suffix):
        path = real_temp_path(parent, suffix)
        created.append(path)
        return path

    class FailingBackupConnection:
        def __init__(self, connection):
            self._connection = connection

        def backup(self, _target):
            for suffix in ("-wal", "-shm"):
                Path(str(created[0]) + suffix).touch()
            raise sqlite3.OperationalError("backup failed")

        def close(self):
            self._connection.close()

    def connect(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        if Path(path) == source:
            return FailingBackupConnection(connection)
        return connection

    monkeypatch.setattr(snapshot_module, "_temp_path", temp_path)
    monkeypatch.setattr(snapshot_module.sqlite3, "connect", connect)
    with pytest.raises(SnapshotError, match="cannot create SQLite backup"):
        create_compressed_snapshot(source)
    assert not list(tmp_path.glob(".aistat-snapshot-*.db*"))


def test_snapshot_compression_failure_cleans_sidecars(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    seeded_db(source)
    created = []
    real_temp_path = snapshot_module._temp_path

    def temp_path(parent, suffix):
        path = real_temp_path(parent, suffix)
        created.append(path)
        return path

    def fail_compress(_payload, compresslevel):
        assert compresslevel == 6
        for suffix in ("-wal", "-shm"):
            Path(str(created[0]) + suffix).touch()
        raise RuntimeError("compression failed")

    monkeypatch.setattr(snapshot_module, "_temp_path", temp_path)
    monkeypatch.setattr(snapshot_module.gzip, "compress", fail_compress)
    with pytest.raises(RuntimeError, match="compression failed"):
        create_compressed_snapshot(source)
    assert not list(tmp_path.glob(".aistat-snapshot-*.db*"))


def test_snapshot_validation_failure_cleans_sidecars(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    seeded_db(source)
    payload = create_compressed_snapshot(source)
    created = []

    def fail_validate(path):
        created.append(path)
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).touch()
        raise SnapshotError("validation failed")

    monkeypatch.setattr(snapshot_module, "validate_snapshot", fail_validate)
    with pytest.raises(SnapshotError, match="validation failed"):
        install_compressed_snapshot(payload, tmp_path / "target.db", 64 * 1024 * 1024)
    assert created
    assert not list(tmp_path.glob(".aistat-snapshot-*.db*"))


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def read(self, _limit=-1):
        return self.body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_publisher_builds_valid_signed_request(tmp_path):
    db_path = tmp_path / "source.db"
    seeded_db(db_path)
    config = Config()
    config.db_path = db_path
    config.publish_url = "https://localhost/api/ingest/snapshot"
    config.publish_interval_seconds = 300
    config.ingest_secret = SECRET
    config.publish_tenant_id = TENANT_ID

    captured = {}

    def opener(request, timeout):
        captured["timeout"] = timeout
        captured["body"] = request.data
        timestamp = request.headers["X-aistat-timestamp"]
        signature = request.headers["X-aistat-signature"]
        assert request.headers["X-aistat-tenant"] == str(TENANT_ID)
        verify_snapshot_signature(
            SECRET,
            TENANT_ID,
            timestamp,
            signature,
            request.data,
            300,
            now=1234,
        )
        data = gzip.decompress(request.data)
        return FakeResponse(
            json.dumps(
                {
                    "status": "ok",
                    "tenant_id": TENANT_ID,
                    "schema_version": 3,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ).encode("utf-8")
        )

    result = publish_once(config, opener=opener, now=1234)
    assert result["status"] == "ok"
    assert captured["body"].startswith(b"\x1f\x8b")
    assert captured["timeout"] == config.publish_timeout_seconds


def test_publisher_rejects_http_even_with_insecure_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AISTAT_ALLOW_INSECURE_PUBLISH", "1")
    db_path = tmp_path / "source.db"
    seeded_db(db_path)
    config = Config()
    config.db_path = db_path
    config.publish_url = "http://example.test/upload"
    config.ingest_secret = SECRET
    config.publish_tenant_id = TENANT_ID
    opener_calls = []

    def opener(*args, **kwargs):
        opener_calls.append((args, kwargs))
        raise AssertionError("HTTP endpoint must fail before network access")

    with pytest.raises(PublishError, match="must use HTTPS"):
        publish_once(config, opener=opener)
    assert opener_calls == []


def test_publisher_rejects_mismatched_host_confirmation(tmp_path):
    db_path = tmp_path / "source.db"
    seeded_db(db_path)
    config = Config()
    config.db_path = db_path
    config.publish_url = "https://localhost/upload"
    config.publish_interval_seconds = 300
    config.ingest_secret = SECRET
    config.publish_tenant_id = TENANT_ID

    def opener(_request, timeout):
        assert timeout == config.publish_timeout_seconds
        return FakeResponse(
            json.dumps(
                {
                    "status": "ok",
                    "tenant_id": TENANT_ID + 1,
                    "schema_version": 3,
                    "size_bytes": 1,
                    "sha256": "wrong",
                }
            ).encode("utf-8")
        )

    with pytest.raises(PublishError, match="does not match"):
        publish_once(config, opener=opener, now=1234)


def test_publish_snapshot_signs_per_tenant(tmp_path):
    """One ingest secret signs any tenant; the tenant is bound into the sig."""
    db_path = tmp_path / "tenant.db"
    seeded_db(db_path)
    config = Config()
    config.publish_url = "https://localhost/api/ingest/snapshot"
    config.publish_interval_seconds = 300
    config.ingest_secret = SECRET
    config.publish_tenant_id = None  # no single-tenant selector configured

    other_tenant = 4242

    def opener(request, timeout):
        assert request.headers["X-aistat-tenant"] == str(other_tenant)
        verify_snapshot_signature(
            SECRET,
            other_tenant,
            request.headers["X-aistat-timestamp"],
            request.headers["X-aistat-signature"],
            request.data,
            300,
            now=1234,
        )
        data = gzip.decompress(request.data)
        return FakeResponse(
            json.dumps(
                {
                    "status": "ok",
                    "tenant_id": other_tenant,
                    "schema_version": SCHEMA_VERSION,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ).encode("utf-8")
        )

    result = publish_snapshot(config, db_path, other_tenant, opener=opener, now=1234)
    assert result["tenant_id"] == other_tenant


def test_publish_snapshot_rejects_wrong_tenant_confirmation(tmp_path):
    db_path = tmp_path / "tenant.db"
    seeded_db(db_path)
    config = Config()
    config.publish_url = "https://localhost/upload"
    config.publish_interval_seconds = 300
    config.ingest_secret = SECRET

    def opener(_request, timeout):
        return FakeResponse(
            json.dumps(
                {"status": "ok", "tenant_id": 99, "schema_version": SCHEMA_VERSION,
                 "size_bytes": 1, "sha256": "wrong"}
            ).encode("utf-8")
        )

    with pytest.raises(PublishError, match="does not match"):
        publish_snapshot(config, db_path, 7, opener=opener, now=1234)
