"""Public WSGI authentication, headers and signed snapshot ingestion."""

import json
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from werkzeug.security import generate_password_hash

from aistat import oauth
from aistat.config import Config
from aistat.db import SCHEMA_VERSION, connect, init_db
from aistat.migrate import migrate_owner_database
from aistat.security import SecurityStore, snapshot_signature
from aistat.snapshot import create_compressed_snapshot
from aistat.wsgi import create_app
from conftest import seed_aggregate_fixture, seed_model_less_fixture

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
    config.oauth_providers = {"google": OAUTH_PROVIDER}
    config.oauth_allowed_emails = frozenset({"allowed@example.com"})

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
    assert mixed["cost_per_sp"] == pytest.approx(0.000125)
    assert mixed["weighted_efficiency"] is None
    null_only = get("?agent=A5")
    assert [m["model"] for m in null_only["models"]] == [None]
    assert null_only["cost_per_sp"] is None
    assert null_only["weighted_efficiency"] is None
    assert null_only["unpriced_tokens"] == 500
    exact = get("?project=P3")
    assert [m["model"] for m in exact["models"]] == ["m-claude", None]
    assert exact["cost_per_sp"] == pytest.approx(0.000125)
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
        monkeypatch, {"sub": "g-1", "email": "allowed@example.com", "name": "Al"}
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
    # the allow-listed OAuth session now reaches private data
    response = client.get("/api/meta", base_url="https://localhost")
    assert response.status_code == 200
    assert response.get_json()["projects"] == []


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
        monkeypatch, {"sub": "g-next", "email": "allowed@example.com"}
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


def test_oauth_callback_unauthorized_is_fail_closed(public_app, monkeypatch):
    app, _ = public_app
    install_fake_http(
        monkeypatch, {"sub": "g-2", "email": "stranger@example.com", "name": "S"}
    )
    client = app.test_client()
    start = client.get("/auth/google/start", base_url="https://localhost")
    state = state_from(start.headers["Location"])
    callback = client.get(
        "/auth/google/callback?state=%s&code=abc" % state,
        base_url="https://localhost",
    )
    assert callback.status_code == 403
    # identity established, but a non-listed email sees no private data
    assert client.get("/api/meta", base_url="https://localhost").status_code == 401


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
        monkeypatch, {"sub": "g-tabs", "email": "allowed@example.com"}
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


def test_password_login_unaffected_by_oauth(public_app):
    app, _ = public_app
    client = app.test_client()
    assert login(client).status_code == 303
    assert client.get("/api/meta", base_url="https://localhost").status_code == 200
