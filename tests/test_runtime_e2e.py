"""Synthetic runtime data lifecycle (FAN-1404, criterion 8).

Drives the real modules the supervisor runs — the signed worker pull/ack
channel, the encrypted worker store and the per-user collector — end to end
against a fake host, proving the whole chain:

    submit -> pull -> encrypted local store -> ack/host erase
           -> collector -> tenant-bound publish -> sync report

plus replace, revoke, restart and credential-epoch transitions. Token values
never appear in the store at rest or in any report.
"""

import json

from aistat import handoff
from aistat.collector import Collector
from aistat.worker_sync import pull_once, report_sync
from aistat.worker_store import WorkerTokenStore

from test_collector import factory_with, make_config, RecordingPublisher

BASE = "https://host.example"
WORKER_SECRET = "w" * 40
TOKEN_1 = "mul-secret-token-epoch-1"
TOKEN_2 = "mul-secret-token-epoch-2"


class Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self, _n=-1):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeHost:
    """Minimal signed worker host: serves pending/revoked, erases on ack."""

    def __init__(self, secret=WORKER_SECRET):
        self.secret = secret
        self.pending = []
        self.revoked = []
        self.erased = []
        self.sync_reports = []
        self.pull_count = 0
        self.bad_signature = 0

    def add_pending(self, user_id, token, epoch, label=None):
        self.pending.append({
            "user_id": user_id,
            "server_url": handoff.OFFICIAL_MULTICA_URL,
            "workspace_label": label,
            "token": token,
            "token_epoch": epoch,
            "lease_id": "lease-{}-{}".format(user_id, epoch),
        })

    def add_revoke(self, user_id, epoch):
        self.revoked.append({"user_id": user_id, "token_epoch": epoch})

    def opener(self, request, timeout=None):
        path = request.full_url[len(BASE):]
        headers = {k.lower(): v for k, v in request.header_items()}
        body = request.data or b""
        try:
            handoff.verify_worker_request(
                self.secret, path,
                headers.get("x-aistat-timestamp"),
                headers.get("x-aistat-nonce"),
                headers.get("x-aistat-signature"),
                body, 300,
            )
        except ValueError:
            self.bad_signature += 1
            raise
        if path == handoff.WORKER_PULL_PATH:
            self.pull_count += 1
            return Resp({"pending": list(self.pending),
                         "revoked": list(self.revoked)})
        if path == handoff.WORKER_ACK_PATH:
            return self._ack(json.loads(body.decode("utf-8")).get("acks") or [])
        raise AssertionError("unexpected path {}".format(path))

    def _ack(self, acks):
        results = []
        for ack in acks:
            user_id = int(ack["user_id"])
            epoch = int(ack["token_epoch"])
            result = ack["result"]
            if result == "stored":
                self.erased.append((user_id, epoch))
                self.pending = [p for p in self.pending
                                if not (p["user_id"] == user_id
                                        and p["token_epoch"] == epoch)]
            elif result == "revoked":
                self.erased.append((user_id, epoch))
                self.revoked = [r for r in self.revoked
                                if not (r["user_id"] == user_id
                                        and r["token_epoch"] == epoch)]
            elif result in ("sync_ok", "sync_error"):
                self.sync_reports.append(
                    (user_id, epoch, result, ack.get("error")))
            results.append({"user_id": user_id, "ok": True})
        return Resp({"results": results})


def worker_config(tmp_path):
    config = make_config(tmp_path)
    config.worker_sync_url = BASE
    config.worker_secret = WORKER_SECRET
    config.worker_pull_interval_seconds = 300
    config.worker_store_path = tmp_path / "store" / "connections.db"
    config.worker_key_path = tmp_path / "key" / "worker.key"
    config.publish_timeout_seconds = 5
    return config


def open_store(config):
    return WorkerTokenStore(config.worker_store_path, config.worker_key_path)


# ---- submit -> pull -> encrypted store -> ack/erase ----------------------

def test_pull_stores_encrypted_and_host_erases(tmp_path):
    config = worker_config(tmp_path)
    host = FakeHost()
    host.add_pending(101, TOKEN_1, epoch=1, label="alpha")

    summary = pull_once(config, opener=host.opener)
    assert summary["stored"] == 1

    store = open_store(config)
    assert store.get_token(101) == TOKEN_1
    # The host physically erased its plaintext copy after the signed ack.
    assert host.pending == []
    assert (101, 1) in host.erased
    # Token is encrypted at rest — the plaintext never appears in the db file.
    raw = config.worker_store_path.read_bytes()
    assert TOKEN_1.encode() not in raw


def test_replace_supersedes_previous_epoch(tmp_path):
    config = worker_config(tmp_path)
    host = FakeHost()
    host.add_pending(101, TOKEN_1, epoch=1)
    pull_once(config, opener=host.opener)

    host.add_pending(101, TOKEN_2, epoch=2, label="beta")
    pull_once(config, opener=host.opener)

    credential = open_store(config).get_credential(101)
    assert credential.token == TOKEN_2
    assert credential.token_epoch == 2


def test_stale_epoch_is_ignored(tmp_path):
    config = worker_config(tmp_path)
    host = FakeHost()
    host.add_pending(101, TOKEN_2, epoch=5)
    pull_once(config, opener=host.opener)

    # A late, lower-epoch push must not overwrite the current credential.
    host.pending = []
    host.add_pending(101, "stale-old-token", epoch=3)
    summary = pull_once(config, opener=host.opener)
    assert summary["stored"] == 0

    credential = open_store(config).get_credential(101)
    assert credential.token == TOKEN_2
    assert credential.token_epoch == 5


def test_revoke_deletes_local_token_and_acks(tmp_path):
    config = worker_config(tmp_path)
    host = FakeHost()
    host.add_pending(101, TOKEN_2, epoch=2)
    pull_once(config, opener=host.opener)
    assert open_store(config).get_token(101) == TOKEN_2

    # A revoke bumps the credential epoch (stored 2 -> revoke 3).
    host.add_revoke(101, epoch=3)
    summary = pull_once(config, opener=host.opener)
    assert summary["revoked"] == 1
    assert open_store(config).get_token(101) is None
    assert (101, 3) in host.erased
    assert host.revoked == []


def test_restart_persists_store_and_revoke_tombstone(tmp_path):
    config = worker_config(tmp_path)
    host = FakeHost()
    host.add_pending(101, TOKEN_1, epoch=1)
    pull_once(config, opener=host.opener)

    # Simulate a supervisor/collector restart: a brand-new store object on the
    # same on-disk paths must read the persisted credential.
    assert open_store(config).get_token(101) == TOKEN_1

    host.add_revoke(101, epoch=2)  # revoke bumps the epoch (stored 1 -> 2)
    pull_once(config, opener=host.opener)
    # A fresh store after restart sees the revoke, not a resurrected token.
    assert open_store(config).get_token(101) is None


def test_bad_worker_signature_is_rejected(tmp_path):
    config = worker_config(tmp_path)
    host = FakeHost(secret="d" * 40)  # host expects a different secret
    host.add_pending(101, TOKEN_1, epoch=1)
    # The worker signs with its own secret; the host rejects the mismatch and
    # nothing is stored (the signed channel is enforced end to end).
    try:
        pull_once(config, opener=host.opener)
    except Exception:
        pass
    assert host.bad_signature >= 1
    assert not config.worker_store_path.exists() or \
        open_store(config).get_token(101) is None


# ---- collector -> tenant-bound publish -> sync report --------------------

def test_collector_publishes_per_tenant_and_reports_sync(tmp_path):
    config = worker_config(tmp_path)
    host = FakeHost()
    host.add_pending(101, TOKEN_1, epoch=1, label="alpha")
    pull_once(config, opener=host.opener)

    store = open_store(config)
    publisher = RecordingPublisher()
    collector = Collector(
        config, store,
        profile_factory=factory_with(),
        publish_fn=publisher,
        report_fn=lambda cfg, user, epoch, ok, error=None: report_sync(
            cfg, user, epoch, ok, error, opener=host.opener),
    )
    outcomes = {o.user_id: o.status for o in collector.collect_once()}
    assert outcomes == {101: "collected"}

    tenant_db = config.worker_tenant_db_path(101)
    assert (tenant_db, 101) in publisher.calls  # published under its own tenant
    assert len(publisher.calls) == 1
    # The per-connection sync outcome reached the host for the user's cabinet,
    # bound to the exact credential epoch, with no token in the report.
    assert (101, 1, "sync_ok", None) in host.sync_reports
