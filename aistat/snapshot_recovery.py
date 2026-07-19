"""Crash-atomic tenant snapshot install journal and recovery.

A tenant ingest replaces two independent durable objects:

* the tenant SQLite database file (swapped with ``os.replace``), and
* the replay watermark row in ``security.db`` (``tenants``).

Those live on different files, so a crash between them could leave a mixed
``new snapshot + old watermark`` (or the reverse) state where a replayed or
stale upload is silently accepted. This module makes the pair crash-atomic
with a small write-ahead journal kept in the *same* database as the watermark:

1. Stage and validate the new database into a temp file (caller's job).
2. ``record_install_intent`` — durably journal the intent (new sha, timestamp,
   staged path) in ``security.db``. This is the decision point.
3. ``swap_staged_into_place`` — ``os.replace`` the staged file over the target.
4. ``commit_install`` — advance the watermark and delete the journal row in one
   transaction.

On restart, ``recover_pending_installs`` reconciles every journal row by
comparing the sha256 of the installed database against the journalled target:

* installed == target  -> the swap already happened: roll forward (advance the
  watermark, drop the journal);
* installed != target and the staged file still matches the target sha -> the
  swap had not happened yet: complete it, then roll forward;
* otherwise (staged file lost/corrupt or unsafe target) -> roll back (drop the
  journal, leave the old database and old watermark untouched).

Every crash point therefore recovers to exactly one of two consistent states:
``old snapshot + old watermark`` or ``new snapshot + new watermark``. The mixed
states are impossible.

The module is deliberately dependency-free (standard library only) and Python
3.6 compatible so both the Flask app and the legacy cPanel WSGI entry point can
share one identical recovery contract.
"""

import hashlib
import os
import shutil

from .tenant import canonical_tenant_id, tenant_db_path

INSTALL_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_install_journal (
    user_id      INTEGER PRIMARY KEY,
    timestamp    INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    snapshot_at  INTEGER NOT NULL,
    staged_path  TEXT NOT NULL
);
"""

_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _chmod_private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _unlink_if_exists(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def cleanup_staged_file(path):
    """Remove a staged snapshot temp file and any SQLite sidecars it made."""
    if not path:
        return
    path = os.fspath(path)
    _unlink_if_exists(path)
    for suffix in _SIDECAR_SUFFIXES:
        _unlink_if_exists(path + suffix)


def _fsync_dir(path):
    """Best-effort fsync of a directory so a rename survives a power loss."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def fsync_file(path):
    """Best-effort fsync of a file's data and metadata."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def file_sha256(path):
    """Return the hex sha256 of ``path`` or ``None`` if it cannot be read.

    Symlinks are refused (``None``): the trusted database is never a symlink,
    and reading through one would defeat the traversal/symlink gates.
    """
    try:
        if os.path.islink(path):
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _staged_within(staged_path, tenants_dir):
    """True when ``staged_path`` is a real file directly inside ``tenants_dir``.

    Recovery only ever swaps a file we ourselves staged in the trusted tenants
    directory. Refusing anything else keeps a tampered journal row from making
    ``os.replace`` pull bytes from an attacker-controlled path.
    """
    try:
        if os.path.islink(staged_path) or not os.path.isfile(staged_path):
            return False
        root = os.path.realpath(os.fspath(tenants_dir))
        parent = os.path.dirname(os.path.realpath(staged_path))
        return parent == root
    except OSError:
        return False


def swap_staged_into_place(staged_path, target_path):
    """Atomically move a validated staged database over ``target_path``.

    The single ``os.replace`` is the file-swap commit point. Everything before
    it (symlink guards, ``.previous`` backup, sidecar cleanup) may raise and
    leaves the old database in place. Everything after it is best-effort and
    never raises, so a caught exception always means the swap did not happen.
    """
    staged_path = os.fspath(staged_path)
    target_path = os.fspath(target_path)
    if os.path.islink(target_path):
        raise ValueError("snapshot target must not be a symlink")
    previous = target_path + ".previous"
    if os.path.islink(previous):
        raise ValueError("snapshot backup must not be a symlink")

    if os.path.exists(target_path):
        shutil.copy2(target_path, previous)
        _chmod_private(previous)
    # Query-only connections never create WAL sidecars; clear any leftovers so
    # the freshly swapped inode is not shadowed by a stale journal.
    for suffix in _SIDECAR_SUFFIXES:
        _unlink_if_exists(target_path + suffix)

    os.replace(staged_path, target_path)

    _chmod_private(target_path)
    fsync_file(target_path)
    _fsync_dir(os.path.dirname(target_path) or ".")


def record_install_intent(
    conn, user_id, timestamp, sha256, snapshot_at, staged_path
):
    """Durably journal the intent to install one staged snapshot.

    Runs its own ``BEGIN IMMEDIATE`` transaction. If a previous journal row for
    this tenant exists (e.g. an earlier attempt that never completed), its
    orphaned staged file is removed after the row is replaced.
    """
    user_id = canonical_tenant_id(user_id)
    staged_path = os.fspath(staged_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute(
            "SELECT staged_path FROM snapshot_install_journal "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO snapshot_install_journal "
            "(user_id, timestamp, sha256, snapshot_at, staged_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, int(timestamp), sha256, int(snapshot_at), staged_path),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if prior is not None:
        prior_path = prior["staged_path"]
        if prior_path and prior_path != staged_path:
            _unlink_if_exists(prior_path)


def clear_install_intent(conn, user_id):
    """Delete a tenant's journal row (roll back). Runs its own transaction."""
    user_id = canonical_tenant_id(user_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM snapshot_install_journal WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def commit_install(conn, user_id, timestamp, sha256, snapshot_at):
    """Advance the watermark and drop the journal row in one transaction.

    Returns ``True`` when the watermark was advanced (the normal roll-forward),
    ``False`` when a newer or equal watermark already existed. The journal row
    is always removed so recovery is idempotent.
    """
    user_id = canonical_tenant_id(user_id)
    timestamp = int(timestamp)
    snapshot_at = int(snapshot_at)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT last_ingest_timestamp FROM tenants WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        advanced = False
        if row is not None and timestamp > int(row["last_ingest_timestamp"]):
            conn.execute(
                "UPDATE tenants SET last_ingest_timestamp = ?, "
                "last_snapshot_at = ?, last_snapshot_sha256 = ? "
                "WHERE user_id = ?",
                (timestamp, snapshot_at, sha256, user_id),
            )
            advanced = True
        conn.execute(
            "DELETE FROM snapshot_install_journal WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        return advanced
    except Exception:
        conn.rollback()
        raise


def pending_installs(conn):
    """Return the journal rows for every in-flight install."""
    return conn.execute(
        "SELECT user_id, timestamp, sha256, snapshot_at, staged_path "
        "FROM snapshot_install_journal ORDER BY user_id"
    ).fetchall()


def recover_pending_installs(conn, tenants_dir):
    """Reconcile every journalled install to a consistent old/old or new/new.

    Must be called while holding the ingest lock so it cannot race a live
    ingest or a second worker's recovery. Returns a summary dict counting the
    ``rolled_forward`` and ``rolled_back`` rows.
    """
    summary = {"rolled_forward": 0, "rolled_back": 0}
    for row in pending_installs(conn):
        user_id = int(row["user_id"])
        target_sha = row["sha256"]
        timestamp = int(row["timestamp"])
        snapshot_at = int(row["snapshot_at"])
        staged_path = row["staged_path"]

        try:
            target = tenant_db_path(tenants_dir, user_id)
        except ValueError:
            # Journal row with a non-canonical id: leave it untouched rather
            # than derive a path from tainted input.
            continue

        installed_sha = file_sha256(target)
        if installed_sha == target_sha:
            # The swap already happened before the crash: roll forward.
            commit_install(conn, user_id, timestamp, target_sha, snapshot_at)
            _unlink_if_exists(staged_path)
            summary["rolled_forward"] += 1
            continue

        if file_sha256(staged_path) == target_sha and _staged_within(
            staged_path, tenants_dir
        ):
            # The staged database is intact: finish the swap, then roll forward.
            try:
                swap_staged_into_place(staged_path, target)
            except (OSError, ValueError):
                clear_install_intent(conn, user_id)
                _unlink_if_exists(staged_path)
                summary["rolled_back"] += 1
                continue
            commit_install(conn, user_id, timestamp, target_sha, snapshot_at)
            summary["rolled_forward"] += 1
            continue

        # The staged database is gone or corrupt: roll back to old/old.
        clear_install_intent(conn, user_id)
        _unlink_if_exists(staged_path)
        summary["rolled_back"] += 1
    return summary
