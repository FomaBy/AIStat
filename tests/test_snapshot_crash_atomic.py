"""Crash-atomicity of the tenant snapshot install + replay watermark.

The tenant database file and the replay watermark row live in two different
files. These tests inject a fault at every boundary of the install sequence and
prove that recovery always lands on exactly one of two consistent states:
``old snapshot + old watermark`` or ``new snapshot + new watermark``. The mixed
``new + old`` and ``old + new`` states are never observable.

The install sequence (see :mod:`aistat.snapshot_recovery`) is:

    stage -> journal intent -> swap file -> commit watermark + drop journal

The recovery driver is shared verbatim by the Flask app and the legacy cPanel
WSGI entry point, so exercising it directly covers both implementations.
"""

import gzip
import hashlib
import sqlite3
from pathlib import Path

import pytest

from aistat.db import connect, init_db
from aistat.security import SecurityStore
from aistat.snapshot import create_compressed_snapshot, stage_compressed_snapshot
from aistat.snapshot_recovery import (
    file_sha256,
    recover_pending_installs,
    swap_staged_into_place,
)
from aistat.tenant import tenant_db_path
from conftest import seed_aggregate_fixture

MAX_BYTES = 64 * 1024 * 1024
OLD_TS = 1000
NEW_TS = 2000


def _snapshot(tmp_path, name, bump):
    """A compressed snapshot plus the sha256 its installed database will have."""
    source = tmp_path / name
    conn = connect(source)
    init_db(conn)
    seed_aggregate_fixture(conn)
    if bump:
        conn.execute(
            "UPDATE daily_usage SET input_tokens = input_tokens + ? "
            "WHERE runtime_id = 'R1'",
            (bump,),
        )
        conn.commit()
    conn.close()
    payload = create_compressed_snapshot(source)
    installed_sha = hashlib.sha256(gzip.decompress(payload)).hexdigest()
    return payload, installed_sha


def _journal_count(store):
    conn = sqlite3.connect(str(store.path))
    try:
        return conn.execute(
            "SELECT count(*) FROM snapshot_install_journal"
        ).fetchone()[0]
    finally:
        conn.close()


def _watermark(store, uid):
    return int(store.get_tenant(uid)["last_ingest_timestamp"])


def _install_clean(store, uid, tenants_dir, payload, ts):
    """Fully install one snapshot the crash-atomic way (no fault injected)."""
    target = Path(tenant_db_path(tenants_dir, uid))
    staged, info = stage_compressed_snapshot(payload, target, MAX_BYTES)
    store.begin_snapshot_install(uid, ts, info.sha256, ts, str(staged))
    swap_staged_into_place(staged, target)
    assert store.finish_snapshot_install(uid, ts, info.sha256, ts) is True
    return info


@pytest.fixture
def tenant_env(tmp_path):
    """A store with one tenant that already holds a clean OLD snapshot."""
    store = SecurityStore(tmp_path / "security.db")
    uid = store.find_or_create_user_by_identity("google", "alice", now=100)
    store.ensure_tenant(uid, now=100)
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()

    old_payload, old_sha = _snapshot(tmp_path, "old.db", bump=0)
    new_payload, new_sha = _snapshot(tmp_path, "new.db", bump=1_000_000)
    assert old_sha != new_sha

    _install_clean(store, uid, tenants_dir, old_payload, OLD_TS)
    target = Path(tenant_db_path(tenants_dir, uid))
    assert file_sha256(target) == old_sha
    assert _watermark(store, uid) == OLD_TS

    return {
        "store": store,
        "uid": uid,
        "tenants_dir": tenants_dir,
        "target": target,
        "old_payload": old_payload,
        "old_sha": old_sha,
        "new_payload": new_payload,
        "new_sha": new_sha,
    }


def _stage_and_journal_new(env):
    """Reach the point where NEW is staged and the intent is journalled."""
    staged, info = stage_compressed_snapshot(
        env["new_payload"], env["target"], MAX_BYTES
    )
    env["store"].begin_snapshot_install(
        env["uid"], NEW_TS, info.sha256, NEW_TS, str(staged)
    )
    return Path(staged), info


def _assert_old(env):
    assert file_sha256(env["target"]) == env["old_sha"]
    assert _watermark(env["store"], env["uid"]) == OLD_TS
    assert _journal_count(env["store"]) == 0


def _assert_new(env):
    assert file_sha256(env["target"]) == env["new_sha"]
    assert _watermark(env["store"], env["uid"]) == NEW_TS
    assert _journal_count(env["store"]) == 0


def _recover(env):
    return env["store"].recover_snapshot_installs(env["tenants_dir"])


# --- Fault injection at each boundary ------------------------------------


def test_crash_before_install_recovers_old(tenant_env):
    # Nothing staged, nothing journalled: a crash before work begins.
    summary = _recover(tenant_env)
    assert summary == {"rolled_forward": 0, "rolled_back": 0}
    _assert_old(tenant_env)


def test_crash_after_stage_before_journal_recovers_old(tenant_env):
    # NEW is staged and validated but the intent was never journalled.
    staged, _info = stage_compressed_snapshot(
        tenant_env["new_payload"], tenant_env["target"], MAX_BYTES
    )
    assert staged.exists()
    summary = _recover(tenant_env)
    assert summary == {"rolled_forward": 0, "rolled_back": 0}
    _assert_old(tenant_env)


def test_crash_after_journal_before_swap_rolls_forward(tenant_env):
    # Intent journalled, tenant DB not yet swapped: the staged file is intact,
    # so recovery completes the install (new + new).
    _stage_and_journal_new(tenant_env)
    assert file_sha256(tenant_env["target"]) == tenant_env["old_sha"]
    summary = _recover(tenant_env)
    assert summary == {"rolled_forward": 1, "rolled_back": 0}
    _assert_new(tenant_env)


def test_crash_after_db_replacement_before_watermark_rolls_forward(tenant_env):
    # The classic danger: file swapped to NEW, watermark still OLD.
    staged, info = _stage_and_journal_new(tenant_env)
    swap_staged_into_place(staged, tenant_env["target"])
    assert file_sha256(tenant_env["target"]) == tenant_env["new_sha"]
    assert _watermark(tenant_env["store"], tenant_env["uid"]) == OLD_TS  # mixed!
    summary = _recover(tenant_env)
    assert summary == {"rolled_forward": 1, "rolled_back": 0}
    _assert_new(tenant_env)


def test_crash_after_watermark_persistence_is_new(tenant_env):
    # Watermark committed and journal dropped: recovery is a no-op.
    _install_clean(
        tenant_env["store"],
        tenant_env["uid"],
        tenant_env["tenants_dir"],
        tenant_env["new_payload"],
        NEW_TS,
    )
    summary = _recover(tenant_env)
    assert summary == {"rolled_forward": 0, "rolled_back": 0}
    _assert_new(tenant_env)


def test_crash_after_journal_with_lost_staged_rolls_back(tenant_env):
    # Intent journalled but the staged database vanished before the swap: the
    # install cannot complete, so recovery rolls back to old + old.
    staged, _info = _stage_and_journal_new(tenant_env)
    staged.unlink()
    summary = _recover(tenant_env)
    assert summary == {"rolled_forward": 0, "rolled_back": 1}
    _assert_old(tenant_env)


def test_crash_after_journal_with_corrupt_staged_rolls_back(tenant_env):
    # A truncated/altered staged file no longer matches the journalled sha256.
    staged, _info = _stage_and_journal_new(tenant_env)
    staged.write_bytes(b"not a sqlite database")
    summary = _recover(tenant_env)
    assert summary == {"rolled_forward": 0, "rolled_back": 1}
    _assert_old(tenant_env)


def test_recovery_is_idempotent(tenant_env):
    staged, info = _stage_and_journal_new(tenant_env)
    swap_staged_into_place(staged, tenant_env["target"])
    assert _recover(tenant_env) == {"rolled_forward": 1, "rolled_back": 0}
    # Running recovery again must not touch a settled state.
    assert _recover(tenant_env) == {"rolled_forward": 0, "rolled_back": 0}
    _assert_new(tenant_env)


def test_exact_replay_rejected_after_recovery(tenant_env):
    # After a roll-forward the watermark is NEW, so re-sending the same
    # timestamp is a replay and must be refused.
    staged, info = _stage_and_journal_new(tenant_env)
    swap_staged_into_place(staged, tenant_env["target"])
    _recover(tenant_env)
    store, uid = tenant_env["store"], tenant_env["uid"]
    assert store.ingest_timestamp_is_fresh(uid, NEW_TS) is False
    assert store.ingest_timestamp_is_fresh(uid, NEW_TS - 1) is False
    assert store.ingest_timestamp_is_fresh(uid, NEW_TS + 1) is True


def test_failed_upload_is_reingestable_after_rollback(tenant_env):
    # A rolled-back (never completed) upload leaves the watermark OLD, so the
    # same timestamp may legitimately be retried — it is not a replay.
    staged, _info = _stage_and_journal_new(tenant_env)
    staged.unlink()
    _recover(tenant_env)
    store, uid = tenant_env["store"], tenant_env["uid"]
    assert store.ingest_timestamp_is_fresh(uid, NEW_TS) is True
    # And a fresh full install then succeeds and is readable.
    _install_clean(
        store, uid, tenant_env["tenants_dir"], tenant_env["new_payload"], NEW_TS
    )
    _assert_new(tenant_env)


def test_recovery_leaves_other_tenant_untouched(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    alice = store.find_or_create_user_by_identity("google", "alice", now=100)
    bob = store.find_or_create_user_by_identity("google", "bob", now=100)
    store.ensure_tenant(alice, now=100)
    store.ensure_tenant(bob, now=100)
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()

    alice_old, alice_old_sha = _snapshot(tmp_path, "alice_old.db", bump=0)
    alice_new, alice_new_sha = _snapshot(tmp_path, "alice_new.db", bump=1_000_000)
    bob_old, bob_old_sha = _snapshot(tmp_path, "bob_old.db", bump=2_000_000)

    _install_clean(store, alice, tenants_dir, alice_old, OLD_TS)
    _install_clean(store, bob, tenants_dir, bob_old, OLD_TS)

    # Crash mid-install on alice only (file swapped, watermark not committed).
    alice_target = Path(tenant_db_path(tenants_dir, alice))
    staged, info = stage_compressed_snapshot(alice_new, alice_target, MAX_BYTES)
    store.begin_snapshot_install(alice, NEW_TS, info.sha256, NEW_TS, str(staged))
    swap_staged_into_place(staged, alice_target)

    summary = store.recover_snapshot_installs(tenants_dir)
    assert summary == {"rolled_forward": 1, "rolled_back": 0}

    assert file_sha256(alice_target) == alice_new_sha
    assert int(store.get_tenant(alice)["last_ingest_timestamp"]) == NEW_TS

    bob_target = Path(tenant_db_path(tenants_dir, bob))
    assert file_sha256(bob_target) == bob_old_sha
    assert int(store.get_tenant(bob)["last_ingest_timestamp"]) == OLD_TS


def test_recovery_refuses_symlink_target_and_rolls_back(tenant_env):
    # Defensive: if the tenant path became a symlink, recovery must not follow
    # it. It rolls back to old + old and leaves the symlink in place.
    staged, _info = _stage_and_journal_new(tenant_env)
    target = tenant_env["target"]
    real_old = target.with_name("real_old.db")
    target.replace(real_old)
    target.symlink_to(real_old)

    summary = _recover(tenant_env)
    assert summary == {"rolled_forward": 0, "rolled_back": 1}

    assert target.is_symlink()
    assert file_sha256(real_old) == tenant_env["old_sha"]
    assert _watermark(tenant_env["store"], tenant_env["uid"]) == OLD_TS
    assert _journal_count(tenant_env["store"]) == 0
    assert not staged.exists()


def test_recover_pending_installs_ignores_noncanonical_tenant(tmp_path):
    # A tainted journal row must not be turned into a filesystem path.
    store = SecurityStore(tmp_path / "security.db")
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()
    conn = sqlite3.connect(str(store.path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO snapshot_install_journal "
            "(user_id, timestamp, sha256, snapshot_at, staged_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (-5, NEW_TS, "0" * 64, NEW_TS, str(tenants_dir / "x.db")),
        )
        conn.commit()
        summary = recover_pending_installs(conn, tenants_dir)
        assert summary == {"rolled_forward": 0, "rolled_back": 0}
        # Row is left untouched rather than acted on with a tainted id.
        assert conn.execute(
            "SELECT count(*) FROM snapshot_install_journal"
        ).fetchone()[0] == 1
    finally:
        conn.close()
