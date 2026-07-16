"""Public WSGI authentication, headers and signed snapshot ingestion."""

import re
import time

import pytest
from werkzeug.security import generate_password_hash

from aistat.config import Config
from aistat.db import connect, init_db
from aistat.security import snapshot_signature
from aistat.snapshot import create_compressed_snapshot
from aistat.wsgi import create_app
from conftest import seed_aggregate_fixture

PASSWORD = "correct horse battery staple"
SESSION_SECRET = "session-" + "s" * 48
INGEST_SECRET = "ingest-" + "i" * 48


@pytest.fixture
def public_app(tmp_path):
    config = Config()
    config.db_path = tmp_path / "public.db"
    config.security_db_path = tmp_path / "security.db"
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

    conn = connect(config.db_path)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()

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
    signature = snapshot_signature(INGEST_SECRET, timestamp, payload)
    client = app.test_client()
    response = client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Signature": signature,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["schema_version"] == 3
    assert config.db_path.with_name("public.db.previous").exists()

    replay = client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Signature": signature,
        },
    )
    assert replay.status_code == 409

    assert login(client).status_code == 303
    summary = client.get(
        "/api/summary", base_url="https://localhost"
    ).get_json()
    assert summary["total_tokens"] == 5_700_000


def test_ingest_rejects_bad_signature_and_invalid_database(public_app):
    app, _ = public_app
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

    signature = snapshot_signature(INGEST_SECRET, timestamp + 1, payload)
    invalid = client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp + 1),
            "X-AIStat-Signature": signature,
        },
    )
    assert invalid.status_code == 422
