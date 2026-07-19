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

from . import handoff
from . import snapshot_recovery
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
    if config.worker_secret is not None:
        worker_secret = _secret_bytes(
            config.worker_secret, "AISTAT_WORKER_SECRET"
        )
        if hmac.compare_digest(worker_secret, session_secret) or (
            hmac.compare_digest(worker_secret, ingest_secret)
        ):
            raise SecurityConfigError(
                "the worker secret must be independent from other secrets"
            )
    try:
        handoff.canonical_official_server_url(config.multica_official_url)
    except ValueError as exc:
        raise SecurityConfigError(
            "AISTAT_MULTICA_OFFICIAL_URL must be exactly https://multica.ai: "
            "{}".format(exc)
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
    if candidate.startswith("//"):
        return default
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        # Malformed authorities (for example an unterminated IPv6 literal)
        # must fail closed instead of turning a public auth route into a 500.
        return default
    if parsed.scheme or parsed.netloc or not candidate.startswith("/"):
        return default
    return candidate


def session_id_hash(sid: str) -> str:
    """Digest stored instead of the raw session id.

    Reading security.db must never yield a value usable as a live cookie, so
    only this one-way hash of the high-entropy id is persisted.
    """
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


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
        # Deleted rows must not linger in free pages: this file temporarily
        # holds user Multica tokens that are erased after the worker handoff.
        conn.execute("PRAGMA secure_delete = ON")
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
                CREATE TABLE IF NOT EXISTS sessions (
                    sid_hash    TEXT PRIMARY KEY,
                    user_id     INTEGER NOT NULL,
                    created_at  INTEGER NOT NULL,
                    expires_at  INTEGER NOT NULL
                );
                """
            )
            conn.executescript(handoff.CONNECTIONS_SCHEMA)
            conn.executescript(snapshot_recovery.INSTALL_JOURNAL_SCHEMA)
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

    def create_session(
        self, user_id: int, ttl_seconds: int, now: Optional[int] = None
    ) -> str:
        """Register a new server-side session and return its secret id.

        Expired rows are purged in the same transaction so the table stays
        bounded by the number of live sessions.
        """
        now = int(time.time()) if now is None else int(now)
        sid = secrets.token_urlsafe(32)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (now,)
            )
            conn.execute(
                "INSERT INTO sessions "
                "(sid_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    session_id_hash(sid),
                    int(user_id),
                    now,
                    now + int(ttl_seconds),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return sid

    def session_is_active(
        self, sid: Optional[str], now: Optional[int] = None
    ) -> bool:
        if not sid or not isinstance(sid, str):
            return False
        now = int(time.time()) if now is None else int(now)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE sid_hash = ? AND expires_at > ?",
                (session_id_hash(sid), now),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def revoke_session(self, sid: Optional[str]) -> None:
        """Atomically revoke exactly the one session behind ``sid``."""
        if not sid or not isinstance(sid, str):
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM sessions WHERE sid_hash = ?",
                (session_id_hash(sid),),
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

    def begin_snapshot_install(
        self,
        user_id: int,
        timestamp: int,
        sha256: str,
        snapshot_at: int,
        staged_path: str,
    ) -> None:
        """Journal the intent to install one staged snapshot (see
        :mod:`aistat.snapshot_recovery`)."""
        conn = self._connect()
        try:
            snapshot_recovery.record_install_intent(
                conn, user_id, timestamp, sha256, snapshot_at, staged_path
            )
        finally:
            conn.close()

    def finish_snapshot_install(
        self,
        user_id: int,
        timestamp: int,
        sha256: str,
        snapshot_at: int,
    ) -> bool:
        """Advance the watermark and drop the journal row atomically."""
        conn = self._connect()
        try:
            return snapshot_recovery.commit_install(
                conn, user_id, timestamp, sha256, snapshot_at
            )
        finally:
            conn.close()

    def abort_snapshot_install(self, user_id: int) -> None:
        """Drop a tenant's journal row without advancing the watermark."""
        conn = self._connect()
        try:
            snapshot_recovery.clear_install_intent(conn, user_id)
        finally:
            conn.close()

    def recover_snapshot_installs(self, tenants_dir) -> dict:
        """Reconcile every journalled install after a restart.

        Callers must hold the ingest lock so recovery cannot race a live
        ingest or a second worker's recovery pass.
        """
        conn = self._connect()
        try:
            return snapshot_recovery.recover_pending_installs(
                conn, tenants_dir
            )
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

    def link_identity_to_owner(
        self,
        provider: str,
        subject: str,
        owner_user_id: int,
        email: Optional[str] = None,
        now: Optional[int] = None,
    ) -> int:
        """Idempotently link ``(provider, subject)`` to the existing owner user.

        Owner-only sign-in never creates a new account: the external identity is
        attached to the pre-existing admin user so a provider login lands on the
        owner tenant. A subject already linked to a *different* user, or an owner
        row that is missing or not an admin, is rejected fail-closed.
        """
        now = int(time.time()) if now is None else int(now)
        owner_user_id = int(owner_user_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT user_id FROM oauth_identities "
                "WHERE provider = ? AND subject = ?",
                (provider, subject),
            ).fetchone()
            if row is not None:
                if int(row["user_id"]) != owner_user_id:
                    conn.rollback()
                    raise SecurityConfigError(
                        "oauth identity is linked to a non-owner user"
                    )
                conn.commit()
                return owner_user_id
            owner = conn.execute(
                "SELECT is_admin FROM users WHERE id = ?", (owner_user_id,)
            ).fetchone()
            if owner is None or int(owner["is_admin"]) != 1:
                conn.rollback()
                raise SecurityConfigError(
                    "owner user is missing or not an admin"
                )
            conn.execute(
                "INSERT INTO oauth_identities "
                "(provider, subject, user_id, email, linked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (provider, subject, owner_user_id, email, now),
            )
            conn.commit()
            return owner_user_id
        finally:
            conn.close()

    def register_or_link_identity(
        self,
        provider: str,
        subject: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        admin_email: Optional[str] = None,
        allowed_emails=None,
        owner_user_id: Optional[int] = None,
        now: Optional[int] = None,
    ) -> dict:
        """Subject-first identity resolution with atomic first registration.

        Returns ``{"user_id", "outcome"}`` where ``outcome`` is one of
        ``"existing"`` (the subject was already registered), ``"linked_owner"``
        (a new subject whose email is the admin email, attached to the owner),
        ``"created"`` (a new ordinary user + identity + empty tenant), or
        ``"denied"`` (a new subject barred by a non-empty allow list — nothing
        is written and ``user_id`` is ``None``).

        Everything a single new registration touches — the ``users`` row, its
        ``oauth_identities`` row and its ``tenants`` registry row — is committed
        in one ``BEGIN IMMEDIATE`` transaction, so concurrent or replayed
        callbacks for one new subject converge on a single account/tenant and an
        injected failure rolls the whole registration back with no partial rows.
        The allow list gates only new subjects: an already-registered subject
        always resolves, and an ordinary user is never merged into or elevated
        to the owner even if its email later equals the admin email.
        """
        now = int(time.time()) if now is None else int(now)
        normalized = email.strip().lower() if email else None
        owner_email = (admin_email or "").strip().lower()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT user_id FROM oauth_identities "
                "WHERE provider = ? AND subject = ?",
                (provider, subject),
            ).fetchone()
            if row is not None:
                conn.commit()
                return {"user_id": int(row["user_id"]), "outcome": "existing"}
            # A brand-new subject. The admin email links to the pre-existing
            # owner; the owner stays the single admin and keeps its own tenant.
            if owner_email and owner_user_id and normalized == owner_email:
                owner = conn.execute(
                    "SELECT is_admin FROM users WHERE id = ?",
                    (int(owner_user_id),),
                ).fetchone()
                if owner is None or int(owner["is_admin"]) != 1:
                    conn.rollback()
                    raise SecurityConfigError(
                        "owner user is missing or not an admin"
                    )
                conn.execute(
                    "INSERT INTO oauth_identities "
                    "(provider, subject, user_id, email, linked_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (provider, subject, int(owner_user_id), email, now),
                )
                conn.commit()
                return {
                    "user_id": int(owner_user_id),
                    "outcome": "linked_owner",
                }
            # Ordinary registration. A non-empty allow list is the only gate,
            # and only for this first sight of the subject.
            if allowed_emails and (
                normalized is None or normalized not in allowed_emails
            ):
                conn.rollback()
                return {"user_id": None, "outcome": "denied"}
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
            conn.execute(
                "INSERT INTO tenants "
                "(user_id, created_at, last_ingest_timestamp) "
                "VALUES (?, ?, 0)",
                (user_id, now),
            )
            conn.commit()
            return {"user_id": user_id, "outcome": "created"}
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

    # --- "connect your Multica" token handoff (shared logic in handoff.py,
    # --- so the legacy contour applies byte-identical state transitions).

    def connection_status(
        self,
        user_id: int,
        pending_ttl_seconds: int = handoff.PENDING_TTL_MAX_SECONDS,
        now: Optional[int] = None,
    ) -> Optional[dict]:
        return handoff.connection_status(
            self._connect, user_id, pending_ttl_seconds, now
        )

    def worker_ready(
        self,
        readiness_ttl_seconds: int = handoff.WORKER_READINESS_TTL_DEFAULT_SECONDS,
        now: Optional[int] = None,
    ) -> bool:
        return handoff.worker_ready(
            self._connect, readiness_ttl_seconds, now
        )

    def submit_connection(
        self,
        user_id: int,
        server_url: str,
        workspace_label: Optional[str],
        token: str,
        now: Optional[int] = None,
    ) -> dict:
        return handoff.submit_connection(
            self._connect, user_id, server_url, workspace_label, token, now
        )

    def revoke_connection(
        self, user_id: int, now: Optional[int] = None
    ) -> bool:
        return handoff.revoke_connection(self._connect, user_id, now)

    def reserve_connection_submission(
        self, user_id: int, now: Optional[int] = None
    ) -> int:
        return handoff.reserve_connection_submission(self._connect, user_id, now)

    def consume_worker_nonce(
        self, nonce: str, max_age_seconds: int, now: Optional[int] = None
    ) -> bool:
        return handoff.consume_worker_nonce(
            self._connect, nonce, max_age_seconds, now
        )

    def lease_pending_connections(
        self,
        pending_ttl_seconds: int = handoff.PENDING_TTL_MAX_SECONDS,
        now: Optional[int] = None,
    ) -> dict:
        return handoff.lease_pending_connections(
            self._connect, pending_ttl_seconds, now
        )

    def apply_worker_acks(
        self, acks, now: Optional[int] = None
    ) -> list:
        return handoff.apply_worker_acks(self._connect, acks, now)


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
