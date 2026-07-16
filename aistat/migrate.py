"""Dependency-free owner migration from one legacy DB to a tenant DB.

The command is Python 3.6-compatible so it can run directly on the shared
hosting environment before the dependency-free legacy WSGI app is restarted.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager

from .tenant import tenant_db_path

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA_VERSION = 4
REQUIRED_TABLES = {
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


class MigrationError(RuntimeError):
    """Raised when owner data cannot be migrated safely."""


def _setting(config, attribute, env_name, default):
    if config is not None:
        value = getattr(config, attribute)
        return os.fspath(value) if attribute.endswith(("path", "dir")) else value
    return os.environ.get(env_name, default)


@contextmanager
def _exclusive_lock(path):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, 0o700)
    with open(path, "a+b") as handle:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _init_security(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingest_state (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            last_timestamp  INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO ingest_state (id, last_timestamp) VALUES (1, 0);
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      INTEGER NOT NULL,
            display_name    TEXT,
            email           TEXT,
            is_admin        INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tenants (
            user_id                INTEGER PRIMARY KEY,
            created_at             INTEGER NOT NULL,
            last_ingest_timestamp  INTEGER NOT NULL DEFAULT 0,
            last_snapshot_at       INTEGER,
            last_snapshot_sha256   TEXT
        );
        """
    )


def _ensure_owner(conn, username, email, now):
    admins = conn.execute(
        "SELECT id FROM users WHERE is_admin = 1 ORDER BY id"
    ).fetchall()
    if len(admins) > 1:
        raise MigrationError("security.db contains multiple admin users")
    if admins:
        return int(admins[0][0])
    normalized_email = email.strip().lower() if email else None
    if normalized_email:
        matches = conn.execute(
            "SELECT id FROM users "
            "WHERE email IS NOT NULL AND lower(email) = ? ORDER BY id",
            (normalized_email,),
        ).fetchall()
        if len(matches) > 1:
            raise MigrationError("AISTAT_ADMIN_EMAIL matches multiple users")
        if matches:
            user_id = int(matches[0][0])
            conn.execute(
                "UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,)
            )
            return user_id
    cursor = conn.execute(
        "INSERT INTO users (created_at, display_name, email, is_admin) "
        "VALUES (?, ?, ?, 1)",
        (now, username, normalized_email),
    )
    return int(cursor.lastrowid)


def _validate_database(path):
    if os.path.islink(path):
        raise MigrationError("database path must not be a symlink")
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        try:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MigrationError("SQLite integrity check failed")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < 1 or version > SCHEMA_VERSION:
                raise MigrationError(
                    "unsupported schema version {}".format(version)
                )
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                raise MigrationError(
                    "database is missing required tables: "
                    + ", ".join(missing)
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise MigrationError("cannot validate SQLite database: {}".format(exc))
    with open(path, "rb") as source:
        digest = hashlib.sha256(source.read()).hexdigest()
    return {
        "sha256": digest,
        "size_bytes": os.path.getsize(path),
        "schema_version": version,
    }


def _coherent_copy(source_path, target_dir):
    handle, temp_path = tempfile.mkstemp(
        prefix=".aistat-owner-migration-", suffix=".db", dir=target_dir
    )
    os.close(handle)
    try:
        _copy_database(source_path, temp_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    finally:
        # Rewriting the copy's journal mode may leave -wal/-shm sidecars;
        # the standalone copy must be the only file left behind.
        for stale in (temp_path + "-wal", temp_path + "-shm"):
            try:
                os.unlink(stale)
            except OSError:
                pass
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    return temp_path


def _copy_database(source_path, temp_path):
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(temp_path)
    try:
        if hasattr(source, "backup"):
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
            target.commit()
        else:
            # Python 3.6 has no Connection.backup(). The hosted source is
            # read-only between signed ingests; under ingest.lock a full WAL
            # checkpoint followed by a file copy is coherent.
            target.close()
            target = None
            busy = source.execute(
                "PRAGMA wal_checkpoint(FULL)"
            ).fetchone()[0]
            if busy:
                raise MigrationError(
                    "cannot checkpoint legacy WAL database before copying"
                )
            source.commit()
            shutil.copy2(source_path, temp_path)
            # The copied file header still marks WAL mode, and without the
            # source -wal/-shm sidecars such a copy cannot be opened
            # read-only; make it a standalone rollback-journal database.
            target = sqlite3.connect(temp_path)
            target.execute("PRAGMA journal_mode = DELETE")
            target.commit()
    except sqlite3.Error as exc:
        raise MigrationError("cannot copy legacy database: {}".format(exc))
    finally:
        if target is not None:
            target.close()
        source.close()


def _install_archive(copy_path, archive_path, expected_sha256):
    """Write the rollback archive via a validated sibling temp file.

    The archive only appears under its final name after a complete,
    validated copy, so a write failure can never leave a partial
    ``.migrated`` behind to block a retry.
    """
    if os.path.exists(archive_path):
        return
    handle, temp_archive = tempfile.mkstemp(
        prefix=".aistat-owner-archive-",
        suffix=".db",
        dir=os.path.dirname(archive_path),
    )
    os.close(handle)
    try:
        shutil.copy2(copy_path, temp_archive)
        try:
            os.chmod(temp_archive, 0o600)
        except OSError:
            pass
        if _validate_database(temp_archive)["sha256"] != expected_sha256:
            raise MigrationError(
                "archive copy does not match the migrated data"
            )
        os.replace(temp_archive, archive_path)
        temp_archive = None
    finally:
        if temp_archive and os.path.exists(temp_archive):
            os.unlink(temp_archive)


def _remove_legacy_source(source_path):
    for path in (source_path, source_path + "-wal", source_path + "-shm"):
        try:
            os.unlink(path)
        except OSError:
            if os.path.exists(path):
                raise


def migrate_owner_database(config=None, now=None):
    """Move the coherent legacy DB contents into the canonical owner tenant."""
    now = int(time.time()) if now is None else int(now)
    db_path = _setting(
        config,
        "db_path",
        "AISTAT_DB_PATH",
        os.path.join(PROJECT_ROOT, "data", "aistat.db"),
    )
    security_db_path = _setting(
        config,
        "security_db_path",
        "AISTAT_SECURITY_DB_PATH",
        os.path.join(PROJECT_ROOT, "data", "security.db"),
    )
    tenants_dir = _setting(
        config,
        "tenants_dir",
        "AISTAT_TENANTS_DIR",
        os.path.join(PROJECT_ROOT, "data", "tenants"),
    )
    admin_username = _setting(
        config, "auth_username", "AISTAT_ADMIN_USERNAME", "admin"
    )
    admin_email = _setting(
        config, "admin_email", "AISTAT_ADMIN_EMAIL", None
    )
    db_path = os.path.abspath(db_path)
    security_db_path = os.path.abspath(security_db_path)
    tenants_dir = os.path.abspath(tenants_dir)
    if not os.path.isdir(tenants_dir):
        os.makedirs(tenants_dir, 0o700)
    try:
        os.chmod(tenants_dir, 0o700)
    except OSError:
        pass
    security_parent = os.path.dirname(security_db_path)
    if security_parent and not os.path.isdir(security_parent):
        os.makedirs(security_parent, 0o700)

    with _exclusive_lock(os.path.join(security_parent, "migration.lock")):
        with _exclusive_lock(os.path.join(security_parent, "ingest.lock")):
            security = sqlite3.connect(security_db_path, timeout=10)
            try:
                security.execute("BEGIN IMMEDIATE")
                _init_security(security)
                owner_user_id = _ensure_owner(
                    security, admin_username, admin_email, now
                )
                target_path = tenant_db_path(tenants_dir, owner_user_id)
                if os.path.abspath(target_path) == db_path:
                    raise MigrationError(
                        "legacy and tenant database paths must differ"
                    )
                legacy_row = security.execute(
                    "SELECT last_timestamp FROM ingest_state WHERE id = 1"
                ).fetchone()
                legacy_timestamp = int(legacy_row[0]) if legacy_row else 0

                migrated = False
                if os.path.exists(db_path):
                    temp_path = _coherent_copy(db_path, tenants_dir)
                    try:
                        candidate = _validate_database(temp_path)
                        archive_path = db_path + ".migrated"
                        if (
                            os.path.exists(archive_path)
                            and _validate_database(archive_path)["sha256"]
                            != candidate["sha256"]
                        ):
                            raise MigrationError(
                                "existing legacy migration archive differs"
                            )
                        if os.path.exists(target_path):
                            existing = _validate_database(target_path)
                            if candidate["sha256"] != existing["sha256"]:
                                raise MigrationError(
                                    "owner tenant already contains different data"
                                )
                            _install_archive(
                                temp_path, archive_path, candidate["sha256"]
                            )
                        else:
                            if os.path.islink(target_path):
                                raise MigrationError(
                                    "tenant database must not be a symlink"
                                )
                            # The rollback archive must be in place before
                            # the tenant DB: an archive failure would
                            # otherwise strand a new target that the
                            # rolled-back security DB never registered.
                            _install_archive(
                                temp_path, archive_path, candidate["sha256"]
                            )
                            os.replace(temp_path, target_path)
                            temp_path = None
                            try:
                                os.chmod(target_path, 0o600)
                            except OSError:
                                pass
                            migrated = True
                    finally:
                        if temp_path and os.path.exists(temp_path):
                            os.unlink(temp_path)
                    _remove_legacy_source(db_path)

                if os.path.exists(target_path):
                    info = _validate_database(target_path)
                    empty = False
                else:
                    info = None
                    empty = True

                security.execute(
                    "INSERT OR IGNORE INTO tenants "
                    "(user_id, created_at, last_ingest_timestamp) "
                    "VALUES (?, ?, ?)",
                    (owner_user_id, now, legacy_timestamp),
                )
                security.execute(
                    "UPDATE tenants SET last_ingest_timestamp = "
                    "MAX(last_ingest_timestamp, ?) WHERE user_id = ?",
                    (legacy_timestamp, owner_user_id),
                )
                if info:
                    security.execute(
                        "UPDATE tenants SET "
                        "last_snapshot_at = COALESCE(last_snapshot_at, ?), "
                        "last_snapshot_sha256 = "
                        "COALESCE(last_snapshot_sha256, ?) WHERE user_id = ?",
                        (now, info["sha256"], owner_user_id),
                    )
                security.commit()
            except Exception:
                security.rollback()
                raise
            finally:
                security.close()
            try:
                os.chmod(security_db_path, 0o600)
            except OSError:
                pass

    result = {
        "status": "ok",
        "owner_user_id": owner_user_id,
        "tenant_path": target_path,
        "migrated": migrated,
        "empty": empty,
    }
    if info:
        result.update(info)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Migrate legacy AIStat data into the owner tenant"
    )
    parser.parse_args(argv)
    try:
        result = migrate_owner_database()
    except (MigrationError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
