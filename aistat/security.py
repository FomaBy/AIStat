"""Security primitives for the public AIStat deployment.

The public host never receives a Multica API token. It has only:

* a password hash for the dashboard login;
* an independent Flask session-signing secret;
* an independent HMAC key for signed SQLite snapshot uploads.

Failed-login counters and the last accepted ingest timestamp are stored in a
small database separate from the replaceable Multica data snapshot.
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

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
INGEST_SIGNATURE_PREFIX = "v1="


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


def snapshot_signature(secret: str, timestamp: int, body: bytes) -> str:
    body_digest = hashlib.sha256(body).hexdigest()
    canonical = f"{timestamp}\n{body_digest}".encode("ascii")
    digest = hmac.new(
        secret.encode("utf-8"), canonical, hashlib.sha256
    ).hexdigest()
    return INGEST_SIGNATURE_PREFIX + digest


def verify_snapshot_signature(
    secret: str,
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
    expected = snapshot_signature(secret, timestamp, body)
    if not signature_header or not hmac.compare_digest(
        expected, signature_header
    ):
        raise ValueError("invalid ingest signature")
    return timestamp


class SecurityStore:
    """Persistent login throttle and ingest replay state."""

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
                """
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

    def last_ingest_timestamp(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_timestamp FROM ingest_state WHERE id = 1"
            ).fetchone()
            return int(row["last_timestamp"])
        finally:
            conn.close()

    def record_ingest_timestamp(self, timestamp: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = int(
                conn.execute(
                    "SELECT last_timestamp FROM ingest_state WHERE id = 1"
                ).fetchone()["last_timestamp"]
            )
            if timestamp <= current:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE ingest_state SET last_timestamp = ? WHERE id = 1",
                (timestamp,),
            )
            conn.commit()
            return True
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
