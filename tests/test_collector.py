"""Per-user collector: tenant isolation, failure isolation, backpressure."""

from aistat.cli_profile import CliProfileError
from aistat.collector import Collector, _TenantLock
from aistat.config import Config
from aistat.db import connect
from aistat.worker_store import WorkerStoreError

from test_poller import make_runner

TOKEN_A = "mul_token_for_user_a_secret"
TOKEN_B = "mul_token_for_user_b_secret"


def make_config(tmp_path):
    config = Config()
    config.cli_profiles_dir = tmp_path / "cli_profiles"
    config.worker_tenants_dir = tmp_path / "worker_tenants"
    config.multica_official_url = "https://multica.ai"
    return config


class FakeStore:
    def __init__(self, connections, tokens):
        self._connections = connections
        self._tokens = tokens

    def list_connections(self):
        return [dict(c) for c in self._connections]

    def get_token(self, user_id):
        value = self._tokens.get(int(user_id))
        if value == "RAISE":
            raise WorkerStoreError("cannot decrypt")
        return value


class FakeProfile:
    """Stand-in profile whose runner serves the shared poller fixtures."""

    instances = []

    def __init__(self, config, user_id, *, login_fail=False, ws_fail=False):
        self.config = config
        self.user_id = user_id
        self.login_fail = login_fail
        self.ws_fail = ws_fail
        self.logged_in_with = None
        self.cleaned = False
        self._runner = make_runner()
        FakeProfile.instances.append(self)

    def login(self, token):
        self.logged_in_with = token
        if self.login_fail:
            raise CliProfileError("official CLI login failed for the connection")

    def select_workspace(self, label):
        if self.ws_fail:
            raise CliProfileError("the connection's workspace could not be resolved")
        return {"id": "ws-" + str(self.user_id)}

    def runner(self, args):
        return self._runner(args)

    def cleanup(self):
        self.cleaned = True

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.cleanup()


def factory_with(behaviors=None):
    """Return a profile_factory that applies per-user behavior overrides."""
    behaviors = behaviors or {}
    FakeProfile.instances = []

    def factory(config, user_id):
        return FakeProfile(config, user_id, **behaviors.get(int(user_id), {}))

    return factory


class RecordingPublisher:
    def __init__(self):
        self.calls = []

    def __call__(self, config, db_path, tenant_id):
        self.calls.append((db_path, tenant_id))
        return {"status": "ok", "tenant_id": tenant_id}


def runtimes_count(db_path):
    conn = connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM runtimes").fetchone()[0]
    finally:
        conn.close()


# -- two tenants collected into strictly separate DBs/snapshots --------------

def test_two_users_land_in_separate_tenant_databases(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 1},
            {"user_id": 202, "workspace_label": "beta", "token_epoch": 3},
        ],
        tokens={101: TOKEN_A, 202: TOKEN_B},
    )
    publisher = RecordingPublisher()
    collector = Collector(
        config, store,
        profile_factory=factory_with(),
        publish_fn=publisher,
        report_fn=None,
    )
    outcomes = collector.collect_once()

    assert {o.user_id: o.status for o in outcomes} == {101: "collected", 202: "collected"}
    db_101 = config.worker_tenant_db_path(101)
    db_202 = config.worker_tenant_db_path(202)
    assert db_101 != db_202
    assert runtimes_count(db_101) == 3
    assert runtimes_count(db_202) == 3
    # each snapshot is published under its own tenant id, from its own db
    assert (db_101, 101) in publisher.calls
    assert (db_202, 202) in publisher.calls
    assert len(publisher.calls) == 2
    # each connection was logged in with its own token, then cleaned up
    by_user = {p.user_id: p for p in FakeProfile.instances}
    assert by_user[101].logged_in_with == TOKEN_A
    assert by_user[202].logged_in_with == TOKEN_B
    assert all(p.cleaned for p in FakeProfile.instances)


# -- one connection's failure does not stop the others -----------------------

def test_login_failure_is_isolated_to_that_connection(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 1},
            {"user_id": 202, "workspace_label": "beta", "token_epoch": 1},
        ],
        tokens={101: TOKEN_A, 202: TOKEN_B},
    )
    publisher = RecordingPublisher()
    collector = Collector(
        config, store,
        profile_factory=factory_with({101: {"login_fail": True}}),
        publish_fn=publisher,
        report_fn=None,
    )
    outcomes = {o.user_id: o for o in collector.collect_once()}

    assert outcomes[101].status == "failed"
    assert outcomes[202].status == "collected"
    # the healthy tenant was still polled and published
    assert runtimes_count(config.worker_tenant_db_path(202)) == 3
    assert publisher.calls == [(config.worker_tenant_db_path(202), 202)]
    # the failed connection was still cleaned up (no residue left behind)
    assert all(p.cleaned for p in FakeProfile.instances)


def test_failure_detail_carries_no_token_or_path(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[{"user_id": 101, "workspace_label": "alpha", "token_epoch": 1}],
        tokens={101: TOKEN_A},
    )
    collector = Collector(
        config, store,
        profile_factory=factory_with({101: {"login_fail": True}}),
        publish_fn=RecordingPublisher(),
        report_fn=None,
    )
    outcome = collector.collect_once()[0]
    assert outcome.status == "failed"
    assert TOKEN_A not in outcome.detail
    assert "aistat-conn" not in outcome.detail
    assert str(tmp_path) not in outcome.detail


# -- revoked / unreadable token ---------------------------------------------

def test_revoked_connection_is_skipped(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[{"user_id": 101, "workspace_label": "alpha", "token_epoch": 1}],
        tokens={101: None},
    )
    publisher = RecordingPublisher()
    collector = Collector(
        config, store, profile_factory=factory_with(),
        publish_fn=publisher, report_fn=None,
    )
    outcome = collector.collect_once()[0]
    assert outcome.status == "skipped"
    assert publisher.calls == []
    assert FakeProfile.instances == []  # never logs a revoked token in


def test_unreadable_token_fails_safely(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[{"user_id": 101, "workspace_label": "alpha", "token_epoch": 1}],
        tokens={101: "RAISE"},
    )
    collector = Collector(
        config, store, profile_factory=factory_with(),
        publish_fn=RecordingPublisher(), report_fn=None,
    )
    outcome = collector.collect_once()[0]
    assert outcome.status == "failed"
    assert TOKEN_A not in outcome.detail


# -- backpressure: a held per-tenant lock blocks a competing poll ------------

def test_held_lock_prevents_competing_poll_of_same_tenant(tmp_path):
    config = make_config(tmp_path)
    config.ensure_cli_profiles_dir()
    store = FakeStore(
        connections=[{"user_id": 101, "workspace_label": "alpha", "token_epoch": 1}],
        tokens={101: TOKEN_A},
    )
    publisher = RecordingPublisher()
    collector = Collector(
        config, store, profile_factory=factory_with(),
        publish_fn=publisher, report_fn=None,
    )
    lock = _TenantLock(config.cli_profiles_dir, 101)
    assert lock.acquire()
    try:
        outcome = collector.collect_once()[0]
        assert outcome.status == "skipped"
        assert "in progress" in outcome.detail
        assert publisher.calls == []
    finally:
        lock.release()
    # once released, the same tenant collects normally
    assert collector.collect_once()[0].status == "collected"


# -- restart idempotency: re-running does not duplicate rows ------------------

def test_second_cycle_is_idempotent(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[{"user_id": 101, "workspace_label": "alpha", "token_epoch": 1}],
        tokens={101: TOKEN_A},
    )
    collector = Collector(
        config, store, profile_factory=factory_with(),
        publish_fn=RecordingPublisher(), report_fn=None,
    )
    collector.collect_once()
    collector.collect_once()
    assert runtimes_count(config.worker_tenant_db_path(101)) == 3


# -- outcome reporting to the host cabinet -----------------------------------

def test_outcome_is_reported_with_epoch(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[{"user_id": 101, "workspace_label": "alpha", "token_epoch": 9}],
        tokens={101: TOKEN_A},
    )
    reports = []

    def report_fn(cfg, user_id, epoch, ok, error):
        reports.append((user_id, epoch, ok, error))

    collector = Collector(
        config, store, profile_factory=factory_with(),
        publish_fn=RecordingPublisher(), report_fn=report_fn,
    )
    collector.collect_once()
    assert reports == [(101, 9, True, None)]
