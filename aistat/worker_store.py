"""Encrypted at-rest store for user Multica tokens on the trusted worker.

Tokens pulled from the public host live only here, AEAD-encrypted (Fernet)
with a key that exists solely on the worker machine and never next to the
ciphertext. Neither this module's runtime dependency (``cryptography``) nor
the key or store files ship in the cPanel package.
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from . import handoff


class WorkerStoreError(RuntimeError):
    """Raised when the encrypted worker store cannot be used safely."""


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
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.store_path, 0o600)
        except OSError:
            pass

    def store_token(
        self,
        user_id: int,
        server_url: str,
        workspace_label: Optional[str],
        token: str,
        token_epoch: int,
        now: Optional[int] = None,
    ) -> None:
        # Validate before encryption or a write transaction. A compromised
        # response/migration cannot pair a PAT with another endpoint in the
        # worker store; empty legacy rows normalize to the exact official URL.
        server_url = handoff.normalize_official_server_url(
            server_url, handoff.OFFICIAL_MULTICA_URL
        )
        now = int(time.time()) if now is None else int(now)
        ciphertext = self._fernet.encrypt(token.encode("utf-8"))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO worker_connections "
                "(user_id, server_url, workspace_label, token_ciphertext, "
                "token_epoch, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(user_id),
                    server_url,
                    workspace_label,
                    ciphertext,
                    int(token_epoch),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_connection(self, user_id: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM worker_connections WHERE user_id = ?",
                (int(user_id),),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_token(self, user_id: int) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT token_ciphertext FROM worker_connections "
                "WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(
                bytes(row["token_ciphertext"])
            ).decode("utf-8")
        except self._invalid_token as exc:
            raise WorkerStoreError(
                "stored token cannot be decrypted with the current worker key"
            ) from exc

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
