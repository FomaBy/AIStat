"""Dependency-free secure handoff of user Multica tokens to the local worker.

A user submits their Multica API token in the dashboard. The public host keeps
it only until the trusted local worker collects it over an authenticated pull
channel; after the worker's acknowledgement the host copy is physically erased
and only an ``active`` marker remains.

This module is intentionally Python 3.6-compatible and imports only the
standard library, because the legacy cPanel WSGI entry point uses it. It holds
everything both public contours must agree on byte-for-byte:

* the worker pull-channel HMAC signature (path + timestamp + nonce + body);
* request validators for tokens, server URLs and workspace labels;
* the ``connections`` schema inside security.db;
* the atomic, tenant-scoped connection state machine
  (``pending -> active``, ``error``, ``revoked``) with lease/ack semantics.

State-machine functions take a ``connect`` callable returning a
``sqlite3.Connection`` with ``row_factory = sqlite3.Row``, so the Flask
``SecurityStore`` and the dependency-free legacy contour share one
implementation instead of two divergent copies.
"""

import hashlib
import hmac
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from .tenant import canonical_tenant_id

WORKER_SIGNATURE_PREFIX = "v1="
WORKER_PULL_PATH = "/api/worker/connection/pull"
WORKER_ACK_PATH = "/api/worker/connection/ack"
# A lease only guards ack ordering against replace/revoke races; the single
# trusted worker re-leases on every pull, so a short lifetime is enough.
WORKER_LEASE_SECONDS = 10 * 60
# Nonces must outlive the whole timestamp acceptance window (± max age) or a
# purged nonce could be replayed while its timestamp is still fresh.
WORKER_NONCE_TTL_FACTOR = 3

# Lifecycle. A submitted token waits in ``pending`` (first time) or
# ``replacement_pending`` (replacing an existing connection) until the worker
# stores it and acks, which flips it to ``active``. Disconnect / expiry /
# account cleanup erase the host token and enter ``revocation_pending``; only a
# worker delete-ack advances that to the terminal ``revoked`` — so ``revoked``
# always means "no worker copy remains", never merely "requested".
CONNECTION_STATUSES = (
    "pending",
    "replacement_pending",
    "active",
    "error",
    "revocation_pending",
    "revoked",
)
# Statuses the worker may lease and collect a plaintext token for.
LEASABLE_STATUSES = ("pending", "replacement_pending")
CONNECTION_WINDOW_SECONDS = 15 * 60
CONNECTION_MAX_SUBMISSIONS = 10

# Plaintext pending-token lifetime on the host. The worker must collect a token
# within this window; afterwards the host physically erases it and treats it as
# a revocation the worker must still confirm. Default 10 minutes, hard-capped
# at 15 so no configuration can leave plaintext sitting longer.
PENDING_TTL_DEFAULT_SECONDS = 10 * 60
PENDING_TTL_MAX_SECONDS = 15 * 60
# How fresh the worker's last authenticated pull must be for the host to accept
# a new token. Default 15 minutes (three default pull cycles).
WORKER_READINESS_TTL_DEFAULT_SECONDS = 15 * 60

# The single official Multica host. The user-supplied server URL is never
# published: every stored connection is pinned to this exact host so a poisoned
# ``server_url`` can never point the worker's use of the PAT at an attacker.
OFFICIAL_MULTICA_URL = "https://multica.ai"

# Reason surfaced in the cabinet when a pending token was never collected.
PENDING_EXPIRED_REASON = (
    "pending token expired before the worker collected it; reconnect to retry"
)

MIN_TOKEN_LENGTH = 8
MAX_TOKEN_LENGTH = 512
MAX_SERVER_URL_LENGTH = 512
MAX_LABEL_LENGTH = 128
MAX_SYNC_ERROR_LENGTH = 500
MAX_ACK_ITEMS = 100

CONNECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    user_id          INTEGER PRIMARY KEY,
    server_url       TEXT NOT NULL,
    workspace_label  TEXT,
    token            TEXT,
    token_epoch      INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    lease_id         TEXT,
    lease_expires_at INTEGER NOT NULL DEFAULT 0,
    revoke_acked_at  INTEGER,
    last_sync_error  TEXT,
    last_synced_at   INTEGER
);
CREATE TABLE IF NOT EXISTS connection_throttle (
    user_id         INTEGER PRIMARY KEY,
    window_started  INTEGER NOT NULL,
    submissions     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_nonces (
    nonce   TEXT PRIMARY KEY,
    seen_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen_at INTEGER NOT NULL
);
"""

_NONCE_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

# Public cabinet view: the raw token and lease internals must never reach an
# HTTP response, so status projections whitelist these columns only.
_STATUS_COLUMNS = (
    "user_id",
    "server_url",
    "workspace_label",
    "status",
    "token_epoch",
    "created_at",
    "updated_at",
    "last_sync_error",
    "last_synced_at",
)


def _now(value):
    return int(time.time()) if value is None else int(value)


def _erase_deleted_pages(conn):
    # security.db must not retain token bytes in freed pages after the
    # post-ack erase, so deleted content is zeroed regardless of how the
    # caller's connection factory is configured.
    conn.execute("PRAGMA secure_delete = ON")


def _record_worker_heartbeat_locked(conn, now):
    # A successful authenticated pull is the worker's liveness signal; the host
    # refuses to accept new tokens unless one landed recently (worker_ready).
    conn.execute(
        "INSERT OR REPLACE INTO worker_status (id, last_seen_at) VALUES (1, ?)",
        (now,),
    )


def _purge_expired_pending_locked(conn, pending_ttl_seconds, now):
    """Physically erase pending tokens the worker never collected in time.

    An expired token is treated as a revocation the worker must still confirm:
    the plaintext is erased now, the epoch bumped so any in-flight lease/ack is
    void, and the connection parked in ``revocation_pending`` so the worker
    deletes any copy it may already hold and acks before it reads ``revoked``.
    """
    cutoff = now - int(pending_ttl_seconds)
    expired = conn.execute(
        "SELECT user_id, token_epoch FROM connections "
        "WHERE status IN ('pending', 'replacement_pending') "
        "AND updated_at <= ?",
        (cutoff,),
    ).fetchall()
    for row in expired:
        conn.execute(
            "UPDATE connections SET token = NULL, "
            "status = 'revocation_pending', token_epoch = ?, updated_at = ?, "
            "lease_id = NULL, lease_expires_at = 0, revoke_acked_at = NULL, "
            "last_sync_error = ? WHERE user_id = ?",
            (
                int(row["token_epoch"]) + 1,
                now,
                PENDING_EXPIRED_REASON,
                int(row["user_id"]),
            ),
        )


def worker_ready(connect, readiness_ttl_seconds, now=None):
    """True while the worker's last authenticated pull is within the window."""
    now = _now(now)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT last_seen_at FROM worker_status WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    return (now - int(row["last_seen_at"])) <= int(readiness_ttl_seconds)


def worker_signature(secret, path, timestamp, nonce, body):
    """Sign one worker pull-channel request.

    The canonical form binds the exact endpoint path, a fresh timestamp, a
    single-use nonce and the SHA-256 of the raw body, so a captured request
    cannot be replayed later, against another endpoint, or with a new body.
    """
    body_digest = hashlib.sha256(body).hexdigest()
    canonical = "aistat-worker-v1\n{}\n{}\n{}\n{}".format(
        path, int(timestamp), nonce, body_digest
    ).encode("ascii")
    digest = hmac.new(
        secret.encode("utf-8"), canonical, hashlib.sha256
    ).hexdigest()
    return WORKER_SIGNATURE_PREFIX + digest


def validate_worker_nonce(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 128:
        raise ValueError("invalid worker nonce")
    if not all(char in _NONCE_ALPHABET for char in value):
        raise ValueError("invalid worker nonce")
    return value


def verify_worker_request(
    secret,
    path,
    timestamp_header,
    nonce_header,
    signature_header,
    body,
    max_age_seconds,
    now=None,
):
    """Validate one signed worker request; return ``(timestamp, nonce)``.

    Raises ``ValueError`` on any failure. Nonce single-use enforcement is a
    separate storage concern (``consume_worker_nonce``) so the pure check
    stays side-effect free.
    """
    try:
        timestamp = int(timestamp_header or "")
    except ValueError:
        raise ValueError("invalid worker timestamp")
    if abs(_now(now) - timestamp) > int(max_age_seconds):
        raise ValueError("stale worker timestamp")
    nonce = validate_worker_nonce(nonce_header)
    expected = worker_signature(secret, path, timestamp, nonce, body)
    if not signature_header or not hmac.compare_digest(
        expected, signature_header
    ):
        raise ValueError("invalid worker signature")
    return timestamp, nonce


def validate_connection_token(value):
    """Return the trimmed token or reject it without echoing its contents."""
    if not isinstance(value, str):
        raise ValueError("invalid token")
    token = value.strip()
    if not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH:
        raise ValueError("invalid token length")
    if not all(0x21 <= ord(char) <= 0x7E for char in token):
        raise ValueError("token contains unsupported characters")
    return token


def validate_server_url(value, default=None):
    """Return a safe server URL, falling back to the owner's default."""
    candidate = (value or "").strip() if isinstance(value, (str, type(None))) else None
    if candidate is None:
        raise ValueError("invalid server URL")
    if not candidate:
        candidate = (default or "").strip()
    if not candidate:
        raise ValueError("server URL is required")
    if len(candidate) > MAX_SERVER_URL_LENGTH:
        raise ValueError("server URL is too long")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in candidate):
        raise ValueError("invalid server URL")
    lowered = candidate.lower()
    if lowered.startswith("https://"):
        host_part = candidate[len("https://"):]
    elif lowered.startswith("http://"):
        host_part = candidate[len("http://"):]
        host = host_part.split("/", 1)[0].split(":", 1)[0].lower()
        if host not in ("localhost", "127.0.0.1"):
            raise ValueError("server URL must use HTTPS")
    else:
        raise ValueError("server URL must use HTTPS")
    if not host_part or host_part.startswith("/"):
        raise ValueError("invalid server URL")
    if "@" in host_part.split("/", 1)[0]:
        raise ValueError("invalid server URL")
    return candidate.rstrip("/")


def canonical_official_server_url(official_url):
    """Validate the operator's official Multica URL and reduce it to scheme+host.

    The result is the only server URL the host will ever store. It must be a
    bare ``https://<host>`` with no credentials, port or path, so there is a
    single, unambiguous canonical form to pin every connection to.
    """
    if not isinstance(official_url, str) or not official_url.strip():
        raise ValueError("official Multica server URL is required")
    structural = validate_server_url(official_url.strip())
    parts = urlsplit(structural)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not host
        or parts.username
        or parts.password
        or "@" in parts.netloc
        or parts.port is not None
        or parts.path not in ("", "/")
    ):
        raise ValueError(
            "official Multica server URL must be a bare https host"
        )
    return "https://" + host


def normalize_official_server_url(value, official_url):
    """Pin a submitted server URL to the single official Multica host.

    The user value is never published: an empty value adopts the official host,
    and any non-empty value must resolve to exactly that host over HTTPS — no
    alternate host, IP literal, port, credentials or path. This is what keeps a
    poisoned ``server_url`` (or a redirect/DNS trick riding on one) from ever
    redirecting the worker's later use of the PAT away from Multica.
    """
    official = canonical_official_server_url(official_url)
    if value is not None and not isinstance(value, str):
        raise ValueError("unsupported Multica server")
    candidate = (value or "").strip()
    if not candidate:
        return official
    parts = urlsplit(validate_server_url(candidate))
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not host
        or parts.username
        or parts.password
        or "@" in parts.netloc
        or parts.port is not None
        or parts.path not in ("", "/")
        or host != urlsplit(official).hostname
    ):
        raise ValueError("unsupported Multica server")
    return official


def validate_workspace_label(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid workspace label")
    label = value.strip()
    if not label:
        return None
    if len(label) > MAX_LABEL_LENGTH:
        raise ValueError("workspace label is too long")
    if any(ord(char) <= 0x1F or ord(char) == 0x7F for char in label):
        raise ValueError("invalid workspace label")
    return label


def _status_view(row):
    view = {}
    for column in _STATUS_COLUMNS:
        view[column] = row[column]
    return view


def connection_status(
    connect, user_id, pending_ttl_seconds=PENDING_TTL_MAX_SECONDS, now=None
):
    """Cabinet projection of one connection; never includes the token.

    Reading status also purges any pending token that outlived its TTL, so an
    expired plaintext can never survive merely because nobody polled the
    worker channel.
    """
    user_id = canonical_tenant_id(user_id)
    now = _now(now)
    conn = connect()
    try:
        _erase_deleted_pages(conn)
        conn.execute("BEGIN IMMEDIATE")
        _purge_expired_pending_locked(conn, pending_ttl_seconds, now)
        row = conn.execute(
            "SELECT {} FROM connections WHERE user_id = ?".format(
                ", ".join(_STATUS_COLUMNS)
            ),
            (user_id,),
        ).fetchone()
        result = _status_view(row) if row else None
        conn.commit()
        return result
    finally:
        conn.close()


def submit_connection(
    connect, user_id, server_url, workspace_label, token, now=None
):
    """Create or replace the user's connection; new epoch, pending state.

    A brand-new connection starts ``pending``; replacing an existing one starts
    ``replacement_pending`` so the cabinet can tell the two apart. Either way a
    fresh ``token_epoch`` voids any in-flight lease/ack for the previous token,
    and the worker's ``INSERT OR REPLACE`` overwrites the old ciphertext when it
    stores the new token.
    """
    user_id = canonical_tenant_id(user_id)
    now = _now(now)
    conn = connect()
    try:
        _erase_deleted_pages(conn)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM users WHERE id = ?", (user_id,)
        ).fetchone() is None:
            conn.rollback()
            raise ValueError("unknown user")
        row = conn.execute(
            "SELECT token_epoch, created_at FROM connections "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            epoch = int(row["token_epoch"]) + 1
            conn.execute(
                "UPDATE connections SET server_url = ?, workspace_label = ?, "
                "token = ?, token_epoch = ?, status = 'replacement_pending', "
                "updated_at = ?, lease_id = NULL, lease_expires_at = 0, "
                "revoke_acked_at = NULL, last_sync_error = NULL "
                "WHERE user_id = ?",
                (server_url, workspace_label, token, epoch, now, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO connections "
                "(user_id, server_url, workspace_label, token, token_epoch, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, 'pending', ?, ?)",
                (user_id, server_url, workspace_label, token, now, now),
            )
        result = _status_view(
            conn.execute(
                "SELECT {} FROM connections WHERE user_id = ?".format(
                    ", ".join(_STATUS_COLUMNS)
                ),
                (user_id,),
            ).fetchone()
        )
        conn.commit()
        return result
    finally:
        conn.close()


def revoke_connection(connect, user_id, now=None):
    """Disconnect / account cleanup: erase the host token, await worker purge.

    The plaintext (if any still sits on the host) is erased at once and the
    connection enters ``revocation_pending``. It only reaches the terminal
    ``revoked`` after the worker acknowledges deleting its own copy, so a
    ``revoked`` connection provably leaves no active worker credential behind.
    Returns ``False`` when the user has no connection; idempotent once the
    revocation is already pending or acknowledged.
    """
    user_id = canonical_tenant_id(user_id)
    now = _now(now)
    conn = connect()
    try:
        _erase_deleted_pages(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, token_epoch FROM connections WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        if row["status"] not in ("revocation_pending", "revoked"):
            conn.execute(
                "UPDATE connections SET token = NULL, "
                "status = 'revocation_pending', token_epoch = ?, "
                "updated_at = ?, lease_id = NULL, lease_expires_at = 0, "
                "revoke_acked_at = NULL WHERE user_id = ?",
                (int(row["token_epoch"]) + 1, now, user_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def connection_retry_after(connect, user_id, now=None):
    """Seconds until the next submission is allowed (0 = allowed now)."""
    user_id = canonical_tenant_id(user_id)
    now = _now(now)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT window_started, submissions FROM connection_throttle "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return 0
        window_end = int(row["window_started"]) + CONNECTION_WINDOW_SECONDS
        if window_end <= now:
            conn.execute(
                "DELETE FROM connection_throttle WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            return 0
        if int(row["submissions"]) >= CONNECTION_MAX_SUBMISSIONS:
            return window_end - now
        return 0
    finally:
        conn.close()


def record_connection_submission(connect, user_id, now=None):
    """Count one intake attempt; mirror of the login throttle shape."""
    user_id = canonical_tenant_id(user_id)
    now = _now(now)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT window_started, submissions FROM connection_throttle "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row or int(row["window_started"]) + CONNECTION_WINDOW_SECONDS <= now:
            window_started, submissions = now, 1
        else:
            window_started = int(row["window_started"])
            submissions = int(row["submissions"]) + 1
        conn.execute(
            "INSERT OR REPLACE INTO connection_throttle "
            "(user_id, window_started, submissions) VALUES (?, ?, ?)",
            (user_id, window_started, submissions),
        )
        conn.commit()
    finally:
        conn.close()


def consume_worker_nonce(connect, nonce, max_age_seconds, now=None):
    """Accept each nonce exactly once inside the timestamp window."""
    now = _now(now)
    ttl = int(max_age_seconds) * WORKER_NONCE_TTL_FACTOR
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM worker_nonces WHERE seen_at <= ?", (now - ttl,)
        )
        try:
            conn.execute(
                "INSERT INTO worker_nonces (nonce, seen_at) VALUES (?, ?)",
                (nonce, now),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        conn.commit()
        return True
    finally:
        conn.close()


def lease_pending_connections(
    connect, pending_ttl_seconds=PENDING_TTL_MAX_SECONDS, now=None
):
    """Record the worker heartbeat, then lease pending tokens + list purges.

    Every authenticated pull is the worker's liveness signal (worker_ready) and
    the periodic TTL sweep: expired plaintext is erased before anything is
    leased. Each pull invalidates previous leases for the returned rows, so
    exactly one lease can ever acknowledge a given epoch. The ``pending`` list
    is the only place a stored token legitimately leaves security.db; the
    ``revoked`` list names connections whose worker copy must be deleted
    (disconnect, expiry or account cleanup) and confirmed before they read
    ``revoked``.
    """
    now = _now(now)
    conn = connect()
    try:
        _erase_deleted_pages(conn)
        conn.execute("BEGIN IMMEDIATE")
        _record_worker_heartbeat_locked(conn, now)
        _purge_expired_pending_locked(conn, pending_ttl_seconds, now)
        pending = []
        for row in conn.execute(
            "SELECT user_id, server_url, workspace_label, token, token_epoch "
            "FROM connections "
            "WHERE status IN ('pending', 'replacement_pending') "
            "AND token IS NOT NULL ORDER BY user_id"
        ).fetchall():
            lease_id = secrets.token_urlsafe(24)
            lease_expires_at = now + WORKER_LEASE_SECONDS
            conn.execute(
                "UPDATE connections SET lease_id = ?, lease_expires_at = ? "
                "WHERE user_id = ?",
                (lease_id, lease_expires_at, int(row["user_id"])),
            )
            pending.append(
                {
                    "user_id": int(row["user_id"]),
                    "server_url": row["server_url"],
                    "workspace_label": row["workspace_label"],
                    "token": row["token"],
                    "token_epoch": int(row["token_epoch"]),
                    "lease_id": lease_id,
                    "lease_expires_at": lease_expires_at,
                }
            )
        revoked = [
            {
                "user_id": int(row["user_id"]),
                "token_epoch": int(row["token_epoch"]),
            }
            for row in conn.execute(
                "SELECT user_id, token_epoch FROM connections "
                "WHERE status = 'revocation_pending' "
                "AND revoke_acked_at IS NULL ORDER BY user_id"
            ).fetchall()
        ]
        conn.commit()
        return {"pending": pending, "revoked": revoked}
    finally:
        conn.close()


def ack_connection_stored(connect, user_id, lease_id, token_epoch, now=None):
    """Worker confirmed encrypted storage: erase the host token, go active.

    The transition happens only for the exact (lease, epoch) pair issued by
    the latest pull, so acks delayed past a replace or revoke change nothing.
    Returns ``(ok, status_or_reason)``.
    """
    try:
        user_id = canonical_tenant_id(user_id)
        token_epoch = int(token_epoch)
    except (TypeError, ValueError):
        return False, "invalid-request"
    if not isinstance(lease_id, str) or not lease_id:
        return False, "invalid-request"
    now = _now(now)
    conn = connect()
    try:
        _erase_deleted_pages(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, token_epoch, lease_id, lease_expires_at "
            "FROM connections WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False, "unknown-connection"
        if (
            row["status"] not in LEASABLE_STATUSES
            or int(row["token_epoch"]) != token_epoch
        ):
            conn.rollback()
            return False, "stale-epoch"
        if not row["lease_id"] or not hmac.compare_digest(
            row["lease_id"], lease_id
        ):
            conn.rollback()
            return False, "stale-lease"
        if int(row["lease_expires_at"]) <= now:
            conn.rollback()
            return False, "stale-lease"
        conn.execute(
            "UPDATE connections SET token = NULL, status = 'active', "
            "updated_at = ?, lease_id = NULL, lease_expires_at = 0, "
            "last_sync_error = NULL WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
        return True, "active"
    finally:
        conn.close()


def ack_connection_revoked(connect, user_id, token_epoch, now=None):
    """Worker confirmed it deleted its copy: advance to the terminal ``revoked``.

    Only a matching ``revocation_pending`` epoch advances, and it does so
    exactly once; a later ack for the same acknowledged epoch is idempotent.
    A stale epoch cannot complete a revocation started for a newer lifecycle.
    """
    try:
        user_id = canonical_tenant_id(user_id)
        token_epoch = int(token_epoch)
    except (TypeError, ValueError):
        return False, "invalid-request"
    now = _now(now)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, token_epoch, revoke_acked_at FROM connections "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False, "unknown-connection"
        if int(row["token_epoch"]) != token_epoch:
            conn.rollback()
            return False, "stale-epoch"
        if row["status"] == "revoked":
            conn.rollback()
            return True, "revoked"
        if row["status"] != "revocation_pending":
            conn.rollback()
            return False, "stale-epoch"
        conn.execute(
            "UPDATE connections SET status = 'revoked', revoke_acked_at = ?, "
            "updated_at = ? WHERE user_id = ?",
            (now, now, user_id),
        )
        conn.commit()
        return True, "revoked"
    finally:
        conn.close()


def record_connection_sync(
    connect, user_id, token_epoch, ok, error=None, now=None
):
    """Worker sync report for the cabinet: ``active`` or ``error`` + message."""
    try:
        user_id = canonical_tenant_id(user_id)
        token_epoch = int(token_epoch)
    except (TypeError, ValueError):
        return False, "invalid-request"
    if error is not None and not isinstance(error, str):
        return False, "invalid-request"
    now = _now(now)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, token_epoch FROM connections WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False, "unknown-connection"
        if int(row["token_epoch"]) != token_epoch:
            conn.rollback()
            return False, "stale-epoch"
        if row["status"] not in ("active", "error"):
            conn.rollback()
            return False, "invalid-state"
        if ok:
            conn.execute(
                "UPDATE connections SET status = 'active', "
                "last_synced_at = ?, last_sync_error = NULL, updated_at = ? "
                "WHERE user_id = ?",
                (now, now, user_id),
            )
            status = "active"
        else:
            message = (error or "sync failed").strip()[:MAX_SYNC_ERROR_LENGTH]
            conn.execute(
                "UPDATE connections SET status = 'error', "
                "last_sync_error = ?, updated_at = ? WHERE user_id = ?",
                (message, now, user_id),
            )
            status = "error"
        conn.commit()
        return True, status
    finally:
        conn.close()


def apply_worker_acks(connect, acks, now=None):
    """Apply a worker ack batch; every item is its own atomic transition."""
    if not isinstance(acks, list) or len(acks) > MAX_ACK_ITEMS:
        raise ValueError("invalid ack batch")
    results = []
    for item in acks:
        if not isinstance(item, dict):
            results.append({"ok": False, "reason": "invalid-request"})
            continue
        user_id = item.get("user_id")
        epoch = item.get("token_epoch")
        result = item.get("result")
        if result == "stored":
            ok, status = ack_connection_stored(
                connect, user_id, item.get("lease_id"), epoch, now=now
            )
        elif result == "revoked":
            ok, status = ack_connection_revoked(
                connect, user_id, epoch, now=now
            )
        elif result in ("sync_ok", "sync_error"):
            ok, status = record_connection_sync(
                connect,
                user_id,
                epoch,
                result == "sync_ok",
                error=item.get("error"),
                now=now,
            )
        else:
            ok, status = False, "invalid-request"
        entry = {"ok": ok}
        try:
            entry["user_id"] = canonical_tenant_id(user_id)
        except (TypeError, ValueError):
            entry["user_id"] = None
        if ok:
            entry["status"] = status
        else:
            entry["reason"] = status
        results.append(entry)
    return results
