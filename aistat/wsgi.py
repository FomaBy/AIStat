"""Authenticated WSGI application for Namecheap Shared Hosting.

Namecheap Shared Hosting supports WSGI but not ASGI. This module exposes the
same aggregate API and static dashboard as the local FastAPI app, while adding:

* mandatory password authentication;
* opaque, HttpOnly, SameSite session cookies whose state lives server-side;
* CSRF protection and failed-login throttling;
* strict host/HTTPS checks and browser security headers;
* HMAC-authenticated atomic SQLite snapshot ingestion.
"""

import hmac
import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from . import __version__, aggregates, handoff, oauth
from .config import Config
from .db import connect_readonly, init_db
from .health import snapshot
from .security import (
    LOGIN_CSRF_TTL_SECONDS,
    OAUTH_STATE_TTL_SECONDS,
    SecurityStore,
    client_key,
    make_login_csrf,
    safe_next_url,
    valid_login_csrf,
    validate_public_config,
    verify_snapshot_signature,
)
from .snapshot import SnapshotError, stage_compressed_snapshot
from .snapshot_recovery import cleanup_staged_file, swap_staged_into_place
from .tenant import canonical_tenant_id

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Short-lived HttpOnly cookie binding OAuth states to the browser that started
# them; only its hash is stored server-side with each state row.
OAUTH_CLIENT_COOKIE = "aistat_oauth_client"

# The browser auth cookie. It holds one opaque CSPRNG token and nothing else:
# identity, expiry, CSRF and revocation are all resolved server-side from the
# ``sessions`` table on every request. No signed/serialized envelope is used.
SESSION_COOKIE = "aistat_session"
# Pre-authentication double-submit cookie for the login form's CSRF defence.
# It carries only a random signed token, never identity.
LOGIN_CSRF_COOKIE = "aistat_login_csrf"


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
    # No Flask client-side session is used: the auth cookie is set and read
    # directly below, so the framework never serializes identity into a signed
    # cookie. ``MAX_CONTENT_LENGTH`` still bounds snapshot uploads.
    app.config.update(MAX_CONTENT_LENGTH=config.max_snapshot_bytes)
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
        # Worker pull-channel routes authenticate every request with the
        # independent worker HMAC secret instead of a browser session.
        "worker_connection_pull",
        "worker_connection_ack",
    }

    session_ttl_seconds = int(config.session_hours) * 3600

    def current_session():
        """Authoritative server-side state for this request's auth cookie.

        The cookie is one opaque token; every protected request resolves it
        against ``security.db`` to a live ``{"user_id", "csrf", "expires_at"}``
        record, or ``None``. Logout/revocation/expiry delete or age out that
        record, so a captured cookie fails closed here, as does any old
        signed/structured cookie (its bytes are not a stored id). Cached on
        ``g`` so one request hits the store once.
        """
        if not hasattr(g, "_auth_session"):
            g._auth_session = security_store.resolve_session(
                request.cookies.get(SESSION_COOKIE)
            )
        return g._auth_session

    def set_session_cookie(response, sid: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            sid,
            max_age=session_ttl_seconds,
            path="/",
            secure=config.session_cookie_secure,
            httponly=True,
            samesite="Lax",
        )

    def clear_cookie(response, name: str) -> None:
        response.set_cookie(
            name,
            "",
            max_age=0,
            expires=0,
            path="/",
            secure=config.session_cookie_secure,
            httponly=True,
            samesite="Lax",
        )

    def request_csrf_ok() -> bool:
        """Validate the submitted CSRF against the current session's secret.

        The token is the server-side session's own CSRF value (delivered only
        through ``/api/session``); it is never carried in the auth cookie.
        """
        sess = current_session()
        if sess is None:
            return False
        candidate = request.headers.get("X-CSRF-Token") or request.form.get(
            "csrf"
        )
        return bool(
            candidate
            and hmac.compare_digest(str(sess["csrf"]), str(candidate))
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
        # One rule for both password and OAuth logins: a request is authorised
        # only while its opaque cookie resolves to a live server-side session.
        if current_session() is not None:
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
        sess = current_session()
        if sess is None:
            return None
        try:
            return canonical_tenant_id(sess["user_id"])
        except ValueError:
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

    # Reconcile any snapshot install a crash left half-applied before this
    # worker serves a request. Under the ingest lock so it cannot race a live
    # ingest or another worker's recovery pass.
    with ingest_lock():
        security_store.recover_snapshot_installs(config.tenants_dir)

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
        next_url = safe_next_url(request.values.get("next"))
        google_login_url = None
        if "google" in config.oauth_providers:
            google_login_url = "/auth/google/start?next=" + quote(
                next_url, safe=""
            )
        yandex_login_url = None
        if "yandex" in config.oauth_providers:
            yandex_login_url = "/auth/yandex/start?next=" + quote(
                next_url, safe=""
            )
        token = make_login_csrf(config.session_secret)
        html = render_template(
            "login.html",
            csrf=token,
            error=error,
            next_url=next_url,
            google_login_url=google_login_url,
            yandex_login_url=yandex_login_url,
        )
        response = app.make_response((html, status))
        # Double-submit login CSRF: the same signed token is embedded in the
        # form and set here, and the POST requires the two to match.
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            token,
            max_age=LOGIN_CSRF_TTL_SECONDS,
            path="/",
            secure=config.session_cookie_secure,
            httponly=True,
            samesite="Lax",
        )
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if current_session() is not None:
                return redirect(safe_next_url(request.args.get("next")), code=303)
            return render_login()

        if not valid_login_csrf(
            config.session_secret,
            request.cookies.get(LOGIN_CSRF_COOKIE),
            request.form.get("csrf"),
        ):
            return render_login("Не удалось проверить форму. Обновите страницу.", 400)

        key = client_key(config.session_secret, request.remote_addr)
        retry_after = security_store.login_retry_after(key)
        if retry_after:
            response = render_login(
                "Слишком много попыток. Повторите вход позже.", 429
            )
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
            response = render_login(message, 429 if retry_after else 401)
            if retry_after:
                response.headers["Retry-After"] = str(retry_after)
            return response

        security_store.clear_login_failures(key)
        next_url = safe_next_url(request.form.get("next"))
        # Rotate: re-authenticating in this same browser invalidates its own
        # previous token server-side, so a captured pre-rotation cookie replays
        # dead. Only the incoming session is revoked, so a parallel session for
        # the same user in another browser stays live.
        security_store.revoke_session(request.cookies.get(SESSION_COOKIE))
        sid = security_store.create_session(owner_user_id, session_ttl_seconds)
        response = redirect(next_url, code=303)
        set_session_cookie(response, sid)
        clear_cookie(response, LOGIN_CSRF_COOKIE)
        return response

    @app.post("/logout")
    def logout():
        if not request_csrf_ok():
            return jsonify({"detail": "invalid CSRF token"}), 400
        security_store.revoke_session(request.cookies.get(SESSION_COOKIE))
        if wants_json():
            response = jsonify({"status": "ok"})
        else:
            response = redirect("/login", code=303)
        clear_cookie(response, SESSION_COOKIE)
        return response

    @app.get("/login.css")
    def login_css():
        return send_from_directory(STATIC_DIR, "login.css")

    def registration_closed():
        """Non-secret page shown when a new user may not register.

        Carries no email or allow-list detail, so it never reveals who is or
        is not permitted; a rejected new subject never reaches a session.
        """
        body = (
            "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            "<title>Регистрация закрыта — AIStat</title>"
            "<link rel=\"stylesheet\" href=\"/login.css\"></head><body>"
            "<main class=\"login-shell\"><section class=\"login-card\">"
            "<h1>AIStat</h1><p class=\"subtitle\">Регистрация сейчас закрыта. "
            "Чтобы получить доступ, обратитесь к администратору.</p>"
            "<p><a href=\"/login\">Вернуться ко входу</a></p>"
            "</section></main></body></html>"
        )
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

        def resolve_identity(subject, email, email_verified, display_name):
            return oauth.open_registration_identity(
                security_store,
                provider,
                subject,
                email,
                email_verified,
                display_name,
                allowed_emails=config.oauth_allowed_emails,
                admin_email=config.admin_email,
                owner_user_id=owner_user_id,
            )

        try:
            result = oauth.finish(
                security_store,
                provider_config,
                request.args,
                client_token,
                resolve_identity,
            )
        except oauth.RegistrationClosedError:
            # A new subject that is not the owner and not allow-listed: no
            # session is created, and the page reveals no allow-list detail.
            return registration_closed()
        except oauth.OAuthError:
            return render_login(
                "Не удалось выполнить вход через провайдера. Попробуйте снова.",
                400,
            )
        # Registration/link succeeded, so access is granted by the active
        # session alone — there is no second, request-time allow-list gate.
        # The cookie is the opaque token; email/provider stay server-side.
        # Rotate the incoming session so a re-auth invalidates the old token.
        security_store.revoke_session(request.cookies.get(SESSION_COOKIE))
        sid = security_store.create_session(
            result["user_id"], session_ttl_seconds
        )
        response = redirect(safe_next_url(result["next_url"]), code=303)
        set_session_cookie(response, sid)
        return response

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "version": __version__})

    @app.get("/api/session")
    def api_session():
        sess = current_session()
        return jsonify(
            {
                "username": config.auth_username,
                "user_id": current_user_id(),
                "csrf": sess["csrf"] if sess else None,
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
            # Never surface the tenant DB's filesystem path over the public
            # contour: it would disclose the server layout and the numeric
            # tenant id embedded in the path. ``db_path`` stays available only
            # to the loopback CLI / local FastAPI contour, which pass it
            # explicitly.
            return snapshot(
                conn,
                db_path=None,
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
            target_path = config.tenant_db_path(tenant_id)
            try:
                staged_path, info = stage_compressed_snapshot(
                    payload, target_path, config.max_snapshot_bytes
                )
            except SnapshotError:
                return jsonify({"detail": "invalid snapshot"}), 422
            # Journal the intent before touching the tenant database so a crash
            # between the file swap and the watermark update recovers to a
            # consistent old/old or new/new state, never a mixed one.
            security_store.begin_snapshot_install(
                tenant_id, timestamp, info.sha256, timestamp, str(staged_path)
            )
            try:
                swap_staged_into_place(staged_path, target_path)
            except (OSError, ValueError):
                # The swap only raises before os.replace, so the tenant DB is
                # untouched: safe to roll back to old/old.
                security_store.abort_snapshot_install(tenant_id)
                cleanup_staged_file(staged_path)
                return jsonify({"detail": "invalid snapshot"}), 422
            if not security_store.finish_snapshot_install(
                tenant_id, timestamp, info.sha256, timestamp
            ):
                # Unreachable while the ingest lock is held: the freshness check
                # above and this commit use the same strict threshold, and no
                # other writer advances the watermark under the lock. The DB is
                # already swapped, so a stuck watermark here is a broken
                # invariant, not a replay — surface it loudly.
                return jsonify({"detail": "snapshot install failed"}), 500
        return jsonify(
            {
                "status": "ok",
                "tenant_id": tenant_id,
                "sha256": info.sha256,
                "size_bytes": info.size_bytes,
                "schema_version": info.schema_version,
            }
        )

    @app.get("/api/connection")
    def api_connection():
        user_id = current_user_id()
        if user_id is None:
            return jsonify({"detail": "authentication required"}), 401
        if not config.multica_connect_enabled:
            return jsonify({"status": "disabled"})
        status = security_store.connection_status(
            user_id, config.connection_pending_ttl_seconds
        )
        if status is None:
            return jsonify({"status": "none"})
        del status["token_epoch"]
        return jsonify(status)

    @app.post("/api/connection")
    def api_connection_submit():
        # Fail closed unless the whole feature is switched on and a worker
        # channel exists — otherwise a stored token could never be collected.
        if not config.multica_connect_enabled:
            return jsonify({"detail": "connection intake is disabled"}), 503
        if not config.worker_secret:
            return jsonify(
                {"detail": "connection intake is not configured"}
            ), 503
        if not request_csrf_ok():
            return jsonify({"detail": "invalid CSRF token"}), 400
        user_id = current_user_id()
        if user_id is None:
            return jsonify({"detail": "authentication required"}), 401
        try:
            retry_after = security_store.reserve_connection_submission(user_id)
        except sqlite3.Error:
            return jsonify({"detail": "connection intake unavailable"}), 503
        if retry_after:
            response = jsonify({"detail": "too many submissions"})
            response.headers["Retry-After"] = str(retry_after)
            return response, 429
        try:
            token = handoff.validate_connection_token(
                request.form.get("token")
            )
            # The user's server URL is never published: the connection is
            # pinned to the single official Multica host.
            server_url = handoff.normalize_official_server_url(
                request.form.get("server_url"), config.multica_official_url
            )
            workspace_label = handoff.validate_workspace_label(
                request.form.get("workspace_label")
            )
        except ValueError as exc:
            # Validator messages never contain the submitted values.
            return jsonify({"detail": str(exc)}), 422
        # No token is written unless the trusted worker has pulled recently;
        # otherwise a stored token could linger uncollected.
        if not security_store.worker_ready(config.worker_readiness_ttl_seconds):
            return jsonify({"detail": "connection worker is not ready"}), 503
        try:
            status = security_store.submit_connection(
                user_id, server_url, workspace_label, token
            )
        except ValueError:
            return jsonify({"detail": "unknown user"}), 400
        del status["token_epoch"]
        return jsonify(status)

    @app.post("/api/connection/revoke")
    def api_connection_revoke():
        if not config.multica_connect_enabled:
            return jsonify({"detail": "connection intake is disabled"}), 503
        if not request_csrf_ok():
            return jsonify({"detail": "invalid CSRF token"}), 400
        user_id = current_user_id()
        if user_id is None:
            return jsonify({"detail": "authentication required"}), 401
        if not security_store.revoke_connection(user_id):
            return jsonify({"detail": "no connection"}), 404
        # `revoked` is only reported once the worker has acked the delete; a
        # fresh revoke reports the intermediate `revocation_pending`.
        status = security_store.connection_status(
            user_id, config.connection_pending_ttl_seconds
        )
        return jsonify({"status": status["status"] if status else "revoked"})

    class ReplayedWorkerNonce(Exception):
        pass

    def verified_worker_body(path: str):
        payload = request.get_data(cache=False, as_text=False)
        _, nonce = handoff.verify_worker_request(
            config.worker_secret,
            path,
            request.headers.get("X-AIStat-Timestamp"),
            request.headers.get("X-AIStat-Nonce"),
            request.headers.get("X-AIStat-Signature"),
            payload,
            config.ingest_max_age_seconds,
        )
        if not security_store.consume_worker_nonce(
            nonce, config.ingest_max_age_seconds
        ):
            raise ReplayedWorkerNonce()
        return payload

    @app.post(handoff.WORKER_PULL_PATH)
    def worker_connection_pull():
        if not (config.multica_connect_enabled and config.worker_secret):
            return jsonify({"detail": "not found"}), 404
        try:
            verified_worker_body(handoff.WORKER_PULL_PATH)
        except ValueError:
            return jsonify({"detail": "worker authentication failed"}), 401
        except ReplayedWorkerNonce:
            return jsonify({"detail": "worker replay rejected"}), 409
        return jsonify(
            security_store.lease_pending_connections(
                config.connection_pending_ttl_seconds
            )
        )

    @app.post(handoff.WORKER_ACK_PATH)
    def worker_connection_ack():
        if not (config.multica_connect_enabled and config.worker_secret):
            return jsonify({"detail": "not found"}), 404
        try:
            payload = verified_worker_body(handoff.WORKER_ACK_PATH)
        except ValueError:
            return jsonify({"detail": "worker authentication failed"}), 401
        except ReplayedWorkerNonce:
            return jsonify({"detail": "worker replay rejected"}), 409
        try:
            body = json.loads(payload.decode("utf-8"))
            results = security_store.apply_worker_acks(body.get("acks"))
        except (UnicodeDecodeError, ValueError, AttributeError):
            return jsonify({"detail": "invalid request body"}), 400
        return jsonify({"results": results})

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
