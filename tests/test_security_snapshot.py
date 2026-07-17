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
from aistat.publish import PublishError, publish_once
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
    install_compressed_snapshot,
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
    config.publish_url = "http://localhost/api/ingest/snapshot"
    config.allow_insecure_publish = True
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


def test_publisher_requires_https_by_default(tmp_path):
    db_path = tmp_path / "source.db"
    seeded_db(db_path)
    config = Config()
    config.db_path = db_path
    config.publish_url = "http://example.test/upload"
    config.ingest_secret = SECRET
    config.publish_tenant_id = TENANT_ID
    with pytest.raises(PublishError):
        publish_once(config)


def test_publisher_rejects_mismatched_host_confirmation(tmp_path):
    db_path = tmp_path / "source.db"
    seeded_db(db_path)
    config = Config()
    config.db_path = db_path
    config.publish_url = "http://localhost/upload"
    config.allow_insecure_publish = True
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
