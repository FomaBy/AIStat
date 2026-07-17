"""Dependency-free cPanel WSGI tests."""

import ast
import importlib
import io
import json
import os
import re
import runpy
import sqlite3
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

import pytest
from werkzeug.security import generate_password_hash

from aistat.db import SCHEMA_VERSION, connect, init_db
from aistat.config import Config
from aistat.migrate import migrate_owner_database
from aistat.security import SecurityStore
from aistat.snapshot import create_compressed_snapshot
from conftest import seed_aggregate_fixture, seed_model_less_fixture

PASSWORD = "correct horse battery staple"
SESSION_SECRET = "legacy-session-" + "s" * 48
INGEST_SECRET = "legacy-ingest-" + "i" * 48


def configure_legacy_env(tmp_path, monkeypatch):
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
    monkeypatch.setenv("AISTAT_OAUTH_PROVIDERS", "google")
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
    monkeypatch.setenv("AISTAT_OAUTH_ALLOWED_EMAILS", "allowed@example.com")
    monkeypatch.setenv("AISTAT_ADMIN_EMAIL", "allowed@example.com")


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    configure_legacy_env(tmp_path, monkeypatch)
    import aistat.legacy_wsgi as module

    module = importlib.reload(module)
    conn = connect(module.DB_PATH)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()
    migrate_owner_database(Config(), now=1000)
    return module


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
        "aistat/legacy_wsgi.py",
        "aistat/migrate.py",
        "aistat/oauth.py",
        "aistat/tenant.py",
        "aistat.cgi",
    ):
        source = open(path, encoding="utf-8").read()
        ast.parse(source, filename=path, feature_version=(3, 6))


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
    assert abs(mixed["cost_per_sp"] - 0.000125) < 1e-9
    assert mixed["weighted_efficiency"] is None
    null_only = get("?agent=A5")
    assert [m["model"] for m in null_only["models"]] == [None]
    assert null_only["cost_per_sp"] is None
    assert null_only["weighted_efficiency"] is None
    assert null_only["unpriced_tokens"] == 500
    exact = get("?project=P3")
    assert [m["model"] for m in exact["models"]] == ["m-claude", None]
    assert abs(exact["cost_per_sp"] - 0.000125) < 1e-9
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
    value = dict(
        part.split("=", 1) for part in cookies.split("; ")
    )["aistat_session"]
    payload = legacy.json.loads(
        legacy._b64decode(value.rsplit(".", 1)[0]).decode("utf-8")
    )
    sid = payload["sid"]
    conn = sqlite3.connect(legacy.SECURITY_DB_PATH)
    try:
        rows = [
            row[0] for row in conn.execute("SELECT sid_hash FROM sessions")
        ]
    finally:
        conn.close()
    assert rows == [legacy._session_id_hash(sid)]
    assert sid not in rows


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
    assert "Войти через Google" in page
    assert "/auth/google/start?next=" in page


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


def test_oauth_login_is_fail_closed_for_stranger(legacy, monkeypatch):
    # owner-only: a verified but non-owner email is refused before any account
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
    status, headers, _ = request(
        legacy.application,
        "/auth/google/callback?state=%s&code=abc" % state,
        cookie=start_cookies,
    )
    assert status == "400 Bad Request"
    cookies = cookie_jar(headers, start_cookies)
    status, _, _ = request(legacy.application, "/api/meta", cookie=cookies)
    assert status == "401 Unauthorized"


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
