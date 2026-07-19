"""Worker-side tests: encrypted token store and handoff pull client."""

import json
import logging
import os
import stat
import threading
from urllib.parse import urlsplit

import pytest

import aistat.worker_sync as worker_sync_module
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
    warm_worker,
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
    store.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, "Team", TOKEN, 1
    )
    raw = (tmp_path / "data" / "worker_connections.db").read_bytes()
    assert TOKEN.encode() not in raw
    assert store.get_token(USER_ID) == TOKEN
    assert mode_of(tmp_path / "data" / "worker_connections.db") == 0o600
    assert mode_of(tmp_path / "keys" / "worker.key") == 0o600
    assert mode_of(tmp_path / "keys") == 0o700
    (connection,) = store.list_connections()
    assert connection["user_id"] == USER_ID
    assert "token" not in connection and "token_ciphertext" not in connection
    credential = store.get_credential(USER_ID)
    assert credential.token == TOKEN
    assert credential.token_epoch == 1
    assert credential.workspace_label == "Team"
    assert TOKEN not in repr(credential)


def test_store_replace_delete_and_reopen(tmp_path):
    store = make_store(tmp_path)
    store.store_token(USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN, 1)
    store.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN + "v2", 2
    )
    # Same key file, fresh instance: data survives, old epoch is replaced.
    reopened = make_store(tmp_path)
    assert reopened.get_token(USER_ID) == TOKEN + "v2"
    assert reopened.list_connections()[0]["token_epoch"] == 2
    assert reopened.delete_connection(USER_ID)
    assert not reopened.delete_connection(USER_ID)
    assert reopened.get_token(USER_ID) is None


def test_store_rejects_stale_writer_and_same_epoch_conflict(tmp_path):
    store = make_store(tmp_path)
    assert store.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, "new", TOKEN + "v2", 2
    )
    assert not store.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, "old", TOKEN, 1
    )
    credential = store.get_credential(USER_ID)
    assert (credential.token, credential.token_epoch, credential.workspace_label) == (
        TOKEN + "v2",
        2,
        "new",
    )
    with pytest.raises(WorkerStoreError, match="conflicting data"):
        store.store_token(
            USER_ID,
            handoff.OFFICIAL_MULTICA_URL,
            "other",
            TOKEN + "other",
            2,
        )


def test_revoke_tombstone_prevents_resurrection_after_reopen(tmp_path):
    store = make_store(tmp_path)
    assert store.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN, 1
    )
    assert store.delete_connection(USER_ID, 2)
    assert store.get_credential(USER_ID) is None
    assert not store.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN, 1
    )

    reopened = make_store(tmp_path)
    assert reopened.get_credential(USER_ID) is None
    assert reopened.delete_connection(USER_ID, 2)  # exact replay is idempotent
    assert not reopened.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN, 2
    )


def test_stale_revoke_cannot_delete_reconnected_epoch(tmp_path):
    store = make_store(tmp_path)
    assert store.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN + "v3", 3
    )
    assert not store.delete_connection(USER_ID, 2)
    credential = store.get_credential(USER_ID)
    assert (credential.token, credential.token_epoch) == (TOKEN + "v3", 3)


def test_credential_fence_is_per_tenant_and_released(tmp_path):
    store = make_store(tmp_path)
    store.store_token(USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN, 1)
    reopened = make_store(tmp_path)
    same_done = threading.Event()
    neighbor_done = threading.Event()

    def replace_same_tenant():
        reopened.store_token(
            USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN + "v2", 2
        )
        same_done.set()

    def store_healthy_neighbor():
        reopened.store_token(
            USER_ID + 1,
            handoff.OFFICIAL_MULTICA_URL,
            None,
            TOKEN + "neighbor",
            1,
        )
        neighbor_done.set()

    with store.credential_fence(USER_ID):
        same_thread = threading.Thread(target=replace_same_tenant)
        neighbor_thread = threading.Thread(target=store_healthy_neighbor)
        same_thread.start()
        neighbor_thread.start()
        assert neighbor_done.wait(timeout=5)
        assert not same_done.wait(timeout=0.1)

    same_thread.join(timeout=5)
    neighbor_thread.join(timeout=5)
    assert same_done.is_set()
    assert not same_thread.is_alive() and not neighbor_thread.is_alive()

    with pytest.raises(RuntimeError):
        with store.credential_fence(USER_ID):
            raise RuntimeError("synthetic failure")
    with store.credential_fence(USER_ID) as fence:
        assert fence.get_credential().token_epoch == 2


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_store_rejects_unsafe_credential_fence_directory(tmp_path, kind):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fence_root = data_dir / ".worker_connection_fences"
    if kind == "symlink":
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        (foreign / "sentinel").write_text("unchanged", encoding="utf-8")
        fence_root.symlink_to(foreign, target_is_directory=True)
    else:
        fence_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WorkerStoreError, match="fence directory"):
        make_store(tmp_path)
    if kind == "symlink":
        assert (foreign / "sentinel").read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("change", ["store", "revoke"])
def test_pull_once_does_not_ack_or_apply_stale_local_epoch(
    tmp_path, monkeypatch, change
):
    config = worker_config(tmp_path)
    store = make_store(tmp_path)
    store.store_token(
        USER_ID, handoff.OFFICIAL_MULTICA_URL, "current", TOKEN + "v3", 3
    )
    if change == "store":
        state = {
            "pending": [
                {
                    "user_id": USER_ID,
                    "server_url": handoff.OFFICIAL_MULTICA_URL,
                    "workspace_label": "stale",
                    "token": TOKEN,
                    "token_epoch": 2,
                    "lease_id": "stale-lease",
                }
            ],
            "revoked": [],
        }
    else:
        state = {
            "pending": [],
            "revoked": [{"user_id": USER_ID, "token_epoch": 2}],
        }
    calls = []

    def fake_call(_config, _opener, path, payload, now=None):
        calls.append((path, payload))
        assert path == handoff.WORKER_PULL_PATH
        return state

    monkeypatch.setattr(worker_sync_module, "_call", fake_call)
    summary = pull_once(config, opener=object())

    assert summary == {"stored": 0, "revoked": 0, "results": []}
    assert calls == [(handoff.WORKER_PULL_PATH, {})]
    current = make_store(tmp_path).get_credential(USER_ID)
    assert (current.token, current.token_epoch, current.workspace_label) == (
        TOKEN + "v3",
        3,
        "current",
    )


def test_pull_once_isolates_store_conflict_and_redacts_local_failure(
    tmp_path, monkeypatch, caplog
):
    config = worker_config(tmp_path)
    conflict_user = 11
    healthy_user = 22
    revoked_user = 33
    conflict_token = TOKEN + "-conflict-secret"
    healthy_token = TOKEN + "-healthy"
    raw_path = str(tmp_path / "private-worker-store")
    raw_cli = "multica auth status --token raw-cli-secret"
    raw_detail = "synthetic-store-exception-detail"

    store = make_store(tmp_path)
    assert store.store_token(
        conflict_user, handoff.OFFICIAL_MULTICA_URL, "original", TOKEN, 1
    )
    assert store.store_token(
        revoked_user, handoff.OFFICIAL_MULTICA_URL, "revoke-me", TOKEN, 1
    )

    original_store_token = WorkerTokenStore.store_token

    def store_token_with_adversarial_detail(self, *args, **kwargs):
        try:
            return original_store_token(self, *args, **kwargs)
        except WorkerStoreError as exc:
            raise WorkerStoreError(
                "{}; token={}; path={}; cli={}; source={}".format(
                    raw_detail, args[3], raw_path, raw_cli, exc
                )
            ) from exc

    monkeypatch.setattr(
        WorkerTokenStore, "store_token", store_token_with_adversarial_detail
    )
    state = {
        "pending": [
            {
                "user_id": conflict_user,
                "server_url": handoff.OFFICIAL_MULTICA_URL,
                "workspace_label": "conflict",
                "token": conflict_token,
                "token_epoch": 1,
                "lease_id": "conflict-lease",
            },
            {
                "user_id": healthy_user,
                "server_url": handoff.OFFICIAL_MULTICA_URL,
                "workspace_label": "healthy",
                "token": healthy_token,
                "token_epoch": 1,
                "lease_id": "healthy-lease",
            },
        ],
        "revoked": [{"user_id": revoked_user, "token_epoch": 2}],
    }
    ack_calls = []

    def fake_call(_config, _opener, path, payload, now=None):
        if path == handoff.WORKER_PULL_PATH:
            assert payload == {}
            return state
        assert path == handoff.WORKER_ACK_PATH
        ack_calls.append(payload)
        return {
            "results": [
                {"ok": True, "user_id": ack["user_id"], "status": ack["result"]}
                for ack in payload["acks"]
            ]
        }

    monkeypatch.setattr(worker_sync_module, "_call", fake_call)
    with caplog.at_level(logging.DEBUG):
        summary = pull_once(config, opener=object())

    assert summary["stored"] == 1
    assert summary["revoked"] == 1
    assert summary["failed"] == [
        {
            "user_id": conflict_user,
            "detail": worker_sync_module.CREDENTIAL_STORE_FAILURE,
        }
    ]
    assert len(summary["results"]) == 2
    assert len(ack_calls) == 1
    ack_by_user = {ack["user_id"]: ack for ack in ack_calls[0]["acks"]}
    assert ack_by_user == {
        healthy_user: {
            "user_id": healthy_user,
            "token_epoch": 1,
            "lease_id": "healthy-lease",
            "result": "stored",
        },
        revoked_user: {
            "user_id": revoked_user,
            "token_epoch": 2,
            "result": "revoked",
        },
    }
    assert conflict_user not in ack_by_user

    reopened = make_store(tmp_path)
    conflict = reopened.get_credential(conflict_user)
    healthy = reopened.get_credential(healthy_user)
    assert (conflict.token, conflict.workspace_label, conflict.token_epoch) == (
        TOKEN,
        "original",
        1,
    )
    assert (healthy.token, healthy.workspace_label, healthy.token_epoch) == (
        healthy_token,
        "healthy",
        1,
    )
    assert reopened.get_credential(revoked_user) is None

    local_surfaces = json.dumps(summary) + caplog.text
    ack_surface = json.dumps(ack_calls)
    for sensitive in (
        conflict_token,
        raw_path,
        raw_cli,
        raw_detail,
        "conflicting data",
    ):
        assert sensitive not in local_surfaces
        assert sensitive not in ack_surface
    assert worker_sync_module.CREDENTIAL_STORE_FAILURE in caplog.text


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
    store.store_token(USER_ID, handoff.OFFICIAL_MULTICA_URL, None, TOKEN, 1)
    (tmp_path / "keys" / "worker.key").unlink()
    rotated = make_store(tmp_path)  # generates a fresh key
    with pytest.raises(WorkerStoreError):
        rotated.get_token(USER_ID)


@pytest.mark.parametrize(
    "server_url",
    ["", handoff.OFFICIAL_MULTICA_URL],
)
def test_store_normalizes_empty_or_exact_legacy_host(tmp_path, server_url):
    store = make_store(tmp_path)
    store.store_token(USER_ID, server_url, None, TOKEN, 1)
    assert store.list_connections()[0]["server_url"] == handoff.OFFICIAL_MULTICA_URL


def test_store_rejects_poisoned_host_before_encrypting_token(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="unsupported Multica server"):
        store.store_token(
            USER_ID, "https://attacker.example", None, TOKEN, 1
        )
    assert store.list_connections() == []
    assert TOKEN.encode() not in (
        tmp_path / "data" / "worker_connections.db"
    ).read_bytes()


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
    warm_worker(client)
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
    warm_worker(client)
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
