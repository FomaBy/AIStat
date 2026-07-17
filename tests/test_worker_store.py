"""Worker-side tests: encrypted token store and handoff pull client."""

import json
import logging
import os
import stat
from urllib.parse import urlsplit

import pytest

from aistat import handoff
from aistat.config import Config
from aistat.worker_store import WorkerStoreError, WorkerTokenStore
from aistat.worker_sync import WorkerSyncError, pull_once, report_sync

from test_connections_wsgi import (
    TOKEN,
    WORKER_SECRET,
    login,
    make_config,
    submit,
)
from aistat.wsgi import create_app

USER_ID = 7


def make_store(tmp_path):
    return WorkerTokenStore(
        tmp_path / "data" / "worker_connections.db",
        tmp_path / "keys" / "worker.key",
    )


def worker_config(tmp_path, **overrides):
    config = Config()
    config.worker_sync_url = "https://aistat.example"
    config.worker_secret = WORKER_SECRET
    config.ingest_secret = "worker-side-ingest-" + "i" * 32
    config.worker_store_path = tmp_path / "data" / "worker_connections.db"
    config.worker_key_path = tmp_path / "keys" / "worker.key"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# --- encrypted store ---------------------------------------------------


def test_store_encrypts_at_rest_with_private_files(tmp_path):
    store = make_store(tmp_path)
    store.store_token(USER_ID, "https://multica.example", "Team", TOKEN, 1)
    raw = (tmp_path / "data" / "worker_connections.db").read_bytes()
    assert TOKEN.encode() not in raw
    assert store.get_token(USER_ID) == TOKEN
    assert mode_of(tmp_path / "data" / "worker_connections.db") == 0o600
    assert mode_of(tmp_path / "keys" / "worker.key") == 0o600
    assert mode_of(tmp_path / "keys") == 0o700
    (connection,) = store.list_connections()
    assert connection["user_id"] == USER_ID
    assert "token" not in connection and "token_ciphertext" not in connection


def test_store_replace_delete_and_reopen(tmp_path):
    store = make_store(tmp_path)
    store.store_token(USER_ID, "https://multica.example", None, TOKEN, 1)
    store.store_token(
        USER_ID, "https://multica.example", None, TOKEN + "v2", 2
    )
    # Same key file, fresh instance: data survives, old epoch is replaced.
    reopened = make_store(tmp_path)
    assert reopened.get_token(USER_ID) == TOKEN + "v2"
    assert reopened.list_connections()[0]["token_epoch"] == 2
    assert reopened.delete_connection(USER_ID)
    assert not reopened.delete_connection(USER_ID)
    assert reopened.get_token(USER_ID) is None


def test_store_refuses_key_next_to_ciphertext(tmp_path):
    with pytest.raises(WorkerStoreError):
        WorkerTokenStore(
            tmp_path / "data" / "worker_connections.db",
            tmp_path / "data" / "worker.key",
        )


def test_store_refuses_invalid_key_file(tmp_path):
    key_path = tmp_path / "keys" / "worker.key"
    key_path.parent.mkdir(parents=True)
    key_path.write_bytes(b"not-a-fernet-key")
    with pytest.raises(WorkerStoreError):
        make_store(tmp_path)


def test_wrong_key_cannot_decrypt(tmp_path):
    store = make_store(tmp_path)
    store.store_token(USER_ID, "https://multica.example", None, TOKEN, 1)
    (tmp_path / "keys" / "worker.key").unlink()
    rotated = make_store(tmp_path)  # generates a fresh key
    with pytest.raises(WorkerStoreError):
        rotated.get_token(USER_ID)


# --- pull client against the real Flask host ---------------------------


class FlaskOpener:
    """Adapts the worker's urllib calls onto a Flask test client."""

    def __init__(self, client):
        self.client = client

    def __call__(self, request, timeout=None):
        response = self.client.post(
            urlsplit(request.full_url).path,
            data=request.data,
            headers=dict(request.header_items()),
            base_url="https://localhost",
        )

        class _Response:
            status = response.status_code

            def getcode(self):
                return response.status_code

            def read(self, size=-1):
                return response.get_data()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        if response.status_code >= 400:
            import urllib.error

            raise urllib.error.HTTPError(
                request.full_url, response.status_code, "error", {}, None
            )
        return _Response()


@pytest.fixture
def host(tmp_path):
    config = make_config(tmp_path / "host")
    (tmp_path / "host").mkdir(parents=True, exist_ok=True)
    app = create_app(config)
    app.config.update(TESTING=True)
    client = app.test_client()
    csrf = login(client)
    return client, config, csrf


def test_end_to_end_handoff_via_real_host(host, tmp_path, caplog):
    client, host_config, csrf = host
    assert submit(client, csrf).status_code == 200
    config = worker_config(tmp_path)
    with caplog.at_level(logging.DEBUG):
        summary = pull_once(config, opener=FlaskOpener(client))
    assert summary["stored"] == 1 and summary["revoked"] == 0
    assert all(entry["ok"] for entry in summary["results"])
    # Worker holds the token encrypted; the host no longer holds it at all.
    store = make_store(tmp_path)
    user_id = store.list_connections()[0]["user_id"]
    assert store.get_token(user_id) == TOKEN
    assert TOKEN.encode() not in config.worker_store_path.read_bytes()
    assert TOKEN.encode() not in host_config.security_db_path.read_bytes()
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()["status"] == "active"
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(summary)

    # Revoke on the host, next worker cycle deletes the local token.
    assert client.post(
        "/api/connection/revoke",
        headers={"X-CSRF-Token": csrf},
        base_url="https://localhost",
    ).status_code == 200
    summary = pull_once(config, opener=FlaskOpener(client))
    assert summary["revoked"] == 1
    assert make_store(tmp_path).get_token(user_id) is None
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()["status"] == "revoked"

    # An idle cycle stores nothing and sends no acks.
    summary = pull_once(config, opener=FlaskOpener(client))
    assert summary == {"stored": 0, "revoked": 0, "results": []}


def test_sync_error_report_reaches_cabinet(host, tmp_path):
    client, _, csrf = host
    assert submit(client, csrf).status_code == 200
    config = worker_config(tmp_path)
    pull_once(config, opener=FlaskOpener(client))
    store = make_store(tmp_path)
    connection = store.list_connections()[0]
    result = report_sync(
        config,
        connection["user_id"],
        connection["token_epoch"],
        ok=False,
        error="multica CLI timeout",
        opener=FlaskOpener(client),
    )
    assert result["ok"] and result["status"] == "error"
    assert client.get(
        "/api/connection", base_url="https://localhost"
    ).get_json()["last_sync_error"] == "multica CLI timeout"


def test_worker_sync_config_validation(tmp_path):
    dummy_opener = object()
    config = worker_config(tmp_path, worker_sync_url=None)
    with pytest.raises(WorkerSyncError):
        pull_once(config, opener=dummy_opener)
    config = worker_config(
        tmp_path, worker_sync_url="http://aistat.example"
    )
    with pytest.raises(WorkerSyncError):
        pull_once(config, opener=dummy_opener)
    config = worker_config(tmp_path, worker_secret="short")
    with pytest.raises(WorkerSyncError):
        pull_once(config, opener=dummy_opener)
    shared = "shared-secret-" + "s" * 32
    config = worker_config(
        tmp_path, worker_secret=shared, ingest_secret=shared
    )
    with pytest.raises(WorkerSyncError):
        pull_once(config, opener=dummy_opener)
    config = worker_config(tmp_path, worker_pull_interval_seconds=10)
    with pytest.raises(WorkerSyncError):
        pull_once(config, opener=dummy_opener)
