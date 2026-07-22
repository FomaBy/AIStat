"""Create, validate and atomically install AIStat SQLite snapshots."""

import gzip
import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple, Set, Tuple

from .db import SCHEMA_VERSION
from .snapshot_recovery import fsync_file, swap_staged_into_place

REQUIRED_TABLES: Set[str] = {
    "runtimes",
    "agents",
    "projects",
    "issues",
    "daily_usage",
    "issue_usage",
    "runs",
    "runtime_activity",
    "sync_state",
    "sync_beats",
    "poll_cycles",
    "model_pricing",
}


class SnapshotError(ValueError):
    """Raised when a snapshot is oversized, invalid or incompatible."""


# A frozen, keyword-constructed value object. Uses ``typing.NamedTuple`` rather
# than ``@dataclass(frozen=True)`` so this module — part of the ``aistat.backup``
# import chain — stays importable on the production host's Python 3.6.8, which
# has no ``dataclasses`` module (FAN-1435).
class SnapshotInfo(NamedTuple):
    sha256: str
    size_bytes: int
    schema_version: int


_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _temp_path(parent: Path, suffix: str) -> Path:
    handle, name = tempfile.mkstemp(
        prefix=".aistat-snapshot-", suffix=suffix, dir=str(parent)
    )
    os.close(handle)
    path = Path(name)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _cleanup_snapshot_temp_files(temp_path: Path) -> None:
    """Remove a temporary SQLite file and every sidecar it may have created."""
    for suffix in ("",) + _SQLITE_SIDECAR_SUFFIXES:
        # ``Path.unlink(missing_ok=True)`` is Python 3.8+; the production host
        # runs 3.6.8, so swallow the missing-file case explicitly instead.
        try:
            Path(str(temp_path) + suffix).unlink()
        except FileNotFoundError:
            pass


def _path_has_open_owner(path: Path) -> bool:
    """Return whether a process currently has ``path`` open.

    Orphan cleanup is best-effort maintenance. If the platform cannot answer
    the ownership question, fail closed and leave the file for a later run.
    """
    try:
        result = subprocess.run(
            ["lsof", "-t", "--", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # ``text=`` is the Python 3.7+ spelling; ``universal_newlines`` is the
            # identical, 3.6-compatible option (the host runs 3.6.8).
            universal_newlines=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.stdout.strip():
        return True
    return result.returncode not in (0, 1)


def cleanup_orphan_snapshot_sidecars(parent: Path) -> int:
    """Remove unused snapshot sidecars from ``parent``.

    Only the sidecars produced by :func:`_temp_path` are considered. A file is
    removed only when its matching temporary database is already gone and the
    sidecar is not open by a process; uncertain ownership, symlinks and
    non-files are skipped. Returns the number of sidecars removed.
    """
    parent = Path(parent)
    removed = 0
    for path in parent.glob(".aistat-snapshot-*.db-*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.name.endswith(_SQLITE_SIDECAR_SUFFIXES)
        ):
            continue
        temp_path = path.with_name(path.name[:-4])
        if temp_path.exists() or _path_has_open_owner(path):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


# ``sqlite3.Connection.backup()`` — SQLite's online backup API — exists only on
# Python 3.7+. The production host runs 3.6.8, so a lock-guarded file copy stands
# in there (FAN-1435). Detected once at import; the owner-publisher on Python
# 3.9+ keeps using the backup API unchanged.
_HAS_SQLITE_BACKUP = hasattr(sqlite3.Connection, "backup")


def _snapshot_with_backup_api(db_path: Path, temp_path: Path) -> None:
    """Coherent copy via SQLite's online backup API (Python 3.7+)."""
    source = sqlite3.connect(str(db_path))
    try:
        target = sqlite3.connect(str(temp_path))
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
            target.commit()
        finally:
            target.close()
    finally:
        source.close()


def _snapshot_with_file_copy(db_path: Path, temp_path: Path) -> None:
    """Coherent copy for Python 3.6, which lacks ``Connection.backup()``.

    Holds a write lock (``BEGIN IMMEDIATE``) on the source so no concurrent
    writer or checkpoint can move it while the main database and any ``-wal``
    sidecar are copied as a consistent pair; the source's on-disk bytes are
    never modified. The copied WAL is then folded into the copied main file and
    the copy is dropped to a self-contained rollback-journal database, matching
    the backup-API path (which also ends in ``journal_mode=DELETE``).
    """
    source = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    try:
        source.execute("BEGIN IMMEDIATE")
        try:
            shutil.copyfile(str(db_path), str(temp_path))
            wal = Path(str(db_path) + "-wal")
            if wal.is_file():
                shutil.copyfile(str(wal), str(temp_path) + "-wal")
        finally:
            source.execute("COMMIT")
    finally:
        source.close()
    dest = sqlite3.connect(str(temp_path))
    try:
        dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dest.execute("PRAGMA journal_mode = DELETE")
        dest.commit()
    finally:
        dest.close()
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        try:
            os.unlink(str(temp_path) + suffix)
        except FileNotFoundError:
            pass


def create_compressed_snapshot(db_path: Path) -> bytes:
    """Copy a possibly WAL-backed database coherently, gzip-compressed.

    Uses SQLite's online backup API on Python 3.7+; on the production host's
    Python 3.6.8 (no ``Connection.backup()``) it falls back to a lock-guarded
    file copy (FAN-1435). Both paths yield a self-contained, integrity-checkable
    rollback-journal database.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise SnapshotError(f"database does not exist: {db_path}")
    cleanup_orphan_snapshot_sidecars(db_path.parent)
    temp_path = _temp_path(db_path.parent, ".db")
    try:
        try:
            if _HAS_SQLITE_BACKUP:
                _snapshot_with_backup_api(db_path, temp_path)
            else:
                _snapshot_with_file_copy(db_path, temp_path)
        except sqlite3.Error as exc:
            raise SnapshotError(f"cannot create SQLite backup: {exc}") from exc
        return gzip.compress(temp_path.read_bytes(), compresslevel=6)
    finally:
        _cleanup_snapshot_temp_files(temp_path)


def _decompress_to_file(payload: bytes, target: Path, max_bytes: int) -> int:
    if len(payload) > max_bytes:
        raise SnapshotError("compressed snapshot exceeds the size limit")
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as source:
            with target.open("wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise SnapshotError(
                            "decompressed snapshot exceeds the size limit"
                        )
                    output.write(chunk)
    except (OSError, EOFError) as exc:
        raise SnapshotError("snapshot is not valid gzip data") from exc
    if total == 0:
        raise SnapshotError("snapshot is empty")
    return total


def validate_snapshot(path: Path) -> SnapshotInfo:
    path = Path(path)
    try:
        with path.open("rb") as source:
            if source.read(16) != b"SQLite format 3\x00":
                raise SnapshotError("snapshot is not a SQLite database")
    except OSError as exc:
        raise SnapshotError(f"cannot read snapshot: {exc}") from exc

    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise SnapshotError(f"SQLite integrity check failed: {check}")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < 1 or version > SCHEMA_VERSION:
                raise SnapshotError(
                    f"unsupported schema version {version}; "
                    f"server supports 1..{SCHEMA_VERSION}"
                )
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                raise SnapshotError(
                    "snapshot is missing required tables: " + ", ".join(missing)
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SnapshotError(f"cannot validate SQLite snapshot: {exc}") from exc

    data = path.read_bytes()
    return SnapshotInfo(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        schema_version=version,
    )


def daily_usage_max_date(path: Path):
    """Return the latest ``daily_usage.date`` in a SQLite file, or ``None``.

    Read-only and deliberately defensive: a missing file, a missing or empty
    ``daily_usage`` table, or any read error all yield ``None`` so a caller
    reads that as "this database carries no usage data". Standard-library only
    and Python 3.6 compatible so the Flask app and the legacy cPanel WSGI entry
    point share one identical check (the ingest freshness guard below).
    """
    path = Path(path)
    try:
        if not path.is_file():
            return None
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(date) FROM daily_usage").fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None
    if not row or row[0] is None:
        return None
    return str(row[0])


def snapshot_is_fresh_enough(staged_path: Path, target_path: Path) -> bool:
    """Whether installing ``staged_path`` over ``target_path`` keeps usage moving forward.

    The host already rejects timestamp *replays* (a snapshot whose signed
    timestamp is not newer than the last accepted one). This adds an orthogonal
    guard on the *data*: a snapshot must never move a tenant's daily usage
    backwards in time. It protects against a stale or degraded publisher — e.g.
    an owner poller whose CLI session lapsed and now emits freshly-timestamped
    but old-dated snapshots — overwriting a tenant database that a healthier
    publisher has already advanced to a newer day.

    Accepts when the target has no usage yet (first ingest / empty tenant) or
    when the staged snapshot's latest usage date is greater than or equal to the
    target's (re-publishing the same day, or a newer day, is always fine).
    Rejects only when the target already holds usage and the staged snapshot is
    strictly older, or carries no usage at all.
    """
    current = daily_usage_max_date(target_path)
    if current is None:
        return True
    incoming = daily_usage_max_date(staged_path)
    if incoming is None:
        return False
    return incoming >= current


def stage_compressed_snapshot(
    payload: bytes, target_path: Path, max_bytes: int
) -> Tuple[Path, SnapshotInfo]:
    """Decompress and validate a snapshot into a temp file next to the target.

    Returns the staged temp path and its :class:`SnapshotInfo`. The target is
    **not** touched — the caller journals the intent and then swaps the staged
    file into place with :func:`snapshot_recovery.swap_staged_into_place`, so a
    crash between the two leaves a recoverable state. The staged file is left on
    disk on success and cleaned up only when staging itself fails.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.is_symlink():
        raise SnapshotError("snapshot target must not be a symlink")
    previous = target_path.with_name(target_path.name + ".previous")
    if previous.is_symlink():
        raise SnapshotError("snapshot backup must not be a symlink")
    temp_path = _temp_path(target_path.parent, ".db")
    try:
        _decompress_to_file(payload, temp_path, max_bytes)
        info = validate_snapshot(temp_path)
        fsync_file(temp_path)
        return temp_path, info
    except BaseException:
        _cleanup_snapshot_temp_files(temp_path)
        raise


def install_compressed_snapshot(
    payload: bytes, target_path: Path, max_bytes: int
) -> SnapshotInfo:
    """Validate then atomically replace one trusted tenant database path.

    Convenience wrapper around :func:`stage_compressed_snapshot` plus the
    shared atomic swap, for callers that do not need the crash-atomic journal.
    """
    target_path = Path(target_path)
    staged_path, info = stage_compressed_snapshot(
        payload, target_path, max_bytes
    )
    try:
        swap_staged_into_place(staged_path, target_path)
    finally:
        _cleanup_snapshot_temp_files(staged_path)
    return info
