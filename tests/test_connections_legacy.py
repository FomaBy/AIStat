"""Legacy cPanel contour route tests for "connect your Multica" (FAN-1220)."""

import importlib
import json
import secrets
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


def load_legacy(tmp_path, monkeypatch, worker_secret=WORKER_SECRET):
    configure_legacy_env(tmp_path, monkeypatch)
    if worker_secret:
        monkeypatch.setenv("AISTAT_WORKER_SECRET", worker_secret)
    else:
        monkeypatch.delenv("AISTAT_WORKER_SECRET", raising=False)
    monkeypatch.setenv("AISTAT_DEFAULT_SERVER_URL", "https://multica.example")
    import aistat.legacy_wsgi as module

    return importlib.reload(module)


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


def test_intake_status_and_throttle(legacy_conn):
    module, tmp_path = legacy_conn
    cookies = login(module)
    csrf = session_csrf(module, cookies)

    status, _, body = request(
        module.application, "/api/connection", cookie=cookies
    )
    assert json.loads(body.decode("utf-8")) == {"status": "none"}

    status, _, body = submit(module, cookies, csrf, workspace_label=" Мой воркспейс ")
    assert status == "200 OK"
    view = json.loads(body.decode("utf-8"))
    assert view["status"] == "pending"
    assert view["server_url"] == "https://multica.example"
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
