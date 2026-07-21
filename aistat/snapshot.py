"""Create, validate and atomically install AIStat SQLite snapshots."""

import gzip
import hashlib
import io
import os
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


def create_compressed_snapshot(db_path: Path) -> bytes:
    """Use SQLite's backup API so WAL-backed local data is copied coherently."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise SnapshotError(f"database does not exist: {db_path}")
    cleanup_orphan_snapshot_sidecars(db_path.parent)
    temp_path = _temp_path(db_path.parent, ".db")
    source = None
    target = None
    try:
        source = sqlite3.connect(str(db_path))
        target = sqlite3.connect(str(temp_path))
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
            target.commit()
        except sqlite3.Error as exc:
            raise SnapshotError(f"cannot create SQLite backup: {exc}") from exc
        return gzip.compress(temp_path.read_bytes(), compresslevel=6)
    finally:
        try:
            if target is not None:
                target.close()
        finally:
            try:
                if source is not None:
                    source.close()
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
