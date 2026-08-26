"""Encrypted off-site backup copies and the isolated restore drill (FAN-3462)."""

import json
import hashlib
import os
import stat
import tarfile
import io
from types import SimpleNamespace
from pathlib import Path

import pytest

from aistat import backup
from aistat import backup_offsite as offsite
from aistat.backup_offsite import (
    KEY_ENV,
    ALERTS_NAME,
    DRILL_REPORT_NAME,
    OffsiteError,
    _decrypt,
    _encrypt,
    push_offsite,
    record_alert,
    restore_drill_offsite,
    rotate_log,
    verify_offsite,
)
from aistat.config import Config
from aistat.db import connect, init_db
from conftest import seed_aggregate_fixture


def _seed_main_db(path):
    conn = connect(path)
    init_db(conn)
    seed_aggregate_fixture(conn)
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
        offsite_backup_dir=tmp_path / "offsite",
        offsite_retention=3,
    )


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "unit-test-passphrase-3458")
    return os.environ[KEY_ENV]


@pytest.fixture
def pushed(cfg, key):
    backup.create_backup(cfg)
    push_offsite(cfg)
    return cfg


def _bundle_file(cfg):
    return next(p for p in cfg.offsite_backup_dir.iterdir() if p.name.endswith(".enc"))


def _replace_bundle_tar(cfg, key, tar_bytes):
    enc = _bundle_file(cfg)
    meta_path = enc.with_name(enc.name[: -len(".tar.gz.enc")] + ".json")
    meta = json.loads(meta_path.read_text())
    blob = _encrypt(key, tar_bytes)
    enc.write_bytes(blob)
    meta["enc_sha256"] = hashlib.sha256(blob).hexdigest()
    meta["tar_sha256"] = hashlib.sha256(tar_bytes).hexdigest()
    meta_path.write_text(json.dumps(meta))


def _tar_file(tar, name, payload):
    info = tarfile.TarInfo(name)
    info.mode = 0o600
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))


def _captured_temp_dirs(monkeypatch):
    created = []
    real_mkdtemp = offsite.tempfile.mkdtemp

    def capture(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(offsite.tempfile, "mkdtemp", capture)
    return created


# --------------------------------------------------------------------------- #
# encryption round trip
# --------------------------------------------------------------------------- #
def test_encryption_roundtrip_and_wrong_key(key):
    blob = _encrypt(key, b"payload")
    assert blob != b"payload"
    assert _decrypt(key, blob) == b"payload"
    with pytest.raises(OffsiteError):
        _decrypt(key + "x", blob)


def test_push_requires_key(cfg, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    backup.create_backup(cfg)
    with pytest.raises(OffsiteError) as exc:
        push_offsite(cfg)
    assert KEY_ENV in str(exc.value)
    # No unencrypted bundle was left behind.
    assert not any(cfg.offsite_backup_dir.iterdir())


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def test_push_creates_encrypted_independent_bundle(cfg, pushed):
    enc = _bundle_file(cfg)
    assert enc.stat().st_size > 0
    # The bundle is opaque ciphertext, not a plain gzip/tar of the databases.
    with pytest.raises((tarfile.TarError, OSError, EOFError)):
        with tarfile.open(fileobj=io.BytesIO(enc.read_bytes()), mode="r:gz") as tar:
            tar.getmembers()
    meta = json.loads(
        enc.with_name(enc.name[: -len(".tar.gz.enc")] + ".json").read_text()
    )
    assert meta["encryption"] == "aes-256-cbc-pbkdf2"
    assert meta["generation"].startswith("aistat-")
    assert KEY_ENV not in json.dumps(meta)
    # Owner-only permissions on the bundle.
    assert stat.S_IMODE(enc.stat().st_mode) & 0o077 == 0
    # The off-site target is outside the local backup tree.
    assert cfg.offsite_backup_dir not in cfg.backup_dir.parents
    assert cfg.backup_dir not in cfg.offsite_backup_dir.parents


def test_push_is_idempotent(pushed):
    result = push_offsite(pushed)
    assert result["pushed"] is False
    assert result["reason"] == "already-published"


def test_push_without_local_generation_records_alert(cfg, key):
    with pytest.raises(OffsiteError):
        push_offsite(cfg)
    events = _alerts(cfg)
    assert len(events) == 1
    assert events[0]["kind"] == "offsite_push_failed"
    assert events[0]["dedupe_key"] == "push:no-local-generation"


def test_push_failure_keeps_last_good_copy(cfg, pushed, monkeypatch):
    # Corrupt the source of a *new* generation so the next push fails midway:
    # resolve succeeds, tar fails because the generation is gone.
    good_enc = _bundle_file(cfg)
    good_bytes = good_enc.read_bytes()
    for gen in cfg.backup_dir.iterdir():
        if gen.is_dir():
            backup._rmtree(gen)
    with pytest.raises(OffsiteError):
        push_offsite(cfg)
    # The previous good bundle is byte-identical and one alert was recorded.
    assert good_enc.read_bytes() == good_bytes
    assert _alerts(cfg)[-1]["kind"] == "offsite_push_failed"
    # No staging leftovers.
    assert not [p for p in cfg.offsite_backup_dir.iterdir() if p.name.startswith(".")]


def test_push_retention_prunes_oldest_bundles(cfg, key):
    for index in range(4):
        backup.create_backup(cfg, now_iso="2026-08-0%dT00:00:00Z" % (index + 1))
        push_offsite(cfg, now_iso="2026-08-0%dT00:00:00Z" % (index + 1))
    bundles = sorted(p for p in cfg.offsite_backup_dir.iterdir() if p.name.endswith(".enc"))
    assert len(bundles) == cfg.offsite_retention
    # The alerts file and drill reports are never pruned by bundle retention.
    record_alert(cfg, "test", "k", "r")
    assert (cfg.offsite_backup_dir / ALERTS_NAME).is_file()


def test_push_rejects_target_inside_backup_tree(cfg, key):
    cfg.offsite_backup_dir = cfg.backup_dir / "offsite"
    backup.create_backup(cfg)
    with pytest.raises(OffsiteError) as exc:
        push_offsite(cfg)
    assert "outside the local backup tree" in str(exc.value)


# --------------------------------------------------------------------------- #
# verify / drill
# --------------------------------------------------------------------------- #
def test_verify_offsite_end_to_end(cfg, pushed, monkeypatch):
    created = _captured_temp_dirs(monkeypatch)
    report = verify_offsite(cfg, "latest")
    assert "aistat.db" in report["verified_members"]
    assert created and all(not path.exists() for path in created)


def test_verify_detects_corrupt_bundle(cfg, pushed):
    enc = _bundle_file(cfg)
    blob = bytearray(enc.read_bytes())
    blob[-8:] = b"tampered"
    enc.write_bytes(bytes(blob))
    with pytest.raises(OffsiteError) as exc:
        verify_offsite(cfg, "latest")
    assert "checksum mismatch" in str(exc.value)


def test_verify_rejects_unsafe_member_and_cleans_all_scratch(cfg, pushed, key, monkeypatch):
    created = _captured_temp_dirs(monkeypatch)
    generation = "aistat-2026-08-26T00-00-00Z"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_file(tar, generation + "/manifest.json", b"{}")
        _tar_file(tar, "../escaped.txt", b"plaintext")
    _replace_bundle_tar(cfg, key, buf.getvalue())

    with pytest.raises(OffsiteError, match="unsafe path"):
        verify_offsite(cfg, "latest")

    assert not list(cfg.offsite_backup_dir.glob(".extract-*"))
    assert not list(cfg.offsite_backup_dir.glob("**/manifest.json"))
    assert not (cfg.offsite_backup_dir.parent / "escaped.txt").exists()
    assert created and all(not path.exists() for path in created)


def test_verify_cleans_all_scratch_on_unexpected_extraction_error(
    cfg, pushed, key, monkeypatch
):
    created = _captured_temp_dirs(monkeypatch)
    generation = "aistat-2026-08-26T00-00-00Z"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_file(tar, generation + "/manifest.json", b"{}")
    _replace_bundle_tar(cfg, key, buf.getvalue())

    original_extract = tarfile.TarFile.extract

    def extract_then_fail(tar, member, *args, **kwargs):
        original_extract(tar, member, *args, **kwargs)
        raise RuntimeError("injected extraction failure")

    monkeypatch.setattr(tarfile.TarFile, "extract", extract_then_fail)
    with pytest.raises(OffsiteError, match="off-site bundle extraction failed"):
        verify_offsite(cfg, "latest")

    assert not list(cfg.offsite_backup_dir.glob(".extract-*"))
    assert not list(cfg.offsite_backup_dir.glob("**/manifest.json"))
    assert created and all(not path.exists() for path in created)


def test_verify_cleans_all_scratch_when_member_validation_fails(
    cfg, pushed, monkeypatch
):
    created = _captured_temp_dirs(monkeypatch)

    def fail_validation(_generation):
        raise OffsiteError("injected validation failure")

    monkeypatch.setattr(offsite, "_check_generation", fail_validation)
    with pytest.raises(OffsiteError, match="off-site verification failed"):
        verify_offsite(cfg, "latest")

    assert not list(cfg.offsite_backup_dir.glob(".extract-*"))
    assert not list(cfg.offsite_backup_dir.glob("**/manifest.json"))
    assert created and all(not path.exists() for path in created)


def test_restore_drill_passes_and_never_touches_live_data(cfg, pushed):
    live_before = (cfg.db_path).read_bytes()
    report = restore_drill_offsite(cfg, "latest")
    assert report["ok"] is True
    assert report["within_rto_target"] is True
    assert report["measured_rto_seconds"] >= 0
    assert "decrypt_extract" in report["steps_seconds"]
    assert "critical_queries" in report["steps_seconds"]
    assert cfg.db_path.read_bytes() == live_before
    persisted = json.loads(
        (cfg.offsite_backup_dir / DRILL_REPORT_NAME).read_text()
    )
    assert persisted["ok"] is True
    # No extraction scratch left behind in the off-site target.
    assert not [p for p in cfg.offsite_backup_dir.iterdir() if p.name.startswith(".extract")]


def test_restore_drill_fails_closed_on_wrong_key(cfg, pushed, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "attacker-guess")
    with pytest.raises(OffsiteError):
        restore_drill_offsite(cfg, "latest")
    events = _alerts(cfg)
    assert events and events[-1]["kind"] == "offsite_drill_failed"
    persisted = json.loads(
        (cfg.offsite_backup_dir / DRILL_REPORT_NAME).read_text()
    )
    assert persisted["ok"] is False


# --------------------------------------------------------------------------- #
# log rotation
# --------------------------------------------------------------------------- #
def test_rotate_log_by_size(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("x" * 100)
    result = rotate_log(log, max_bytes=10, keep=3)
    assert result["rotated"] is True
    assert (tmp_path / "backup.log.1").read_text() == "x" * 100
    assert not log.exists()


def test_rotate_log_keeps_bound_and_audit_evidence(tmp_path):
    log = tmp_path / "backup.log"
    evidence = tmp_path / "manifest.json"
    evidence.write_text("{}")
    for generation in range(6):
        log.write_text("g%d" % generation * 50)
        rotate_log(log, max_bytes=10, keep=2)
    assert (tmp_path / "backup.log.2").is_file()
    assert not (tmp_path / "backup.log.3").exists()
    # Audit evidence next to the log is untouched.
    assert evidence.read_text() == "{}"


def test_rotate_log_by_age(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("stale")
    old = log.stat().st_mtime - 40 * 86400
    os.utime(str(log), (old, old))
    result = rotate_log(log, max_bytes=10 ** 9, keep=2, max_age_days=30)
    assert result["rotated"] is True
    assert result["reason"] == "age"


def test_rotate_log_noop_under_limits(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("small and fresh")
    assert rotate_log(log, max_bytes=10 ** 6, keep=2)["rotated"] is False


# --------------------------------------------------------------------------- #
def _alerts(cfg):
    path = cfg.offsite_backup_dir / ALERTS_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _assert_sentinel_absent(cfg, sentinel, captured=""):
    assert sentinel not in captured
    for path in cfg.offsite_backup_dir.rglob("*"):
        if path.is_file():
            assert sentinel not in path.read_bytes().decode("utf-8", "replace")


def test_openssl_failure_does_not_echo_secret_in_exception_or_streams(
    key, monkeypatch, capsys
):
    sentinel = "offsite-encryption-sentinel-9f3c"

    def fail_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=("openssl failure: " + sentinel).encode(),
        )

    monkeypatch.setattr(offsite.subprocess, "run", fail_run)
    with pytest.raises(OffsiteError) as exc:
        _encrypt(sentinel, b"payload")

    captured = capsys.readouterr()
    assert sentinel not in str(exc.value)
    assert sentinel not in captured.out + captured.err


def test_alert_reason_is_redacted_before_persistence(cfg):
    sentinel = "offsite-alert-sentinel-2a71"

    event = record_alert(cfg, "test", "test:sentinel", sentinel)

    assert event["reason"] != sentinel
    _assert_sentinel_absent(cfg, sentinel)


def test_metadata_write_failure_does_not_echo_secret_anywhere(
    cfg, key, monkeypatch, capsys, caplog
):
    sentinel = "offsite-metadata-sentinel-6d20"
    backup.create_backup(cfg)

    def fail_replace(*_args, **_kwargs):
        raise OSError(sentinel)

    monkeypatch.setattr(offsite.os, "replace", fail_replace)
    with pytest.raises(OffsiteError) as exc:
        push_offsite(cfg)

    captured = capsys.readouterr()
    assert sentinel not in str(exc.value)
    _assert_sentinel_absent(cfg, sentinel, captured.out + captured.err + caplog.text)


def test_drill_decryption_failure_does_not_echo_secret_in_report_alert_or_cli(
    cfg, pushed, monkeypatch, capsys, caplog
):
    sentinel = "offsite-decrypt-sentinel-4b8e"
    monkeypatch.setattr(offsite, "_decrypt", lambda *_args: (_ for _ in ()).throw(
        OffsiteError(sentinel)
    ))
    monkeypatch.setattr(offsite, "Config", lambda: cfg)

    assert offsite.main(["drill"]) == 1

    captured = capsys.readouterr()
    _assert_sentinel_absent(cfg, sentinel, captured.out + captured.err + caplog.text)
