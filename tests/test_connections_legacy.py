"""Legacy cPanel contour route tests for "connect your Multica" (FAN-1220)."""

import concurrent.futures
import importlib
import json
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlencode

import pytest

from aistat import handoff
from test_legacy_wsgi import (
    configure_legacy_env,
    cookie_jar,
    header_values,
    login,
    request,
)

WORKER_SECRET = "legacy-worker-" + "w" * 48
TOKEN = "mlt_legacy_secret_token_5e4d3c2b1a09"


def load_legacy(
    tmp_path, monkeypatch, worker_secret=WORKER_SECRET, connect_enabled=True
):
    configure_legacy_env(tmp_path, monkeypatch)
    if worker_secret:
        monkeypatch.setenv("AISTAT_WORKER_SECRET", worker_secret)
    else:
        monkeypatch.delenv("AISTAT_WORKER_SECRET", raising=False)
    monkeypatch.setenv(
        "AISTAT_MULTICA_CONNECT_ENABLED", "1" if connect_enabled else "0"
    )
    monkeypatch.setenv(
        "AISTAT_MULTICA_OFFICIAL_URL", handoff.OFFICIAL_MULTICA_URL
    )
    import aistat.legacy_wsgi as module

    return importlib.reload(module)


def warm_worker(module):
    """Register worker readiness before a user connects (see WSGI helper)."""
    status, _, _ = worker_call(module, handoff.WORKER_PULL_PATH)
    assert status == "200 OK"


@pytest.fixture
def legacy_conn(tmp_path, monkeypatch):
    return load_legacy(tmp_path, monkeypatch), tmp_path


def session_csrf(module, cookies):
    status, _, body = request(module.application, "/api/session", cookie=cookies)
    assert status == "200 OK"
    return json.loads(body.decode("utf-8"))["csrf"]


def submit(module, cookies, csrf, token=TOKEN, **overrides):
    data = {"csrf": csrf, "token": token}
    data.update(overrides)
    return request(
        module.application,
        "/api/connection",
        method="POST",
        body=urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cookie=cookies,
    )


def worker_call(module, path, payload=None, secret=WORKER_SECRET, **kwargs):
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    timestamp = kwargs.get("timestamp", int(time.time()))
    nonce = kwargs.get("nonce") or secrets.token_urlsafe(24)
    signature = kwargs.get("signature") or handoff.worker_signature(
        secret, path, timestamp, nonce, body
    )
    return request(
        module.application,
        path,
        method="POST",
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Nonce": nonce,
            "X-AIStat-Signature": signature,
        },
    )


def test_connection_routes_require_session_and_csrf(legacy_conn):
    module, _ = legacy_conn
    status, _, _ = request(module.application, "/api/connection")
    assert status == "401 Unauthorized"
    status, _, _ = request(
        module.application,
        "/api/connection",
        method="POST",
        body=urlencode({"token": TOKEN}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == "401 Unauthorized"
    cookies = login(module)
    status, _, body = submit(module, cookies, "wrong-csrf")
    assert status == "400 Bad Request"
    assert b"invalid CSRF token" in body


def test_authenticated_dashboard_serves_shared_connection_cabinet(legacy_conn):
    module, _ = legacy_conn
    cookies = login(module)
    status, _, body = request(module.application, "/", cookie=cookies)
    assert status == "200 OK"
    html = body.decode("utf-8")
    assert 'id="connection-cabinet"' in html
    assert 'id="connection-token"' in html
    assert 'type="password"' in html
    assert "https://multica.ai" in html
    assert "server_url" not in html


def test_intake_status_and_throttle(legacy_conn):
    module, tmp_path = legacy_conn
    cookies = login(module)
    csrf = session_csrf(module, cookies)

    status, _, body = request(
        module.application, "/api/connection", cookie=cookies
    )
    assert json.loads(body.decode("utf-8")) == {"status": "none"}

    warm_worker(module)
    status, _, body = submit(module, cookies, csrf, workspace_label=" Мой воркспейс ")
    assert status == "200 OK"
    view = json.loads(body.decode("utf-8"))
    assert view["status"] == "pending"
    assert "server_url" not in view
    assert view["workspace_label"] == "Мой воркспейс"
    assert "token" not in view and "token_epoch" not in view
    assert TOKEN.encode() not in body

    # Invalid input is refused without echoing the submitted values.
    status, _, body = submit(
        module, cookies, csrf, server_url="http://evil.example"
    )
    assert status == "422 Unprocessable Entity"
    assert b"evil.example" not in body

    # Both submissions above already count: attempts are recorded before
    # validation, so garbage cannot bypass the throttle.
    for _ in range(handoff.CONNECTION_MAX_SUBMISSIONS - 2):
        status, _, _ = submit(module, cookies, csrf)
        assert status == "200 OK"
    status, headers, _ = submit(module, cookies, csrf)
    assert status == "429 Too Many Requests"
    assert int(header_values(headers, "Retry-After")[0]) > 0


def test_intake_rate_limit_is_atomic_under_concurrency(legacy_conn):
    module, tmp_path = legacy_conn
    cookies = login(module)
    csrf = session_csrf(module, cookies)
    warm_worker(module)
    barrier = threading.Barrier(12)

    def attempt(index):
        barrier.wait()
        status, headers, _ = submit(
            module, cookies, csrf, token=TOKEN + str(index)
        )
        retry = header_values(headers, "Retry-After")
        return status, retry[0] if retry else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(attempt, range(12)))

    assert [status for status, _ in outcomes].count("200 OK") == 10
    rejected = [
        retry for status, retry in outcomes if status == "429 Too Many Requests"
    ]
    assert len(rejected) == 2
    assert all(int(retry) > 0 for retry in rejected)
    conn = sqlite3.connect(str(tmp_path / "security.db"))
    try:
        row = conn.execute(
            "SELECT submissions FROM connection_throttle"
        ).fetchone()
    finally:
        conn.close()
    assert row == (handoff.CONNECTION_MAX_SUBMISSIONS,)


def test_intake_throttle_storage_error_fails_closed(legacy_conn, monkeypatch):
    module, tmp_path = legacy_conn
    cookies = login(module)
    csrf = session_csrf(module, cookies)
    warm_worker(module)

    def fail_reservation(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic throttle failure")

    def unexpected_validation(*_args, **_kwargs):
        pytest.fail("PAT validation must not run after throttle storage failure")

    monkeypatch.setattr(
        module.handoff, "reserve_connection_submission", fail_reservation
    )
    monkeypatch.setattr(
        module.handoff, "validate_connection_token", unexpected_validation
    )
    status, _, body = submit(module, cookies, csrf)
    assert status == "503 Service Unavailable"
    assert TOKEN.encode() not in body
    assert TOKEN.encode() not in (tmp_path / "security.db").read_bytes()


def test_worker_channel_auth_and_replay(legacy_conn):
    module, _ = legacy_conn
    status, _, _ = request(
        module.application, handoff.WORKER_PULL_PATH, method="POST", body=b"{}"
    )
    assert status == "401 Unauthorized"
    status, _, _ = worker_call(module, handoff.WORKER_PULL_PATH, secret="x" * 48)
    assert status == "401 Unauthorized"
    status, _, _ = worker_call(
        module, handoff.WORKER_PULL_PATH, timestamp=int(time.time()) - 3600
    )
    assert status == "401 Unauthorized"

    timestamp = int(time.time())
    nonce = secrets.token_urlsafe(24)
    status, _, _ = worker_call(
        module, handoff.WORKER_PULL_PATH, timestamp=timestamp, nonce=nonce
    )
    assert status == "200 OK"
    status, _, _ = worker_call(
        module, handoff.WORKER_PULL_PATH, timestamp=timestamp, nonce=nonce
    )
    assert status == "409 Conflict"


def test_full_handoff_replace_and_revoke(legacy_conn):
    module, tmp_path = legacy_conn
    security_db = tmp_path / "security.db"
    cookies = login(module)
    csrf = session_csrf(module, cookies)

    warm_worker(module)
    assert submit(module, cookies, csrf)[0] == "200 OK"
    assert TOKEN.encode() in security_db.read_bytes()

    status, _, body = worker_call(module, handoff.WORKER_PULL_PATH)
    assert status == "200 OK"
    (entry,) = json.loads(body.decode("utf-8"))["pending"]
    assert entry["token"] == TOKEN

    status, _, body = worker_call(
        module,
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
    assert status == "200 OK"
    assert json.loads(body.decode("utf-8"))["results"][0]["ok"]
    # Confirmed handoff physically removes the token from security.db.
    assert TOKEN.encode() not in security_db.read_bytes()
    status, _, body = request(
        module.application, "/api/connection", cookie=cookies
    )
    assert json.loads(body.decode("utf-8"))["status"] == "active"

    # Replace: new epoch goes pending again; a stale ack cannot touch it.
    assert submit(module, cookies, csrf, token=TOKEN + "next")[0] == "200 OK"
    status, _, body = worker_call(
        module,
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
    result = json.loads(body.decode("utf-8"))["results"][0]
    assert not result["ok"] and result["reason"] == "stale-epoch"

    # Revoke erases the pending token at once and flags the worker.
    status, _, body = request(
        module.application,
        "/api/connection/revoke",
        method="POST",
        body=b"",
        headers={"X-CSRF-Token": csrf},
        cookie=cookies,
    )
    assert status == "200 OK"
    assert (TOKEN + "next").encode() not in security_db.read_bytes()
    status, _, body = worker_call(module, handoff.WORKER_PULL_PATH)
    state = json.loads(body.decode("utf-8"))
    assert state["pending"] == []
    (revoked,) = state["revoked"]
    status, _, body = worker_call(
        module,
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
    assert json.loads(body.decode("utf-8"))["results"][0]["ok"]
    status, _, body = worker_call(module, handoff.WORKER_PULL_PATH)
    assert json.loads(body.decode("utf-8"))["revoked"] == []
    status, _, body = request(
        module.application, "/api/connection", cookie=cookies
    )
    assert json.loads(body.decode("utf-8"))["status"] == "revoked"


def test_disabled_worker_channel_fails_closed(tmp_path, monkeypatch):
    module = load_legacy(tmp_path, monkeypatch, worker_secret=None)
    cookies = login(module)
    csrf = session_csrf(module, cookies)
    status, _, _ = submit(module, cookies, csrf)
    assert status == "503 Service Unavailable"
    assert TOKEN.encode() not in (tmp_path / "security.db").read_bytes()
    status, _, _ = request(
        module.application, handoff.WORKER_PULL_PATH, method="POST", body=b"{}"
    )
    assert status == "404 Not Found"


def test_short_or_reused_worker_secret_refused(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError):
        load_legacy(tmp_path, monkeypatch, worker_secret="short")
    from test_legacy_wsgi import INGEST_SECRET

    with pytest.raises(RuntimeError):
        load_legacy(tmp_path, monkeypatch, worker_secret=INGEST_SECRET)
    # Leave a valid module loaded so later reloads elsewhere start clean.
    load_legacy(tmp_path, monkeypatch)


def test_feature_flag_off_makes_everything_fail_closed(tmp_path, monkeypatch):
    module = load_legacy(tmp_path, monkeypatch, connect_enabled=False)
    cookies = login(module)
    csrf = session_csrf(module, cookies)
    _, _, body = request(
        module.application, "/api/connection", cookie=cookies
    )
    assert json.loads(body.decode("utf-8")) == {"status": "disabled"}
    status, _, _ = submit(module, cookies, csrf)
    assert status == "503 Service Unavailable"
    assert TOKEN.encode() not in (tmp_path / "security.db").read_bytes()
    status, _, _ = request(
        module.application,
        "/api/connection/revoke",
        method="POST",
        body=b"",
        headers={"X-CSRF-Token": csrf},
        cookie=cookies,
    )
    assert status == "503 Service Unavailable"
    status, _, _ = worker_call(module, handoff.WORKER_PULL_PATH)
    assert status == "404 Not Found"
    status, _, _ = worker_call(module, handoff.WORKER_ACK_PATH, {"acks": []})
    assert status == "404 Not Found"


def test_intake_pins_connection_to_official_host(legacy_conn):
    module, _ = legacy_conn
    cookies = login(module)
    csrf = session_csrf(module, cookies)
    warm_worker(module)
    for bad in (
        "https://evil.example",
        "https://multica.ai.evil.com",
        "http://multica.ai",
        "https://multica.ai:8443",
        "https://127.0.0.1",
        "https://user@multica.ai",
        "https://multica.ai/",
        "https://multica.ai?query=1",
        "https://multica.ai#fragment",
    ):
        status, _, body = submit(module, cookies, csrf, server_url=bad)
        assert status == "422 Unprocessable Entity", bad
        assert bad.encode() not in body
    status, _, body = submit(
        module, cookies, csrf, server_url=handoff.OFFICIAL_MULTICA_URL
    )
    assert status == "200 OK"
    view = json.loads(body.decode("utf-8"))
    assert "server_url" not in view
    conn = module._security_connection()
    try:
        stored = conn.execute("SELECT server_url FROM connections").fetchone()[0]
    finally:
        conn.close()
    assert stored == handoff.OFFICIAL_MULTICA_URL


def test_legacy_config_cannot_override_the_official_multica_host(legacy_conn):
    module, _ = legacy_conn
    module.MULTICA_OFFICIAL_URL = "https://attacker.example"
    with pytest.raises(RuntimeError, match="exactly https://multica.ai"):
        module._validate_config()


def test_intake_requires_a_ready_worker(tmp_path, monkeypatch):
    module = load_legacy(tmp_path, monkeypatch)
    cookies = login(module)
    csrf = session_csrf(module, cookies)
    status, _, _ = submit(module, cookies, csrf)
    assert status == "503 Service Unavailable"
    assert TOKEN.encode() not in (tmp_path / "security.db").read_bytes()
    warm_worker(module)
    status, _, _ = submit(module, cookies, csrf)
    assert status == "200 OK"
    assert TOKEN.encode() in (tmp_path / "security.db").read_bytes()


def test_pending_token_purged_from_host_after_ttl(tmp_path, monkeypatch):
    module = load_legacy(tmp_path, monkeypatch)
    module.CONNECTION_PENDING_TTL = 0  # expire immediately for the test
    security_db = tmp_path / "security.db"
    cookies = login(module)
    csrf = session_csrf(module, cookies)
    warm_worker(module)
    assert submit(module, cookies, csrf)[0] == "200 OK"
    assert TOKEN.encode() in security_db.read_bytes()
    # The next worker pull finds the token expired: erased and handed over as a
    # revocation to confirm instead of being leased.
    _, _, body = worker_call(module, handoff.WORKER_PULL_PATH)
    state = json.loads(body.decode("utf-8"))
    assert state["pending"] == []
    assert len(state["revoked"]) == 1
    assert TOKEN.encode() not in security_db.read_bytes()
    _, _, body = request(
        module.application, "/api/connection", cookie=cookies
    )
    assert json.loads(body.decode("utf-8"))["status"] == "revocation_pending"
