"""Dependency-free cPanel WSGI tests."""

import ast
import base64
import gzip
import hashlib
import hmac
import importlib
import io
import json
import os
import re
import runpy
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

import pytest
from werkzeug.security import generate_password_hash

from aistat.db import SCHEMA_VERSION, connect, init_db
from aistat.config import Config
from aistat.migrate import migrate_owner_database
from aistat.security import SecurityStore, snapshot_signature
from aistat.snapshot import create_compressed_snapshot, daily_usage_max_date
from conftest import (
    assert_opaque_session_cookie,
    seed_aggregate_fixture,
    seed_model_less_fixture,
)

PASSWORD = "correct horse battery staple"
SESSION_SECRET = "legacy-session-" + "s" * 48
INGEST_SECRET = "legacy-ingest-" + "i" * 48


def configure_legacy_env(tmp_path, monkeypatch, allowed_emails="allowed@example.com"):
    monkeypatch.setenv("AISTAT_DB_PATH", str(tmp_path / "public.db"))
    monkeypatch.setenv("AISTAT_SECURITY_DB_PATH", str(tmp_path / "security.db"))
    monkeypatch.setenv("AISTAT_TENANTS_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("AISTAT_ALLOWED_HOSTS", "localhost,aistat.app")
    monkeypatch.setenv("AISTAT_FORCE_HTTPS", "0")
    monkeypatch.setenv("AISTAT_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("AISTAT_ADMIN_USERNAME", "sergey")
    monkeypatch.setenv(
        "AISTAT_PASSWORD_HASH",
        generate_password_hash(PASSWORD, method="pbkdf2:sha256:600000"),
    )
    monkeypatch.setenv("AISTAT_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("AISTAT_INGEST_SECRET", INGEST_SECRET)
    monkeypatch.setenv("AISTAT_OAUTH_PROVIDERS", "google,yandex")
    monkeypatch.setenv(
        "AISTAT_OAUTH_GOOGLE_AUTHORIZE_URL", "https://accounts.example/authorize"
    )
    monkeypatch.setenv("AISTAT_OAUTH_GOOGLE_TOKEN_URL", "https://oauth.example/token")
    monkeypatch.setenv(
        "AISTAT_OAUTH_GOOGLE_USERINFO_URL", "https://api.example/userinfo"
    )
    monkeypatch.setenv("AISTAT_OAUTH_GOOGLE_SCOPES", "openid email profile")
    monkeypatch.setenv("AISTAT_OAUTH_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AISTAT_OAUTH_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "AISTAT_OAUTH_GOOGLE_REDIRECT_URI", "https://localhost/auth/google/callback"
    )
    # Yandex ID mirrors the documented production setup: same generic schema
    # plus the explicit opt-in for its claimless-but-confirmed userinfo email.
    monkeypatch.setenv(
        "AISTAT_OAUTH_YANDEX_AUTHORIZE_URL", "https://yandex.example/authorize"
    )
    monkeypatch.setenv("AISTAT_OAUTH_YANDEX_TOKEN_URL", "https://oauth.example/token")
    monkeypatch.setenv(
        "AISTAT_OAUTH_YANDEX_USERINFO_URL", "https://api.example/userinfo"
    )
    monkeypatch.setenv("AISTAT_OAUTH_YANDEX_SCOPES", "login:email login:info")
    monkeypatch.setenv("AISTAT_OAUTH_YANDEX_CLIENT_ID", "ya-client-id")
    monkeypatch.setenv("AISTAT_OAUTH_YANDEX_CLIENT_SECRET", "ya-client-secret")
    monkeypatch.setenv(
        "AISTAT_OAUTH_YANDEX_REDIRECT_URI", "https://localhost/auth/yandex/callback"
    )
    monkeypatch.setenv("AISTAT_OAUTH_YANDEX_ASSUME_EMAIL_VERIFIED", "1")
    monkeypatch.setenv("AISTAT_OAUTH_ALLOWED_EMAILS", allowed_emails)
    monkeypatch.setenv("AISTAT_ADMIN_EMAIL", "allowed@example.com")


def _boot_legacy(tmp_path):
    import aistat.legacy_wsgi as module

    module = importlib.reload(module)
    conn = connect(module.DB_PATH)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()
    migrate_owner_database(Config(), now=1000)
    return module


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    configure_legacy_env(tmp_path, monkeypatch)
    return _boot_legacy(tmp_path)


@pytest.fixture
def legacy_open(tmp_path, monkeypatch):
    # empty allow list => open registration for any verified Google user
    configure_legacy_env(tmp_path, monkeypatch, allowed_emails="")
    return _boot_legacy(tmp_path)


def request(app, path, method="GET", body=b"", headers=None, cookie=None):
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "HTTP_HOST": "localhost",
            "HTTPS": "on",
            "wsgi.url_scheme": "https",
            "REMOTE_ADDR": "127.0.0.1",
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
        }
    )
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    for key, value in (headers or {}).items():
        normalized = key.upper().replace("-", "_")
        if normalized == "CONTENT_TYPE":
            environ["CONTENT_TYPE"] = value
        else:
            environ["HTTP_" + normalized] = value
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = response_headers

    response_body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], response_body


def header_values(headers, name):
    return [value for key, value in headers if key.lower() == name.lower()]


def cookie_jar(headers, existing=""):
    values = {}
    if existing:
        for part in existing.split("; "):
            name, value = part.split("=", 1)
            values[name] = value
    for header in header_values(headers, "Set-Cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        for name, morsel in cookie.items():
            if int(morsel["max-age"] or "1") == 0:
                values.pop(name, None)
            else:
                values[name] = morsel.value
    return "; ".join("{}={}".format(k, v) for k, v in values.items())


def login(module):
    status, headers, page = request(module.application, "/login")
    assert status == "200 OK"
    csrf = re.search(
        rb'name="csrf" value="([^"]+)"', page
    ).group(1).decode("ascii")
    cookies = cookie_jar(headers)
    body = urlencode(
        {
            "csrf": csrf,
            "username": "sergey",
            "password": PASSWORD,
            "next": "/",
        }
    ).encode("utf-8")
    status, headers, _ = request(
        module.application,
        "/login",
        method="POST",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cookie=cookies,
    )
    assert status == "303 See Other"
    return cookie_jar(headers, cookies)


def test_source_parses_as_python_36():
    # legacy_wsgi imports oauth at runtime on cPanel's Python 3.6, so the
    # shared core must parse as 3.6 too.
    for path in (
        "aistat/aggregates.py",
        "aistat/endpoints.py",
        "aistat/handoff.py",
        "aistat/legacy_wsgi.py",
        "aistat/migrate.py",
        "aistat/oauth.py",
        "aistat/snapshot.py",
        "aistat/snapshot_recovery.py",
        "aistat/tenant.py",
        "aistat.cgi",
    ):
        source = open(path, encoding="utf-8").read()
        ast.parse(source, filename=path, feature_version=(3, 6))


def test_ingest_rejects_snapshot_with_older_usage_data(legacy, tmp_path):
    """FAN-1442 on the production (legacy 3.6 CGI) contour: a stale snapshot
    with a fresh timestamp must be refused by the data-freshness guard, leaving
    the tenant database untouched; a newer snapshot still installs."""
    module = legacy
    owner_id = migrate_owner_database(Config())["owner_user_id"]
    owner_path = Config().tenant_db_path(owner_id)

    def build(mutate_sql=None):
        src = tmp_path / "legacy_ingest_src.db"
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
        return request(
            module.application,
            "/api/ingest/snapshot",
            method="POST",
            body=payload,
            headers={
                "Content-Type": "application/vnd.aistat.snapshot+gzip",
                "X-AIStat-Timestamp": str(ts),
                "X-AIStat-Tenant": str(owner_id),
                "X-AIStat-Signature": snapshot_signature(
                    INGEST_SECRET, owner_id, ts, payload
                ),
            },
        )

    base_ts = int(time.time())
    # Baseline: owner tenant already holds the fixture (latest day 2026-01-02);
    # re-installing the same latest day is accepted.
    assert post(build(), base_ts)[0] == "200 OK"
    assert daily_usage_max_date(owner_path) == "2026-01-02"
    baseline = owner_path.read_bytes()

    # Same latest day but one runtime/model row missing: rejecting it must not
    # touch the tenant database.
    degraded = build(
        "DELETE FROM daily_usage WHERE runtime_id = 'R2' "
        "AND model = 'm-mystery' AND date = '2026-01-02';"
    )
    status, _, _ = post(degraded, base_ts + 10)
    assert status == "409 Conflict"
    assert owner_path.read_bytes() == baseline

    lower = build(
        "UPDATE daily_usage SET input_tokens = input_tokens - 1 "
        "WHERE runtime_id = 'R4' AND model = 'm-claude' "
        "AND date = '2026-01-02';"
    )
    status, _, _ = post(lower, base_ts + 20)
    assert status == "409 Conflict"
    assert owner_path.read_bytes() == baseline

    # Stale snapshot (older max date) with a strictly newer timestamp: rejected.
    stale = build("DELETE FROM daily_usage WHERE date = '2026-01-02';")
    status, _, _ = post(stale, base_ts + 30)
    assert status == "409 Conflict"
    assert owner_path.read_bytes() == baseline

    # A genuinely newer snapshot still installs.
    fresh = build(
        "INSERT INTO daily_usage (runtime_id, model, date, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, "
        "cost_credits, cost_priced, synced_at) VALUES "
        "('R1', 'm-claude', '2026-01-03', 1, 0, 0, 0, NULL, NULL, 0, "
        "'2026-01-03T00:00:00Z');"
    )
    assert post(fresh, base_ts + 40)[0] == "200 OK"
    assert daily_usage_max_date(owner_path) == "2026-01-03"


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


def state_from(headers):
    location = header_values(headers, "Location")[0]
    return parse_qs(urlsplit(location).query)["state"][0]


def oauth_login(module, monkeypatch, identity, next_url="/", cookie=None):
    """Drive a full mock-provider Google login through the real legacy app.

    Only the provider's HTTPS egress is stubbed; the whole redirect loop, the
    browser-binding cookie and every policy/store write run for real. Returns
    ``(status, headers, body, cookies)``.
    """
    install_fake_http(monkeypatch, identity)
    status, headers, _ = request(
        module.application,
        "/auth/google/start?" + urlencode({"next": next_url}),
        cookie=cookie,
    )
    state = state_from(headers)
    start_cookies = cookie_jar(headers, cookie or "")
    status, headers, body = request(
        module.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    return status, headers, body, cookie_jar(headers, start_cookies)


def meta_projects(module, cookies):
    status, _, body = request(module.application, "/api/meta", cookie=cookies)
    assert status == "200 OK"
    return {p["id"] for p in module.json.loads(body.decode("utf-8"))["projects"]}


def user_id_for_subject(module, subject):
    conn = sqlite3.connect(module.SECURITY_DB_PATH)
    try:
        row = conn.execute(
            "SELECT user_id FROM oauth_identities WHERE subject = ?", (subject,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def account_counts_for(module, subject, email):
    conn = sqlite3.connect(module.SECURITY_DB_PATH)
    try:
        return {
            "users": conn.execute(
                "SELECT COUNT(*) FROM users WHERE email = ?", (email,)
            ).fetchone()[0],
            "identities": conn.execute(
                "SELECT COUNT(*) FROM oauth_identities WHERE subject = ?",
                (subject,),
            ).fetchone()[0],
        }
    finally:
        conn.close()


def seed_legacy_tenant(module, user_id, project_id, title):
    path = os.path.join(module.TENANTS_DIR, "%d.db" % int(user_id))
    conn = connect(path)
    init_db(conn)
    conn.execute(
        "INSERT INTO projects (id, title, status, synced_at) "
        "VALUES (?, ?, 'in_progress', '2026-01-02T00:00:00Z')",
        (project_id, title),
    )
    conn.commit()
    conn.close()


def test_cgi_loads_only_aistat_private_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "aistat.env"
    env_file.write_text(
        "# production settings\n"
        "AISTAT_ALLOWED_HOSTS=aistat.app\n"
        "UNRELATED_SECRET=must-not-load\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AISTAT_CGI_ENV_FILE", str(env_file))
    monkeypatch.delenv("AISTAT_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)

    namespace = runpy.run_path("aistat.cgi", run_name="aistat_cgi_test")
    namespace["_load_private_environment"]()

    assert os.environ["AISTAT_ALLOWED_HOSTS"] == "aistat.app"
    assert "UNRELATED_SECRET" not in os.environ


def test_cgi_drops_attacker_controlled_proxy_header(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid")
    namespace = runpy.run_path("aistat.cgi", run_name="aistat_cgi_test")

    namespace["_drop_untrusted_cgi_proxy"]()

    assert "HTTP_PROXY" not in os.environ


def test_login_api_and_security_headers(legacy):
    status, _, _ = request(legacy.application, "/api/meta")
    assert status == "401 Unauthorized"
    cookies = login(legacy)
    status, headers, body = request(
        legacy.application, "/api/meta", cookie=cookies
    )
    assert status == "200 OK"
    data = legacy.json.loads(body.decode("utf-8"))
    assert [p["title"] for p in data["projects"]] == ["Alpha", "Beta"]
    assert header_values(headers, "X-Frame-Options") == ["DENY"]
    assert "Secure" in cookies or "aistat_session=" in cookies


def test_model_efficiency_endpoint(legacy):
    status, _, _ = request(legacy.application, "/api/model-efficiency")
    assert status == "401 Unauthorized"
    cookies = login(legacy)
    status, _, body = request(
        legacy.application, "/api/model-efficiency", cookie=cookies
    )
    assert status == "200 OK"
    data = legacy.json.loads(body.decode("utf-8"))
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]
    assert abs(data["cost_per_sp"] - 0.0005) < 1e-9


def test_efficiency_breakdown_endpoint(legacy):
    status, _, _ = request(legacy.application, "/api/efficiency-breakdown")
    assert status == "401 Unauthorized"
    cookies = login(legacy)
    status, _, body = request(
        legacy.application,
        "/api/efficiency-breakdown?from=2026-01-01T10%3A00Z"
        "&to=2026-01-01T10%3A30Z&agent=A2&model=m-shared",
        cookie=cookies,
    )
    assert status == "200 OK"
    data = legacy.json.loads(body.decode("utf-8"))
    assert data["time"]["granularity"] == "hour"
    assert data["time"]["rows"][0]["total_tokens"] == 375


def test_projects_filtered_cost_matches_model_efficiency(legacy):
    # FAN-1251: /api/projects and /api/model-efficiency must agree ($0.002)
    # for the combined project+agent+model+time filter.
    cookies = login(legacy)

    def get(path):
        status, _, body = request(legacy.application, path, cookie=cookies)
        assert status == "200 OK"
        return legacy.json.loads(body.decode("utf-8"))

    query = ("?from=2026-01-01T10%3A00Z&to=2026-01-01T11%3A00Z"
             "&project=P1&agent=A2&model=m-shared")
    projects = get("/api/projects" + query)["projects"]
    alpha = {p["title"]: p for p in projects}["Alpha"]
    assert alpha["total_tokens"] == 750
    assert abs(alpha["cost_usd"] - 0.002) < 1e-9
    eff = get("/api/model-efficiency" + query)
    assert abs(eff["cost_usd"] - 0.002) < 1e-9


def test_model_efficiency_filters(legacy):
    # FAN-1244: one filtered run-overlap set for cost, hours and models.
    cookies = login(legacy)

    def get(query):
        status, _, body = request(
            legacy.application, "/api/model-efficiency" + query, cookie=cookies
        )
        assert status == "200 OK"
        return legacy.json.loads(body.decode("utf-8"))

    agent = get("?agent=A2")
    assert [m["model"] for m in agent["models"]] == ["m-shared"]
    assert abs(agent["cost_usd"] - 0.002) < 1e-9
    assert abs(agent["active_hours"] - 1.0) < 1e-9
    assert abs(agent["weighted_efficiency"] - 0.0008) < 1e-9
    model = get("?model=m-shared")
    assert [m["model"] for m in model["models"]] == ["m-shared"]
    assert abs(model["weighted_efficiency"] - 0.0008) < 1e-9
    window = get("?from=2026-01-01T10%3A00Z&to=2026-01-01T10%3A30Z")
    assert abs(window["cost_usd"] - 0.00125) < 1e-9
    assert abs(window["active_hours"] - 1.0) < 1e-9
    combined = get("?from=2026-01-01T10%3A00Z&to=2026-01-01T10%3A30Z"
                   "&project=P1&agent=A2&model=m-shared")
    assert [m["model"] for m in combined["models"]] == ["m-shared"]
    assert abs(combined["active_hours"] - 0.5) < 1e-9
    assert abs(combined["weighted_efficiency"] - 0.0016) < 1e-9


def test_model_efficiency_keeps_model_less_share(legacy):
    # FAN-1247: requests read the migrated owner tenant DB — the only tenant
    # the fixture creates — so the mixed fixture is seeded there.
    tenant_dbs = [
        os.path.join(legacy.TENANTS_DIR, name)
        for name in os.listdir(legacy.TENANTS_DIR) if name.endswith(".db")
    ]
    assert len(tenant_dbs) == 1
    conn = connect(tenant_dbs[0])
    seed_model_less_fixture(conn)
    conn.close()
    cookies = login(legacy)

    def get(query):
        status, _, body = request(
            legacy.application, "/api/model-efficiency" + query, cookie=cookies
        )
        assert status == "200 OK"
        return legacy.json.loads(body.decode("utf-8"))

    mixed = get("?from=2026-01-04&to=2026-01-04&project=P3")
    assert [m["model"] for m in mixed["models"]] == ["m-claude", None]
    assert mixed["unpriced_tokens"] == 500
    assert mixed["has_unpriced"] is True
    assert abs(mixed["active_hours"] - 2.0) < 1e-6
    assert abs(mixed["cost_per_sp"] - 0.00025) < 1e-9  # priced 2 SP (FAN-1188)
    assert mixed["weighted_efficiency"] is None
    null_only = get("?agent=A5")
    assert [m["model"] for m in null_only["models"]] == [None]
    assert null_only["cost_per_sp"] is None
    assert null_only["weighted_efficiency"] is None
    assert null_only["unpriced_tokens"] == 500
    exact = get("?project=P3")
    assert [m["model"] for m in exact["models"]] == ["m-claude", None]
    assert abs(exact["cost_per_sp"] - 0.00025) < 1e-9
    assert exact["weighted_efficiency"] is None


def test_summary_estimation_flags(legacy):
    cookies = login(legacy)
    status, _, body = request(
        legacy.application, "/api/summary?model=m-shared", cookie=cookies
    )
    assert status == "200 OK"
    data = legacy.json.loads(body.decode("utf-8"))
    # FAN-1241: exact model tokens, run-share attributed SP and tokens/SP.
    assert data["estimated"] is False
    assert data["sp_estimated"] is True
    assert data["efficiency_estimated"] is True
    assert abs(data["story_points"] - 2.5) < 1e-9
    assert abs(data["tokens_per_sp"] - 300.0) < 1e-9
    status, _, body = request(legacy.application, "/api/summary", cookie=cookies)
    assert status == "200 OK"
    exact = legacy.json.loads(body.decode("utf-8"))
    assert exact["sp_estimated"] is False
    assert exact["efficiency_estimated"] is False


def test_legacy_hour_filters_accept_repeated_dimensions(legacy):
    cookies = login(legacy)
    status, _, body = request(
        legacy.application,
        "/api/summary?from=2026-01-01T10%3A00Z&to=2026-01-01T11%3A00Z"
        "&project=P1&agent=A2&model=m-shared",
        cookie=cookies,
    )
    assert status == "200 OK"
    assert legacy.json.loads(body.decode("utf-8"))["total_tokens"] == 600000


def test_legacy_agents_count_only_overlapping_hour_runs(legacy):
    cookies = login(legacy)
    status, _, body = request(
        legacy.application,
        "/api/agents?from=2026-01-01T10%3A00Z&to=2026-01-01T11%3A00Z"
        "&project=P1&agent=A2&model=m-shared",
        cookie=cookies,
    )
    assert status == "200 OK"
    agents = legacy.json.loads(body.decode("utf-8"))["agents"]
    assert {agent["agent_id"]: agent["runs"] for agent in agents} == {"A2": 1}


def test_legacy_agent_count_and_worktime(legacy):
    cookies = login(legacy)
    status, _, body = request(legacy.application, "/api/summary", cookie=cookies)
    assert status == "200 OK"
    s = legacy.json.loads(body.decode("utf-8"))
    assert s["agent_count"] == 3
    assert s["agent_work_seconds"] == 21600
    status, _, body = request(legacy.application, "/api/agents", cookie=cookies)
    assert status == "200 OK"
    agents = legacy.json.loads(body.decode("utf-8"))["agents"]
    assert sum(a["work_seconds"] for a in agents) == s["agent_work_seconds"]
    assert sum(1 for a in agents if a["work_seconds"] > 0) == s["agent_count"]


def test_logout_requires_csrf(legacy):
    cookies = login(legacy)
    status, _, body = request(
        legacy.application, "/api/session", cookie=cookies
    )
    csrf = legacy.json.loads(body.decode("utf-8"))["csrf"]
    status, _, _ = request(
        legacy.application,
        "/logout",
        method="POST",
        headers={"X-CSRF-Token": "wrong"},
        cookie=cookies,
    )
    assert status == "400 Bad Request"
    status, headers, _ = request(
        legacy.application,
        "/logout",
        method="POST",
        headers={"X-CSRF-Token": csrf},
        cookie=cookies,
    )
    assert status == "303 See Other"
    assert "Max-Age=0" in "\n".join(header_values(headers, "Set-Cookie"))


def test_logout_accepts_form_csrf_for_shared_host_waf(legacy):
    cookies = login(legacy)
    status, _, body = request(
        legacy.application, "/api/session", cookie=cookies
    )
    csrf = legacy.json.loads(body.decode("utf-8"))["csrf"]
    form = urlencode({"csrf": csrf}).encode("utf-8")

    status, headers, _ = request(
        legacy.application,
        "/logout",
        method="POST",
        body=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cookie=cookies,
    )

    assert status == "303 See Other"
    assert "Max-Age=0" in "\n".join(header_values(headers, "Set-Cookie"))


def session_csrf(module, cookies):
    status, _, body = request(module.application, "/api/session", cookie=cookies)
    assert status == "200 OK"
    return module.json.loads(body.decode("utf-8"))["csrf"]


def logout(module, cookies):
    status, _, _ = request(
        module.application,
        "/logout",
        method="POST",
        headers={"X-CSRF-Token": session_csrf(module, cookies)},
        cookie=cookies,
    )
    assert status == "303 See Other"


def test_logout_revokes_replayed_session_cookie(legacy):
    # FAN-1229: a cookie captured before logout must die server-side, not
    # only in the browser jar.
    cookies = login(legacy)
    logout(legacy, cookies)
    for _ in range(3):
        status, _, _ = request(legacy.application, "/api/meta", cookie=cookies)
        assert status == "401 Unauthorized"
    # the stale cookie may not resurrect a login redirect loop either
    status, _, _ = request(legacy.application, "/login", cookie=cookies)
    assert status == "200 OK"


def test_invalid_logout_csrf_does_not_revoke_session(legacy):
    cookies = login(legacy)
    status, _, _ = request(
        legacy.application,
        "/logout",
        method="POST",
        headers={"X-CSRF-Token": "wrong"},
        cookie=cookies,
    )
    assert status == "400 Bad Request"
    status, _, _ = request(legacy.application, "/api/meta", cookie=cookies)
    assert status == "200 OK"


def test_logout_revokes_only_the_current_session(legacy):
    first = login(legacy)
    second = login(legacy)
    logout(legacy, first)
    assert request(legacy.application, "/api/meta", cookie=first)[0] == (
        "401 Unauthorized"
    )
    assert request(legacy.application, "/api/meta", cookie=second)[0] == (
        "200 OK"
    )


def test_session_state_survives_cgi_process_restart(legacy):
    # Session validity and revocation live in security.db, so neither a live
    # session nor a logout may be forgotten when the CGI process is recycled.
    cookies = login(legacy)
    restarted = importlib.reload(legacy)
    status, _, _ = request(restarted.application, "/api/meta", cookie=cookies)
    assert status == "200 OK"
    logout(restarted, cookies)
    replayed = importlib.reload(restarted)
    for _ in range(3):
        status, _, _ = request(
            replayed.application, "/api/meta", cookie=cookies
        )
        assert status == "401 Unauthorized"


def test_expired_session_record_is_rejected_and_purged(legacy):
    cookies = login(legacy)
    conn = sqlite3.connect(legacy.SECURITY_DB_PATH)
    try:
        conn.execute(
            "UPDATE sessions SET expires_at = ?",
            (int(legacy.time.time()) - 1,),
        )
        conn.commit()
    finally:
        conn.close()
    status, _, _ = request(legacy.application, "/api/meta", cookie=cookies)
    assert status == "401 Unauthorized"
    # the next login purges expired rows inside its own transaction
    login(legacy)
    conn = sqlite3.connect(legacy.SECURITY_DB_PATH)
    try:
        count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_session_table_stores_only_hashed_ids(legacy):
    cookies = login(legacy)
    # The cookie is now the opaque token itself — no envelope to decode.
    sid = dict(
        part.split("=", 1) for part in cookies.split("; ")
    )["aistat_session"]
    assert "." not in sid  # no signed/serialized structure
    conn = sqlite3.connect(legacy.SECURITY_DB_PATH)
    try:
        rows = [
            row[0] for row in conn.execute("SELECT sid_hash FROM sessions")
        ]
    finally:
        conn.close()
    assert rows == [legacy._session_id_hash(sid)]
    assert sid not in rows


def _session_cookie_value(cookies):
    return dict(part.split("=", 1) for part in cookies.split("; "))[
        "aistat_session"
    ]


def test_password_session_cookie_is_opaque_legacy(legacy):
    # AC1: the legacy cookie is one opaque token, not a base64+HMAC envelope.
    cookies = login(legacy)
    sid = _session_cookie_value(cookies)
    csrf = session_csrf(legacy, cookies)
    assert_opaque_session_cookie(
        sid, ["sergey", "allowed@example.com", "google", csrf]
    )


def test_google_session_cookie_is_opaque_legacy(legacy, monkeypatch):
    # AC1: an OAuth login gets the same opaque cookie in the legacy contour.
    status, _, _, cookies = oauth_login(
        legacy,
        monkeypatch,
        {"sub": "g-op-legacy", "email": "allowed@example.com",
         "email_verified": True},
    )
    assert status == "303 See Other"
    sid = _session_cookie_value(cookies)
    csrf = session_csrf(legacy, cookies)
    assert_opaque_session_cookie(
        sid, ["allowed@example.com", "google", "g-op-legacy", csrf]
    )


def test_old_structured_cookie_fails_closed_even_with_live_row_legacy(legacy):
    # AC3: a genuine old base64+HMAC cookie, plus tampered/unknown tokens, fail
    # closed with no private access while their inner SID row is still live.
    expires = int(legacy.time.time()) + 3600
    sid = legacy._create_session_record(legacy.OWNER_USER_ID, expires)
    assert legacy._resolve_session_record(sid) is not None

    payload = {
        "u": "sergey",
        "uid": legacy.OWNER_USER_ID,
        "sid": sid,
        "exp": expires,
        "csrf": "x",
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        )
        .decode("ascii")
        .rstrip("=")
    )
    structured = encoded + "." + legacy._sign(encoded, legacy.SESSION_SECRET)
    for value in (structured, sid + ".tampered", "totally-unknown-token"):
        cookie = "aistat_session=" + value
        assert request(legacy.application, "/api/meta", cookie=cookie)[0] == (
            "401 Unauthorized"
        ), value
        assert request(legacy.application, "/login", cookie=cookie)[0] == (
            "200 OK"
        ), value
    assert legacy._resolve_session_record(sid) is not None


def test_reauth_rotates_and_invalidates_previous_token_legacy(legacy):
    # AC4: re-auth in the same browser rotates the token and kills the previous
    # one; the captured pre-rotation cookie replays dead.
    first = login(legacy)
    assert request(legacy.application, "/api/meta", cookie=first)[0] == "200 OK"

    token = legacy._make_login_csrf()
    body = urlencode(
        {"csrf": token, "username": "sergey", "password": PASSWORD, "next": "/"}
    ).encode("utf-8")
    combined = first + "; aistat_login_csrf=" + token
    status, headers, _ = request(
        legacy.application,
        "/login",
        method="POST",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cookie=combined,
    )
    assert status == "303 See Other"
    second = cookie_jar(headers, first)
    for _ in range(3):
        assert request(legacy.application, "/api/meta", cookie=first)[0] == (
            "401 Unauthorized"
        )
    assert request(legacy.application, "/api/meta", cookie=second)[0] == "200 OK"


def test_oauth_logout_revokes_replayed_cookie(legacy, monkeypatch):
    install_fake_http(
        monkeypatch,
        {"sub": "g-out", "email": "allowed@example.com", "email_verified": True},
    )
    status, headers, _ = request(legacy.application, "/auth/google/start")
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, headers, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "303 See Other"
    cookies = cookie_jar(headers, start_cookies)
    logout(legacy, cookies)
    for _ in range(3):
        status, _, _ = request(legacy.application, "/api/meta", cookie=cookies)
        assert status == "401 Unauthorized"


def test_signed_snapshot_ingest(legacy, tmp_path):
    source = tmp_path / "source.db"
    conn = connect(source)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.execute(
        "UPDATE daily_usage SET input_tokens = input_tokens + 1000000 "
        "WHERE runtime_id = 'R1'"
    )
    conn.commit()
    conn.close()
    payload = create_compressed_snapshot(source)
    timestamp = int(legacy.time.time())
    signature = legacy._snapshot_signature(
        legacy.OWNER_USER_ID, timestamp, payload
    )
    status, _, body = request(
        legacy.application,
        "/api/ingest/snapshot",
        method="POST",
        body=payload,
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Tenant": str(legacy.OWNER_USER_ID),
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Signature": signature,
        },
    )
    assert status == "200 OK"
    assert (
        legacy.json.loads(body.decode("utf-8"))["schema_version"]
        == SCHEMA_VERSION
    )
    cookies = login(legacy)
    status, _, body = request(
        legacy.application, "/api/summary", cookie=cookies
    )
    assert status == "200 OK"
    assert legacy.json.loads(body.decode("utf-8"))["total_tokens"] == 5_700_000


def test_snapshot_replay_is_per_tenant_and_unknown_fails_closed(
    legacy, tmp_path
):
    store = SecurityStore(legacy.SECURITY_DB_PATH)
    bob_id = store.find_or_create_user_by_identity(
        "google", "bob", email="bob@example.com", now=100
    )
    store.ensure_tenant(bob_id, now=100)
    source = tmp_path / "multi-tenant.db"
    conn = connect(source)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()
    payload = create_compressed_snapshot(source)
    timestamp = int(legacy.time.time())

    def upload(tenant_id, signed_tenant_id=None, at=timestamp):
        signed_tenant_id = (
            tenant_id if signed_tenant_id is None else signed_tenant_id
        )
        return request(
            legacy.application,
            "/api/ingest/snapshot",
            method="POST",
            body=payload,
            headers={
                "Content-Type": "application/vnd.aistat.snapshot+gzip",
                "X-AIStat-Tenant": str(tenant_id),
                "X-AIStat-Timestamp": str(at),
                "X-AIStat-Signature": legacy._snapshot_signature(
                    signed_tenant_id, at, payload
                ),
            },
        )

    assert upload(legacy.OWNER_USER_ID)[0] == "200 OK"
    assert upload(bob_id)[0] == "200 OK"
    bob_path = legacy.tenant_db_path(legacy.TENANTS_DIR, bob_id)
    with open(bob_path, "rb") as source_file:
        bob_bytes = source_file.read()
    assert upload(legacy.OWNER_USER_ID)[0] == "409 Conflict"
    assert upload(
        bob_id, signed_tenant_id=legacy.OWNER_USER_ID
    )[0] == "401 Unauthorized"
    with open(bob_path, "rb") as source_file:
        assert source_file.read() == bob_bytes

    unknown_id = bob_id + 1000
    assert upload(unknown_id)[0] == "401 Unauthorized"
    assert not os.path.exists(
        legacy.tenant_db_path(legacy.TENANTS_DIR, unknown_id)
    )
    assert upload(legacy.OWNER_USER_ID, at=timestamp + 1)[0] == "200 OK"
    with open(bob_path, "rb") as source_file:
        assert source_file.read() == bob_bytes


def test_snapshot_age_and_size_limits_are_enforced_per_request(
    legacy, tmp_path
):
    source = tmp_path / "limits.db"
    conn = connect(source)
    init_db(conn)
    # A non-degrading snapshot: seeded so it does not move the tenant's usage
    # backwards and trip the FAN-1442 data-freshness guard (this test asserts
    # age/validity/size enforcement, not freshness).
    seed_aggregate_fixture(conn)
    conn.close()
    payload = create_compressed_snapshot(source)
    stale_at = int(legacy.time.time()) - legacy.INGEST_MAX_AGE - 1
    status, _, _ = request(
        legacy.application,
        "/api/ingest/snapshot",
        method="POST",
        body=payload,
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Tenant": str(legacy.OWNER_USER_ID),
            "X-AIStat-Timestamp": str(stale_at),
            "X-AIStat-Signature": legacy._snapshot_signature(
                legacy.OWNER_USER_ID, stale_at, payload
            ),
        },
    )
    assert status == "401 Unauthorized"

    invalid_at = int(legacy.time.time())
    invalid_payload = b"not gzip"
    status, _, _ = request(
        legacy.application,
        "/api/ingest/snapshot",
        method="POST",
        body=invalid_payload,
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Tenant": str(legacy.OWNER_USER_ID),
            "X-AIStat-Timestamp": str(invalid_at),
            "X-AIStat-Signature": legacy._snapshot_signature(
                legacy.OWNER_USER_ID, invalid_at, invalid_payload
            ),
        },
    )
    assert status == "422 Unprocessable Entity"
    status, _, _ = request(
        legacy.application,
        "/api/ingest/snapshot",
        method="POST",
        body=payload,
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Tenant": str(legacy.OWNER_USER_ID),
            "X-AIStat-Timestamp": str(invalid_at),
            "X-AIStat-Signature": legacy._snapshot_signature(
                legacy.OWNER_USER_ID, invalid_at, payload
            ),
        },
    )
    assert status == "200 OK"

    legacy.MAX_SNAPSHOT_BYTES = len(payload) - 1
    status, _, _ = request(
        legacy.application,
        "/api/ingest/snapshot",
        method="POST",
        body=payload,
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Tenant": str(legacy.OWNER_USER_ID),
            "X-AIStat-Timestamp": str(int(legacy.time.time())),
            "X-AIStat-Signature": "unused",
        },
    )
    assert status == "413 Payload Too Large"


def test_host_allowlist(legacy):
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/login",
            "HTTP_HOST": "evil.example",
            "HTTPS": "on",
            "wsgi.url_scheme": "https",
            "wsgi.input": io.BytesIO(b""),
            "CONTENT_LENGTH": "0",
        }
    )
    captured = {}
    body = b"".join(
        legacy.application(
            environ,
            lambda status, headers: captured.update(
                {"status": status, "headers": headers}
            ),
        )
    )
    assert captured["status"] == "400 Bad Request"
    assert body == b"Invalid host"


def test_login_page_shows_google_button(legacy):
    status, _, body = request(legacy.application, "/login")
    assert status == "200 OK"
    page = body.decode("utf-8")
    assert "Войти / зарегистрироваться через Google" in page
    assert "/auth/google/start?next=" in page


def test_login_page_shows_yandex_button(legacy):
    status, _, body = request(legacy.application, "/login")
    assert status == "200 OK"
    page = body.decode("utf-8")
    assert "Войти / зарегистрироваться через Яндекс" in page
    assert "/auth/yandex/start?next=" in page


def yandex_oauth_login(module, monkeypatch, identity, next_url="/", cookie=None):
    """Drive a full mock-provider Yandex login through the real legacy app.

    Same shape as :func:`oauth_login`, but over the ``/auth/yandex/*`` routes
    of the env-configured Yandex provider.
    """
    install_fake_http(monkeypatch, identity)
    status, headers, _ = request(
        module.application,
        "/auth/yandex/start?" + urlencode({"next": next_url}),
        cookie=cookie,
    )
    assert status == "303 See Other"
    assert header_values(headers, "Location")[0].startswith(
        "https://yandex.example/authorize"
    )
    state = state_from(headers)
    start_cookies = cookie_jar(headers, cookie or "")
    status, headers, body = request(
        module.application,
        "/auth/yandex/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    return status, headers, body, cookie_jar(headers, start_cookies)


def test_yandex_login_registers_once_and_reuses_account(
    legacy_open, monkeypatch
):
    module = legacy_open
    # Yandex-shaped userinfo: id/default_email/real_name and no verified
    # claim; the env opt-in AISTAT_OAUTH_YANDEX_ASSUME_EMAIL_VERIFIED covers it
    identity = {
        "id": "ya-1",
        "default_email": "user@yandex.example",
        "real_name": "Юзер",
    }
    status, headers, _, cookies = yandex_oauth_login(
        module, monkeypatch, identity, "/api/meta"
    )
    assert status == "303 See Other"
    assert header_values(headers, "Location") == ["/api/meta"]
    # a fresh ordinary account sees its own empty tenant, not owner data
    assert meta_projects(module, cookies) == set()
    first_user = user_id_for_subject(module, "ya-1")
    assert first_user is not None

    # a repeat login with the same subject returns the same account
    status, _, _, cookies = yandex_oauth_login(module, monkeypatch, identity)
    assert status == "303 See Other"
    assert user_id_for_subject(module, "ya-1") == first_user
    assert account_counts_for(module, "ya-1", "user@yandex.example") == {
        "users": 1, "identities": 1
    }


def test_yandex_outsider_denied_under_nonempty_allowlist(legacy, monkeypatch):
    # allow-listed mode: a new unlisted Yandex subject is refused registration
    # even though its email counts as verified via the provider opt-in
    install_fake_http(
        monkeypatch,
        {"id": "ya-2", "default_email": "stranger@yandex.example"},
    )
    status, headers, _ = request(legacy.application, "/auth/yandex/start")
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, _, body = request(
        legacy.application,
        "/auth/yandex/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "403 Forbidden"
    assert "Регистрация сейчас закрыта" in body.decode("utf-8")
    assert user_id_for_subject(legacy, "ya-2") is None


def test_oauth_unknown_provider_is_404(legacy):
    status, _, _ = request(legacy.application, "/auth/nope/start")
    assert status == "404 Not Found"


def install_forbidden_http(monkeypatch):
    def forbidden_urlopen(request, timeout=None):
        raise AssertionError(
            "provider must not be contacted: " + request.full_url
        )

    monkeypatch.setattr("aistat.oauth.urlopen", forbidden_urlopen)


def test_oauth_start_redirects_to_provider_with_state(legacy):
    status, headers, _ = request(
        legacy.application, "/auth/google/start?next=/api/meta"
    )
    assert status == "303 See Other"
    location = header_values(headers, "Location")[0]
    assert location.startswith("https://accounts.example/authorize")
    assert "state=" in location
    binding = next(
        value
        for value in header_values(headers, "Set-Cookie")
        if value.startswith("aistat_oauth_client=")
    )
    assert "HttpOnly" in binding
    assert "Secure" in binding
    assert "SameSite=Lax" in binding
    assert "Path=/auth" in binding


def test_oauth_login_grants_access_for_owner_email(legacy, monkeypatch):
    install_fake_http(
        monkeypatch,
        {
            "sub": "g-1",
            "email": "allowed@example.com",
            "email_verified": True,
            "name": "Al",
        },
    )
    status, headers, _ = request(
        legacy.application, "/auth/google/start?next=/api/meta"
    )
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, headers, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "303 See Other"
    assert header_values(headers, "Location") == ["/api/meta"]
    cookies = cookie_jar(headers, start_cookies)
    # the short-lived browser binding remains for other overlapping flows
    assert "aistat_oauth_client" in cookies
    status, _, body = request(legacy.application, "/api/meta", cookie=cookies)
    assert status == "200 OK"
    data = legacy.json.loads(body.decode("utf-8"))
    # linked to the owner tenant: the owner's own data, not an empty account
    assert {p["id"] for p in data["projects"]} == {"P1", "P2"}


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
    legacy, monkeypatch, next_url, expected_location
):
    install_fake_http(
        monkeypatch,
        {"sub": "g-next", "email": "allowed@example.com", "email_verified": True},
    )
    status, headers, _ = request(
        legacy.application, "/auth/google/start?" + urlencode({"next": next_url})
    )
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, headers, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "303 See Other"
    assert header_values(headers, "Location") == [expected_location]


def test_oauth_login_denies_outsider_under_nonempty_allowlist(
    legacy, monkeypatch
):
    # the legacy env configures a non-empty allow list, so a verified new
    # subject that is neither the owner nor allow-listed is refused registration
    install_fake_http(
        monkeypatch,
        {
            "sub": "g-2",
            "email": "stranger@example.com",
            "email_verified": True,
            "name": "S",
        },
    )
    status, headers, _ = request(legacy.application, "/auth/google/start")
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, headers, body = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "403 Forbidden"
    page = body.decode("utf-8")
    assert "Регистрация сейчас закрыта" in page
    assert "stranger@example.com" not in page
    assert "allowed@example.com" not in page
    cookies = cookie_jar(headers, start_cookies)
    status, _, _ = request(legacy.application, "/api/meta", cookie=cookies)
    assert status == "401 Unauthorized"


def test_open_registration_first_login_creates_own_empty_tenant(
    legacy_open, monkeypatch
):
    module = legacy_open
    identity = {"sub": "new-1", "email": "new@example.com", "email_verified": True}
    status, _, _, cookies = oauth_login(module, monkeypatch, identity, "/api/meta")
    assert status == "303 See Other"
    # a fresh ordinary account sees only its own empty tenant, not owner data
    assert meta_projects(module, cookies) == set()
    new_id = user_id_for_subject(module, "new-1")

    conn = sqlite3.connect(module.SECURITY_DB_PATH)
    try:
        is_admin = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (new_id,)
        ).fetchone()[0]
        has_tenant = conn.execute(
            "SELECT 1 FROM tenants WHERE user_id = ?", (new_id,)
        ).fetchone()
        admins = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(is_admin) == 0
    assert has_tenant is not None
    assert int(admins) == 1

    # a repeat login with the same subject returns the same account
    status, _, _, _ = oauth_login(module, monkeypatch, identity, "/api/meta")
    assert status == "303 See Other"
    assert account_counts_for(module, "new-1", "new@example.com") == {
        "users": 1, "identities": 1
    }


def test_open_registration_email_change_keeps_single_account(
    legacy_open, monkeypatch
):
    module = legacy_open
    status, _, _, _ = oauth_login(
        module, monkeypatch,
        {"sub": "chg", "email": "old@example.com", "email_verified": True},
    )
    assert status == "303 See Other"
    first_id = user_id_for_subject(module, "chg")
    # the provider later reports a different verified email for the same subject
    status, _, _, _ = oauth_login(
        module, monkeypatch,
        {"sub": "chg", "email": "fresh@example.com", "email_verified": True},
    )
    assert status == "303 See Other"
    assert user_id_for_subject(module, "chg") == first_id
    assert account_counts_for(module, "chg", "old@example.com")["identities"] == 1


def test_open_registration_ab_tenant_isolation(legacy_open, monkeypatch):
    module = legacy_open
    _, _, _, a_cookies = oauth_login(
        module, monkeypatch,
        {"sub": "sub-a", "email": "shared@example.com", "email_verified": True},
    )
    _, _, _, b_cookies = oauth_login(
        module, monkeypatch,
        {"sub": "sub-b", "email": "shared@example.com", "email_verified": True},
    )
    a_id = user_id_for_subject(module, "sub-a")
    b_id = user_id_for_subject(module, "sub-b")
    assert a_id != b_id
    seed_legacy_tenant(module, a_id, "PA", "A private project")
    # A sees only A's data; B sees neither A's project nor the owner's P1/P2
    assert meta_projects(module, a_cookies) == {"PA"}
    assert meta_projects(module, b_cookies) == set()


def test_open_registration_admin_email_links_owner(legacy_open, monkeypatch):
    module = legacy_open
    status, _, _, cookies = oauth_login(
        module, monkeypatch,
        {"sub": "owner-sub", "email": "Allowed@Example.com",
         "email_verified": True},
        "/api/meta",
    )
    assert status == "303 See Other"
    # the admin email links to the pre-existing owner: owner tenant, owner data
    assert user_id_for_subject(module, "owner-sub") == module.OWNER_USER_ID
    assert meta_projects(module, cookies) == {"P1", "P2"}
    conn = sqlite3.connect(module.SECURITY_DB_PATH)
    try:
        admins = [
            row[0]
            for row in conn.execute("SELECT id FROM users WHERE is_admin = 1")
        ]
    finally:
        conn.close()
    assert admins == [module.OWNER_USER_ID]


def test_open_registration_ordinary_subject_not_elevated_by_admin_email(
    legacy_open, monkeypatch
):
    module = legacy_open
    _, _, _, _ = oauth_login(
        module, monkeypatch,
        {"sub": "not-owner", "email": "person@example.com",
         "email_verified": True},
    )
    ordinary_id = user_id_for_subject(module, "not-owner")
    assert ordinary_id != module.OWNER_USER_ID
    # the same subject later presents the admin email: it stays ordinary and
    # never merges into or is elevated to the owner
    _, _, _, cookies = oauth_login(
        module, monkeypatch,
        {"sub": "not-owner", "email": "allowed@example.com",
         "email_verified": True},
        "/api/meta",
    )
    assert user_id_for_subject(module, "not-owner") == ordinary_id
    assert meta_projects(module, cookies) == set()  # not the owner's P1/P2
    conn = sqlite3.connect(module.SECURITY_DB_PATH)
    try:
        is_admin = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (ordinary_id,)
        ).fetchone()[0]
        admins = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(is_admin) == 0
    assert int(admins) == 1


def test_open_registration_simultaneous_callbacks_one_account(
    legacy_open, monkeypatch
):
    module = legacy_open
    identity = {"sub": "sim", "email": "sim@example.com", "email_verified": True}
    install_fake_http(monkeypatch, identity)
    # two overlapping flows for the same brand-new subject (shared binding)
    status, headers, _ = request(module.application, "/auth/google/start")
    first_state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, headers, _ = request(module.application, "/auth/google/start",
                                 cookie=start_cookies)
    second_state = state_from(headers)
    start_cookies = cookie_jar(headers, start_cookies)
    first = request(
        module.application,
        "/auth/google/callback?state=%s&code=a" % first_state,
        cookie=start_cookies,
    )
    second = request(
        module.application,
        "/auth/google/callback?state=%s&code=b" % second_state,
        cookie=start_cookies,
    )
    assert first[0] == "303 See Other"
    assert second[0] == "303 See Other"
    assert account_counts_for(module, "sim", "sim@example.com") == {
        "users": 1, "identities": 1
    }


def test_open_registration_logout_to_other_user_no_leak(
    legacy_open, monkeypatch
):
    module = legacy_open
    _, _, _, a_cookies = oauth_login(
        module, monkeypatch,
        {"sub": "user-a", "email": "a@example.com", "email_verified": True},
    )
    a_id = user_id_for_subject(module, "user-a")
    seed_legacy_tenant(module, a_id, "PA", "A private project")
    assert meta_projects(module, a_cookies) == {"PA"}

    # logout revokes the server-side session (CSRF-protected POST)
    status, headers, _ = request(
        module.application,
        "/logout",
        method="POST",
        headers={"X-CSRF-Token": session_csrf(module, a_cookies)},
        cookie=a_cookies,
    )
    assert status == "303 See Other"
    after_logout = cookie_jar(headers, a_cookies)
    status, _, _ = request(module.application, "/api/meta", cookie=after_logout)
    assert status == "401 Unauthorized"

    # a different user then logging in never inherits A's data
    _, _, _, b_cookies = oauth_login(
        module, monkeypatch,
        {"sub": "user-b", "email": "b@example.com", "email_verified": True},
    )
    assert user_id_for_subject(module, "user-b") != a_id
    assert meta_projects(module, b_cookies) == set()


def test_open_registration_registered_user_keeps_access_after_delisting(
    legacy_open, monkeypatch, tmp_path
):
    module = legacy_open
    _, _, _, cookies = oauth_login(
        module, monkeypatch,
        {"sub": "keeper", "email": "keeper@example.com", "email_verified": True},
    )
    assert meta_projects(module, cookies) == set()  # registered, has access

    # an operator reconfigures a non-empty allow list that excludes them and
    # the worker restarts (module reload) over the same security.db
    monkeypatch.setenv("AISTAT_OAUTH_ALLOWED_EMAILS", "someone@else.com")
    reloaded = importlib.reload(module)
    # the already-registered user keeps access — no request-time allow-list gate
    status, _, _ = request(reloaded.application, "/api/meta", cookie=cookies)
    assert status == "200 OK"
    # but a brand-new outsider can no longer register
    status, _, _, outsider = oauth_login(
        reloaded, monkeypatch,
        {"sub": "late", "email": "late@example.com", "email_verified": True},
    )
    assert status == "403 Forbidden"
    status, _, _ = request(reloaded.application, "/api/meta", cookie=outsider)
    assert status == "401 Unauthorized"


def _legacy_counts(module):
    conn = sqlite3.connect(module.SECURITY_DB_PATH)
    try:
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "identities": conn.execute(
                "SELECT COUNT(*) FROM oauth_identities"
            ).fetchone()[0],
            "tenants": conn.execute(
                "SELECT COUNT(*) FROM tenants"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def test_legacy_register_concurrent_new_subject_yields_one_account(legacy_open):
    module = legacy_open
    store = module._LegacyOAuthStore()
    before = _legacy_counts(module)

    def register(_index):
        return store.register_or_link_identity(
            "google", "race-sub", email="race@example.com",
            allowed_emails=frozenset(), now=100,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(register, range(8)))

    assert len({r["user_id"] for r in results}) == 1
    assert sorted(r["outcome"] for r in results).count("created") == 1
    after = _legacy_counts(module)
    # exactly one user, identity and tenant were added by the race
    assert after["users"] == before["users"] + 1
    assert after["identities"] == before["identities"] + 1
    assert after["tenants"] == before["tenants"] + 1


class _FailingConnection:
    def __init__(self, real, fail_fragment):
        self._real = real
        self._fail_fragment = fail_fragment

    def execute(self, sql, *args):
        if self._fail_fragment in sql:
            raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_legacy_register_injected_failure_rolls_back(legacy_open, monkeypatch):
    module = legacy_open
    store = module._LegacyOAuthStore()
    before = _legacy_counts(module)
    original = module._security_connection

    def failing_connection():
        return _FailingConnection(original(), "INSERT INTO tenants")

    monkeypatch.setattr(module, "_security_connection", failing_connection)
    with pytest.raises(sqlite3.OperationalError):
        store.register_or_link_identity(
            "google", "sub-x", email="x@example.com",
            allowed_emails=frozenset(), now=100,
        )
    monkeypatch.setattr(module, "_security_connection", original)
    # the whole registration rolled back — no partial user/identity/tenant rows
    assert _legacy_counts(module) == before


def test_oauth_login_is_fail_closed_for_unverified_owner(legacy, monkeypatch):
    install_fake_http(
        monkeypatch,
        {"sub": "g-uv", "email": "allowed@example.com", "email_verified": False},
    )
    status, headers, _ = request(legacy.application, "/auth/google/start")
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, _, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "400 Bad Request"


def _legacy_session_count(module):
    conn = sqlite3.connect(module.SECURITY_DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
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
def test_oauth_login_rejects_malformed_verified_email_fail_closed(
    legacy, monkeypatch, bad_email
):
    module = legacy
    before = _legacy_counts(module)
    before_sessions = _legacy_session_count(module)
    install_fake_http(
        monkeypatch,
        {"sub": "g-bad", "email": bad_email, "email_verified": True},
    )
    status, headers, _ = request(module.application, "/auth/google/start")
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, headers, body = request(
        module.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    # generic 400 login failure (not the closed-registration 403), no session
    assert status == "400 Bad Request"
    assert "Регистрация сейчас закрыта".encode("utf-8") not in body
    after_cookies = cookie_jar(headers, start_cookies)
    # no user/identity/tenant/session row was written for the rejected subject
    assert _legacy_counts(module) == before
    assert _legacy_session_count(module) == before_sessions
    status, _, _ = request(module.application, "/api/meta", cookie=after_cookies)
    assert status == "401 Unauthorized"

    # the state was consumed, so replaying the same callback stays fail-closed
    status, _, _ = request(
        module.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "400 Bad Request"
    assert _legacy_counts(module) == before
    assert _legacy_session_count(module) == before_sessions


def test_oauth_login_rejects_malformed_email_for_registered_subject(
    legacy_open, monkeypatch
):
    module = legacy_open
    # a subject registers cleanly first
    status, _, _, _ = oauth_login(
        module, monkeypatch,
        {"sub": "known", "email": "known@example.com", "email_verified": True},
    )
    assert status == "303 See Other"
    after_register = _legacy_counts(module)
    after_sessions = _legacy_session_count(module)

    # the same, already-registered subject returns with a structurally malformed
    # verified email. Identity is subject-first, but validation runs before it,
    # so this fails closed with the same generic 400: no new rows, no new session
    # and a replay-proof state.
    install_fake_http(
        monkeypatch,
        {"sub": "known", "email": "a@b..example", "email_verified": True},
    )
    status, headers, _ = request(module.application, "/auth/google/start")
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, headers, body = request(
        module.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "400 Bad Request"
    assert "Регистрация сейчас закрыта".encode("utf-8") not in body
    assert _legacy_counts(module) == after_register
    assert _legacy_session_count(module) == after_sessions
    after_cookies = cookie_jar(headers, start_cookies)
    status, _, _ = request(module.application, "/api/meta", cookie=after_cookies)
    assert status == "401 Unauthorized"

    # the consumed state cannot be replayed
    status, _, _ = request(
        module.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "400 Bad Request"
    assert _legacy_counts(module) == after_register
    assert _legacy_session_count(module) == after_sessions


def test_oauth_login_stores_whitespace_padded_email_canonically(
    legacy_open, monkeypatch
):
    module = legacy_open
    status, _, _, _ = oauth_login(
        module, monkeypatch,
        {"sub": "pad", "email": "  New@Example.com  ", "email_verified": True},
    )
    assert status == "303 See Other"
    # stored trimmed (case preserved) in both the user and identity rows
    conn = sqlite3.connect(module.SECURITY_DB_PATH)
    try:
        row = conn.execute(
            "SELECT u.email, oi.email "
            "FROM oauth_identities oi JOIN users u ON u.id = oi.user_id "
            "WHERE oi.subject = ?",
            ("pad",),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "New@Example.com"
    assert row[1] == "New@Example.com"


def test_oauth_callback_rejects_forged_state(legacy):
    status, _, _ = request(
        legacy.application, "/auth/google/callback?state=forged&code=abc"
    )
    assert status == "400 Bad Request"
    status, _, _ = request(legacy.application, "/api/meta")
    assert status == "401 Unauthorized"


def test_oauth_state_is_single_use(legacy, monkeypatch):
    install_fake_http(
        monkeypatch,
        {"sub": "g-3", "email": "allowed@example.com", "email_verified": True},
    )
    status, headers, _ = request(legacy.application, "/auth/google/start")
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    first = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert first[0] == "303 See Other"
    # replaying the now-consumed state is rejected
    second = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert second[0] == "400 Bad Request"


def test_oauth_callback_rejects_cross_client_callback(legacy, monkeypatch):
    # login CSRF: a different browser with its own binding cookie cannot
    # complete the flow with a leaked-but-valid state; the provider is never
    # contacted and no session is created
    install_forbidden_http(monkeypatch)
    status, headers, _ = request(
        legacy.application, "/auth/google/start?next=/api/meta"
    )
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    _, other_headers, _ = request(
        legacy.application, "/auth/google/start"
    )
    other_cookies = cookie_jar(other_headers)
    status, headers, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=other_cookies,
    )
    assert status == "400 Bad Request"
    assert not any(
        value.startswith("aistat_session=")
        for value in header_values(headers, "Set-Cookie")
    )
    # the hijack attempt consumed the state: the initiating client's retry
    # is rejected too
    status, _, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "400 Bad Request"
    status, _, _ = request(legacy.application, "/api/meta")
    assert status == "401 Unauthorized"


def test_oauth_overlapping_starts_share_browser_binding(legacy, monkeypatch):
    install_fake_http(
        monkeypatch,
        {"sub": "g-tabs", "email": "allowed@example.com", "email_verified": True},
    )
    _, first_headers, _ = request(
        legacy.application, "/auth/google/start"
    )
    first_state = state_from(first_headers)
    cookies = cookie_jar(first_headers)
    _, second_headers, _ = request(
        legacy.application, "/auth/google/start", cookie=cookies
    )
    second_state = state_from(second_headers)
    second_cookies = cookie_jar(second_headers, cookies)
    assert first_state != second_state
    assert second_cookies == cookies

    first = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=first" % first_state,
        cookie=second_cookies,
    )
    cookies_after_first = cookie_jar(first[1], second_cookies)
    second = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=second" % second_state,
        cookie=cookies_after_first,
    )
    assert first[0] == "303 See Other"
    assert second[0] == "303 See Other"


def test_oauth_error_callback_burns_state(legacy, monkeypatch):
    # a provider error is terminal: the same state must not work with a code
    install_forbidden_http(monkeypatch)
    status, headers, _ = request(legacy.application, "/auth/google/start")
    state = state_from(headers)
    start_cookies = cookie_jar(headers)
    status, _, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&error=access_denied" % state,
        cookie=start_cookies,
    )
    assert status == "400 Bad Request"
    status, _, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "400 Bad Request"
    status, _, _ = request(legacy.application, "/api/meta")
    assert status == "401 Unauthorized"


def test_legacy_bootstrap_migrates_old_oauth_state_schema(
    tmp_path, monkeypatch
):
    configure_legacy_env(tmp_path, monkeypatch)
    path = tmp_path / "security.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE oauth_state ("
            "state TEXT PRIMARY KEY, provider TEXT NOT NULL, "
            "next_url TEXT, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO oauth_state "
            "(state, provider, next_url, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-state", "google", "/", 100),
        )
        conn.commit()
    finally:
        conn.close()

    import aistat.legacy_wsgi as module

    module = importlib.reload(module)
    conn = sqlite3.connect(str(path))
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(oauth_state)")
        }
        row = conn.execute(
            "SELECT client_hash FROM oauth_state WHERE state = ?",
            ("legacy-state",),
        ).fetchone()
    finally:
        conn.close()
    assert "client_hash" in columns
    assert row == (None,)
    assert module._LegacyOAuthStore().take_oauth_state(
        "legacy-state", now=101
    )["client_hash"] is None


# --- FAN-1366: crash-atomic snapshot install + replay watermark -----------


class _Boom(Exception):
    """Stand-in for a process crash at a chosen point in the ingest flow."""


def _new_snapshot(tmp_path, name, bump):
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


def _legacy_ingest(legacy, payload, ts, tenant_id=None):
    tenant_id = legacy.OWNER_USER_ID if tenant_id is None else tenant_id
    return request(
        legacy.application,
        "/api/ingest/snapshot",
        method="POST",
        body=payload,
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Tenant": str(tenant_id),
            "X-AIStat-Timestamp": str(ts),
            "X-AIStat-Signature": legacy._snapshot_signature(
                tenant_id, ts, payload
            ),
        },
    )


def _legacy_tenant_sha(legacy, uid):
    path = legacy.tenant_db_path(legacy.TENANTS_DIR, uid)
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _legacy_watermark(legacy, uid):
    return int(legacy._tenant_record(uid)["last_ingest_timestamp"])


def _legacy_journal_count(legacy):
    conn = sqlite3.connect(legacy.SECURITY_DB_PATH)
    try:
        return conn.execute(
            "SELECT count(*) FROM snapshot_install_journal"
        ).fetchone()[0]
    finally:
        conn.close()


def test_legacy_crash_after_swap_before_watermark_recovers(
    legacy, tmp_path, monkeypatch
):
    uid = legacy.OWNER_USER_ID
    old_wm = _legacy_watermark(legacy, uid)
    payload, new_sha = _new_snapshot(tmp_path, "new.db", 1_000_000)
    ts = int(legacy.time.time())

    monkeypatch.setattr(
        legacy,
        "_finish_snapshot_install",
        lambda *a, **k: (_ for _ in ()).throw(_Boom()),
    )
    status, _, _ = _legacy_ingest(legacy, payload, ts)
    assert status == "500 Internal Server Error"

    # File swapped to NEW, watermark still OLD, intent journalled.
    assert _legacy_tenant_sha(legacy, uid) == new_sha
    assert _legacy_watermark(legacy, uid) == old_wm
    assert _legacy_journal_count(legacy) == 1
    monkeypatch.undo()

    # Restart recovery reconciles to new + new.
    legacy._recover_snapshot_installs()
    assert _legacy_tenant_sha(legacy, uid) == new_sha
    assert _legacy_watermark(legacy, uid) == ts
    assert _legacy_journal_count(legacy) == 0
    assert _legacy_ingest(legacy, payload, ts)[0] == "409 Conflict"


def test_legacy_crash_after_journal_before_swap_recovers(
    legacy, tmp_path, monkeypatch
):
    uid = legacy.OWNER_USER_ID
    old_sha = _legacy_tenant_sha(legacy, uid)
    old_wm = _legacy_watermark(legacy, uid)
    payload, new_sha = _new_snapshot(tmp_path, "new.db", 1_000_000)
    ts = int(legacy.time.time())

    monkeypatch.setattr(
        legacy.snapshot_recovery,
        "swap_staged_into_place",
        lambda *a, **k: (_ for _ in ()).throw(_Boom()),
    )
    status, _, _ = _legacy_ingest(legacy, payload, ts)
    assert status == "500 Internal Server Error"

    assert _legacy_tenant_sha(legacy, uid) == old_sha
    assert _legacy_watermark(legacy, uid) == old_wm
    assert _legacy_journal_count(legacy) == 1
    monkeypatch.undo()

    legacy._recover_snapshot_installs()
    assert _legacy_tenant_sha(legacy, uid) == new_sha
    assert _legacy_watermark(legacy, uid) == ts
    assert _legacy_journal_count(legacy) == 0


def test_legacy_swap_failure_rolls_back_in_request(
    legacy, tmp_path, monkeypatch
):
    uid = legacy.OWNER_USER_ID
    old_sha = _legacy_tenant_sha(legacy, uid)
    old_wm = _legacy_watermark(legacy, uid)
    payload, _new_sha = _new_snapshot(tmp_path, "new.db", 1_000_000)
    ts = int(legacy.time.time())

    monkeypatch.setattr(
        legacy.snapshot_recovery,
        "swap_staged_into_place",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    status, _, _ = _legacy_ingest(legacy, payload, ts)
    assert status == "422 Unprocessable Entity"

    assert _legacy_tenant_sha(legacy, uid) == old_sha
    assert _legacy_watermark(legacy, uid) == old_wm
    assert _legacy_journal_count(legacy) == 0
    leftovers = [
        name
        for name in os.listdir(legacy.TENANTS_DIR)
        if name.startswith(".aistat-snapshot-") and name.endswith(".db")
    ]
    assert leftovers == []


def test_legacy_rejects_symlink_target_without_touching_state(
    legacy, tmp_path
):
    uid = legacy.OWNER_USER_ID
    old_wm = _legacy_watermark(legacy, uid)
    target = legacy.tenant_db_path(legacy.TENANTS_DIR, uid)
    real = target + ".real"
    os.replace(target, real)
    os.symlink(real, target)

    payload, _new_sha = _new_snapshot(tmp_path, "new.db", 1_000_000)
    ts = int(legacy.time.time())
    status, _, _ = _legacy_ingest(legacy, payload, ts)
    assert status == "422 Unprocessable Entity"
    assert os.path.islink(target)
    assert _legacy_watermark(legacy, uid) == old_wm
    assert _legacy_journal_count(legacy) == 0
