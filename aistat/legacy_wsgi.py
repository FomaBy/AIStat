"""Dependency-free authenticated WSGI app for cPanel's system Python.

Namecheap's generic Passenger application currently runs ``/usr/bin/python3``
with a package index capped at Flask 2.0.3. This entry point deliberately uses
only the Python 3.6 standard library, while preserving the security contract of
the modern Flask WSGI app:

* signed HttpOnly/Secure/SameSite sessions;
* CSRF-protected login/logout and persistent login throttling;
* strict Host/HTTPS checks and browser security headers;
* HMAC-authenticated, replay-protected, atomic SQLite snapshot ingestion.
"""

import base64
import fcntl
import gzip
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import secrets
import shutil
import sqlite3
import tempfile
import time
import traceback
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__, aggregates, oauth
from .db import SCHEMA_VERSION, init_db
from .tenant import (
    canonical_tenant_id,
    snapshot_signature as tenant_snapshot_signature,
    tenant_db_path,
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
OAUTH_STATE_TTL_SECONDS = 10 * 60
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
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 15 * 60


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DB_PATH = os.environ.get(
    "AISTAT_DB_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "aistat.db")),
)
SECURITY_DB_PATH = os.environ.get(
    "AISTAT_SECURITY_DB_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "security.db")),
)
TENANTS_DIR = os.environ.get(
    "AISTAT_TENANTS_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "tenants")),
)
ALLOWED_HOSTS = {
    value.strip().lower()
    for value in os.environ.get(
        "AISTAT_ALLOWED_HOSTS", "localhost,127.0.0.1"
    ).split(",")
    if value.strip()
}
FORCE_HTTPS = _env_bool("AISTAT_FORCE_HTTPS", False)
COOKIE_SECURE = _env_bool("AISTAT_SESSION_COOKIE_SECURE", FORCE_HTTPS)
ADMIN_USERNAME = os.environ.get("AISTAT_ADMIN_USERNAME", "admin")
ADMIN_EMAIL = os.environ.get("AISTAT_ADMIN_EMAIL") or None
PASSWORD_HASH = os.environ.get("AISTAT_PASSWORD_HASH", "")
SESSION_SECRET = os.environ.get("AISTAT_SESSION_SECRET", "")
INGEST_SECRET = os.environ.get("AISTAT_INGEST_SECRET", "")
SESSION_SECONDS = int(os.environ.get("AISTAT_SESSION_HOURS", "12")) * 3600
INGEST_MAX_AGE = int(os.environ.get("AISTAT_INGEST_MAX_AGE_SECONDS", "300"))
MAX_SNAPSHOT_BYTES = int(
    os.environ.get("AISTAT_MAX_SNAPSHOT_BYTES", str(64 * 1024 * 1024))
)
CREDITS_PER_USD = float(os.environ.get("AISTAT_CREDITS_PER_USD", "1.0"))
OAUTH_PROVIDERS = oauth.providers_from_env(os.environ)
OAUTH_ALLOWED_EMAILS = oauth.allowed_emails_from_env(os.environ)


def _validate_config():
    for name, value in (
        ("AISTAT_SESSION_SECRET", SESSION_SECRET),
        ("AISTAT_INGEST_SECRET", INGEST_SECRET),
    ):
        if len(value.encode("utf-8")) < 32:
            raise RuntimeError(name + " must contain at least 32 bytes")
    if hmac.compare_digest(
        SESSION_SECRET.encode("utf-8"), INGEST_SECRET.encode("utf-8")
    ):
        raise RuntimeError("session and ingest secrets must be independent")
    if not ADMIN_USERNAME or not PASSWORD_HASH:
        raise RuntimeError("dashboard credentials are not configured")
    if os.path.abspath(DB_PATH) == os.path.abspath(SECURITY_DB_PATH):
        raise RuntimeError("data and security databases must be different")
    tenants_dir = os.path.realpath(TENANTS_DIR)
    security_db_path = os.path.realpath(SECURITY_DB_PATH)
    if (
        security_db_path == tenants_dir
        or os.path.dirname(security_db_path) == tenants_dir
    ):
        raise RuntimeError("tenant directory and security database must be different")
    if not ALLOWED_HOSTS:
        raise RuntimeError("AISTAT_ALLOWED_HOSTS must not be empty")


def _chmod_private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _bootstrap():
    for path in (SECURITY_DB_PATH, os.path.join(TENANTS_DIR, ".keep")):
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, 0o700)
    conn = sqlite3.connect(SECURITY_DB_PATH)
    conn.row_factory = sqlite3.Row
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
        conn.execute("BEGIN IMMEDIATE")
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(oauth_state)")
        }
        if "client_hash" not in columns:
            conn.execute("ALTER TABLE oauth_state ADD COLUMN client_hash TEXT")
        admins = conn.execute(
            "SELECT id FROM users WHERE is_admin = 1 ORDER BY id"
        ).fetchall()
        if len(admins) > 1:
            raise RuntimeError("security.db contains multiple admin users")
        if admins:
            owner_user_id = int(admins[0]["id"])
        else:
            matches = []
            if ADMIN_EMAIL:
                matches = conn.execute(
                    "SELECT id FROM users "
                    "WHERE email IS NOT NULL AND lower(email) = ? ORDER BY id",
                    (ADMIN_EMAIL.strip().lower(),),
                ).fetchall()
                if len(matches) > 1:
                    raise RuntimeError(
                        "AISTAT_ADMIN_EMAIL matches multiple users"
                    )
            if matches:
                owner_user_id = int(matches[0]["id"])
                conn.execute(
                    "UPDATE users SET is_admin = 1 WHERE id = ?",
                    (owner_user_id,),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO users "
                    "(created_at, display_name, email, is_admin) "
                    "VALUES (?, ?, ?, 1)",
                    (int(time.time()), ADMIN_USERNAME, ADMIN_EMAIL),
                )
                owner_user_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()
    _chmod_private(SECURITY_DB_PATH)
    try:
        os.chmod(TENANTS_DIR, 0o700)
    except OSError:
        pass
    return owner_user_id


_validate_config()
OWNER_USER_ID = _bootstrap()


def _b64encode(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value):
    value += "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _sign(data, secret):
    return hmac.new(
        secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _cookie_header(name, value, max_age, path="/"):
    parts = [
        "{}={}".format(name, value),
        "Path={}".format(path),
        "Max-Age={}".format(max_age),
        "HttpOnly",
        "SameSite=Lax",
    ]
    if COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)


def _cookies(environ):
    cookie = SimpleCookie()
    try:
        cookie.load(environ.get("HTTP_COOKIE", ""))
    except Exception:
        return {}
    return {key: morsel.value for key, morsel in cookie.items()}


def _make_session():
    payload = {
        "u": ADMIN_USERNAME,
        "uid": OWNER_USER_ID,
        "exp": int(time.time()) + SESSION_SECONDS,
        "csrf": secrets.token_urlsafe(32),
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return encoded + "." + _sign(encoded, SESSION_SECRET), payload


def _make_oauth_session(user_id, email, provider):
    payload = {
        "uid": int(user_id),
        "email": email,
        "provider": provider,
        "exp": int(time.time()) + SESSION_SECONDS,
        "csrf": secrets.token_urlsafe(32),
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return encoded + "." + _sign(encoded, SESSION_SECRET), payload


def _read_session(environ):
    value = _cookies(environ).get("aistat_session")
    if not value or "." not in value:
        return None
    encoded, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(_sign(encoded, SESSION_SECRET), signature):
        return None
    try:
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


def _email_authorized(email):
    return oauth.is_email_authorized(OAUTH_ALLOWED_EMAILS, email)


def _session_grants_access(payload):
    """Whether a valid session may reach private data (fail-closed for OAuth).

    The admin password session is unchanged. An OAuth session is honoured only
    while its email stays on the allow-list, so de-listing revokes access on the
    next request.
    """
    if not payload:
        return False
    if payload.get("u") == ADMIN_USERNAME:
        return True
    if payload.get("uid") and _email_authorized(payload.get("email")):
        return True
    return False


def _make_login_csrf():
    timestamp = str(int(time.time()))
    token = secrets.token_urlsafe(24)
    value = timestamp + "." + token
    return value + "." + _sign("login:" + value, SESSION_SECRET)


def _valid_login_csrf(environ, candidate):
    cookie_value = _cookies(environ).get("aistat_login_csrf")
    if not candidate or not cookie_value or not hmac.compare_digest(
        candidate, cookie_value
    ):
        return False
    try:
        timestamp, token, signature = candidate.split(".", 2)
        timestamp_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - timestamp_int) > 600:
        return False
    expected = _sign("login:" + timestamp + "." + token, SESSION_SECRET)
    return hmac.compare_digest(expected, signature)


def _safe_next(value):
    if not value:
        return "/"
    # Special-scheme URL parsers treat a backslash as a path separator, so
    # ``/\\evil.example`` would leave this origin in a browser.  Never place
    # control characters in the raw Location header either.
    if "\\" in value or any(
        ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F for char in value
    ):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/"):
        return "/"
    if value.startswith("//"):
        return "/"
    return value


def _verify_password(password):
    try:
        method, salt, expected = PASSWORD_HASH.split("$", 2)
        scheme, algorithm, iterations = method.split(":", 2)
        if scheme != "pbkdf2":
            return False
        actual = hashlib.pbkdf2_hmac(
            algorithm,
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _client_key(environ):
    address = environ.get("REMOTE_ADDR", "unknown").encode("utf-8")
    return hmac.new(
        SESSION_SECRET.encode("utf-8"), address, hashlib.sha256
    ).hexdigest()


def _security_connection():
    conn = sqlite3.connect(SECURITY_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _login_retry_after(key):
    now = int(time.time())
    conn = _security_connection()
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


def _record_login_failure(key):
    now = int(time.time())
    conn = _security_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT window_started, failures, locked_until "
            "FROM login_throttle WHERE client_key = ?",
            (key,),
        ).fetchone()
        if not row or row["window_started"] + LOGIN_WINDOW_SECONDS <= now:
            window_started, failures, locked_until = now, 1, 0
        else:
            window_started = row["window_started"]
            failures = row["failures"] + 1
            locked_until = row["locked_until"]
        if failures >= LOGIN_MAX_FAILURES:
            locked_until = max(locked_until, now + LOGIN_LOCK_SECONDS)
        conn.execute(
            """
            INSERT OR REPLACE INTO login_throttle
                (client_key, window_started, failures, locked_until)
            VALUES (?, ?, ?, ?)
            """,
            (key, window_started, failures, locked_until),
        )
        conn.commit()
        return max(0, locked_until - now)
    finally:
        conn.close()


def _clear_login_failures(key):
    conn = _security_connection()
    try:
        conn.execute("DELETE FROM login_throttle WHERE client_key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def _tenant_record(user_id):
    user_id = canonical_tenant_id(user_id)
    conn = _security_connection()
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


def _ingest_timestamp_is_fresh(user_id, timestamp):
    tenant = _tenant_record(user_id)
    return bool(
        tenant and int(timestamp) > int(tenant["last_ingest_timestamp"])
    )


def _record_tenant_snapshot(user_id, timestamp, sha256):
    user_id = canonical_tenant_id(user_id)
    conn = _security_connection()
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
            (timestamp, timestamp, sha256, user_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


class _LegacyOAuthStore(object):
    """Account/state operations for the OAuth core, on the shared security db.

    Mirrors ``aistat.security.SecurityStore``'s account methods with the same
    SQL and one-time semantics, kept inline so ``legacy_wsgi`` stays free of the
    ``dataclasses``-based config import and remains Python 3.6-clean.
    """

    def put_oauth_state(
        self, state, provider, next_url=None, client_hash=None, now=None
    ):
        now = int(time.time()) if now is None else int(now)
        conn = _security_connection()
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

    def take_oauth_state(self, state, now=None):
        now = int(time.time()) if now is None else int(now)
        conn = _security_connection()
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

    def find_or_create_user_by_identity(
        self, provider, subject, email=None, display_name=None, now=None
    ):
        now = int(time.time()) if now is None else int(now)
        conn = _security_connection()
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


def _secure_headers(environ):
    headers = [
        ("Cache-Control", "no-store"),
        (
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        ),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        (
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        ),
        ("Referrer-Policy", "no-referrer"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
    ]
    if _is_secure(environ):
        headers.append(
            ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        )
    return headers


def _respond(environ, start_response, status, body=b"", content_type=None, headers=None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    response_headers = list(headers or [])
    if content_type:
        response_headers.append(("Content-Type", content_type))
    response_headers.append(("Content-Length", str(len(body))))
    response_headers.extend(_secure_headers(environ))
    start_response(status, response_headers)
    return [body]


def _json_response(environ, start_response, status, data, headers=None):
    return _respond(
        environ,
        start_response,
        status,
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        "application/json; charset=utf-8",
        headers,
    )


def _is_secure(environ):
    forwarded = environ.get("HTTP_X_FORWARDED_PROTO", "").split(",", 1)[0].strip()
    return (
        forwarded == "https"
        or environ.get("HTTPS", "").lower() in ("on", "1", "true")
        or environ.get("wsgi.url_scheme") == "https"
    )


def _host(environ):
    value = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "")
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")].lower()
    return value.split(":", 1)[0].rstrip(".").lower()


def _query(environ):
    return parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)


def _first(values, name, default=None):
    value = values.get(name)
    return value[0] if value else default


def _read_form(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        length = 0
    if length < 0 or length > 64 * 1024:
        raise ValueError("invalid form size")
    body = environ["wsgi.input"].read(length)
    return parse_qs(body.decode("utf-8"), keep_blank_values=True)


def _empty_data_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _data_connection(session):
    try:
        user_id = canonical_tenant_id(session.get("uid"))
    except (AttributeError, ValueError):
        return _empty_data_connection()
    if _tenant_record(user_id) is None:
        return _empty_data_connection()
    path = tenant_db_path(TENANTS_DIR, user_id)
    if not os.path.isfile(path):
        return _empty_data_connection()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sync_state(conn):
    beat = conn.execute(
        "SELECT seq, at, phase FROM sync_beats WHERE id = 1"
    ).fetchone()
    cycle = conn.execute(
        "SELECT id, started_at, finished_at, sources_ok, sources_failed "
        "FROM poll_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "beat": dict(beat) if beat else None,
        "cycle": dict(cycle) if cycle else None,
    }


def _health(conn):
    count_queries = {
        "runtimes": "SELECT COUNT(*) FROM runtimes",
        "agents": "SELECT COUNT(*) FROM agents",
        "projects": "SELECT COUNT(*) FROM projects",
        "issues": "SELECT COUNT(*) FROM issues",
        "daily_usage": "SELECT COUNT(*) FROM daily_usage",
        "issue_usage": "SELECT COUNT(*) FROM issue_usage",
        "runs": "SELECT COUNT(*) FROM runs",
        "runtime_activity": "SELECT COUNT(*) FROM runtime_activity",
        "model_pricing": "SELECT COUNT(*) FROM model_pricing",
        "poll_cycles": "SELECT COUNT(*) FROM poll_cycles",
    }
    row_counts = {
        table: conn.execute(query).fetchone()[0]
        for table, query in count_queries.items()
    }
    failing = [
        dict(row)
        for row in conn.execute(
            "SELECT source, last_error_at, last_error FROM sync_state "
            "WHERE ok = 0 ORDER BY source"
        )
    ]
    span = conn.execute(
        "SELECT MIN(date), MAX(date), COUNT(*) FROM daily_usage"
    ).fetchone()
    pricing = conn.execute(
        "SELECT model FROM model_pricing WHERE unpriced = 1 ORDER BY model"
    ).fetchall()
    return {
        "status": "degraded" if failing else "ok",
        "row_counts": row_counts,
        "failing_sources": failing,
        "daily_usage_span": {
            "first_date": span[0],
            "last_date": span[1],
            "rows": span[2],
        },
        "pricing": {
            "credits_per_usd": CREDITS_PER_USD,
            "unpriced_models_in_usage": [row[0] for row in pricing],
        },
        "sync": _sync_state(conn),
    }


def _render_login(csrf, next_url, error=None):
    error_html = ""
    if error:
        error_html = '<p class="error" role="alert">{}</p>'.format(
            html.escape(error)
        )
    return """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход — AIStat</title>
  <link rel="stylesheet" href="/login.css">
</head>
<body>
  <main class="login-shell">
    <section class="login-card" aria-labelledby="login-title">
      <div class="brand-mark" aria-hidden="true">AI</div>
      <h1 id="login-title">AIStat</h1>
      <p class="subtitle">Закрытая статистика использования Multica</p>
      {error}
      <form method="post" action="/login" autocomplete="on">
        <input type="hidden" name="csrf" value="{csrf}">
        <input type="hidden" name="next" value="{next_url}">
        <label>Имя пользователя
          <input type="text" name="username" maxlength="128"
                 autocomplete="username" required autofocus>
        </label>
        <label>Пароль
          <input type="password" name="password"
                 autocomplete="current-password" required>
        </label>
        <button type="submit">Войти</button>
      </form>
      <p class="security-note">
        Соединение защищено HTTPS. Пароль хранится только как стойкий хеш.
      </p>
    </section>
  </main>
</body>
</html>""".format(
        error=error_html,
        csrf=html.escape(csrf, quote=True),
        next_url=html.escape(next_url, quote=True),
    )


def _render_oauth_pending(email):
    return """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Нет доступа — AIStat</title>
  <link rel="stylesheet" href="/login.css">
</head>
<body>
  <main class="login-shell">
    <section class="login-card">
      <h1>AIStat</h1>
      <p class="subtitle">Вход выполнен как {email}, но у аккаунта пока нет
      доступа к статистике. Обратитесь к администратору.</p>
      <p><a href="/login">Войти как администратор</a></p>
    </section>
  </main>
</body>
</html>""".format(
        email=html.escape(email or "—")
    )


def _oauth(environ, start_response, path):
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "auth" or parts[2] not in ("start", "callback"):
        return _respond(environ, start_response, "404 Not Found", "Not found")
    provider = OAUTH_PROVIDERS.get(parts[1])
    if provider is None:
        return _respond(environ, start_response, "404 Not Found", "Not found")
    if environ.get("REQUEST_METHOD", "GET") != "GET":
        return _respond(environ, start_response, "405 Method Not Allowed")
    store = _LegacyOAuthStore()
    query = _query(environ)
    if parts[2] == "start":
        next_url = _safe_next(_first(query, "next", "/"))
        client_token = _cookies(environ).get("aistat_oauth_client")
        if not oauth.is_valid_client_token(client_token):
            client_token = oauth.generate_client_token()
        authorize_url = oauth.begin(store, provider, next_url, client_token)
        return _respond(
            environ,
            start_response,
            "303 See Other",
            headers=[
                ("Location", authorize_url),
                (
                    "Set-Cookie",
                    _cookie_header(
                        "aistat_oauth_client",
                        client_token,
                        OAUTH_STATE_TTL_SECONDS,
                        path="/auth",
                    ),
                ),
            ],
        )
    client_token = _cookies(environ).get("aistat_oauth_client")
    params = {
        "state": _first(query, "state"),
        "code": _first(query, "code"),
        "error": _first(query, "error"),
    }
    try:
        result = oauth.finish(store, provider, params, client_token)
    except oauth.OAuthError:
        csrf = _make_login_csrf()
        headers = [
            ("Set-Cookie", _cookie_header("aistat_login_csrf", csrf, 600))
        ]
        return _respond(
            environ,
            start_response,
            "400 Bad Request",
            _render_login(
                csrf,
                "/",
                "Не удалось выполнить вход через провайдера. Попробуйте снова.",
            ),
            "text/html; charset=utf-8",
            headers,
        )
    session_value, _payload = _make_oauth_session(
        result["user_id"], result["email"], parts[1]
    )
    set_cookie = (
        "Set-Cookie",
        _cookie_header("aistat_session", session_value, SESSION_SECONDS),
    )
    if not _email_authorized(result["email"]):
        return _respond(
            environ,
            start_response,
            "403 Forbidden",
            _render_oauth_pending(result["email"]),
            "text/html; charset=utf-8",
            [set_cookie],
        )
    next_url = _safe_next(result["next_url"])
    return _respond(
        environ,
        start_response,
        "303 See Other",
        headers=[("Location", next_url), set_cookie],
    )


def _serve_static(environ, start_response, name):
    allowed = {
        "index.html",
        "app.js",
        "style.css",
        "login.css",
        "vendor/chart.umd.min.js",
    }
    if name not in allowed:
        return _respond(environ, start_response, "404 Not Found", "Not found")
    path = os.path.join(STATIC_DIR, *name.split("/"))
    try:
        with open(path, "rb") as source:
            body = source.read()
    except OSError:
        return _respond(environ, start_response, "404 Not Found", "Not found")
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type == "application/javascript":
        content_type += "; charset=utf-8"
    return _respond(environ, start_response, "200 OK", body, content_type)


def _snapshot_signature(tenant_id, timestamp, body):
    return tenant_snapshot_signature(
        INGEST_SECRET, tenant_id, timestamp, body
    )


def _verify_snapshot_request(environ, body):
    try:
        tenant_id = canonical_tenant_id(
            environ.get("HTTP_X_AISTAT_TENANT", "")
        )
        timestamp = int(environ.get("HTTP_X_AISTAT_TIMESTAMP", ""))
    except ValueError:
        raise ValueError("invalid snapshot authentication")
    if abs(int(time.time()) - timestamp) > INGEST_MAX_AGE:
        raise ValueError("stale timestamp")
    supplied = environ.get("HTTP_X_AISTAT_SIGNATURE", "")
    expected = _snapshot_signature(tenant_id, timestamp, body)
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("invalid signature")
    return tenant_id, timestamp


def _unlink_if_exists(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _install_snapshot(payload, target_path):
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot too large")
    parent = os.path.dirname(target_path)
    if os.path.islink(target_path):
        raise ValueError("snapshot target is a symlink")
    previous = target_path + ".previous"
    if os.path.islink(previous):
        raise ValueError("snapshot backup is a symlink")
    handle, temp_path = tempfile.mkstemp(
        prefix=".aistat-snapshot-", suffix=".db", dir=parent
    )
    os.close(handle)
    _chmod_private(temp_path)
    total = 0
    try:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as source:
                with open(temp_path, "wb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_SNAPSHOT_BYTES:
                            raise ValueError("snapshot too large")
                        output.write(chunk)
        except (OSError, EOFError):
            raise ValueError("invalid gzip")

        with open(temp_path, "rb") as source:
            if source.read(16) != b"SQLite format 3\x00":
                raise ValueError("not SQLite")
        conn = sqlite3.connect(temp_path)
        try:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("integrity check failed")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < 1 or version > SCHEMA_VERSION:
                raise ValueError("unsupported schema")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if REQUIRED_TABLES - tables:
                raise ValueError("missing tables")
        finally:
            conn.close()

        if os.path.exists(target_path):
            shutil.copy2(target_path, previous)
            _chmod_private(previous)
        _unlink_if_exists(target_path + "-wal")
        _unlink_if_exists(target_path + "-shm")
        os.replace(temp_path, target_path)
        _chmod_private(target_path)
        with open(target_path, "rb") as source:
            data = source.read()
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "schema_version": version,
        }
    finally:
        _unlink_if_exists(temp_path)


def _authenticated(environ):
    return _read_session(environ)


def _login(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    query = _query(environ)
    if method == "GET":
        session = _read_session(environ)
        if session and _session_grants_access(session):
            return _respond(
                environ,
                start_response,
                "303 See Other",
                headers=[("Location", _safe_next(_first(query, "next", "/")))],
            )
        csrf = _make_login_csrf()
        headers = [("Set-Cookie", _cookie_header("aistat_login_csrf", csrf, 600))]
        return _respond(
            environ,
            start_response,
            "200 OK",
            _render_login(csrf, _safe_next(_first(query, "next", "/"))),
            "text/html; charset=utf-8",
            headers,
        )
    if method != "POST":
        return _respond(environ, start_response, "405 Method Not Allowed")
    try:
        form = _read_form(environ)
    except ValueError:
        return _respond(environ, start_response, "400 Bad Request")
    csrf = _first(form, "csrf", "")
    next_url = _safe_next(_first(form, "next", "/"))
    if not _valid_login_csrf(environ, csrf):
        return _respond(
            environ,
            start_response,
            "400 Bad Request",
            _render_login(
                _make_login_csrf(),
                next_url,
                "Не удалось проверить форму. Обновите страницу.",
            ),
            "text/html; charset=utf-8",
        )
    key = _client_key(environ)
    retry = _login_retry_after(key)
    if retry:
        return _respond(
            environ,
            start_response,
            "429 Too Many Requests",
            _render_login(
                csrf, next_url, "Слишком много попыток. Повторите вход позже."
            ),
            "text/html; charset=utf-8",
            [("Retry-After", str(retry))],
        )
    username_ok = hmac.compare_digest(
        _first(form, "username", ""), ADMIN_USERNAME
    )
    password_ok = _verify_password(_first(form, "password", ""))
    if not (username_ok and password_ok):
        retry = _record_login_failure(key)
        message = (
            "Слишком много попыток. Повторите вход позже."
            if retry
            else "Неверное имя пользователя или пароль."
        )
        headers = [("Retry-After", str(retry))] if retry else []
        return _respond(
            environ,
            start_response,
            "429 Too Many Requests" if retry else "401 Unauthorized",
            _render_login(csrf, next_url, message),
            "text/html; charset=utf-8",
            headers,
        )
    _clear_login_failures(key)
    session_value, _payload = _make_session()
    headers = [
        ("Location", next_url),
        (
            "Set-Cookie",
            _cookie_header("aistat_session", session_value, SESSION_SECONDS),
        ),
        ("Set-Cookie", _cookie_header("aistat_login_csrf", "", 0)),
    ]
    return _respond(environ, start_response, "303 See Other", headers=headers)


def _ingest(environ, start_response):
    if environ.get("REQUEST_METHOD") != "POST":
        return _respond(environ, start_response, "405 Method Not Allowed")
    if (
        environ.get("CONTENT_TYPE", "").split(";", 1)[0]
        != "application/vnd.aistat.snapshot+gzip"
    ):
        return _json_response(
            environ, start_response, "415 Unsupported Media Type", {"detail": "unsupported content type"}
        )
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        length = 0
    if length <= 0 or length > MAX_SNAPSHOT_BYTES:
        return _json_response(
            environ, start_response, "413 Payload Too Large", {"detail": "snapshot too large"}
        )
    body = environ["wsgi.input"].read(length)
    try:
        tenant_id, timestamp = _verify_snapshot_request(environ, body)
    except ValueError:
        return _json_response(
            environ, start_response, "401 Unauthorized", {"detail": "snapshot authentication failed"}
        )
    tenant = _tenant_record(tenant_id)
    if tenant is None:
        return _json_response(
            environ, start_response, "401 Unauthorized", {"detail": "snapshot authentication failed"}
        )
    # File paths use the canonical id read from security.db, not raw input.
    tenant_id = int(tenant["user_id"])
    lock_path = os.path.join(os.path.dirname(SECURITY_DB_PATH), "ingest.lock")
    with open(lock_path, "a+b") as lock:
        _chmod_private(lock_path)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if not _ingest_timestamp_is_fresh(tenant_id, timestamp):
                return _json_response(
                    environ, start_response, "409 Conflict", {"detail": "snapshot replay rejected"}
                )
            try:
                info = _install_snapshot(
                    body, tenant_db_path(TENANTS_DIR, tenant_id)
                )
            except ValueError:
                return _json_response(
                    environ, start_response, "422 Unprocessable Entity", {"detail": "invalid snapshot"}
                )
            if not _record_tenant_snapshot(
                tenant_id, timestamp, info["sha256"]
            ):
                return _json_response(
                    environ, start_response, "409 Conflict", {"detail": "snapshot replay rejected"}
                )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    info["status"] = "ok"
    info["tenant_id"] = tenant_id
    return _json_response(environ, start_response, "200 OK", info)


def _api(environ, start_response, path):
    query = _query(environ)
    session = _read_session(environ)
    if path == "/api/session":
        return _json_response(
            environ,
            start_response,
            "200 OK",
            {
                "username": ADMIN_USERNAME,
                "user_id": session.get("uid"),
                "csrf": session["csrf"],
            },
        )
    try:
        filters = aggregates.make_filters(
            _first(query, "from"), _first(query, "to"),
            query.get("project"), query.get("agent"), query.get("model"),
        )
    except ValueError as exc:
        return _json_response(
            environ, start_response, "422 Unprocessable Entity", {"detail": str(exc)}
        )
    conn = _data_connection(session)
    try:
        if path == "/api/meta":
            data = aggregates.meta(conn)
        elif path == "/api/summary":
            data = aggregates.summary(
                conn,
                credits_per_usd=CREDITS_PER_USD,
                filters=filters,
            )
        elif path == "/api/daily":
            try:
                data = aggregates.daily_series(
                    conn,
                    _first(query, "group", "model"),
                    filters=filters,
                )
            except ValueError as exc:
                return _json_response(
                    environ, start_response, "422 Unprocessable Entity", {"detail": str(exc)}
                )
        elif path == "/api/agents":
            data = {
                "agents": aggregates.agent_totals(
                    conn, filters=filters,
                )
            }
        elif path == "/api/projects":
            data = {
                "projects": aggregates.projects_overview(
                    conn, credits_per_usd=CREDITS_PER_USD, filters=filters,
                )
            }
        elif path == "/api/efficiency":
            raw_limit = _first(query, "limit")
            try:
                limit = int(raw_limit) if raw_limit else None
            except ValueError:
                limit = 0
            if limit is not None and not 1 <= limit <= 1000:
                return _json_response(
                    environ, start_response, "422 Unprocessable Entity", {"detail": "invalid limit"}
                )
            data = {
                "issues": aggregates.issue_efficiency(
                    conn, limit=limit, filters=filters,
                )
            }
        elif path == "/api/model-efficiency":
            data = aggregates.efficiency_breakdown(conn, filters=filters)
        elif path in ("/api/health", "/health"):
            data = _health(conn)
        elif path == "/api/sync":
            data = _sync_state(conn)
        elif path == "/api/events":
            return _respond(environ, start_response, "204 No Content")
        else:
            return _json_response(
                environ, start_response, "404 Not Found", {"detail": "not found"}
            )
        return _json_response(environ, start_response, "200 OK", data)
    finally:
        conn.close()


def application(environ, start_response):
    try:
        host = _host(environ)
        if host not in ALLOWED_HOSTS:
            return _respond(environ, start_response, "400 Bad Request", "Invalid host")
        if FORCE_HTTPS and not _is_secure(environ):
            if environ.get("REQUEST_METHOD", "GET") not in ("GET", "HEAD"):
                return _respond(environ, start_response, "400 Bad Request", "HTTPS required")
            location = "https://" + environ.get("HTTP_HOST", host) + environ.get(
                "PATH_INFO", "/"
            )
            if environ.get("QUERY_STRING"):
                location += "?" + environ["QUERY_STRING"]
            return _respond(
                environ,
                start_response,
                "308 Permanent Redirect",
                headers=[("Location", location)],
            )

        path = environ.get("PATH_INFO") or "/"
        if path == "/healthz":
            return _json_response(
                environ,
                start_response,
                "200 OK",
                {"status": "ok", "version": __version__},
            )
        if path == "/login":
            return _login(environ, start_response)
        if path == "/login.css":
            return _serve_static(environ, start_response, "login.css")
        if path == "/api/ingest/snapshot":
            return _ingest(environ, start_response)
        if path.startswith("/auth/"):
            return _oauth(environ, start_response, path)

        session = _authenticated(environ)
        if not session or not _session_grants_access(session):
            if path.startswith("/api/") or path == "/health":
                return _json_response(
                    environ,
                    start_response,
                    "401 Unauthorized",
                    {"detail": "authentication required"},
                )
            next_url = quote(
                _safe_next(
                    path
                    + (
                        "?" + environ["QUERY_STRING"]
                        if environ.get("QUERY_STRING")
                        else ""
                    )
                ),
                safe="",
            )
            return _respond(
                environ,
                start_response,
                "303 See Other",
                headers=[("Location", "/login?next=" + next_url)],
            )

        if path == "/logout":
            if environ.get("REQUEST_METHOD") != "POST":
                return _respond(environ, start_response, "405 Method Not Allowed")
            candidate = environ.get("HTTP_X_CSRF_TOKEN", "")
            if not candidate:
                candidate = _first(_read_form(environ), "csrf", "")
            if not hmac.compare_digest(candidate, session.get("csrf", "")):
                return _json_response(
                    environ, start_response, "400 Bad Request", {"detail": "invalid CSRF token"}
                )
            return _respond(
                environ,
                start_response,
                "303 See Other",
                headers=[
                    ("Location", "/login"),
                    ("Set-Cookie", _cookie_header("aistat_session", "", 0)),
                ],
            )
        if path.startswith("/api/") or path == "/health":
            return _api(environ, start_response, path)
        if path == "/":
            return _serve_static(environ, start_response, "index.html")
        return _serve_static(environ, start_response, path.lstrip("/"))
    except Exception:
        error_stream = environ.get("wsgi.errors")
        if error_stream is not None:
            traceback.print_exc(file=error_stream)
        return _respond(
            environ, start_response, "500 Internal Server Error", "Internal server error"
        )
