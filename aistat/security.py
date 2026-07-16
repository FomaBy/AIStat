"""Security primitives for the public AIStat deployment.

The public host never receives a Multica API token. It has only:

* a password hash for the dashboard login;
* an independent Flask session-signing secret;
* an independent HMAC key for signed SQLite snapshot uploads.

Failed-login counters, user accounts, the tenant registry and per-tenant
snapshot/replay state are stored in a small database separate from replaceable
Multica data snapshots.
"""

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .config import Config
from .tenant import (
    canonical_tenant_id,
    snapshot_signature as tenant_snapshot_signature,
)

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
OAUTH_STATE_TTL_SECONDS = 10 * 60


class SecurityConfigError(ValueError):
    """Raised when production security settings are absent or unsafe."""


def _secret_bytes(value: Optional[str], name: str) -> bytes:
    if not value or len(value.encode("utf-8")) < 32:
        raise SecurityConfigError(f"{name} must contain at least 32 bytes")
    return value.encode("utf-8")


def validate_public_config(config: Config) -> None:
    if not config.auth_username or len(config.auth_username) > 128:
        raise SecurityConfigError("AISTAT_ADMIN_USERNAME is invalid")
    if not config.auth_password_hash or "$" not in config.auth_password_hash:
        raise SecurityConfigError(
            "AISTAT_PASSWORD_HASH must be a Werkzeug password hash"
        )
    session_secret = _secret_bytes(
        config.session_secret, "AISTAT_SESSION_SECRET"
    )
    ingest_secret = _secret_bytes(
        config.ingest_secret, "AISTAT_INGEST_SECRET"
    )
    if hmac.compare_digest(session_secret, ingest_secret):
        raise SecurityConfigError(
            "session and ingest secrets must be independent"
        )
    if not config.allowed_hosts:
        raise SecurityConfigError("AISTAT_ALLOWED_HOSTS must not be empty")
    if config.db_path.resolve() == config.security_db_path.resolve():
        raise SecurityConfigError(
            "AISTAT_DB_PATH and AISTAT_SECURITY_DB_PATH must be different"
        )
    tenants_dir = config.tenants_dir.resolve()
    security_db_path = config.security_db_path.resolve()
    if security_db_path == tenants_dir or security_db_path.parent == tenants_dir:
        raise SecurityConfigError(
            "AISTAT_TENANTS_DIR must be a directory separate from security.db"
        )
    if config.session_hours < 1 or config.session_hours > 168:
        raise SecurityConfigError("AISTAT_SESSION_HOURS must be between 1 and 168")
    if config.ingest_max_age_seconds < 30 or config.ingest_max_age_seconds > 3600:
        raise SecurityConfigError(
            "AISTAT_INGEST_MAX_AGE_SECONDS must be between 30 and 3600"
        )


def csrf_token(session) -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def validate_csrf(session, candidate: Optional[str]) -> bool:
    expected = session.get("_csrf")
    return bool(
        expected
        and candidate
        and hmac.compare_digest(str(expected), str(candidate))
    )


def safe_next_url(candidate: Optional[str], default: str = "/") -> str:
    if not candidate:
        return default
    # Browsers normalize ``\\`` to ``/`` in special URLs.  Thus a value such
    # as ``/\\evil.example`` looks path-relative to ``urlsplit`` but is an
    # authority-relative redirect to a browser.  Control characters are also
    # unsafe in a response Location header, so reject both classes before URL
    # parsing rather than trying to repair them.
    if "\\" in candidate or any(
        ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F
        for char in candidate
    ):
        return default
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not candidate.startswith("/"):
        return default
    if candidate.startswith("//"):
        return default
    return candidate


def client_key(session_secret: str, remote_addr: Optional[str]) -> str:
    address = (remote_addr or "unknown").encode("utf-8")
    return hmac.new(
        session_secret.encode("utf-8"), address, hashlib.sha256
    ).hexdigest()


def snapshot_signature(
    secret: str, tenant_id: int, timestamp: int, body: bytes
) -> str:
    return tenant_snapshot_signature(secret, tenant_id, timestamp, body)


def verify_snapshot_signature(
    secret: str,
    tenant_id: int,
    timestamp_header: Optional[str],
    signature_header: Optional[str],
    body: bytes,
    max_age_seconds: int,
    now: Optional[int] = None,
) -> int:
    try:
        timestamp = int(timestamp_header or "")
    except ValueError as exc:
        raise ValueError("invalid ingest timestamp") from exc
    now = int(time.time()) if now is None else int(now)
    if abs(now - timestamp) > max_age_seconds:
        raise ValueError("stale ingest timestamp")
    expected = snapshot_signature(secret, tenant_id, timestamp, body)
    if not signature_header or not hmac.compare_digest(
        expected, signature_header
    ):
        raise ValueError("invalid ingest signature")
    return timestamp


class SecurityStore:
    """Persistent login throttle, ingest replay state and user accounts.

    Accounts live here rather than in the Multica data snapshot because that
    database is replaced wholesale on every ingest (``os.replace``), which
    would wipe any user rows stored alongside it.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS login_throttle (
                    client_key      TEXT PRIMARY KEY,
                    window_started  INTEGER NOT NULL,
                    failures        INTEGER NOT NULL,
                    locked_until    INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS ingest_state (
                    id              INTEGER PRIMARY KEY CHECK (id = 1),
                    last_timestamp  INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO ingest_state (id, last_timestamp)
                VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS users (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at      INTEGER NOT NULL,
                    display_name    TEXT,
                    email           TEXT,
                    is_admin        INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_identities (
                    provider        TEXT NOT NULL,
                    subject         TEXT NOT NULL,
                    user_id         INTEGER NOT NULL,
                    email           TEXT,
                    linked_at       INTEGER NOT NULL,
                    PRIMARY KEY (provider, subject)
                );
                CREATE TABLE IF NOT EXISTS oauth_state (
                    state           TEXT PRIMARY KEY,
                    provider        TEXT NOT NULL,
                    next_url        TEXT,
                    created_at      INTEGER NOT NULL,
                    client_hash     TEXT
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
            # Serialize the inspect-and-alter migration across WSGI workers.
            # Without the write lock, concurrent first starts can both observe
            # the old schema and one loses the ALTER race with a duplicate
            # column error.
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(oauth_state)")
            }
            if "client_hash" not in columns:
                conn.execute(
                    "ALTER TABLE oauth_state ADD COLUMN client_hash TEXT"
                )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def login_retry_after(self, key: str, now: Optional[int] = None) -> int:
        now = int(time.time()) if now is None else int(now)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT window_started, failures, locked_until "
                "FROM login_throttle WHERE client_key = ?",
                (key,),
            ).fetchone()
            if not row:
                return 0
            if row["locked_until"] > now:
                return row["locked_until"] - now
            if row["window_started"] + LOGIN_WINDOW_SECONDS <= now:
                conn.execute(
                    "DELETE FROM login_throttle WHERE client_key = ?", (key,)
                )
                conn.commit()
            return 0
        finally:
            conn.close()

    def record_login_failure(
        self, key: str, now: Optional[int] = None
    ) -> int:
        now = int(time.time()) if now is None else int(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT window_started, failures, locked_until "
                "FROM login_throttle WHERE client_key = ?",
                (key,),
            ).fetchone()
            if not row or row["window_started"] + LOGIN_WINDOW_SECONDS <= now:
                window_started = now
                failures = 1
                locked_until = 0
            else:
                window_started = row["window_started"]
                failures = row["failures"] + 1
                locked_until = row["locked_until"]
            if failures >= LOGIN_MAX_FAILURES:
                locked_until = max(locked_until, now + LOGIN_LOCK_SECONDS)
            conn.execute(
                """
                INSERT INTO login_throttle
                    (client_key, window_started, failures, locked_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_key) DO UPDATE SET
                    window_started = excluded.window_started,
                    failures = excluded.failures,
                    locked_until = excluded.locked_until
                """,
                (key, window_started, failures, locked_until),
            )
            conn.commit()
            return max(0, locked_until - now)
        finally:
            conn.close()

    def clear_login_failures(self, key: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM login_throttle WHERE client_key = ?", (key,)
            )
            conn.commit()
        finally:
            conn.close()

    def ensure_owner_user(
        self,
        username: str,
        email: Optional[str] = None,
        now: Optional[int] = None,
    ) -> int:
        """Return the one password-admin user, creating it when absent."""
        now = int(time.time()) if now is None else int(now)
        normalized_email = email.strip().lower() if email else None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            admins = conn.execute(
                "SELECT id FROM users WHERE is_admin = 1 ORDER BY id"
            ).fetchall()
            if len(admins) > 1:
                conn.rollback()
                raise SecurityConfigError(
                    "security.db contains multiple admin users"
                )
            if admins:
                conn.commit()
                return int(admins[0]["id"])

            if normalized_email:
                matches = conn.execute(
                    "SELECT id FROM users "
                    "WHERE email IS NOT NULL AND lower(email) = ? ORDER BY id",
                    (normalized_email,),
                ).fetchall()
                if len(matches) > 1:
                    conn.rollback()
                    raise SecurityConfigError(
                        "AISTAT_ADMIN_EMAIL matches multiple users"
                    )
                if matches:
                    user_id = int(matches[0]["id"])
                    conn.execute(
                        "UPDATE users SET is_admin = 1 WHERE id = ?",
                        (user_id,),
                    )
                    conn.commit()
                    return user_id

            cursor = conn.execute(
                "INSERT INTO users "
                "(created_at, display_name, email, is_admin) "
                "VALUES (?, ?, ?, 1)",
                (now, username, normalized_email),
            )
            user_id = int(cursor.lastrowid)
            conn.commit()
            return user_id
        finally:
            conn.close()

    def owner_user_id(self) -> Optional[int]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id FROM users WHERE is_admin = 1 ORDER BY id"
            ).fetchall()
            if len(rows) > 1:
                raise SecurityConfigError(
                    "security.db contains multiple admin users"
                )
            return int(rows[0]["id"]) if rows else None
        finally:
            conn.close()

    def ensure_tenant(
        self,
        user_id: int,
        now: Optional[int] = None,
        initial_ingest_timestamp: int = 0,
    ) -> dict:
        user_id = canonical_tenant_id(user_id)
        now = int(time.time()) if now is None else int(now)
        initial_ingest_timestamp = int(initial_ingest_timestamp)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone() is None:
                conn.rollback()
                raise ValueError("tenant user does not exist")
            conn.execute(
                "INSERT OR IGNORE INTO tenants "
                "(user_id, created_at, last_ingest_timestamp) "
                "VALUES (?, ?, ?)",
                (user_id, now, initial_ingest_timestamp),
            )
            if initial_ingest_timestamp:
                conn.execute(
                    "UPDATE tenants SET last_ingest_timestamp = "
                    "MAX(last_ingest_timestamp, ?) WHERE user_id = ?",
                    (initial_ingest_timestamp, user_id),
                )
            row = conn.execute(
                "SELECT user_id, created_at, last_ingest_timestamp, "
                "last_snapshot_at, last_snapshot_sha256 "
                "FROM tenants WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            conn.commit()
            return dict(row)
        finally:
            conn.close()

    def get_tenant(self, user_id: int) -> Optional[dict]:
        user_id = canonical_tenant_id(user_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT user_id, created_at, last_ingest_timestamp, "
                "last_snapshot_at, last_snapshot_sha256 "
                "FROM tenants WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def ingest_timestamp_is_fresh(
        self, user_id: int, timestamp: int
    ) -> bool:
        tenant = self.get_tenant(user_id)
        return bool(
            tenant and int(timestamp) > int(tenant["last_ingest_timestamp"])
        )

    def record_tenant_snapshot(
        self,
        user_id: int,
        timestamp: int,
        sha256: str,
        snapshot_at: Optional[int] = None,
    ) -> bool:
        user_id = canonical_tenant_id(user_id)
        timestamp = int(timestamp)
        snapshot_at = timestamp if snapshot_at is None else int(snapshot_at)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_ingest_timestamp FROM tenants WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            current = int(row["last_ingest_timestamp"])
            if timestamp <= current:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE tenants SET last_ingest_timestamp = ?, "
                "last_snapshot_at = ?, last_snapshot_sha256 = ? "
                "WHERE user_id = ?",
                (timestamp, snapshot_at, sha256, user_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def find_or_create_user_by_identity(
        self,
        provider: str,
        subject: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        now: Optional[int] = None,
    ) -> int:
        """Return the AIStat user id linked to ``(provider, subject)``.

        The same external identity always maps to the same user; a new user
        and identity are created atomically on first sight.
        """
        now = int(time.time()) if now is None else int(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT user_id FROM oauth_identities "
                "WHERE provider = ? AND subject = ?",
                (provider, subject),
            ).fetchone()
            if row:
                conn.commit()
                return int(row["user_id"])
            cursor = conn.execute(
                "INSERT INTO users (created_at, display_name, email, is_admin) "
                "VALUES (?, ?, ?, 0)",
                (now, display_name, email),
            )
            user_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO oauth_identities "
                "(provider, subject, user_id, email, linked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (provider, subject, user_id, email, now),
            )
            conn.commit()
            return user_id
        finally:
            conn.close()

    def put_oauth_state(
        self,
        state: str,
        provider: str,
        next_url: Optional[str] = None,
        client_hash: Optional[str] = None,
        now: Optional[int] = None,
    ) -> None:
        """Persist a one-time OAuth ``state`` value with a bounded lifetime.

        ``client_hash`` is the hash of the browser-binding token minted when
        the flow started; the callback must present the matching token.
        """
        now = int(time.time()) if now is None else int(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM oauth_state WHERE created_at + ? <= ?",
                (OAUTH_STATE_TTL_SECONDS, now),
            )
            conn.execute(
                "INSERT INTO oauth_state "
                "(state, provider, next_url, created_at, client_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (state, provider, next_url, now, client_hash),
            )
            conn.commit()
        finally:
            conn.close()

    def take_oauth_state(
        self, state: str, now: Optional[int] = None
    ) -> Optional[dict]:
        """Consume a stored OAuth ``state`` exactly once.

        Returns ``{"provider", "next_url", "client_hash"}`` for a fresh, known
        state and ``None`` for an unknown, already-consumed or expired one.
        The row is deleted on any match so a state can never be replayed.
        """
        now = int(time.time()) if now is None else int(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT provider, next_url, created_at, client_hash "
                "FROM oauth_state WHERE state = ?",
                (state,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute("DELETE FROM oauth_state WHERE state = ?", (state,))
            conn.commit()
            if row["created_at"] + OAUTH_STATE_TTL_SECONDS <= now:
                return None
            return {
                "provider": row["provider"],
                "next_url": row["next_url"],
                "client_hash": row["client_hash"],
            }
        finally:
            conn.close()


def _main(argv=None) -> int:
    from werkzeug.security import generate_password_hash

    parser = argparse.ArgumentParser(description="AIStat security helper")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-secret", help="print a new 384-bit URL-safe secret")
    sub.add_parser("hash-password", help="prompt for a password and print its hash")
    args = parser.parse_args(argv)

    if args.command == "generate-secret":
        print(secrets.token_urlsafe(48))
        return 0

    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Repeat password: ")
    if not password or password != confirmation:
        parser.error("passwords do not match or are empty")
    print(generate_password_hash(password, method="pbkdf2:sha256:600000"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
