"""Create, validate and atomically install AIStat SQLite snapshots."""

import gzip
import hashlib
import io
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Set

from .db import SCHEMA_VERSION

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


@dataclass(frozen=True)
class SnapshotInfo:
    sha256: str
    size_bytes: int
    schema_version: int


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


def create_compressed_snapshot(db_path: Path) -> bytes:
    """Use SQLite's backup API so WAL-backed local data is copied coherently."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise SnapshotError(f"database does not exist: {db_path}")
    temp_path = _temp_path(db_path.parent, ".db")
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(temp_path))
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
        target.commit()
    except sqlite3.Error as exc:
        raise SnapshotError(f"cannot create SQLite backup: {exc}") from exc
    finally:
        target.close()
        source.close()
    try:
        return gzip.compress(temp_path.read_bytes(), compresslevel=6)
    finally:
        temp_path.unlink(missing_ok=True)


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


def install_compressed_snapshot(
    payload: bytes, target_path: Path, max_bytes: int
) -> SnapshotInfo:
    """Validate then atomically replace one trusted tenant database path."""
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

        if target_path.exists():
            shutil.copy2(target_path, previous)
            try:
                os.chmod(previous, 0o600)
            except OSError:
                pass

        # The hosted app uses query-only connections and never creates WAL
        # sidecars. Remove leftovers from the initial empty bootstrap database
        # before the new inode becomes visible.
        for suffix in ("-wal", "-shm"):
            target_path.with_name(target_path.name + suffix).unlink(
                missing_ok=True
            )
        os.replace(temp_path, target_path)
        try:
            os.chmod(target_path, 0o600)
        except OSError:
            pass
        return info
    finally:
        temp_path.unlink(missing_ok=True)
