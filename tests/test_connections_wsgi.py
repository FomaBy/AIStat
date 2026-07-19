"""Flask-contour route tests for "connect your Multica" (FAN-1220)."""

import concurrent.futures
import json
import logging
import re
import secrets
import sqlite3
import threading
import time

import pytest
from werkzeug.security import generate_password_hash

from aistat import handoff
from aistat.config import Config
from aistat.security import SecurityConfigError
from aistat.wsgi import create_app

PASSWORD = "correct horse battery staple"
SESSION_SECRET = "conn-session-" + "s" * 48
INGEST_SECRET = "conn-ingest-" + "i" * 48
WORKER_SECRET = "conn-worker-" + "w" * 48
TOKEN = "mlt_flask_secret_token_0a1b2c3d4e5f"


def make_config(tmp_path, worker_secret=WORKER_SECRET, connect_enabled=True):
    config = Config()
    config.db_path = tmp_path / "public.db"
    config.security_db_path = tmp_path / "security.db"
    config.tenants_dir = tmp_path / "tenants"
    config.auth_username = "sergey"
    config.auth_password_hash = generate_password_hash(
        PASSWORD, method="pbkdf2:sha256:600000"
    )
    config.session_secret = SESSION_SECRET
    config.ingest_secret = INGEST_SECRET
    config.worker_secret = worker_secret
    config.multica_connect_enabled = connect_enabled
    config.multica_official_url = handoff.OFFICIAL_MULTICA_URL
    config.allowed_hosts = ("localhost", "testserver", "aistat.app")
    config.force_https = False
    config.session_cookie_secure = True
    return config


@pytest.fixture
def conn_app(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config)
    app.config.update(TESTING=True)
    return app, config


def csrf_from(page):
    match = re.search(r'name="csrf" value="([^"]+)"', page.get_data(as_text=True))
    assert match
    return match.group(1)


def login(client):
    page = client.get("/login", base_url="https://localhost")
    response = client.post(
        "/login",
        data={
            "csrf": csrf_from(page),
            "username": "sergey",
            "password": PASSWORD,
            "next": "/",
        },
        base_url="https://localhost",
    )
    assert response.status_code == 303
    return client.get(
        "/api/session", base_url="https://localhost"
    ).get_json()["csrf"]


def submit(client, csrf, token=TOKEN, **overrides):
    data = {"csrf": csrf, "token": token}
    data.update(overrides)
    return client.post(
        "/api/connection", data=data, base_url="https://localhost"
    )


def worker_call(client, path, payload=None, secret=WORKER_SECRET, **kwargs):
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    timestamp = kwargs.get("timestamp", int(time.time()))
    nonce = kwargs.get("nonce") or secrets.token_urlsafe(24)
    signature = kwargs.get("signature") or handoff.worker_signature(
        secret, path, timestamp, nonce, body
    )
    return client.post(
        path,
        data=body,
        content_type="application/json",
        headers={
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Nonce": nonce,
            "X-AIStat-Signature": signature,
        },
        base_url="https://localhost",
    )


def warm_worker(client):
    """Register worker readiness: a running worker pulls before a user connects.

    The host refuses intake unless the worker's last authenticated pull is
    fresh, so tests must simulate that live worker before submitting.
    """
    assert worker_call(client, handoff.WORKER_PULL_PATH).status_code == 200


def handoff_to_worker(client, csrf):
    """Submit + pull + ack; returns the pulled entry."""
    warm_worker(client)
    assert submit(client, csrf).status_code == 200
    (entry,) = worker_call(
        client, handoff.WORKER_PULL_PATH
    ).get_json()["pending"]
    ack = worker_call(
        client,
        handoff.WORKER_ACK_PATH,
        {
            "acks": [
                {
                    "user_id": entry["user_id"],
                    "token_epoch": entry["token_epoch"],
                    "lease_id": entry["lease_id"],
                    "result": "stored",
                }
            ]
        },
    )
    assert ack.get_json()["results"][0]["ok"]
    return entry


def test_connection_routes_require_session(conn_app):
    app, _ = conn_app
    client = app.test_client()
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).status_code == 401
    assert client.post(
        "/api/connection", data={"token": TOKEN}, base_url="https://localhost"
    ).status_code == 401
    assert client.post(
        "/api/connection/revoke", base_url="https://localhost"
    ).status_code == 401


def test_authenticated_dashboard_serves_shared_connection_cabinet(conn_app):
    app, _ = conn_app
    client = app.test_client()
    login(client)
    page = client.get("/", base_url="https://localhost")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="connection-cabinet"' in html
    assert 'id="connection-token"' in html
    assert 'type="password"' in html
    assert "https://multica.ai" in html
    assert "server_url" not in html


def test_intake_requires_csrf(conn_app):
    app, _ = conn_app
    client = app.test_client()
    login(client)
    assert submit(client, "").status_code == 400
    assert submit(client, "wrong-token").status_code == 400
    assert client.post(
        "/api/connection/revoke", base_url="https://localhost"
    ).status_code == 400


def test_intake_validates_input_without_echoing_it(conn_app):
    app, _ = conn_app
    client = app.test_client()
    csrf = login(client)
    for data in (
        {"token": "short"},
        {"token": TOKEN, "server_url": "http://evil.example"},
        {"token": TOKEN, "workspace_label": "x" * 200},
    ):
        response = submit(client, csrf, **data)
        assert response.status_code == 422
        text = response.get_data(as_text=True)
        assert TOKEN not in text and "evil.example" not in text
    warm_worker(client)
    for _ in range(handoff.CONNECTION_MAX_SUBMISSIONS - 3):
        assert submit(client, csrf).status_code == 200
    assert submit(client, csrf).status_code == 429


def test_intake_pending_and_status_never_expose_token(conn_app, caplog):
    app, config = conn_app
    client = app.test_client()
    csrf = login(client)
    warm_worker(client)
    with caplog.at_level(logging.DEBUG):
        response = submit(client, csrf, workspace_label=" My space ")
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "pending"
        assert "server_url" not in body
        assert body["workspace_label"] == "My space"
        assert "token" not in body and "token_epoch" not in body
        status = client.get(
            "/api/connection", base_url="https://localhost"
        )
        assert status.get_json()["status"] == "pending"
        assert TOKEN not in status.get_data(as_text=True)
    assert TOKEN not in caplog.text
    # The token exists only inside security.db until the worker collects it.
    assert TOKEN.encode() in config.security_db_path.read_bytes()


def test_intake_rate_limited_like_login_throttle(conn_app):
    app, _ = conn_app
    client = app.test_client()
    csrf = login(client)
    warm_worker(client)
    for _ in range(handoff.CONNECTION_MAX_SUBMISSIONS):
        assert submit(client, csrf).status_code == 200
    blocked = submit(client, csrf)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


def test_intake_rate_limit_is_atomic_under_concurrency(conn_app):
    app, config = conn_app
    warm_worker(app.test_client())
    barrier = threading.Barrier(12)

    def attempt(index):
        with app.test_client() as client:
            csrf = login(client)
            barrier.wait()
            response = submit(client, csrf, token=TOKEN + str(index))
            return response.status_code, response.headers.get("Retry-After")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(attempt, range(12)))

    assert [status for status, _ in outcomes].count(200) == 10
    rejected = [retry for status, retry in outcomes if status == 429]
    assert len(rejected) == 2
    assert all(int(retry) > 0 for retry in rejected)
    conn = sqlite3.connect(str(config.security_db_path))
    try:
        row = conn.execute(
            "SELECT submissions FROM connection_throttle"
        ).fetchone()
    finally:
        conn.close()
    assert row == (handoff.CONNECTION_MAX_SUBMISSIONS,)


def test_intake_throttle_storage_error_fails_closed(conn_app, monkeypatch):
    app, config = conn_app
    client = app.test_client()
    csrf = login(client)
    warm_worker(client)

    def fail_reservation(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic throttle failure")

    def unexpected_validation(*_args, **_kwargs):
        pytest.fail("PAT validation must not run after throttle storage failure")

    monkeypatch.setattr(
        handoff, "reserve_connection_submission", fail_reservation
    )
    monkeypatch.setattr(
        handoff, "validate_connection_token", unexpected_validation
    )
    response = submit(client, csrf)
    assert response.status_code == 503
    assert TOKEN not in response.get_data(as_text=True)
    assert TOKEN.encode() not in config.security_db_path.read_bytes()


def test_worker_pull_requires_valid_signature(conn_app):
    app, _ = conn_app
    client = app.test_client()
    assert client.post(
        handoff.WORKER_PULL_PATH, data=b"{}", base_url="https://localhost"
    ).status_code == 401
    assert worker_call(
        client, handoff.WORKER_PULL_PATH, secret="x" * 48
    ).status_code == 401
    assert worker_call(
        client,
        handoff.WORKER_PULL_PATH,
        timestamp=int(time.time()) - 3600,
    ).status_code == 401
    assert worker_call(
        client, handoff.WORKER_PULL_PATH, nonce="bad nonce!"
    ).status_code == 401


def test_worker_replay_is_rejected(conn_app):
    app, _ = conn_app
    client = app.test_client()
    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(24)
    first = worker_call(
        client, handoff.WORKER_PULL_PATH, timestamp=timestamp, nonce=nonce
    )
    assert first.status_code == 200
    replay = worker_call(
        client, handoff.WORKER_PULL_PATH, timestamp=timestamp, nonce=nonce
    )
    assert replay.status_code == 409


def test_full_handoff_erases_token_from_host(conn_app, caplog):
    app, config = conn_app
    client = app.test_client()
    csrf = login(client)
    with caplog.at_level(logging.DEBUG):
        entry = handoff_to_worker(client, csrf)
    assert entry["token"] == TOKEN
    status = client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()
    assert status["status"] == "active"
    # After the acknowledged handoff no file on the host retains the token:
    # not security.db (physically erased), not the data DB, not tenant
    # snapshot files.
    for path in config.security_db_path.parent.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), str(path)
    assert TOKEN not in caplog.text


def test_stale_ack_after_replace_is_rejected(conn_app):
    app, config = conn_app
    client = app.test_client()
    csrf = login(client)
    warm_worker(client)
    assert submit(client, csrf, token=TOKEN + "old1").status_code == 200
    (entry,) = worker_call(
        client, handoff.WORKER_PULL_PATH
    ).get_json()["pending"]
    assert submit(client, csrf, token=TOKEN + "new1").status_code == 200
    ack = worker_call(
        client,
        handoff.WORKER_ACK_PATH,
        {
            "acks": [
                {
                    "user_id": entry["user_id"],
                    "token_epoch": entry["token_epoch"],
                    "lease_id": entry["lease_id"],
                    "result": "stored",
                }
            ]
        },
    )
    result = ack.get_json()["results"][0]
    assert not result["ok"] and result["reason"] == "stale-epoch"
    # The replaced token is already gone from the host file.
    assert (TOKEN + "old1").encode() not in config.security_db_path.read_bytes()
    status = client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()
    assert status["status"] == "replacement_pending"


def test_revoke_flow_reaches_worker_and_erases_token(conn_app):
    app, config = conn_app
    client = app.test_client()
    csrf = login(client)
    handoff_to_worker(client, csrf)
    revoke = client.post(
        "/api/connection/revoke",
        headers={"X-CSRF-Token": csrf},
        base_url="https://localhost",
    )
    assert revoke.status_code == 200
    # `revoked` is only reported after the worker acks the delete; the fresh
    # revoke first surfaces the intermediate state.
    assert revoke.get_json()["status"] == "revocation_pending"
    state = worker_call(client, handoff.WORKER_PULL_PATH).get_json()
    assert state["pending"] == []
    (revoked,) = state["revoked"]
    ack = worker_call(
        client,
        handoff.WORKER_ACK_PATH,
        {
            "acks": [
                {
                    "user_id": revoked["user_id"],
                    "token_epoch": revoked["token_epoch"],
                    "result": "revoked",
                }
            ]
        },
    )
    assert ack.get_json()["results"][0]["ok"]
    assert worker_call(
        client, handoff.WORKER_PULL_PATH
    ).get_json()["revoked"] == []
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()["status"] == "revoked"
    assert TOKEN.encode() not in config.security_db_path.read_bytes()
    # Revoking again reports the connection as already gone.
    assert client.post(
        "/api/connection/revoke",
        headers={"X-CSRF-Token": csrf},
        base_url="https://localhost",
    ).status_code == 200


def test_worker_sync_reports_surface_in_cabinet(conn_app):
    app, _ = conn_app
    client = app.test_client()
    csrf = login(client)
    entry = handoff_to_worker(client, csrf)
    report = worker_call(
        client,
        handoff.WORKER_ACK_PATH,
        {
            "acks": [
                {
                    "user_id": entry["user_id"],
                    "token_epoch": entry["token_epoch"],
                    "result": "sync_error",
                    "error": "multica CLI exited with 1",
                }
            ]
        },
    )
    assert report.get_json()["results"][0]["status"] == "error"
    status = client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()
    assert status["status"] == "error"
    assert status["last_sync_error"] == "multica CLI exited with 1"


def test_disabled_worker_channel_fails_closed(tmp_path):
    config = make_config(tmp_path, worker_secret=None)
    app = create_app(config)
    app.config.update(TESTING=True)
    client = app.test_client()
    csrf = login(client)
    assert submit(client, csrf).status_code == 503
    assert TOKEN.encode() not in config.security_db_path.read_bytes()
    assert client.post(
        handoff.WORKER_PULL_PATH, data=b"{}", base_url="https://localhost"
    ).status_code == 404


def test_feature_flag_off_makes_everything_fail_closed(tmp_path):
    config = make_config(tmp_path, connect_enabled=False)
    app = create_app(config)
    app.config.update(TESTING=True)
    client = app.test_client()
    csrf = login(client)
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()["status"] == "disabled"
    assert submit(client, csrf).status_code == 503
    assert TOKEN.encode() not in config.security_db_path.read_bytes()
    assert client.post(
        "/api/connection/revoke",
        headers={"X-CSRF-Token": csrf},
        base_url="https://localhost",
    ).status_code == 503
    # The worker pull/ack channel is invisible while the feature is off.
    assert worker_call(
        client, handoff.WORKER_PULL_PATH
    ).status_code == 404
    assert worker_call(
        client, handoff.WORKER_ACK_PATH, {"acks": []}
    ).status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example",
        "https://multica.ai.evil.com",
        "http://multica.ai",
        "https://multica.ai:8443",
        "https://127.0.0.1",
        "https://user@multica.ai",
        "https://multica.ai/",
        "https://multica.ai/path",
        "https://multica.ai?query=1",
        "https://multica.ai#fragment",
        "HTTPS://multica.ai",
        " https://multica.ai ",
    ],
)
def test_intake_rejects_every_noncanonical_host_without_storage(conn_app, bad):
    app, config = conn_app
    client = app.test_client()
    csrf = login(client)
    warm_worker(client)
    response = submit(client, csrf, server_url=bad)
    assert response.status_code == 422
    assert bad not in response.get_data(as_text=True)
    assert TOKEN.encode() not in config.security_db_path.read_bytes()


@pytest.mark.parametrize("legacy_url", [None, "", handoff.OFFICIAL_MULTICA_URL])
def test_intake_accepts_only_empty_or_exact_official_legacy_value(
    conn_app, legacy_url
):
    app, config = conn_app
    client = app.test_client()
    csrf = login(client)
    warm_worker(client)
    kwargs = {} if legacy_url is None else {"server_url": legacy_url}
    named = submit(client, csrf, **kwargs)
    assert named.status_code == 200
    assert "server_url" not in named.get_json()
    conn = sqlite3.connect(str(config.security_db_path))
    try:
        stored = conn.execute("SELECT server_url FROM connections").fetchone()[0]
    finally:
        conn.close()
    assert stored == handoff.OFFICIAL_MULTICA_URL


def test_public_config_cannot_override_the_official_multica_host(tmp_path):
    config = make_config(tmp_path)
    config.multica_official_url = "https://attacker.example"
    with pytest.raises(SecurityConfigError, match="exactly https://multica.ai"):
        create_app(config)
    assert not config.security_db_path.exists()


def test_intake_requires_a_ready_worker(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config)
    app.config.update(TESTING=True)
    client = app.test_client()
    csrf = login(client)
    # No worker has pulled yet: intake fails closed and stores nothing.
    response = submit(client, csrf)
    assert response.status_code == 503
    assert TOKEN.encode() not in config.security_db_path.read_bytes()
    # Once the worker is live (a fresh authenticated pull), intake succeeds.
    warm_worker(client)
    assert submit(client, csrf).status_code == 200
    assert TOKEN.encode() in config.security_db_path.read_bytes()
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()["status"] == "pending"


def test_pending_token_purged_from_host_after_ttl(tmp_path):
    config = make_config(tmp_path)
    config.connection_pending_ttl_seconds = 0  # expire immediately for the test
    app = create_app(config)
    app.config.update(TESTING=True)
    client = app.test_client()
    csrf = login(client)
    warm_worker(client)
    assert submit(client, csrf).status_code == 200
    assert TOKEN.encode() in config.security_db_path.read_bytes()
    # The next worker pull finds the token expired: it is erased and handed over
    # as a revocation to confirm instead of being leased.
    state = worker_call(client, handoff.WORKER_PULL_PATH).get_json()
    assert state["pending"] == []
    assert len(state["revoked"]) == 1
    assert TOKEN.encode() not in config.security_db_path.read_bytes()
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()["status"] == "revocation_pending"


def test_revoke_before_worker_lease_is_ack_gated(conn_app):
    app, config = conn_app
    client = app.test_client()
    csrf = login(client)
    warm_worker(client)
    # Revoke a token the worker never leased: the host erases it at once but
    # still waits for the worker to confirm no copy exists.
    assert submit(client, csrf).status_code == 200
    revoke = client.post(
        "/api/connection/revoke",
        headers={"X-CSRF-Token": csrf},
        base_url="https://localhost",
    )
    assert revoke.status_code == 200
    assert revoke.get_json()["status"] == "revocation_pending"
    assert TOKEN.encode() not in config.security_db_path.read_bytes()
    state = worker_call(client, handoff.WORKER_PULL_PATH).get_json()
    assert state["pending"] == []
    (revoked,) = state["revoked"]
    ack = worker_call(
        client,
        handoff.WORKER_ACK_PATH,
        {
            "acks": [
                {
                    "user_id": revoked["user_id"],
                    "token_epoch": revoked["token_epoch"],
                    "result": "revoked",
                }
            ]
        },
    )
    assert ack.get_json()["results"][0]["ok"]
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()["status"] == "revoked"
