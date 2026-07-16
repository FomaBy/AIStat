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

from markupsafe import escape

from . import __version__, aggregates, oauth
from .config import Config
from .db import connect_readonly, init_db
from .health import snapshot
from .security import (
    OAUTH_STATE_TTL_SECONDS,
    SecurityStore,
    client_key,
    csrf_token,
    safe_next_url,
    validate_csrf,
    validate_public_config,
    verify_snapshot_signature,
)
from .snapshot import SnapshotError, install_compressed_snapshot
from .tenant import canonical_tenant_id

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Short-lived HttpOnly cookie binding OAuth states to the browser that started
# them; only its hash is stored server-side with each state row.
OAUTH_CLIENT_COOKIE = "aistat_oauth_client"


def create_app(config: Optional[Config] = None) -> Flask:
    config = config or Config()
    validate_public_config(config)
    config.ensure_security_db_dir()
    config.ensure_tenants_dir()

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
    owner_user_id = security_store.ensure_owner_user(
        config.auth_username, config.admin_email
    )
    ingest_lock_path = config.security_db_path.with_name("ingest.lock")

    public_endpoints = {
        "login",
        "login_css",
        "healthz",
        "ingest_snapshot",
        "oauth_start",
        "oauth_callback",
    }

    def oauth_session_authorized() -> bool:
        """True when the current session is an allow-listed OAuth login.

        Re-checked on every request so removing an email from the allow-list
        revokes access immediately. Fail-closed: no allow-list => no access.
        """
        return bool(session.get("user_id")) and oauth.is_email_authorized(
            config.oauth_allowed_emails, session.get("email")
        )

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
        if oauth_session_authorized():
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

    def empty_data_connection() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def current_user_id() -> Optional[int]:
        raw = session.get("user_id")
        if raw is not None:
            try:
                return canonical_tenant_id(raw)
            except ValueError:
                return None
        if session.get("user") == config.auth_username:
            return owner_user_id
        return None

    def data_connection() -> sqlite3.Connection:
        user_id = current_user_id()
        if user_id is None or security_store.get_tenant(user_id) is None:
            return empty_data_connection()
        path = config.tenant_db_path(user_id)
        if not path.is_file():
            return empty_data_connection()
        return connect_readonly(path)

    def query_filters():
        return aggregates.make_filters(
            request.args.get("from"), request.args.get("to"),
            request.args.getlist("project"), request.args.getlist("agent"),
            request.args.getlist("model"),
        )

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
        session["user_id"] = owner_user_id
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

    def oauth_pending(email):
        shown = escape(email or "—")
        body = (
            "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            "<title>Нет доступа — AIStat</title>"
            "<link rel=\"stylesheet\" href=\"/login.css\"></head><body>"
            "<main class=\"login-shell\"><section class=\"login-card\">"
            "<h1>AIStat</h1><p class=\"subtitle\">Вход выполнен как {email}, "
            "но у аккаунта пока нет доступа к статистике. Обратитесь к "
            "администратору.</p><p><a href=\"/login\">Войти как администратор</a>"
            "</p></section></main></body></html>"
        ).format(email=shown)
        return body, 403

    @app.get("/auth/<provider>/start")
    def oauth_start(provider):
        provider_config = config.oauth_providers.get(provider)
        if provider_config is None:
            abort(404)
        next_url = safe_next_url(request.args.get("next"))
        client_token = request.cookies.get(OAUTH_CLIENT_COOKIE)
        if not oauth.is_valid_client_token(client_token):
            client_token = oauth.generate_client_token()
        authorize_url = oauth.begin(
            security_store, provider_config, next_url, client_token
        )
        response = redirect(authorize_url, code=303)
        # SameSite=Lax survives the top-level redirect back from the provider
        # while staying invisible to scripts and other sites.
        response.set_cookie(
            OAUTH_CLIENT_COOKIE,
            client_token,
            max_age=OAUTH_STATE_TTL_SECONDS,
            path="/auth",
            secure=config.session_cookie_secure,
            httponly=True,
            samesite="Lax",
        )
        return response

    @app.get("/auth/<provider>/callback")
    def oauth_callback(provider):
        provider_config = config.oauth_providers.get(provider)
        if provider_config is None:
            abort(404)
        client_token = request.cookies.get(OAUTH_CLIENT_COOKIE)
        try:
            result = oauth.finish(
                security_store, provider_config, request.args, client_token
            )
        except oauth.OAuthError:
            body, status = render_login(
                "Не удалось выполнить вход через провайдера. Попробуйте снова.",
                400,
            )
            return body, status
        session.clear()
        session["user_id"] = result["user_id"]
        session["email"] = result["email"]
        session["provider"] = provider
        session.permanent = True
        csrf_token(session)
        if not oauth.is_email_authorized(
            config.oauth_allowed_emails, result["email"]
        ):
            return oauth_pending(result["email"])
        return redirect(safe_next_url(result["next_url"]), code=303)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": __version__})

    @app.get("/api/session")
    def api_session():
        return jsonify(
            {
                "username": config.auth_username,
                "user_id": current_user_id(),
                "csrf": csrf_token(session),
            }
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
        try:
            filters = query_filters()
        except ValueError as exc:
            return jsonify({"detail": str(exc)}), 422
        conn = data_connection()
        try:
            return jsonify(
                aggregates.summary(
                    conn,
                    credits_per_usd=config.credits_per_usd,
                    filters=filters,
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
                    filters=query_filters(),
                )
            except ValueError as exc:
                return jsonify({"detail": str(exc)}), 422
            return jsonify(result)
        finally:
            conn.close()

    @app.get("/api/agents")
    def api_agents():
        try:
            filters = query_filters()
        except ValueError as exc:
            return jsonify({"detail": str(exc)}), 422
        conn = data_connection()
        try:
            return jsonify(
                {
                    "agents": aggregates.agent_totals(
                        conn, filters=filters,
                    )
                }
            )
        finally:
            conn.close()

    @app.get("/api/projects")
    def api_projects():
        try:
            filters = query_filters()
        except ValueError as exc:
            return jsonify({"detail": str(exc)}), 422
        conn = data_connection()
        try:
            return jsonify(
                {
                    "projects": aggregates.projects_overview(
                        conn, credits_per_usd=config.credits_per_usd,
                        filters=filters,
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
        try:
            filters = query_filters()
        except ValueError as exc:
            return jsonify({"detail": str(exc)}), 422
        conn = data_connection()
        try:
            return jsonify(
                {
                    "issues": aggregates.issue_efficiency(
                        conn, limit=limit, filters=filters,
                    )
                }
            )
        finally:
            conn.close()

    @app.get("/api/model-efficiency")
    def api_model_efficiency():
        try:
            filters = query_filters()
        except ValueError as exc:
            return jsonify({"detail": str(exc)}), 422
        conn = data_connection()
        try:
            return jsonify(
                aggregates.efficiency_breakdown(conn, filters=filters)
            )
        finally:
            conn.close()

    @app.get("/api/efficiency-breakdown")
    def api_efficiency_breakdown():
        try:
            filters = query_filters()
        except ValueError as exc:
            return jsonify({"detail": str(exc)}), 422
        conn = data_connection()
        try:
            return jsonify(aggregates.efficiency_chart_breakdown(conn, filters=filters))
        finally:
            conn.close()

    def health_payload():
        conn = data_connection()
        try:
            return snapshot(
                conn,
                db_path=(
                    str(config.tenant_db_path(current_user_id()))
                    if current_user_id()
                    else None
                ),
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
            tenant_id = canonical_tenant_id(
                request.headers.get("X-AIStat-Tenant")
            )
            timestamp = verify_snapshot_signature(
                config.ingest_secret,
                tenant_id,
                request.headers.get("X-AIStat-Timestamp"),
                request.headers.get("X-AIStat-Signature"),
                payload,
                config.ingest_max_age_seconds,
            )
        except ValueError:
            return jsonify({"detail": "snapshot authentication failed"}), 401
        tenant = security_store.get_tenant(tenant_id)
        if tenant is None:
            return jsonify({"detail": "snapshot authentication failed"}), 401
        # From this point on, paths are derived from the canonical id read
        # back from security.db, never directly from the request header.
        tenant_id = int(tenant["user_id"])
        with ingest_lock():
            if not security_store.ingest_timestamp_is_fresh(
                tenant_id, timestamp
            ):
                return jsonify({"detail": "snapshot replay rejected"}), 409
            try:
                info = install_compressed_snapshot(
                    payload,
                    config.tenant_db_path(tenant_id),
                    config.max_snapshot_bytes,
                )
            except SnapshotError:
                return jsonify({"detail": "invalid snapshot"}), 422
            if not security_store.record_tenant_snapshot(
                tenant_id, timestamp, info.sha256
            ):
                return jsonify({"detail": "snapshot replay rejected"}), 409
        return jsonify(
            {
                "status": "ok",
                "tenant_id": tenant_id,
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
