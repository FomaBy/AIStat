"""Authenticated WSGI application for Namecheap Shared Hosting.

Namecheap Shared Hosting supports WSGI but not ASGI. This module exposes the
same aggregate API and static dashboard as the local FastAPI app, while adding:

* mandatory password authentication;
* signed, HttpOnly, SameSite session cookies;
* CSRF protection and failed-login throttling;
* strict host/HTTPS checks and browser security headers;
* HMAC-authenticated atomic SQLite snapshot ingestion.
"""

import hmac
import fcntl
import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from . import __version__, aggregates
from .config import Config
from .db import connect, connect_readonly, init_db
from .health import snapshot
from .security import (
    SecurityStore,
    client_key,
    csrf_token,
    safe_next_url,
    validate_csrf,
    validate_public_config,
    verify_snapshot_signature,
)
from .snapshot import SnapshotError, install_compressed_snapshot

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def create_app(config: Optional[Config] = None) -> Flask:
    config = config or Config()
    validate_public_config(config)
    config.ensure_db_dir()
    config.ensure_security_db_dir()

    # Let the app boot before its first signed snapshot arrives.
    if not config.db_path.exists():
        bootstrap = connect(config.db_path)
        try:
            init_db(bootstrap)
        finally:
            bootstrap.close()

    app = Flask(
        __name__,
        static_folder=None,
        template_folder=str(TEMPLATE_DIR),
    )
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1
    )
    app.secret_key = config.session_secret
    app.permanent_session_lifetime = timedelta(hours=config.session_hours)
    app.config.update(
        MAX_CONTENT_LENGTH=config.max_snapshot_bytes,
        SESSION_COOKIE_NAME="aistat_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=config.session_cookie_secure,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    security_store = SecurityStore(config.security_db_path)
    ingest_lock_path = config.security_db_path.with_name("ingest.lock")

    public_endpoints = {
        "login",
        "login_css",
        "healthz",
        "ingest_snapshot",
    }

    def normalized_host() -> str:
        host = request.host.rsplit("@", 1)[-1]
        if host.startswith("[") and "]" in host:
            return host[1 : host.index("]")].lower()
        return host.split(":", 1)[0].rstrip(".").lower()

    def wants_json() -> bool:
        return request.path.startswith("/api/") or (
            request.accept_mimetypes.best == "application/json"
        )

    @app.before_request
    def enforce_request_boundary():
        host = normalized_host()
        if host not in config.allowed_hosts:
            abort(400, description="invalid host")

        if config.force_https and not request.is_secure:
            if request.method in {"GET", "HEAD"}:
                target = "https://" + request.host + request.full_path
                if target.endswith("?"):
                    target = target[:-1]
                return redirect(target, code=308)
            abort(400, description="HTTPS is required")

        if request.endpoint in public_endpoints:
            return None
        if session.get("user") == config.auth_username:
            return None
        if wants_json():
            return jsonify({"detail": "authentication required"}), 401
        return redirect(
            "/login?next=" + quote(safe_next_url(request.full_path), safe=""),
            code=303,
        )

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    def data_connection() -> sqlite3.Connection:
        return connect_readonly(config.db_path)

    @contextmanager
    def ingest_lock():
        with ingest_lock_path.open("a+b") as handle:
            try:
                os.chmod(ingest_lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def last_sync_state() -> dict:
        conn = data_connection()
        try:
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
        finally:
            conn.close()

    def render_login(error=None, status=200):
        response = render_template(
            "login.html",
            csrf=csrf_token(session),
            error=error,
            next_url=safe_next_url(request.values.get("next")),
        )
        return response, status

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if session.get("user") == config.auth_username:
                return redirect(safe_next_url(request.args.get("next")), code=303)
            return render_login()

        if not validate_csrf(session, request.form.get("csrf")):
            return render_login("Не удалось проверить форму. Обновите страницу.", 400)

        key = client_key(config.session_secret, request.remote_addr)
        retry_after = security_store.login_retry_after(key)
        if retry_after:
            response, status = render_login(
                "Слишком много попыток. Повторите вход позже.", 429
            )
            response = app.make_response((response, status))
            response.headers["Retry-After"] = str(retry_after)
            return response

        username_ok = hmac.compare_digest(
            request.form.get("username", ""), config.auth_username
        )
        password_ok = check_password_hash(
            config.auth_password_hash, request.form.get("password", "")
        )
        if not (username_ok and password_ok):
            retry_after = security_store.record_login_failure(key)
            message = (
                "Слишком много попыток. Повторите вход позже."
                if retry_after
                else "Неверное имя пользователя или пароль."
            )
            response, status = render_login(
                message, 429 if retry_after else 401
            )
            response = app.make_response((response, status))
            if retry_after:
                response.headers["Retry-After"] = str(retry_after)
            return response

        security_store.clear_login_failures(key)
        next_url = safe_next_url(request.form.get("next"))
        session.clear()
        session["user"] = config.auth_username
        session.permanent = True
        csrf_token(session)
        return redirect(next_url, code=303)

    @app.post("/logout")
    def logout():
        candidate = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
        if not validate_csrf(session, candidate):
            return jsonify({"detail": "invalid CSRF token"}), 400
        session.clear()
        if wants_json():
            return jsonify({"status": "ok"})
        return redirect("/login", code=303)

    @app.get("/login.css")
    def login_css():
        return send_from_directory(STATIC_DIR, "login.css")

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": __version__})

    @app.get("/api/session")
    def api_session():
        return jsonify(
            {"username": config.auth_username, "csrf": csrf_token(session)}
        )

    @app.get("/api/meta")
    def api_meta():
        conn = data_connection()
        try:
            return jsonify(aggregates.meta(conn))
        finally:
            conn.close()

    @app.get("/api/summary")
    def api_summary():
        conn = data_connection()
        try:
            return jsonify(
                aggregates.summary(
                    conn,
                    request.args.get("from"),
                    request.args.get("to"),
                    request.args.get("project"),
                    credits_per_usd=config.credits_per_usd,
                )
            )
        finally:
            conn.close()

    @app.get("/api/daily")
    def api_daily():
        conn = data_connection()
        try:
            try:
                result = aggregates.daily_series(
                    conn,
                    request.args.get("group", "model"),
                    request.args.get("from"),
                    request.args.get("to"),
                    request.args.get("project"),
                )
            except ValueError as exc:
                return jsonify({"detail": str(exc)}), 422
            return jsonify(result)
        finally:
            conn.close()

    @app.get("/api/agents")
    def api_agents():
        conn = data_connection()
        try:
            return jsonify(
                {
                    "agents": aggregates.agent_totals(
                        conn,
                        request.args.get("from"),
                        request.args.get("to"),
                        request.args.get("project"),
                    )
                }
            )
        finally:
            conn.close()

    @app.get("/api/projects")
    def api_projects():
        conn = data_connection()
        try:
            return jsonify(
                {
                    "projects": aggregates.projects_overview(
                        conn, credits_per_usd=config.credits_per_usd
                    )
                }
            )
        finally:
            conn.close()

    @app.get("/api/efficiency")
    def api_efficiency():
        limit_value = request.args.get("limit")
        try:
            limit = int(limit_value) if limit_value else None
        except ValueError:
            return jsonify({"detail": "limit must be an integer"}), 422
        if limit is not None and not 1 <= limit <= 1000:
            return jsonify({"detail": "limit must be between 1 and 1000"}), 422
        conn = data_connection()
        try:
            return jsonify(
                {
                    "issues": aggregates.issue_efficiency(
                        conn, request.args.get("project"), limit
                    )
                }
            )
        finally:
            conn.close()

    def health_payload():
        conn = data_connection()
        try:
            return snapshot(
                conn,
                db_path=str(config.db_path),
                credits_per_usd=config.credits_per_usd,
            )
        finally:
            conn.close()

    @app.get("/health")
    @app.get("/api/health")
    def api_health():
        return jsonify(health_payload())

    @app.get("/api/sync")
    def api_sync():
        return jsonify(last_sync_state())

    @app.get("/api/events")
    def api_events():
        # Passenger/LiteSpeed can buffer long WSGI streams. The frontend
        # automatically falls back to /api/sync polling on this response.
        return "", 204

    @app.post("/api/ingest/snapshot")
    def ingest_snapshot():
        if request.mimetype != "application/vnd.aistat.snapshot+gzip":
            return jsonify({"detail": "unsupported content type"}), 415
        payload = request.get_data(cache=False, as_text=False)
        try:
            timestamp = verify_snapshot_signature(
                config.ingest_secret,
                request.headers.get("X-AIStat-Timestamp"),
                request.headers.get("X-AIStat-Signature"),
                payload,
                config.ingest_max_age_seconds,
            )
        except ValueError:
            return jsonify({"detail": "snapshot authentication failed"}), 401
        with ingest_lock():
            if not security_store.record_ingest_timestamp(timestamp):
                return jsonify({"detail": "snapshot replay rejected"}), 409
            try:
                info = install_compressed_snapshot(
                    payload, config.db_path, config.max_snapshot_bytes
                )
            except SnapshotError:
                return jsonify({"detail": "invalid snapshot"}), 422
        return jsonify(
            {
                "status": "ok",
                "sha256": info.sha256,
                "size_bytes": info.size_bytes,
                "schema_version": info.schema_version,
            }
        )

    @app.get("/")
    def dashboard():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/<path:asset>")
    def dashboard_asset(asset):
        allowed = {
            "app.js",
            "style.css",
            "vendor/chart.umd.min.js",
        }
        if asset not in allowed:
            abort(404)
        return send_from_directory(STATIC_DIR, asset)

    return app
