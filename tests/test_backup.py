"""Backup, verify, restore and self-test of AIStat's SQLite data (FAN-1185)."""

import gzip
import sqlite3
from pathlib import Path

import pytest

from aistat import backup
from aistat.backup import (
    BackupError,
    clean,
    create_backup,
    list_backups,
    resolve_backup,
    restore_backup,
    self_test,
    verify_backup,
)
from aistat.config import Config
from aistat.db import connect, init_db
from conftest import seed_aggregate_fixture


def _seed_main_db(path: Path) -> None:
    conn = connect(path)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()


def _make_generic_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def _issue_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def cfg(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _seed_main_db(data / "aistat.db")
    return Config(
        db_path=data / "aistat.db",
        security_db_path=data / "security.db",
        worker_store_path=data / "worker_connections.db",
        tenants_dir=data / "tenants",
        backup_dir=data / "backups",
        backup_retention=14,
    )


def test_create_produces_verified_generation(cfg):
    gen = create_backup(cfg)
    assert gen.is_dir()
    manifest = backup.load_manifest(gen)
    members = {m["label"]: m for m in manifest["members"]}
    assert "aistat.db" in members
    main = members["aistat.db"]
    assert main["integrity"] == "ok"
    assert main["schema_version"] == 4
    assert main["row_counts"]["issues"] == 5
    assert (gen / "aistat.db.gz").is_file()
    # The compressed member is genuinely smaller than the raw database.
    assert main["gz_size_bytes"] < main["size_bytes"]


def test_list_orders_newest_first_and_latest_resolves(cfg):
    create_backup(cfg, now_iso="2026-07-21T05:00:00Z")
    create_backup(cfg, now_iso="2026-07-21T06:00:00Z")
    backups = list_backups(cfg)
    assert len(backups) == 2
    assert backups[0]["name"] > backups[1]["name"]
    assert resolve_backup(cfg, "latest").name == backups[0]["name"]


def test_verify_detects_corruption(cfg):
    gen = create_backup(cfg)
    report = verify_backup(cfg, gen.name)
    assert "aistat.db" in report["verified_members"]
    # Replace the member with valid gzip of non-database bytes.
    (gen / "aistat.db.gz").write_bytes(gzip.compress(b"not a database"))
    with pytest.raises(BackupError):
        verify_backup(cfg, gen.name)


def test_restore_roundtrip_keeps_pre_restore_copy(cfg):
    create_backup(cfg)
    conn = connect(cfg.db_path)
    conn.execute("DELETE FROM issues")
    conn.commit()
    conn.close()
    assert _issue_count(cfg.db_path) == 0

    report = restore_backup(cfg, "latest", dry_run=False)
    assert any(m["label"] == "aistat.db" for m in report["restored"])
    assert (cfg.db_path.parent / "aistat.db.pre-restore").is_file()
    assert _issue_count(cfg.db_path) == 5


def test_dry_run_restore_touches_nothing(cfg):
    create_backup(cfg)
    conn = connect(cfg.db_path)
    conn.execute("DELETE FROM issues")
    conn.commit()
    conn.close()

    report = restore_backup(cfg, "latest", dry_run=True)
    assert report["dry_run"] is True
    assert report["would_restore"]
    assert _issue_count(cfg.db_path) == 0
    assert not (cfg.db_path.parent / "aistat.db.pre-restore").exists()


def test_self_test_passes_without_touching_live(cfg):
    report = self_test(cfg)
    assert report["ok"] is True
    assert {m["label"] for m in report["members"]} == {"aistat.db"}
    assert not (cfg.db_path.parent / "aistat.db.pre-restore").exists()
    assert _issue_count(cfg.db_path) == 5


def test_retention_prunes_oldest(cfg):
    cfg.backup_retention = 2
    for hour in ("01", "02", "03", "04"):
        create_backup(cfg, now_iso="2026-07-21T%s:00:00Z" % hour)
    names = [b["name"] for b in list_backups(cfg)]
    assert names == ["aistat-20260721T040000Z", "aistat-20260721T030000Z"]


def test_includes_security_and_worker_when_present(cfg):
    _make_generic_db(cfg.security_db_path)
    _make_generic_db(cfg.worker_store_path)
    gen = create_backup(cfg)
    labels = {m["label"] for m in backup.load_manifest(gen)["members"]}
    assert {"aistat.db", "security.db", "worker_connections.db"} <= labels
    # A restore of the whole generation is atomic per member and reversible.
    report = restore_backup(cfg, gen.name, dry_run=False)
    assert len(report["restored"]) == 3


def test_no_databases_is_an_error(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    cfg = Config(
        db_path=data / "aistat.db",
        security_db_path=data / "security.db",
        worker_store_path=data / "worker_connections.db",
        tenants_dir=data / "tenants",
        backup_dir=data / "backups",
    )
    with pytest.raises(BackupError):
        create_backup(cfg)


def test_clean_removes_orphan_snapshot_sidecar(cfg):
    orphan = cfg.db_path.parent / ".aistat-snapshot-testonly.db-shm"
    orphan.write_bytes(b"stale")

    preview = clean(cfg, dry_run=True)
    assert str(orphan) in preview["orphan_sidecars"]
    assert orphan.exists()

    applied = clean(cfg, dry_run=False)
    assert applied["removed"] >= 1
    assert not orphan.exists()
