"""Encrypted at-rest store for user Multica tokens on the trusted worker.

Tokens pulled from the public host live only here, AEAD-encrypted (Fernet)
with a key that exists solely on the worker machine and never next to the
ciphertext. Neither this module's runtime dependency (``cryptography``) nor
the key or store files ship in the cPanel package.
"""

import fcntl
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import handoff
from .tenant import canonical_tenant_id


class WorkerStoreError(RuntimeError):
    """Raised when the encrypted worker store cannot be used safely."""


@dataclass(frozen=True)
class WorkerCredential:
    """One coherent decrypted credential version; token stays out of repr."""

    user_id: int
    server_url: str
    workspace_label: Optional[str]
    token_epoch: int
    token: str = field(repr=False)


_LOCAL_FENCES: Dict[str, threading.Lock] = {}
_LOCAL_FENCES_GUARD = threading.Lock()


def _local_fence(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCAL_FENCES_GUARD:
        lock = _LOCAL_FENCES.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_FENCES[key] = lock
        return lock


class CredentialFence:
    """Short per-tenant process/thread fence for credential version changes."""

    def __init__(self, store: "WorkerTokenStore", user_id: int):
        self._store = store
        self.user_id = canonical_tenant_id(user_id)
        self._path = store._fence_root / "conn-{}.lock".format(self.user_id)
        self._local_lock = _local_fence(self._path)
        self._fd: Optional[int] = None

    def __enter__(self) -> "CredentialFence":
        self._local_lock.acquire()
        fd: Optional[int] = None
        try:
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkerStoreError(
                    "the credential version fence is not a regular file"
                )
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._fd = fd
            return self
        except WorkerStoreError:
            if fd is not None:
                os.close(fd)
            self._local_lock.release()
            raise
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            self._local_lock.release()
            raise WorkerStoreError(
                "the credential version fence could not be acquired"
            ) from exc

    def __exit__(self, *_exc) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
                self._local_lock.release()

    def get_credential(self) -> Optional[WorkerCredential]:
        """Read token and epoch from one SQLite row while fenced."""
        self._require_acquired()
        return self._store._get_credential_unlocked(self.user_id)

    def is_current(self, token_epoch: int) -> bool:
        """Return whether the expected active epoch is still current."""
        self._require_acquired()
        return self._store._is_current_unlocked(self.user_id, token_epoch)

    def _require_acquired(self) -> None:
        if self._fd is None:
            raise WorkerStoreError("the credential version fence is not acquired")


def _load_fernet():
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:
        raise WorkerStoreError(
            "the 'cryptography' package is required on the worker machine; "
            "install it from requirements.txt (it must never ship to cPanel)"
        ) from exc
    return Fernet, InvalidToken


class WorkerTokenStore:
    """SQLite store whose token column holds only Fernet ciphertext."""

    def __init__(self, store_path, key_path):
        self.store_path = Path(store_path)
        self.key_path = Path(key_path)
        if self.store_path.resolve().parent == self.key_path.resolve().parent:
            raise WorkerStoreError(
                "the worker key must not live in the same directory "
                "as the encrypted store"
            )
        fernet_cls, self._invalid_token = _load_fernet()
        self._fernet = fernet_cls(self._load_or_create_key(fernet_cls))
        self._fence_root = self.store_path.parent / ".worker_connection_fences"
        self._init_store()

    def _load_or_create_key(self, fernet_cls) -> bytes:
        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
            try:
                fernet_cls(key)
            except (ValueError, TypeError) as exc:
                raise WorkerStoreError(
                    "the worker key file is not a valid Fernet key"
                ) from exc
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
            return key
        key = fernet_cls.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.key_path.parent.chmod(0o700)
        except OSError:
            pass
        descriptor = os.open(
            self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            os.write(descriptor, key)
        finally:
            os.close(descriptor)
        return key

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.store_path), timeout=10)
        conn.row_factory = sqlite3.Row
        # Deleted/replaced ciphertext must not linger in free pages either.
        conn.execute("PRAGMA secure_delete = ON")
        return conn

    def _init_store(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_fence_root()
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_connections (
                    user_id          INTEGER PRIMARY KEY,
                    server_url       TEXT NOT NULL,
                    workspace_label  TEXT,
                    token_ciphertext BLOB NOT NULL,
                    token_epoch      INTEGER NOT NULL,
                    updated_at       INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_connection_versions (
                    user_id      INTEGER PRIMARY KEY,
                    token_epoch  INTEGER NOT NULL,
                    state        TEXT NOT NULL CHECK (
                        state IN ('stored', 'revoked')
                    ),
                    updated_at   INTEGER NOT NULL
                )
                """
            )
            # One-time compatible migration for stores created before the
            # durable version watermark existed.
            conn.execute(
                "INSERT OR REPLACE INTO worker_connection_versions "
                "(user_id, token_epoch, state, updated_at) "
                "SELECT c.user_id, c.token_epoch, 'stored', c.updated_at "
                "FROM worker_connections c "
                "LEFT JOIN worker_connection_versions v "
                "ON v.user_id = c.user_id "
                "WHERE v.user_id IS NULL OR c.token_epoch > v.token_epoch"
            )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.store_path, 0o600)
        except OSError:
            pass

    def _ensure_fence_root(self) -> None:
        try:
            self._fence_root.mkdir(mode=0o700, exist_ok=True)
            info = os.lstat(self._fence_root)
        except OSError as exc:
            raise WorkerStoreError(
                "the credential version fence directory could not be prepared"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkerStoreError(
                "the credential version fence directory is unsafe"
            )
        try:
            os.chmod(self._fence_root, 0o700)
        except OSError:
            pass

    def credential_fence(self, user_id: int) -> CredentialFence:
        """Return the common per-tenant version fence used by sync/collector."""
        return CredentialFence(self, user_id)

    def store_token(
        self,
        user_id: int,
        server_url: str,
        workspace_label: Optional[str],
        token: str,
        token_epoch: int,
        now: Optional[int] = None,
    ) -> bool:
        # Validate before encryption or a write transaction. A compromised
        # response/migration cannot pair a PAT with another endpoint in the
        # worker store; empty legacy rows normalize to the exact official URL.
        server_url = handoff.normalize_official_server_url(
            server_url, handoff.OFFICIAL_MULTICA_URL
        )
        now = int(time.time()) if now is None else int(now)
        user_id = canonical_tenant_id(user_id)
        token_epoch = int(token_epoch)
        with self.credential_fence(user_id):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                version = conn.execute(
                    "SELECT token_epoch, state FROM worker_connection_versions "
                    "WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if version is not None:
                    current_epoch = int(version["token_epoch"])
                    if token_epoch < current_epoch:
                        conn.rollback()
                        return False
                    if token_epoch == current_epoch:
                        if version["state"] != "stored":
                            conn.rollback()
                            return False
                        current = self._get_credential_from_connection(
                            conn, user_id
                        )
                        if current is None or (
                            current.server_url != server_url
                            or current.workspace_label != workspace_label
                            or current.token != token
                        ):
                            raise WorkerStoreError(
                                "the same credential epoch has conflicting data"
                            )
                        conn.commit()
                        return True

                ciphertext = self._fernet.encrypt(token.encode("utf-8"))
                conn.execute(
                    "INSERT OR REPLACE INTO worker_connections "
                    "(user_id, server_url, workspace_label, token_ciphertext, "
                    "token_epoch, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        server_url,
                        workspace_label,
                        ciphertext,
                        token_epoch,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO worker_connection_versions "
                    "(user_id, token_epoch, state, updated_at) "
                    "VALUES (?, ?, 'stored', ?)",
                    (user_id, token_epoch, now),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    def delete_connection(
        self, user_id: int, token_epoch: Optional[int] = None
    ) -> bool:
        """Delete only a current/newer epoch and preserve a revoke tombstone."""
        user_id = canonical_tenant_id(user_id)
        explicit_epoch = token_epoch is not None
        with self.credential_fence(user_id):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                version = conn.execute(
                    "SELECT token_epoch, state FROM worker_connection_versions "
                    "WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if token_epoch is None:
                    if version is None:
                        conn.rollback()
                        return False
                    token_epoch = int(version["token_epoch"])
                else:
                    token_epoch = int(token_epoch)

                if version is not None:
                    current_epoch = int(version["token_epoch"])
                    if token_epoch < current_epoch:
                        conn.rollback()
                        return False
                    if token_epoch == current_epoch:
                        if version["state"] == "revoked":
                            conn.rollback()
                            return explicit_epoch
                        if explicit_epoch:
                            conn.rollback()
                            return False

                conn.execute(
                    "DELETE FROM worker_connections WHERE user_id = ?",
                    (user_id,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO worker_connection_versions "
                    "(user_id, token_epoch, state, updated_at) "
                    "VALUES (?, ?, 'revoked', ?)",
                    (user_id, token_epoch, int(time.time())),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    def get_token(self, user_id: int) -> Optional[str]:
        credential = self.get_credential(user_id)
        return None if credential is None else credential.token

    def get_credential(self, user_id: int) -> Optional[WorkerCredential]:
        """Read token, routing metadata and epoch as one atomic version."""
        with self.credential_fence(user_id) as fence:
            return fence.get_credential()

    def _get_credential_unlocked(
        self, user_id: int
    ) -> Optional[WorkerCredential]:
        conn = self._connect()
        try:
            return self._get_credential_from_connection(conn, user_id)
        finally:
            conn.close()

    def _get_credential_from_connection(
        self, conn: sqlite3.Connection, user_id: int
    ) -> Optional[WorkerCredential]:
        row = conn.execute(
            "SELECT user_id, server_url, workspace_label, token_ciphertext, "
            "token_epoch FROM worker_connections WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            token = self._fernet.decrypt(
                bytes(row["token_ciphertext"])
            ).decode("utf-8")
        except self._invalid_token as exc:
            raise WorkerStoreError(
                "stored token cannot be decrypted with the current worker key"
            ) from exc
        return WorkerCredential(
            user_id=int(row["user_id"]),
            server_url=str(row["server_url"]),
            workspace_label=row["workspace_label"],
            token_epoch=int(row["token_epoch"]),
            token=token,
        )

    def _is_current_unlocked(self, user_id: int, token_epoch: int) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT v.token_epoch FROM worker_connection_versions v "
                "JOIN worker_connections c ON c.user_id = v.user_id "
                "AND c.token_epoch = v.token_epoch "
                "WHERE v.user_id = ? AND v.state = 'stored'",
                (int(user_id),),
            ).fetchone()
        finally:
            conn.close()
        return row is not None and int(row["token_epoch"]) == int(token_epoch)

    def list_connections(self) -> List[dict]:
        """Store contents without token material, for logs and diagnostics."""
        conn = self._connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT user_id, server_url, workspace_label, "
                    "token_epoch, updated_at FROM worker_connections "
                    "ORDER BY user_id"
                ).fetchall()
            ]
        finally:
            conn.close()
