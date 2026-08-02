"""Backup, verify, restore and self-test of AIStat's SQLite data (FAN-1185)."""

import gzip
import hashlib
import json
import shutil
import sqlite3
import stat
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
from aistat.db import SCHEMA_VERSION, connect, init_db
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
        worker_tenants_dir=data / "worker_tenants",
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
    assert main["schema_version"] == SCHEMA_VERSION
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
        worker_tenants_dir=data / "worker_tenants",
        backup_dir=data / "backups",
    )
    with pytest.raises(BackupError):
        create_backup(cfg)


# ---- per-tenant stores (FAN-2031) ----------------------------------------

def _tenant_db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE runtimes (id INTEGER)")
    conn.executemany("INSERT INTO runtimes VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM runtimes").fetchone()[0]
    finally:
        conn.close()


def test_backs_up_both_tenant_stores_without_basename_collision(cfg):
    # The collector writes worker_tenants/<user>.db; the ingest side keeps
    # tenants/<user>.db. One user id => the same basename in both stores.
    _tenant_db(cfg.tenants_dir / "7.db", rows=2)
    _tenant_db(cfg.worker_tenants_dir / "7.db", rows=9)

    gen = create_backup(cfg)
    members = {m["label"]: m for m in backup.load_manifest(gen)["members"]}
    assert {"tenants/7.db", "worker_tenants/7.db"} <= set(members)

    # Wipe both live databases, then prove each one restores into its own
    # store, byte-for-byte as captured, instead of one overwriting the other.
    (cfg.worker_tenants_dir / "7.db").write_bytes(b"")
    (cfg.tenants_dir / "7.db").write_bytes(b"")

    report = restore_backup(cfg, gen.name, dry_run=False)
    restored = {m["label"]: m["target"] for m in report["restored"]}
    assert restored["worker_tenants/7.db"] == str(cfg.worker_tenants_dir / "7.db")
    assert restored["tenants/7.db"] == str(cfg.tenants_dir / "7.db")
    for label, path in (
        ("worker_tenants/7.db", cfg.worker_tenants_dir / "7.db"),
        ("tenants/7.db", cfg.tenants_dir / "7.db"),
    ):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == members[label]["sha256"]
    assert _row_count(cfg.worker_tenants_dir / "7.db") == 9
    assert _row_count(cfg.tenants_dir / "7.db") == 2


def test_self_test_covers_the_worker_tenant_store(cfg):
    _tenant_db(cfg.worker_tenants_dir / "42.db", rows=3)

    report = self_test(cfg)
    members = {m["label"]: m for m in report["members"]}
    assert members["worker_tenants/42.db"]["row_counts"] == {"runtimes": 3}


def test_restore_recreates_a_missing_worker_store_owner_only(cfg):
    _tenant_db(cfg.worker_tenants_dir / "7.db", rows=1)
    gen = create_backup(cfg)
    shutil.rmtree(str(cfg.worker_tenants_dir))

    restore_backup(cfg, gen.name, dry_run=False)
    assert _row_count(cfg.worker_tenants_dir / "7.db") == 1
    assert stat.S_IMODE(cfg.worker_tenants_dir.stat().st_mode) == 0o700


def test_symlinked_tenant_database_is_never_backed_up(cfg, tmp_path):
    outside = tmp_path / "outside.db"
    _tenant_db(outside, rows=1)
    cfg.worker_tenants_dir.mkdir(parents=True, exist_ok=True)
    (cfg.worker_tenants_dir / "9.db").symlink_to(outside)

    labels = {m["label"] for m in backup.load_manifest(create_backup(cfg))["members"]}
    assert not any(label.startswith("worker_tenants/") for label in labels)


@pytest.mark.parametrize(
    "label",
    [
        "worker_tenants/../../escape.db",
        "worker_tenants//7.db",
        "worker_tenants/nested/7.db",
        "unknown_store/7.db",
        "worker_tenants/.env",
        "../escape.db",
    ],
)
def test_restore_refuses_a_tampered_member_label(cfg, label):
    gen = create_backup(cfg)
    manifest = backup.load_manifest(gen)
    manifest["members"][0]["label"] = label
    (gen / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError):
        restore_backup(cfg, gen.name, dry_run=True)


def test_legacy_bare_tenant_label_still_restores_into_tenants(cfg):
    _tenant_db(cfg.tenants_dir / "7.db", rows=4)
    gen = create_backup(cfg)
    manifest = backup.load_manifest(gen)
    # A generation written before the stores were labelled: bare basename.
    for member in manifest["members"]:
        if member["label"] == "tenants/7.db":
            member["label"] = "7.db"
    (gen / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = restore_backup(cfg, gen.name, dry_run=True)
    targets = {m["label"]: m["target"] for m in report["would_restore"]}
    assert targets["7.db"] == str(cfg.tenants_dir / "7.db")


def test_clean_removes_orphan_snapshot_sidecar(cfg):
    orphan = cfg.db_path.parent / ".aistat-snapshot-testonly.db-shm"
    orphan.write_bytes(b"stale")

    preview = clean(cfg, dry_run=True)
    assert str(orphan) in preview["orphan_sidecars"]
    assert orphan.exists()

    applied = clean(cfg, dry_run=False)
    assert applied["removed"] >= 1
    assert not orphan.exists()
