"""Public WSGI authentication, headers and signed snapshot ingestion."""

import base64
import gzip
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from werkzeug.security import generate_password_hash

from aistat import oauth
from aistat.config import Config
from aistat.db import SCHEMA_VERSION, connect, init_db
from aistat.migrate import migrate_owner_database
from aistat.security import SecurityStore, make_login_csrf, snapshot_signature
from aistat.snapshot import create_compressed_snapshot
from aistat.wsgi import create_app
from conftest import (
    assert_opaque_session_cookie,
    seed_aggregate_fixture,
    seed_model_less_fixture,
)

PASSWORD = "correct horse battery staple"
SESSION_SECRET = "session-" + "s" * 48
INGEST_SECRET = "ingest-" + "i" * 48

OAUTH_PROVIDER = oauth.OAuthProvider(
    name="google",
    authorize_url="https://accounts.example/authorize",
    token_url="https://oauth.example/token",
    userinfo_url="https://api.example/userinfo",
    scopes=("openid", "email", "profile"),
    client_id="client-id",
    client_secret="client-secret",
    redirect_uri="https://localhost/auth/google/callback",
)

# Yandex ID: same generic flow, but userinfo exposes only confirmed addresses
# and carries no verified-email claim, so the provider opts into
# assume_email_verified (mirrors AISTAT_OAUTH_YANDEX_ASSUME_EMAIL_VERIFIED=1).
YANDEX_PROVIDER = oauth.OAuthProvider(
    name="yandex",
    authorize_url="https://yandex.example/authorize",
    token_url="https://oauth.example/token",
    userinfo_url="https://api.example/userinfo",
    scopes=("login:email", "login:info"),
    client_id="ya-client-id",
    client_secret="ya-client-secret",
    redirect_uri="https://localhost/auth/yandex/callback",
    assume_email_verified=True,
)


class _FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        if size is None or size < 0:
            data, self._data = self._data, b""
            return data
        data, self._data = self._data[:size], self._data[size:]
        return data

    def close(self):
        pass


def install_fake_http(monkeypatch, identity):
    def fake_urlopen(request, timeout=None):
        if request.full_url.endswith("/token"):
            return _FakeResponse({"access_token": "at"})
        return _FakeResponse(identity)

    monkeypatch.setattr("aistat.oauth.urlopen", fake_urlopen)


def state_from(location):
    return parse_qs(urlsplit(location).query)["state"][0]


def complete_oauth_login(client, monkeypatch, identity, next_url="/"):
    """Drive a full mock-provider Google login through the real Flask app.

    The provider's HTTPS egress (token + userinfo) is the only thing stubbed;
    the /auth/google/start -> provider -> /auth/google/callback redirect loop,
    the browser-binding cookie and every policy/store write run for real.
    """
    install_fake_http(monkeypatch, identity)
    start = client.get(
        "/auth/google/start?" + urlencode({"next": next_url}),
        base_url="https://localhost",
    )
    state = state_from(start.headers["Location"])
    return client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )


def _project_ids(client):
    response = client.get("/api/meta", base_url="https://localhost")
    assert response.status_code == 200
    return {p["id"] for p in response.get_json()["projects"]}


@pytest.fixture
def public_app(tmp_path):
    config = Config()
    config.db_path = tmp_path / "public.db"
    config.security_db_path = tmp_path / "security.db"
    config.tenants_dir = tmp_path / "tenants"
    config.credits_per_usd = 2.0
    config.auth_username = "sergey"
    config.auth_password_hash = generate_password_hash(
        PASSWORD, method="pbkdf2:sha256:600000"
    )
    config.session_secret = SESSION_SECRET
    config.ingest_secret = INGEST_SECRET
    config.allowed_hosts = ("localhost", "testserver", "aistat.app")
    config.force_https = False
    config.session_cookie_secure = True
    config.oauth_providers = {
        "google": OAUTH_PROVIDER,
        "yandex": YANDEX_PROVIDER,
    }
    config.oauth_allowed_emails = frozenset({"allowed@example.com"})
    config.admin_email = "allowed@example.com"

    conn = connect(config.db_path)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()

    migration = migrate_owner_database(config, now=1000)
    config.publish_tenant_id = migration["owner_user_id"]
    app = create_app(config)
    app.config.update(TESTING=True)
    return app, config


def csrf_from(page) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', page.get_data(as_text=True))
    assert match
    return match.group(1)


def login(client, password=PASSWORD):
    page = client.get("/login", base_url="https://localhost")
    return client.post(
        "/login",
        data={
            "csrf": csrf_from(page),
            "username": "sergey",
            "password": password,
            "next": "/",
        },
        follow_redirects=False,
        base_url="https://localhost",
    )


def test_dashboard_and_api_require_login(public_app):
    app, _ = public_app
    client = app.test_client()
    assert client.get("/api/meta").status_code == 401
    dashboard = client.get("/")
    assert dashboard.status_code == 303
    assert dashboard.headers["Location"].startswith("/login")


def test_login_cookie_api_and_logout_csrf(public_app):
    app, _ = public_app
    client = app.test_client()
    response = login(client)
    assert response.status_code == 303
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie

    meta = client.get("/api/meta", base_url="https://localhost")
    assert meta.status_code == 200
    assert [p["title"] for p in meta.get_json()["projects"]] == ["Alpha", "Beta"]

    auth = client.get("/api/session", base_url="https://localhost").get_json()
    assert auth["username"] == "sergey"
    assert client.post(
        "/logout",
        headers={"X-CSRF-Token": "wrong"},
        base_url="https://localhost",
    ).status_code == 400
    assert client.post(
        "/logout",
        headers={"X-CSRF-Token": auth["csrf"]},
        base_url="https://localhost",
    ).status_code == 303
    assert client.get("/api/meta").status_code == 401


def session_cookie_from(response):
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith("aistat_session="):
            return header.split(";", 1)[0]
    raise AssertionError("no session cookie issued")


def replay(app, cookie):
    # use_cookies=False so the client's own (empty) jar does not overwrite
    # the replayed Cookie header.
    return app.test_client(use_cookies=False).get(
        "/api/meta", base_url="https://localhost", headers={"Cookie": cookie}
    )


def test_logout_revokes_replayed_session_cookie(public_app):
    # FAN-1229: replaying a cookie captured before logout must fail closed.
    app, _ = public_app
    client = app.test_client()
    stolen = session_cookie_from(login(client))
    assert replay(app, stolen).status_code == 200

    auth = client.get("/api/session", base_url="https://localhost").get_json()
    denied = client.post(
        "/logout",
        headers={"X-CSRF-Token": "wrong"},
        base_url="https://localhost",
    )
    assert denied.status_code == 400
    # an invalid CSRF token must not revoke anything
    assert replay(app, stolen).status_code == 200

    out = client.post(
        "/logout",
        headers={"X-CSRF-Token": auth["csrf"]},
        base_url="https://localhost",
    )
    assert out.status_code == 303
    for _ in range(3):
        assert replay(app, stolen).status_code == 401
    # the dead cookie renders the login form instead of a redirect loop
    page = app.test_client(use_cookies=False).get(
        "/login", base_url="https://localhost", headers={"Cookie": stolen}
    )
    assert page.status_code == 200


def test_logout_revokes_only_the_current_session(public_app):
    app, _ = public_app
    first = app.test_client()
    second = app.test_client()
    assert login(first).status_code == 303
    assert login(second).status_code == 303
    csrf = first.get(
        "/api/session", base_url="https://localhost"
    ).get_json()["csrf"]
    assert first.post(
        "/logout",
        headers={"X-CSRF-Token": csrf},
        base_url="https://localhost",
    ).status_code == 303
    assert first.get(
        "/api/meta", base_url="https://localhost"
    ).status_code == 401
    assert second.get(
        "/api/meta", base_url="https://localhost"
    ).status_code == 200


def test_session_revocation_survives_worker_restart(public_app):
    # Sessions live in security.db, so a fresh worker process honours both
    # existing sessions and revocations performed by another worker.
    app, config = public_app
    client = app.test_client()
    stolen = session_cookie_from(login(client))
    restarted = create_app(config)
    restarted.config.update(TESTING=True)
    assert replay(restarted, stolen).status_code == 200
    csrf = client.get(
        "/api/session", base_url="https://localhost"
    ).get_json()["csrf"]
    assert client.post(
        "/logout",
        headers={"X-CSRF-Token": csrf},
        base_url="https://localhost",
    ).status_code == 303
    for _ in range(3):
        assert replay(restarted, stolen).status_code == 401


def test_oauth_logout_revokes_replayed_cookie(public_app, monkeypatch):
    app, _ = public_app
    install_fake_http(
        monkeypatch,
        {"sub": "g-out", "email": "allowed@example.com", "email_verified": True},
    )
    client = app.test_client()
    start = client.get(
        "/auth/google/start", base_url="https://localhost"
    )
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc"
        % state_from(start.headers["Location"]),
        base_url="https://localhost",
    )
    assert callback.status_code == 303
    stolen = session_cookie_from(callback)
    assert replay(app, stolen).status_code == 200
    csrf = client.get(
        "/api/session", base_url="https://localhost"
    ).get_json()["csrf"]
    assert client.post(
        "/logout",
        headers={"X-CSRF-Token": csrf},
        base_url="https://localhost",
    ).status_code == 303
    for _ in range(3):
        assert replay(app, stolen).status_code == 401


def _cookie_value(header):
    return header.split(";", 1)[0].split("=", 1)[1]


def test_password_session_cookie_is_opaque(public_app):
    # AC1/AC2/AC8: the auth cookie is one opaque token — no decodable email,
    # username, provider, user id, CSRF or serialized envelope — set HttpOnly,
    # Secure, SameSite=Lax, Path=/.
    app, config = public_app
    client = app.test_client()
    response = login(client)
    header = next(
        h for h in response.headers.getlist("Set-Cookie")
        if h.startswith("aistat_session=")
    )
    sid = _cookie_value(header)
    auth = client.get("/api/session", base_url="https://localhost").get_json()
    assert_opaque_session_cookie(
        sid,
        ["sergey", config.admin_email, "google", auth["csrf"]],
    )
    assert auth["csrf"] and auth["csrf"] != sid
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=Lax" in header
    assert "Path=/" in header


def test_google_session_cookie_is_opaque(public_app, monkeypatch):
    # AC1: an OAuth login gets the same opaque cookie — no email/subject/provider.
    app, _ = public_app
    client = app.test_client()
    callback = complete_oauth_login(
        client,
        monkeypatch,
        {"sub": "g-opaque", "email": "allowed@example.com", "email_verified": True},
    )
    assert callback.status_code == 303
    sid = _cookie_value(session_cookie_from(callback))
    auth = client.get("/api/session", base_url="https://localhost").get_json()
    assert_opaque_session_cookie(
        sid,
        ["allowed@example.com", "google", "g-opaque", auth["csrf"]],
    )


def test_old_structured_cookie_fails_closed_even_with_live_row(public_app):
    # AC3: an old signed/serialized cookie (or a byte-modified/unknown token)
    # fails closed with no private access even while its inner SID row is live.
    app, config = public_app
    store = SecurityStore(config.security_db_path)
    owner = config.publish_tenant_id
    sid = store.create_session(owner, 3600)
    assert store.session_is_active(sid)

    envelope = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"user": "sergey", "user_id": owner, "sid": sid, "_csrf": "x"}
            ).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"), envelope.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    structured_cookies = (
        envelope + "." + signature,   # a full old-style signed envelope
        sid + ".tampered",            # inner id smuggled into an envelope shape
        sid[:-4] + ("AAAA" if not sid.endswith("AAAA") else "BBBB"),  # byte-modified
        "totally-unknown-token",      # never issued
    )
    for value in structured_cookies:
        cookie = "aistat_session=" + value
        assert replay(app, cookie).status_code == 401, value
        page = app.test_client(use_cookies=False).get(
            "/login", base_url="https://localhost", headers={"Cookie": cookie}
        )
        assert page.status_code == 200, value
    # None of the failed reads disturbed the genuine live row.
    assert store.session_is_active(sid)


def test_reauth_rotates_and_invalidates_previous_token(public_app):
    # AC4: re-authenticating in the same browser rotates the token and kills the
    # previous one, so a captured pre-rotation cookie replays dead.
    app, _ = public_app
    client = app.test_client()
    first = session_cookie_from(login(client))
    assert replay(app, first).status_code == 200

    # Re-auth while still holding cookie A: mint a login CSRF the way the form
    # would and POST /login again with A still in the jar.
    token = make_login_csrf(SESSION_SECRET)
    client.set_cookie("aistat_login_csrf", token)
    response = client.post(
        "/login",
        data={
            "csrf": token,
            "username": "sergey",
            "password": PASSWORD,
            "next": "/",
        },
        base_url="https://localhost",
    )
    assert response.status_code == 303
    second = session_cookie_from(response)
    assert _cookie_value(second) != _cookie_value(first)
    for _ in range(3):
        assert replay(app, first).status_code == 401
    assert replay(app, second).status_code == 200


def test_wsgi_hour_filters_accept_repeated_dimensions(public_app):
    app, _ = public_app
    client = app.test_client()
    assert login(client).status_code == 303
    response = client.get(
        "/api/summary",
        query_string=[
            ("from", "2026-01-01T10:00Z"),
            ("to", "2026-01-01T11:00Z"),
            ("project", "P1"),
            ("agent", "A2"),
            ("model", "m-shared"),
        ],
        base_url="https://localhost",
    )
    assert response.status_code == 200
    assert response.get_json()["total_tokens"] == 600_000


def test_wsgi_agents_count_only_overlapping_hour_runs(public_app):
    app, _ = public_app
    client = app.test_client()
    assert login(client).status_code == 303
    response = client.get(
        "/api/agents",
        query_string=[
            ("from", "2026-01-01T10:00Z"), ("to", "2026-01-01T11:00Z"),
            ("project", "P1"), ("agent", "A2"), ("model", "m-shared"),
        ],
        base_url="https://localhost",
    )
    assert response.status_code == 200
    assert {agent["agent_id"]: agent["runs"]
            for agent in response.get_json()["agents"]} == {"A2": 1}


def test_wsgi_agent_count_and_worktime(public_app):
    app, _ = public_app
    client = app.test_client()
    assert login(client).status_code == 303
    s = client.get("/api/summary", base_url="https://localhost").get_json()
    assert s["agent_count"] == 3
    assert s["agent_work_seconds"] == 21600
    agents = client.get(
        "/api/agents", base_url="https://localhost"
    ).get_json()["agents"]
    assert sum(a["work_seconds"] for a in agents) == s["agent_work_seconds"]
    assert sum(1 for a in agents if a["work_seconds"] > 0) == s["agent_count"]


def test_login_is_csrf_protected_and_throttled(public_app):
    app, _ = public_app
    client = app.test_client()
    assert client.post(
        "/login",
        data={"username": "sergey", "password": PASSWORD},
    ).status_code == 400

    statuses = []
    for _ in range(5):
        statuses.append(login(client, password="wrong").status_code)
    assert statuses[:4] == [401, 401, 401, 401]
    assert statuses[4] == 429
    assert login(client).status_code == 429


def test_security_headers_and_host_allowlist(public_app):
    app, _ = public_app
    client = app.test_client()
    response = client.get("/login")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/login", headers={"Host": "evil.example"}).status_code == 400


def test_https_redirect_and_hsts(public_app):
    app, config = public_app
    config.force_https = True
    secure_app = create_app(config)
    secure_app.config.update(TESTING=True)
    client = secure_app.test_client()
    response = client.get("/login", base_url="http://aistat.app")
    assert response.status_code == 308
    secure = client.get("/login", base_url="https://aistat.app")
    assert "max-age=31536000" in secure.headers["Strict-Transport-Security"]


def test_signed_snapshot_install_and_replay_rejection(public_app, tmp_path):
    app, config = public_app
    source_path = tmp_path / "source.db"
    conn = connect(source_path)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.execute(
        "UPDATE daily_usage SET input_tokens = input_tokens + 1000000 "
        "WHERE runtime_id = 'R1'"
    )
    conn.commit()
    conn.close()
    payload = create_compressed_snapshot(source_path)

    timestamp = int(time.time())
    signature = snapshot_signature(
        INGEST_SECRET, config.publish_tenant_id, timestamp, payload
    )
    client = app.test_client()
    response = client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Tenant": str(config.publish_tenant_id),
            "X-AIStat-Signature": signature,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["schema_version"] == SCHEMA_VERSION
    owner_path = config.tenant_db_path(config.publish_tenant_id)
    assert owner_path.with_name(owner_path.name + ".previous").exists()

    replay = client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Tenant": str(config.publish_tenant_id),
            "X-AIStat-Signature": signature,
        },
    )
    assert replay.status_code == 409

    assert login(client).status_code == 303
    summary = client.get(
        "/api/summary", base_url="https://localhost"
    ).get_json()
    assert summary["total_tokens"] == 5_700_000


def test_ingest_rejects_snapshot_with_older_usage_data(public_app, tmp_path):
    """FAN-1442: a snapshot whose daily_usage is older than the tenant's current
    data must be rejected even with a fresh (non-replay) timestamp, so a lapsed
    owner poller cannot overwrite the newer connected-collector snapshot."""
    from aistat.snapshot import daily_usage_max_date

    app, config = public_app
    owner_path = config.tenant_db_path(config.publish_tenant_id)

    def build(mutate_sql=None):
        src = tmp_path / "ingest_src.db"
        if src.exists():
            src.unlink()
        conn = connect(src)
        init_db(conn)
        seed_aggregate_fixture(conn)
        if mutate_sql:
            conn.executescript(mutate_sql)
        conn.commit()
        conn.close()
        return create_compressed_snapshot(src)

    def post(payload, ts):
        return app.test_client().post(
            "/api/ingest/snapshot",
            data=payload,
            content_type="application/vnd.aistat.snapshot+gzip",
            headers={
                "X-AIStat-Timestamp": str(ts),
                "X-AIStat-Tenant": str(config.publish_tenant_id),
                "X-AIStat-Signature": snapshot_signature(
                    INGEST_SECRET, config.publish_tenant_id, ts, payload
                ),
            },
        )

    base_ts = int(time.time())
    # Baseline install: fixture's latest usage day is 2026-01-02.
    assert post(build(), base_ts).status_code == 200
    assert daily_usage_max_date(owner_path) == "2026-01-02"

    # Stale snapshot (older max date) with a strictly newer timestamp: it clears
    # the replay guard but must be refused by the data-freshness guard.
    stale = build("DELETE FROM daily_usage WHERE date = '2026-01-02';")
    rejected = post(stale, base_ts + 10)
    assert rejected.status_code == 409
    assert rejected.get_json()["detail"] == (
        "snapshot older than current data rejected"
    )
    assert daily_usage_max_date(owner_path) == "2026-01-02"  # unchanged

    # A genuinely newer snapshot still installs.
    fresh = build(
        "INSERT INTO daily_usage (runtime_id, model, date, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, "
        "cost_credits, cost_priced, synced_at) VALUES "
        "('R1', 'm-claude', '2026-01-03', 1, 0, 0, 0, NULL, NULL, 0, "
        "'2026-01-03T00:00:00Z');"
    )
    assert post(fresh, base_ts + 20).status_code == 200
    assert daily_usage_max_date(owner_path) == "2026-01-03"


def test_model_efficiency_endpoint_behind_auth(public_app):
    app, _ = public_app
    client = app.test_client()
    assert client.get("/api/model-efficiency").status_code == 401
    login(client)
    data = client.get(
        "/api/model-efficiency", base_url="https://localhost"
    ).get_json()
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]
    assert data["cost_per_sp"] == pytest.approx(0.0005)


def test_efficiency_breakdown_endpoint_behind_auth(public_app):
    app, _ = public_app
    client = app.test_client()
    assert client.get("/api/efficiency-breakdown").status_code == 401
    login(client)
    data = client.get(
        "/api/efficiency-breakdown?from=2026-01-01T10%3A00Z"
        "&to=2026-01-01T10%3A30Z&agent=A2&model=m-shared",
        base_url="https://localhost",
    ).get_json()
    assert data["time"]["granularity"] == "hour"
    assert data["time"]["rows"][0]["total_tokens"] == 375


def test_projects_filtered_cost_matches_model_efficiency_behind_auth(public_app):
    # FAN-1251: /api/projects and /api/model-efficiency must agree ($0.002)
    # for the combined project+agent+model+time filter.
    app, _ = public_app
    client = app.test_client()
    login(client)
    query = ("?from=2026-01-01T10%3A00Z&to=2026-01-01T11%3A00Z"
             "&project=P1&agent=A2&model=m-shared")
    projects = client.get(
        "/api/projects" + query, base_url="https://localhost"
    ).get_json()["projects"]
    alpha = {p["title"]: p for p in projects}["Alpha"]
    assert alpha["total_tokens"] == pytest.approx(750)
    assert alpha["cost_usd"] == pytest.approx(0.002)
    eff = client.get(
        "/api/model-efficiency" + query, base_url="https://localhost"
    ).get_json()
    assert eff["cost_usd"] == pytest.approx(0.002)


def test_model_efficiency_filters_behind_auth(public_app):
    # FAN-1244: one filtered run-overlap set for cost, hours and models.
    app, _ = public_app
    client = app.test_client()
    login(client)

    def get(query):
        return client.get(
            "/api/model-efficiency" + query, base_url="https://localhost"
        ).get_json()

    agent = get("?agent=A2")
    assert [m["model"] for m in agent["models"]] == ["m-shared"]
    assert agent["cost_usd"] == pytest.approx(0.002)
    assert agent["active_hours"] == pytest.approx(1.0)
    assert agent["weighted_efficiency"] == pytest.approx(0.0008)
    model = get("?model=m-shared")
    assert [m["model"] for m in model["models"]] == ["m-shared"]
    assert model["weighted_efficiency"] == pytest.approx(0.0008)
    window = get("?from=2026-01-01T10%3A00Z&to=2026-01-01T10%3A30Z")
    assert window["cost_usd"] == pytest.approx(0.00125)
    assert window["active_hours"] == pytest.approx(1.0)
    combined = get("?from=2026-01-01T10%3A00Z&to=2026-01-01T10%3A30Z"
                   "&project=P1&agent=A2&model=m-shared")
    assert [m["model"] for m in combined["models"]] == ["m-shared"]
    assert combined["active_hours"] == pytest.approx(0.5)
    assert combined["weighted_efficiency"] == pytest.approx(0.0016)


def test_model_efficiency_keeps_model_less_share_behind_auth(public_app):
    # FAN-1247: the app reads the migrated owner tenant DB, so the mixed
    # known/model-null fixture is seeded there.
    app, config = public_app
    conn = connect(config.tenant_db_path(config.publish_tenant_id))
    seed_model_less_fixture(conn)
    conn.close()
    client = app.test_client()
    login(client)

    def get(query):
        return client.get(
            "/api/model-efficiency" + query, base_url="https://localhost"
        ).get_json()

    mixed = get("?from=2026-01-04&to=2026-01-04&project=P3")
    assert [m["model"] for m in mixed["models"]] == ["m-claude", None]
    assert mixed["unpriced_tokens"] == 500
    assert mixed["has_unpriced"] is True
    assert mixed["active_hours"] == pytest.approx(2.0)
    assert mixed["cost_per_sp"] == pytest.approx(0.00025)  # priced 2 SP (FAN-1188)
    assert mixed["weighted_efficiency"] is None
    null_only = get("?agent=A5")
    assert [m["model"] for m in null_only["models"]] == [None]
    assert null_only["cost_per_sp"] is None
    assert null_only["weighted_efficiency"] is None
    assert null_only["unpriced_tokens"] == 500
    exact = get("?project=P3")
    assert [m["model"] for m in exact["models"]] == ["m-claude", None]
    assert exact["cost_per_sp"] == pytest.approx(0.00025)
    assert exact["weighted_efficiency"] is None


def test_summary_estimation_flags_behind_auth(public_app):
    app, _ = public_app
    client = app.test_client()
    login(client)
    data = client.get(
        "/api/summary?model=m-shared", base_url="https://localhost"
    ).get_json()
    # FAN-1241: exact model tokens, run-share attributed SP and tokens/SP.
    assert data["estimated"] is False
    assert data["sp_estimated"] is True
    assert data["efficiency_estimated"] is True
    assert data["story_points"] == pytest.approx(2.5)
    assert data["tokens_per_sp"] == pytest.approx(300.0)
    exact = client.get("/api/summary", base_url="https://localhost").get_json()
    assert exact["sp_estimated"] is False
    assert exact["efficiency_estimated"] is False


def test_ingest_rejects_bad_signature_and_invalid_database(public_app):
    app, config = public_app
    client = app.test_client()
    timestamp = int(time.time())
    payload = b"not a gzip snapshot"
    assert client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Signature": "v1=bad",
        },
    ).status_code == 401

    signature = snapshot_signature(
        INGEST_SECRET, config.publish_tenant_id, timestamp + 1, payload
    )
    invalid = client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp + 1),
            "X-AIStat-Tenant": str(config.publish_tenant_id),
            "X-AIStat-Signature": signature,
        },
    )
    assert invalid.status_code == 422

    valid_source = config.tenants_dir.parent / "valid-after-invalid.db"
    conn = connect(valid_source)
    init_db(conn)
    # A non-degrading snapshot: seeded so it does not move the tenant's usage
    # backwards and trip the FAN-1442 data-freshness guard (this test asserts
    # signature/validity handling, not freshness).
    seed_aggregate_fixture(conn)
    conn.close()
    valid_payload = create_compressed_snapshot(valid_source)
    accepted = client.post(
        "/api/ingest/snapshot",
        data=valid_payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp + 1),
            "X-AIStat-Tenant": str(config.publish_tenant_id),
            "X-AIStat-Signature": snapshot_signature(
                INGEST_SECRET,
                config.publish_tenant_id,
                timestamp + 1,
                valid_payload,
            ),
        },
    )
    assert accepted.status_code == 200


def test_ingest_isolated_between_tenants_and_rejects_unknown(
    public_app, tmp_path
):
    app, config = public_app
    store = SecurityStore(config.security_db_path)
    bob_id = store.find_or_create_user_by_identity(
        "google", "bob", email="bob@example.com", now=100
    )
    store.ensure_tenant(bob_id, now=100)

    source = tmp_path / "tenant-source.db"
    conn = connect(source)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.execute(
        "UPDATE daily_usage SET input_tokens = input_tokens + 222 "
        "WHERE runtime_id = 'R1'"
    )
    conn.commit()
    conn.close()
    payload = create_compressed_snapshot(source)
    timestamp = int(time.time())
    client = app.test_client()

    def upload(tenant_id, signed_tenant_id=None, at=timestamp):
        signed_tenant_id = (
            tenant_id if signed_tenant_id is None else signed_tenant_id
        )
        return client.post(
            "/api/ingest/snapshot",
            data=payload,
            content_type="application/vnd.aistat.snapshot+gzip",
            headers={
                "X-AIStat-Tenant": str(tenant_id),
                "X-AIStat-Timestamp": str(at),
                "X-AIStat-Signature": snapshot_signature(
                    INGEST_SECRET, signed_tenant_id, at, payload
                ),
            },
        )

    assert upload(config.publish_tenant_id).status_code == 200
    assert upload(bob_id).status_code == 200
    bob_path = config.tenant_db_path(bob_id)
    bob_bytes = bob_path.read_bytes()
    assert upload(config.publish_tenant_id).status_code == 409
    assert upload(bob_id, signed_tenant_id=config.publish_tenant_id).status_code == 401
    assert bob_path.read_bytes() == bob_bytes

    unknown_id = bob_id + 1000
    assert upload(unknown_id).status_code == 401
    assert not config.tenant_db_path(unknown_id).exists()

    assert upload(config.publish_tenant_id, at=timestamp + 1).status_code == 200
    assert bob_path.read_bytes() == bob_bytes


def test_ingest_age_and_size_limits(public_app, tmp_path):
    app, config = public_app
    source = tmp_path / "limits.db"
    conn = connect(source)
    init_db(conn)
    conn.close()
    payload = create_compressed_snapshot(source)
    stale_at = int(time.time()) - config.ingest_max_age_seconds - 1
    stale = app.test_client().post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Tenant": str(config.publish_tenant_id),
            "X-AIStat-Timestamp": str(stale_at),
            "X-AIStat-Signature": snapshot_signature(
                INGEST_SECRET, config.publish_tenant_id, stale_at, payload
            ),
        },
    )
    assert stale.status_code == 401

    config.max_snapshot_bytes = len(payload) - 1
    limited = create_app(config)
    limited.config.update(TESTING=True)
    oversized = limited.test_client().post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Tenant": str(config.publish_tenant_id),
            "X-AIStat-Timestamp": str(int(time.time())),
            "X-AIStat-Signature": "unused",
        },
    )
    assert oversized.status_code == 413


def test_oauth_unknown_provider_is_404(public_app):
    app, _ = public_app
    client = app.test_client()
    assert (
        client.get("/auth/nope/start", base_url="https://localhost").status_code
        == 404
    )


def test_login_page_shows_google_button(public_app):
    app, _ = public_app
    client = app.test_client()
    page = client.get("/login", base_url="https://localhost").get_data(
        as_text=True
    )
    assert "Войти / зарегистрироваться через Google" in page
    assert 'href="/auth/google/start?next=' in page


def test_login_page_shows_yandex_button(public_app):
    app, _ = public_app
    client = app.test_client()
    page = client.get("/login", base_url="https://localhost").get_data(
        as_text=True
    )
    assert "Войти / зарегистрироваться через Яндекс" in page
    assert 'href="/auth/yandex/start?next=' in page


def test_yandex_callback_registers_once_and_reuses_account(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()  # open registration
    # Yandex-shaped userinfo: id/default_email/real_name, no verified claim
    install_fake_http(
        monkeypatch,
        {
            "id": "ya-1",
            "default_email": "user@yandex.example",
            "real_name": "Юзер",
        },
    )

    def yandex_login(client):
        start = client.get(
            "/auth/yandex/start?next=/api/meta", base_url="https://localhost"
        )
        assert start.status_code == 303
        assert start.headers["Location"].startswith(
            "https://yandex.example/authorize"
        )
        state = state_from(start.headers["Location"])
        return client.get(
            "/auth/yandex/callback?state=%s&code=abc" % state,
            base_url="https://localhost",
        )

    first_client = app.test_client()
    callback = yandex_login(first_client)
    assert callback.status_code == 303
    assert callback.headers["Location"] == "/api/meta"
    # a fresh ordinary account sees its own empty tenant, not the owner's data
    assert _project_ids(first_client) == set()

    # the same Yandex subject from another browser lands in the same account
    second_client = app.test_client()
    assert yandex_login(second_client).status_code == 303
    assert _project_ids(second_client) == set()

    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT user_id FROM oauth_identities "
            "WHERE provider = ? AND subject = ?",
            ("yandex", "ya-1"),
        ).fetchall()
        users = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE email = ?",
            ("user@yandex.example",),
        ).fetchone()["n"]
    finally:
        conn.close()
    # exactly one identity row and one user: the repeat login reused them
    assert len(rows) == 1
    assert users == 1


def test_yandex_outsider_denied_under_nonempty_allowlist(
    public_app, monkeypatch
):
    app, config = public_app
    # the fixture's allow list stays active: an unlisted new Yandex subject
    # must not register even though its email counts as verified
    install_fake_http(
        monkeypatch,
        {"id": "ya-2", "default_email": "stranger@yandex.example"},
    )
    client = app.test_client()
    start = client.get("/auth/yandex/start", base_url="https://localhost")
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/yandex/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 403
    assert "Регистрация сейчас закрыта" in callback.get_data(as_text=True)
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401


def test_oauth_start_redirects_to_provider_with_state(public_app):
    app, _ = public_app
    client = app.test_client()
    response = client.get(
        "/auth/google/start?next=/api/meta", base_url="https://localhost"
    )
    assert response.status_code == 303
    location = response.headers["Location"]
    assert location.startswith("https://accounts.example/authorize")
    assert "state=" in location


def test_oauth_callback_authorized_grants_access(public_app, monkeypatch):
    app, _ = public_app
    install_fake_http(
        monkeypatch,
        {
            "sub": "g-1",
            "email": "allowed@example.com",
            "email_verified": True,
            "name": "Al",
        },
    )
    client = app.test_client()
    start = client.get(
        "/auth/google/start?next=/api/meta", base_url="https://localhost"
    )
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 303
    assert callback.headers["Location"] == "/api/meta"
    # the owner's Google login is linked to the owner tenant, so it reaches the
    # owner's own private data (not a fresh, empty account)
    response = client.get("/api/meta", base_url="https://localhost")
    assert response.status_code == 200
    assert {p["id"] for p in response.get_json()["projects"]} == {"P1", "P2"}


def test_oauth_callback_owner_with_empty_allowlist_grants_access(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()
    install_fake_http(
        monkeypatch,
        {
            "sub": "g-empty-allowlist",
            "email": "allowed@example.com",
            "email_verified": True,
        },
    )
    client = app.test_client()
    start = client.get(
        "/auth/google/start?next=/api/meta", base_url="https://localhost"
    )
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 303
    assert callback.headers["Location"] == "/api/meta"
    assert client.get("/api/meta", base_url="https://localhost").status_code == 200


@pytest.mark.parametrize(
    ("next_url", "expected_location"),
    [
        ("/api/meta?tab=security", "/api/meta?tab=security"),
        (
            "/api/meta?return=https://example.test",
            "/api/meta?return=https://example.test",
        ),
        ("https://evil.example/path", "/"),
        ("//evil.example/path", "/"),
        ("//[evil.example/path", "/"),
        ("https://[evil.example/path", "/"),
        (r"/\evil.example/path", "/"),
        ("/api\r\nevil.example", "/"),
        ("/api\x00evil.example", "/"),
        ("http:\\evil.example/path", "/"),
    ],
)
def test_oauth_callback_sanitizes_next_url_for_browser(
    public_app, monkeypatch, next_url, expected_location
):
    app, _ = public_app
    install_fake_http(
        monkeypatch,
        {"sub": "g-next", "email": "allowed@example.com", "email_verified": True},
    )
    client = app.test_client()
    start = client.get(
        "/auth/google/start?" + urlencode({"next": next_url}),
        base_url="https://localhost",
    )
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 303
    assert callback.headers.getlist("Location") == [expected_location]


def test_oauth_callback_denies_outsider_under_nonempty_allowlist(
    public_app, monkeypatch
):
    app, config = public_app
    # public_app configures a non-empty allow list, so a verified new subject
    # that is neither the owner nor allow-listed is refused registration
    install_fake_http(
        monkeypatch,
        {
            "sub": "g-2",
            "email": "stranger@example.com",
            "email_verified": True,
            "name": "S",
        },
    )
    client = app.test_client()
    start = client.get("/auth/google/start", base_url="https://localhost")
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    # closed-registration page, no session, and no allow-list detail leaked
    assert callback.status_code == 403
    body = callback.get_data(as_text=True)
    assert "Регистрация сейчас закрыта" in body
    assert "stranger@example.com" not in body
    assert "allowed@example.com" not in body
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401
    # and no user/identity/tenant row was written for the stranger
    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM oauth_identities WHERE subject = ?", ("g-2",)
        ).fetchone()
        users = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE email = ?",
            ("stranger@example.com",),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert row is None
    assert users == 0


def test_oauth_callback_unverified_owner_email_is_rejected(
    public_app, monkeypatch
):
    app, _ = public_app
    # even the owner email is refused when the provider has not verified it
    install_fake_http(
        monkeypatch,
        {"sub": "g-uv", "email": "allowed@example.com", "email_verified": False},
    )
    client = app.test_client()
    start = client.get("/auth/google/start", base_url="https://localhost")
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 400
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401


def _security_counts(config):
    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        return {
            "users": conn.execute(
                "SELECT COUNT(*) AS n FROM users"
            ).fetchone()["n"],
            "oauth_identities": conn.execute(
                "SELECT COUNT(*) AS n FROM oauth_identities"
            ).fetchone()["n"],
            "tenants": conn.execute(
                "SELECT COUNT(*) AS n FROM tenants"
            ).fetchone()["n"],
            "sessions": conn.execute(
                "SELECT COUNT(*) AS n FROM sessions"
            ).fetchone()["n"],
        }
    finally:
        conn.close()


@pytest.mark.parametrize(
    "bad_email",
    [
        "   ", "\t\n", "", "not-an-email", "a b@example.com",
        # dotted-domain / dot-structure false accepts from the QA report
        "a@b..example", "a@.b.example", "a@b.example.", "a..b@example.com",
        # non-ASCII / IDN / EAI must fail closed
        "user@exämple.com",
        # literal C1 / zero-width / bidi control code points
        "a@b\u0081.example.com",   # U+0081
        "a@b\u200bexample.com",    # U+200B
        "a@ex\u202eample.com",     # U+202E
        # LDH hyphen violation and length overflow
        "a@-bad.example.com", "a" * 65 + "@example.com",
    ],
)
def test_oauth_callback_rejects_malformed_verified_email_fail_closed(
    public_app, monkeypatch, bad_email
):
    app, config = public_app
    # a *verified* identity whose email is whitespace-only or structurally
    # malformed must fail closed exactly like any other bad login: the same
    # generic 400 page, no account rows, no session and a replay-proof state
    install_fake_http(
        monkeypatch,
        {"sub": "g-bad", "email": bad_email, "email_verified": True, "name": "B"},
    )
    client = app.test_client()
    start = client.get("/auth/google/start", base_url="https://localhost")
    state = state_from(start.headers["Location"])
    before = _security_counts(config)

    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    # the generic login-failure page (not the closed-registration 403)
    assert callback.status_code == 400
    body = callback.get_data(as_text=True)
    assert "Регистрация сейчас закрыта" not in body
    # no user/identity/tenant/session row was written by the rejected callback
    assert _security_counts(config) == before
    # and the browser is not authenticated
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401

    # the state was consumed, so replaying the same callback still fails closed
    replay = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert replay.status_code == 400
    assert _security_counts(config) == before


def test_oauth_callback_rejects_malformed_email_for_registered_subject(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()  # open registration
    # a subject registers cleanly first
    first = complete_oauth_login(
        app.test_client(),
        monkeypatch,
        {"sub": "g-known", "email": "known@example.com", "email_verified": True},
    )
    assert first.status_code == 303
    after_register = _security_counts(config)

    # the same, already-registered subject returns with a structurally malformed
    # verified email. Identity is subject-first, but validation runs before it,
    # so this fails closed with the same generic 400: no new rows, no session and
    # a replay-proof state — an existing account cannot be logged in on a bad
    # address either.
    client = app.test_client()
    install_fake_http(
        monkeypatch,
        {"sub": "g-known", "email": "a@b..example", "email_verified": True},
    )
    start = client.get("/auth/google/start", base_url="https://localhost")
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 400
    assert _security_counts(config) == after_register
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401

    # the consumed state cannot be replayed
    replay = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert replay.status_code == 400
    assert _security_counts(config) == after_register


def test_oauth_callback_stores_whitespace_padded_email_canonically(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()  # open registration
    # a valid verified email arrives wrapped in harmless whitespace
    install_fake_http(
        monkeypatch,
        {"sub": "g-pad", "email": "  New@Example.com  ", "email_verified": True},
    )
    client = app.test_client()
    start = client.get("/auth/google/start", base_url="https://localhost")
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 303
    # it is stored trimmed (case preserved) in both the user and identity rows
    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT u.email AS user_email, oi.email AS id_email "
            "FROM oauth_identities oi JOIN users u ON u.id = oi.user_id "
            "WHERE oi.subject = ?",
            ("g-pad",),
        ).fetchone()
    finally:
        conn.close()
    assert row["user_email"] == "New@Example.com"
    assert row["id_email"] == "New@Example.com"


def test_oauth_callback_rejects_forged_state(public_app):
    app, _ = public_app
    client = app.test_client()
    callback = client.get(
        "/auth/google/callback?state=forged&code=abc",
        base_url="https://localhost",
    )
    assert callback.status_code == 400
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401


def install_forbidden_http(monkeypatch):
    def forbidden_urlopen(request, timeout=None):
        raise AssertionError(
            "provider must not be contacted: " + request.full_url
        )

    monkeypatch.setattr("aistat.oauth.urlopen", forbidden_urlopen)


def test_oauth_start_sets_browser_binding_cookie(public_app):
    app, _ = public_app
    client = app.test_client()
    response = client.get(
        "/auth/google/start?next=/api/meta", base_url="https://localhost"
    )
    cookie = next(
        header
        for header in response.headers.getlist("Set-Cookie")
        if header.startswith("aistat_oauth_client=")
    )
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/auth" in cookie


def test_oauth_callback_rejects_cross_client_callback(public_app, monkeypatch):
    # login CSRF: a different browser with its own binding cookie cannot
    # complete the flow with a leaked-but-valid state, and no token exchange
    # is attempted
    app, _ = public_app
    install_forbidden_http(monkeypatch)
    initiator = app.test_client()
    initiator_start = initiator.get(
        "/auth/google/start?next=/api/meta", base_url="https://localhost"
    )
    state = state_from(initiator_start.headers["Location"])
    other_browser = app.test_client()
    other_browser.get("/auth/google/start", base_url="https://localhost")
    callback = other_browser.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 400
    assert (
        other_browser.get("/api/meta", base_url="https://localhost").status_code
        == 401
    )
    # the hijack attempt consumed the state, so it cannot be retried anywhere
    replay = initiator.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert replay.status_code == 400


def test_oauth_overlapping_starts_share_browser_binding(public_app, monkeypatch):
    app, _ = public_app
    install_fake_http(
        monkeypatch,
        {"sub": "g-tabs", "email": "allowed@example.com", "email_verified": True},
    )
    client = app.test_client()
    first = client.get("/auth/google/start", base_url="https://localhost")
    first_state = state_from(first.headers["Location"])
    first_cookie = client.get_cookie("aistat_oauth_client", path="/auth")
    second = client.get("/auth/google/start", base_url="https://localhost")
    second_state = state_from(second.headers["Location"])
    second_cookie = client.get_cookie("aistat_oauth_client", path="/auth")
    assert first_state != second_state
    assert first_cookie is not None
    assert second_cookie is not None
    assert first_cookie.value == second_cookie.value

    first_callback = client.get(
        "/auth/google/callback?state=%s&code=first" % first_state,
        base_url="https://localhost",
    )
    second_callback = client.get(
        "/auth/google/callback?state=%s&code=second" % second_state,
        base_url="https://localhost",
    )
    assert first_callback.status_code == 303
    assert second_callback.status_code == 303


def test_oauth_error_callback_burns_state(public_app, monkeypatch):
    # a provider error is terminal: the same state must not work with a code
    app, config = public_app
    install_forbidden_http(monkeypatch)
    client = app.test_client()
    start = client.get("/auth/google/start", base_url="https://localhost")
    state = state_from(start.headers["Location"])
    errored = client.get(
        "/auth/google/callback?state=%s&error=access_denied" % state,
        base_url="https://localhost",
    )
    assert errored.status_code == 400
    # The browser binding remains available for other pending flows, so the
    # replay below proves state consumption rather than merely a missing cookie.
    assert client.get_cookie("aistat_oauth_client", path="/auth") is not None
    conn = sqlite3.connect(str(config.security_db_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM oauth_state WHERE state = ?", (state,)
        ).fetchone()[0] == 0
    finally:
        conn.close()
    retry = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert retry.status_code == 400
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401


def _seed_tenant_with_project(config, user_id, project_id, title):
    path = config.tenant_db_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    init_db(conn)
    conn.execute(
        "INSERT INTO projects (id, title, status, synced_at) "
        "VALUES (?, ?, 'in_progress', '2026-01-02T00:00:00Z')",
        (project_id, title),
    )
    conn.commit()
    conn.close()


def _session_user_id(client):
    return client.get(
        "/api/session", base_url="https://localhost"
    ).get_json()["user_id"]


def _logout(client):
    csrf = client.get(
        "/api/session", base_url="https://localhost"
    ).get_json()["csrf"]
    assert client.post(
        "/logout", headers={"X-CSRF-Token": csrf}, base_url="https://localhost"
    ).status_code == 303


def test_open_registration_first_login_creates_own_empty_tenant(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()  # open registration
    client = app.test_client()
    identity = {"sub": "new-1", "email": "new@example.com", "email_verified": True}
    callback = complete_oauth_login(client, monkeypatch, identity, "/api/meta")
    assert callback.status_code == 303
    assert callback.headers["Location"] == "/api/meta"
    # a fresh ordinary account sees only its own empty tenant, never owner data
    assert _project_ids(client) == set()
    new_id = _session_user_id(client)

    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        is_admin = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (new_id,)
        ).fetchone()["is_admin"]
        has_tenant = conn.execute(
            "SELECT 1 FROM tenants WHERE user_id = ?", (new_id,)
        ).fetchone()
        admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert int(is_admin) == 0
    assert has_tenant is not None
    assert int(admins) == 1  # the owner is still the only admin

    # signing in again with the same subject returns the same account
    again = complete_oauth_login(client, monkeypatch, identity, "/api/meta")
    assert again.status_code == 303
    assert _session_user_id(client) == new_id


def test_open_registration_email_change_keeps_single_account(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()
    client = app.test_client()
    first = complete_oauth_login(
        client, monkeypatch,
        {"sub": "chg", "email": "old@example.com", "email_verified": True},
    )
    assert first.status_code == 303
    original_id = _session_user_id(client)
    # the provider later reports a different verified email for the same subject
    second = complete_oauth_login(
        client, monkeypatch,
        {"sub": "chg", "email": "brand-new@example.com", "email_verified": True},
    )
    assert second.status_code == 303
    assert _session_user_id(client) == original_id

    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        identities = conn.execute(
            "SELECT COUNT(*) AS n FROM oauth_identities WHERE subject = ?",
            ("chg",),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert identities == 1


def test_open_registration_ab_tenant_isolation(public_app, monkeypatch):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()
    a_client = app.test_client()
    b_client = app.test_client()
    # two distinct subjects that happen to share one email stay separate users
    assert complete_oauth_login(
        a_client, monkeypatch,
        {"sub": "sub-a", "email": "shared@example.com", "email_verified": True},
    ).status_code == 303
    assert complete_oauth_login(
        b_client, monkeypatch,
        {"sub": "sub-b", "email": "shared@example.com", "email_verified": True},
    ).status_code == 303
    a_id = _session_user_id(a_client)
    b_id = _session_user_id(b_client)
    assert a_id != b_id

    _seed_tenant_with_project(config, a_id, "PA", "A private project")
    # A sees only A's data; B sees neither A's project nor the owner's P1/P2
    assert _project_ids(a_client) == {"PA"}
    assert _project_ids(b_client) == set()


def test_open_registration_admin_email_links_owner_single_admin(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()
    owner_id = config.publish_tenant_id
    client = app.test_client()
    callback = complete_oauth_login(
        client, monkeypatch,
        {"sub": "owner-sub", "email": "Allowed@Example.com",
         "email_verified": True},
        "/api/meta",
    )
    assert callback.status_code == 303
    # the admin email links to the pre-existing owner: owner tenant, owner data
    assert _session_user_id(client) == owner_id
    assert _project_ids(client) == {"P1", "P2"}

    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        admins = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM users WHERE is_admin = 1"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert admins == [owner_id]


def test_open_registration_ordinary_subject_not_elevated_by_admin_email(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()
    owner_id = config.publish_tenant_id
    client = app.test_client()
    # an ordinary subject registers with its own email
    assert complete_oauth_login(
        client, monkeypatch,
        {"sub": "not-owner", "email": "person@example.com",
         "email_verified": True},
    ).status_code == 303
    ordinary_id = _session_user_id(client)
    assert ordinary_id != owner_id

    # the same subject later presents the admin email — it must NOT be merged
    # into or elevated to the owner; it stays its own ordinary empty account
    assert complete_oauth_login(
        client, monkeypatch,
        {"sub": "not-owner", "email": "allowed@example.com",
         "email_verified": True},
        "/api/meta",
    ).status_code == 303
    assert _session_user_id(client) == ordinary_id
    assert _project_ids(client) == set()  # not the owner's P1/P2

    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        is_admin = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (ordinary_id,)
        ).fetchone()["is_admin"]
        admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert int(is_admin) == 0
    assert int(admins) == 1


def test_open_registration_simultaneous_callbacks_one_account(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()
    identity = {"sub": "sim", "email": "sim@example.com", "email_verified": True}
    install_fake_http(monkeypatch, identity)
    client = app.test_client()
    # two overlapping flows for the same brand-new subject (shared binding)
    first_start = client.get("/auth/google/start", base_url="https://localhost")
    second_start = client.get("/auth/google/start", base_url="https://localhost")
    first_state = state_from(first_start.headers["Location"])
    second_state = state_from(second_start.headers["Location"])
    first = client.get(
        "/auth/google/callback?state=%s&code=a" % first_state,
        base_url="https://localhost",
    )
    second = client.get(
        "/auth/google/callback?state=%s&code=b" % second_state,
        base_url="https://localhost",
    )
    assert first.status_code == 303
    assert second.status_code == 303

    store = SecurityStore(config.security_db_path)
    conn = store._connect()
    try:
        users = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE email = ?",
            ("sim@example.com",),
        ).fetchone()["n"]
        identities = conn.execute(
            "SELECT COUNT(*) AS n FROM oauth_identities WHERE subject = ?",
            ("sim",),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert users == 1
    assert identities == 1


def test_open_registration_registered_user_keeps_access_after_delisting(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()  # open at registration time
    client = app.test_client()
    assert complete_oauth_login(
        client, monkeypatch,
        {"sub": "keeper", "email": "keeper@example.com", "email_verified": True},
    ).status_code == 303
    assert _project_ids(client) == set()  # registered, has access

    # an operator now configures a non-empty allow list that excludes them.
    config.oauth_allowed_emails = frozenset({"someone@else.com"})
    # the already-registered user keeps access — there is no request-time gate
    assert client.get(
        "/api/meta", base_url="https://localhost"
    ).status_code == 200
    # but a brand-new outsider can no longer register
    outsider = app.test_client()
    denied = complete_oauth_login(
        outsider, monkeypatch,
        {"sub": "late", "email": "late@example.com", "email_verified": True},
    )
    assert denied.status_code == 403
    assert outsider.get(
        "/api/meta", base_url="https://localhost"
    ).status_code == 401


def test_open_registration_logout_to_other_user_no_leak(
    public_app, monkeypatch
):
    app, config = public_app
    config.oauth_allowed_emails = frozenset()
    client = app.test_client()
    assert complete_oauth_login(
        client, monkeypatch,
        {"sub": "user-a", "email": "a@example.com", "email_verified": True},
    ).status_code == 303
    a_id = _session_user_id(client)
    _seed_tenant_with_project(config, a_id, "PA", "A private project")
    assert _project_ids(client) == {"PA"}

    _logout(client)
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401

    # a different user logging in on the same browser never inherits A's data
    assert complete_oauth_login(
        client, monkeypatch,
        {"sub": "user-b", "email": "b@example.com", "email_verified": True},
    ).status_code == 303
    assert _session_user_id(client) != a_id
    assert _project_ids(client) == set()


def test_password_login_unaffected_by_oauth(public_app):
    app, _ = public_app
    client = app.test_client()
    assert login(client).status_code == 303
    assert client.get("/api/meta", base_url="https://localhost").status_code == 200


# --- FAN-1366: crash-atomic snapshot install + replay watermark -----------


class _Boom(Exception):
    """Stand-in for a process crash at a chosen point in the ingest flow."""


def _snapshot_payload(tmp_path, name, bump):
    source = tmp_path / name
    conn = connect(source)
    init_db(conn)
    seed_aggregate_fixture(conn)
    if bump:
        conn.execute(
            "UPDATE daily_usage SET input_tokens = input_tokens + ? "
            "WHERE runtime_id = 'R1'",
            (bump,),
        )
        conn.commit()
    conn.close()
    payload = create_compressed_snapshot(source)
    return payload, hashlib.sha256(gzip.decompress(payload)).hexdigest()


def _ingest(client, config, payload, timestamp):
    return client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Tenant": str(config.publish_tenant_id),
            "X-AIStat-Signature": snapshot_signature(
                INGEST_SECRET, config.publish_tenant_id, timestamp, payload
            ),
        },
    )


def _tenant_sha(config):
    return hashlib.sha256(
        config.tenant_db_path(config.publish_tenant_id).read_bytes()
    ).hexdigest()


def _watermark(config):
    store = SecurityStore(config.security_db_path)
    return int(store.get_tenant(config.publish_tenant_id)["last_ingest_timestamp"])


def _journal_count(config):
    conn = sqlite3.connect(str(config.security_db_path))
    try:
        return conn.execute(
            "SELECT count(*) FROM snapshot_install_journal"
        ).fetchone()[0]
    finally:
        conn.close()


def test_ingest_crash_after_swap_before_watermark_recovers_on_restart(
    public_app, tmp_path, monkeypatch
):
    app, config = public_app
    old_sha = _tenant_sha(config)
    old_wm = _watermark(config)
    payload, new_sha = _snapshot_payload(tmp_path, "new.db", 1_000_000)
    timestamp = int(time.time())

    monkeypatch.setattr(
        SecurityStore,
        "finish_snapshot_install",
        lambda *a, **k: (_ for _ in ()).throw(_Boom()),
    )
    with pytest.raises(_Boom):
        _ingest(app.test_client(), config, payload, timestamp)

    # Durable mixed state: file swapped to NEW, watermark still OLD.
    assert _tenant_sha(config) == new_sha
    assert _watermark(config) == old_wm
    assert _journal_count(config) == 1
    monkeypatch.undo()

    # Restart: create_app runs recovery under the ingest lock.
    restarted = create_app(config)
    assert _tenant_sha(config) == new_sha
    assert _watermark(config) == timestamp
    assert _journal_count(config) == 0

    client = restarted.test_client()
    assert _ingest(client, config, payload, timestamp).status_code == 409
    assert login(client).status_code == 303
    summary = client.get("/api/summary", base_url="https://localhost").get_json()
    assert summary["total_tokens"] == 5_700_000


def test_ingest_crash_after_journal_before_swap_recovers_on_restart(
    public_app, tmp_path, monkeypatch
):
    app, config = public_app
    old_sha = _tenant_sha(config)
    old_wm = _watermark(config)
    payload, new_sha = _snapshot_payload(tmp_path, "new.db", 1_000_000)
    timestamp = int(time.time())

    monkeypatch.setattr(
        "aistat.wsgi.swap_staged_into_place",
        lambda *a, **k: (_ for _ in ()).throw(_Boom()),
    )
    with pytest.raises(_Boom):
        _ingest(app.test_client(), config, payload, timestamp)

    # Tenant DB untouched, but the intent is journalled and staged intact.
    assert _tenant_sha(config) == old_sha
    assert _watermark(config) == old_wm
    assert _journal_count(config) == 1
    monkeypatch.undo()

    create_app(config)
    assert _tenant_sha(config) == new_sha
    assert _watermark(config) == timestamp
    assert _journal_count(config) == 0


def test_ingest_swap_failure_rolls_back_in_request(
    public_app, tmp_path, monkeypatch
):
    app, config = public_app
    old_sha = _tenant_sha(config)
    old_wm = _watermark(config)
    payload, _new_sha = _snapshot_payload(tmp_path, "new.db", 1_000_000)
    timestamp = int(time.time())

    monkeypatch.setattr(
        "aistat.wsgi.swap_staged_into_place",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    response = _ingest(app.test_client(), config, payload, timestamp)
    assert response.status_code == 422

    # Rolled back in-request: old snapshot, old watermark, no journal, no leak.
    assert _tenant_sha(config) == old_sha
    assert _watermark(config) == old_wm
    assert _journal_count(config) == 0
    leftovers = list(config.tenants_dir.glob(".aistat-snapshot-*.db"))
    assert leftovers == []


def test_ingest_rejects_symlink_target_without_touching_state(
    public_app, tmp_path
):
    app, config = public_app
    old_wm = _watermark(config)
    target = config.tenant_db_path(config.publish_tenant_id)
    real = target.with_name("real.db")
    target.replace(real)
    target.symlink_to(real)

    payload, _new_sha = _snapshot_payload(tmp_path, "new.db", 1_000_000)
    timestamp = int(time.time())
    response = _ingest(app.test_client(), config, payload, timestamp)
    assert response.status_code == 422
    assert target.is_symlink()
    assert _watermark(config) == old_wm
    assert _journal_count(config) == 0


def test_concurrent_same_tenant_ingests_are_serialized(public_app, tmp_path):
    app, config = public_app
    low_payload, _ = _snapshot_payload(tmp_path, "low.db", 1_000_000)
    high_payload, high_sha = _snapshot_payload(tmp_path, "high.db", 2_000_000)
    base = int(time.time())
    low_ts, high_ts = base, base + 1

    barrier = threading.Barrier(2)
    results = {}

    def run(name, payload, ts):
        client = app.test_client()
        barrier.wait()
        results[name] = _ingest(client, config, payload, ts).status_code

    threads = [
        threading.Thread(target=run, args=("low", low_payload, low_ts)),
        threading.Thread(target=run, args=("high", high_payload, high_ts)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Whatever the interleaving, the higher timestamp wins and the file is a
    # whole, untorn snapshot — never a mix of the two.
    assert results["high"] == 200
    assert results["low"] in (200, 409)
    assert _tenant_sha(config) == high_sha
    assert _watermark(config) == high_ts
    assert _journal_count(config) == 0

    client = app.test_client()
    assert login(client).status_code == 303
    summary = client.get("/api/summary", base_url="https://localhost").get_json()
    assert summary["total_tokens"] == 6_700_000
