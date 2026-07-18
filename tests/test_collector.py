"""Per-user collector: tenant isolation, failure isolation, backpressure."""

import hashlib
import os
import stat

import pytest

import aistat.cli_profile as cli_profile_module
import aistat.collector as collector_module
from aistat.cli_profile import (
    CliProfileError,
    ConnectionCliProfile,
    ExecResult,
)
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
        self.get_token_calls = []

    def list_connections(self):
        return [dict(c) for c in self._connections]

    def get_token(self, user_id):
        self.get_token_calls.append(int(user_id))
        value = self._tokens.get(int(user_id))
        if value == "RAISE":
            raise WorkerStoreError("cannot decrypt")
        return value


class FakeProfile:
    """Stand-in profile whose runner serves the shared poller fixtures."""

    instances = []

    def __init__(
        self,
        config,
        user_id,
        *,
        login_fail=False,
        ws_fail=False,
        cleanup_fail=False,
        discard_fail=False,
    ):
        self.config = config
        self.user_id = user_id
        self.login_fail = login_fail
        self.ws_fail = ws_fail
        self.cleanup_fail = cleanup_fail
        self.discard_fail = discard_fail
        self.logged_in_with = None
        self.cleaned = False
        self.discarded = False
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
        if self.cleanup_fail:
            raise CliProfileError("the connection profile residue could not be removed")

    def discard_residue(self):
        self.discarded = True
        if self.discard_fail:
            raise CliProfileError("the connection profile residue could not be removed")

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


class RecordingExecutor:
    """Real-profile executor with optional synthetic lifecycle side effects."""

    def __init__(self, *, login_rc=0, logout_rc=0, on_login=None):
        self.login_rc = login_rc
        self.logout_rc = logout_rc
        self.on_login = on_login
        self.calls = []

    def raw(self, args, *, prepend, env, stdin=None):
        call = {
            "kind": "raw",
            "args": list(args),
            "prepend": list(prepend),
            "env": dict(env),
            "stdin": stdin,
        }
        self.calls.append(call)
        if call["args"] == ["login", "--token"]:
            if self.on_login is not None:
                self.on_login()
            return ExecResult(self.login_rc, "", "synthetic login detail")
        if call["args"] == ["auth", "logout"]:
            return ExecResult(self.logout_rc, "", "synthetic logout detail")
        raise AssertionError("unexpected raw command: {!r}".format(call["args"]))

    def json(self, args, *, prepend, env):
        call = {
            "kind": "json",
            "args": list(args),
            "prepend": list(prepend),
            "env": dict(env),
            "stdin": None,
        }
        self.calls.append(call)
        if call["args"] == ["workspace", "list"]:
            return [{"id": "ws-alpha", "name": "Alpha", "slug": "alpha"}]
        raise AssertionError("unexpected JSON command: {!r}".format(call["args"]))


class RecordingConnectionCliProfile(ConnectionCliProfile):
    """Real profile that records which cleanup traversal Collector requested."""

    def __init__(self, *args, lifecycle_events, **kwargs):
        super().__init__(*args, **kwargs)
        self.lifecycle_events = lifecycle_events

    def cleanup(self):
        self.lifecycle_events.append((self.user_id, "cleanup"))
        return super().cleanup()

    def discard_residue(self):
        self.lifecycle_events.append((self.user_id, "discard"))
        return super().discard_residue()


class RealProfileFactory:
    def __init__(self, executors=None):
        self.executors = executors or {}
        self.instances = []
        self.lifecycle_events = []

    def __call__(self, config, user_id):
        executor = self.executors.setdefault(int(user_id), RecordingExecutor())
        profile = RecordingConnectionCliProfile(
            config,
            user_id,
            executor=executor,
            lifecycle_events=self.lifecycle_events,
        )
        self.instances.append(profile)
        return profile


def install_lock_probe(monkeypatch):
    """Wrap the real lock so tests observe both calls and released descriptors."""
    events = []
    delegates = []
    original = collector_module._TenantLock

    class LockProbe:
        def __init__(self, root, user_id):
            events.append((int(user_id), "init"))
            self._delegate = original(root, user_id)
            delegates.append(self._delegate)

        def acquire(self):
            events.append((self._delegate._path.name, "acquire"))
            acquired = self._delegate.acquire()
            events.append((self._delegate._path.name, "acquired", acquired))
            return acquired

        def release(self):
            events.append((self._delegate._path.name, "release"))
            return self._delegate.release()

    monkeypatch.setattr(collector_module, "_TenantLock", LockProbe)
    return events, delegates


def tree_snapshot(root):
    """Content, type and mode snapshot without depending on directory order."""
    snapshot = {}
    paths = [root] + sorted(root.rglob("*"), key=lambda path: str(path))
    for path in paths:
        rel = "." if path == root else str(path.relative_to(root))
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            snapshot[rel] = ("symlink", mode, os.readlink(str(path)))
        elif path.is_dir():
            snapshot[rel] = ("directory", mode)
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[rel] = ("file", mode, digest)
    return snapshot


def install_unsafe_profile_component(config, tmp_path, user_id, component, kind):
    """Create one static unsafe component and return immutable-state evidence."""
    profile_name = "aistat-conn-{}".format(user_id)
    paths = {
        "home": config.cli_profiles_dir,
        "dot_multica": config.cli_profiles_dir / ".multica",
        "profiles": config.cli_profiles_dir / ".multica" / "profiles",
        "tenant_profile": (
            config.cli_profiles_dir / ".multica" / "profiles" / profile_name
        ),
    }
    unsafe = paths[component]
    unsafe.parent.mkdir(parents=True, exist_ok=True)

    if kind == "symlink":
        foreign = tmp_path / "foreign-{}".format(component)
        foreign.mkdir()
        leaf_by_component = {
            "home": foreign / ".multica" / "profiles" / profile_name,
            "dot_multica": foreign / "profiles" / profile_name,
            "profiles": foreign / profile_name,
            "tenant_profile": foreign,
        }
        leaf = leaf_by_component[component]
        leaf.mkdir(parents=True, exist_ok=True)
        (leaf / "sentinel.bin").write_bytes(b"synthetic foreign sentinel\x00")
        (foreign / "inventory.txt").write_text("foreign inventory", encoding="utf-8")
        before = tree_snapshot(foreign)
        unsafe.symlink_to(foreign, target_is_directory=True)
        return {
            "unsafe": unsafe,
            "foreign": foreign,
            "before": before,
            "link_target": os.readlink(str(unsafe)),
            "expected_error": (
                "connection profile storage is unsafe: a symlink is not permitted"
            ),
        }

    unsafe.write_bytes(b"synthetic non-directory sentinel\x00")
    unsafe.chmod(0o640)
    return {
        "unsafe": unsafe,
        "before_bytes": unsafe.read_bytes(),
        "before_mode": stat.S_IMODE(unsafe.lstat().st_mode),
        "expected_error": "connection profile storage is unsafe: not a directory",
    }


def assert_unsafe_component_unchanged(evidence):
    unsafe = evidence["unsafe"]
    if "foreign" in evidence:
        assert unsafe.is_symlink()
        assert os.readlink(str(unsafe)) == evidence["link_target"]
        assert tree_snapshot(evidence["foreign"]) == evidence["before"]
    else:
        assert unsafe.is_file()
        assert not unsafe.is_symlink()
        assert unsafe.read_bytes() == evidence["before_bytes"]
        assert stat.S_IMODE(unsafe.lstat().st_mode) == evidence["before_mode"]


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


# -- collector storage preflight: no lock/token/profile/lifecycle side effect -

def test_poisoned_persisted_host_fails_before_token_or_profile_lifecycle(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[
            {
                "user_id": 101,
                "server_url": "https://attacker.example",
                "workspace_label": "alpha",
                "token_epoch": 12,
            }
        ],
        tokens={101: TOKEN_A},
    )
    factory = RealProfileFactory()
    publisher = RecordingPublisher()
    reports = []
    collector = Collector(
        config,
        store,
        profile_factory=factory,
        publish_fn=publisher,
        report_fn=lambda cfg, user, epoch, ok, error: reports.append(
            (user, epoch, ok, error)
        ),
    )

    outcome = collector.collect_once()[0]

    assert (outcome.user_id, outcome.status, outcome.detail) == (
        101,
        "failed",
        "unsupported Multica server",
    )
    assert reports == [(101, 12, False, "unsupported Multica server")]
    assert store.get_token_calls == []
    assert factory.instances == []
    assert factory.lifecycle_events == []
    assert factory.executors == {}
    assert publisher.calls == []
    assert not os.path.lexists(str(config.cli_profiles_dir / "conn-101.lock"))
    assert not config.worker_tenant_db_path(101).exists()


@pytest.mark.parametrize(
    "component",
    ["home", "dot_multica", "profiles", "tenant_profile"],
)
@pytest.mark.parametrize("kind", ["symlink", "non_directory"])
def test_unsafe_storage_fails_before_all_collector_side_effects(
    tmp_path, monkeypatch, component, kind
):
    config = make_config(tmp_path)
    evidence = install_unsafe_profile_component(
        config, tmp_path, 101, component, kind
    )
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 7}
        ],
        tokens={101: TOKEN_A},
    )
    factory = RealProfileFactory()
    publisher = RecordingPublisher()
    reports = []
    poll_calls = []
    lock_events, lock_delegates = install_lock_probe(monkeypatch)

    collector = Collector(
        config,
        store,
        profile_factory=factory,
        publish_fn=publisher,
        report_fn=lambda cfg, user, epoch, ok, error: reports.append(
            (user, epoch, ok, error)
        ),
        poll_fn=lambda *args: poll_calls.append(args),
    )
    outcomes = collector.collect_once()

    expected_error = evidence["expected_error"]
    assert [(o.user_id, o.status, o.detail) for o in outcomes] == [
        (101, "failed", expected_error)
    ]
    assert reports == [(101, 7, False, expected_error)]
    assert store.get_token_calls == []
    assert factory.instances == []
    assert factory.lifecycle_events == []
    assert factory.executors == {}
    assert publisher.calls == []
    assert poll_calls == []
    assert lock_events == []
    assert lock_delegates == []
    assert not os.path.lexists(str(config.cli_profiles_dir / "conn-101.lock"))
    assert not config.worker_tenant_db_path(101).exists()
    assert_unsafe_component_unchanged(evidence)
    for forbidden in (
        TOKEN_A,
        str(tmp_path),
        "aistat-conn-101",
        "synthetic login detail",
        "synthetic logout detail",
    ):
        assert forbidden not in outcomes[0].detail


def test_storage_probe_oserror_is_redacted_and_isolated(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 11}
        ],
        tokens={101: TOKEN_A},
    )
    factory = RealProfileFactory()
    reports = []
    lock_events, _ = install_lock_probe(monkeypatch)
    original_lstat = cli_profile_module.os.lstat

    def deny_profile_root(path):
        if os.fspath(path) == os.fspath(config.cli_profiles_dir):
            raise OSError(13, "synthetic secret filesystem detail", os.fspath(path))
        return original_lstat(path)

    monkeypatch.setattr(cli_profile_module.os, "lstat", deny_profile_root)
    collector = Collector(
        config,
        store,
        profile_factory=factory,
        publish_fn=RecordingPublisher(),
        report_fn=lambda cfg, user, epoch, ok, error: reports.append(
            (user, epoch, ok, error)
        ),
    )

    outcome = collector.collect_once()[0]

    expected = "connection profile storage could not be verified"
    assert (outcome.user_id, outcome.status, outcome.detail) == (101, "failed", expected)
    assert reports == [(101, 11, False, expected)]
    assert store.get_token_calls == []
    assert factory.instances == []
    assert lock_events == []
    assert TOKEN_A not in outcome.detail
    assert str(tmp_path) not in outcome.detail
    assert "synthetic secret filesystem detail" not in outcome.detail


def test_unsafe_tenant_does_not_block_healthy_neighbor(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    evidence = install_unsafe_profile_component(
        config, tmp_path, 101, "tenant_profile", "symlink"
    )
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 4},
            {"user_id": 202, "workspace_label": "alpha", "token_epoch": 9},
        ],
        tokens={101: TOKEN_A, 202: TOKEN_B},
    )
    healthy_executor = RecordingExecutor()
    factory = RealProfileFactory({202: healthy_executor})
    publisher = RecordingPublisher()
    poll_calls = []
    reports = []
    lock_events, lock_delegates = install_lock_probe(monkeypatch)
    collector = Collector(
        config,
        store,
        profile_factory=factory,
        publish_fn=publisher,
        report_fn=lambda cfg, user, epoch, ok, error: reports.append(
            (user, epoch, ok, error)
        ),
        poll_fn=lambda cfg, conn, runner: poll_calls.append(runner),
    )

    outcomes = collector.collect_once()

    assert [(o.user_id, o.status) for o in outcomes] == [
        (101, "failed"),
        (202, "collected"),
    ]
    assert store.get_token_calls == [202]
    assert [profile.user_id for profile in factory.instances] == [202]
    assert 101 not in factory.executors
    assert factory.lifecycle_events == [(202, "cleanup")]
    assert publisher.calls == [(config.worker_tenant_db_path(202), 202)]
    assert len(poll_calls) == 1
    assert reports == [
        (
            101,
            4,
            False,
            "connection profile storage is unsafe: a symlink is not permitted",
        ),
        (202, 9, True, None),
    ]
    assert not any(event[0] == 101 for event in lock_events)
    assert any(event[0] == 202 for event in lock_events)
    assert all(lock._fd is None for lock in lock_delegates)
    assert not os.path.lexists(str(config.cli_profiles_dir / "conn-101.lock"))
    assert_unsafe_component_unchanged(evidence)


def test_trusted_login_failure_still_removes_partial_residue(tmp_path):
    config = make_config(tmp_path)
    residue = (
        config.cli_profiles_dir / ".multica" / "profiles" / "aistat-conn-101"
    )

    def write_partial_residue():
        residue.mkdir(parents=True, exist_ok=True)
        (residue / "config.json").write_text(
            '{"token": "synthetic partial credential"}', encoding="utf-8"
        )

    executor = RecordingExecutor(login_rc=1, on_login=write_partial_residue)
    factory = RealProfileFactory({101: executor})
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 5}
        ],
        tokens={101: TOKEN_A},
    )
    publisher = RecordingPublisher()
    poll_calls = []
    reports = []
    collector = Collector(
        config,
        store,
        profile_factory=factory,
        publish_fn=publisher,
        report_fn=lambda cfg, user, epoch, ok, error: reports.append(
            (user, epoch, ok, error)
        ),
        poll_fn=lambda *args: poll_calls.append(args),
    )

    outcome = collector.collect_once()[0]

    assert outcome.status == "failed"
    assert outcome.detail == "official CLI login failed for the connection"
    assert [call["args"] for call in executor.calls] == [
        ["login", "--token"],
        ["auth", "logout"],
    ]
    assert not residue.exists()
    assert factory.lifecycle_events == [(101, "cleanup")]
    assert publisher.calls == []
    assert poll_calls == []
    assert reports == [(101, 5, False, outcome.detail)]


def test_logout_failure_still_collects_and_removes_residue(tmp_path):
    config = make_config(tmp_path)
    residue = (
        config.cli_profiles_dir / ".multica" / "profiles" / "aistat-conn-101"
    )

    def write_live_residue():
        residue.mkdir(parents=True, exist_ok=True)
        (residue / "config.json").write_text(
            '{"token": "synthetic live credential"}', encoding="utf-8"
        )

    executor = RecordingExecutor(logout_rc=1, on_login=write_live_residue)
    factory = RealProfileFactory({101: executor})
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 6}
        ],
        tokens={101: TOKEN_A},
    )
    publisher = RecordingPublisher()
    reports = []
    collector = Collector(
        config,
        store,
        profile_factory=factory,
        publish_fn=publisher,
        report_fn=lambda cfg, user, epoch, ok, error: reports.append(
            (user, epoch, ok, error)
        ),
        poll_fn=lambda *args: None,
    )

    outcome = collector.collect_once()[0]

    assert outcome.status == "collected"
    assert [call["args"] for call in executor.calls] == [
        ["login", "--token"],
        ["workspace", "list"],
        ["auth", "logout"],
    ]
    assert not residue.exists()
    assert publisher.calls == [(config.worker_tenant_db_path(101), 101)]
    assert reports == [(101, 6, True, None)]


def test_revoked_connection_real_profile_removes_residue_without_executor(tmp_path):
    config = make_config(tmp_path)
    residue = (
        config.cli_profiles_dir / ".multica" / "profiles" / "aistat-conn-101"
    )
    residue.mkdir(parents=True)
    (residue / "config.json").write_text(
        '{"token": "synthetic revoked credential"}', encoding="utf-8"
    )
    executor = RecordingExecutor()
    factory = RealProfileFactory({101: executor})
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 8}
        ],
        tokens={101: None},
    )
    publisher = RecordingPublisher()
    poll_calls = []
    collector = Collector(
        config,
        store,
        profile_factory=factory,
        publish_fn=publisher,
        report_fn=None,
        poll_fn=lambda *args: poll_calls.append(args),
    )

    outcome = collector.collect_once()[0]

    assert (outcome.user_id, outcome.status, outcome.detail) == (
        101,
        "skipped",
        "connection was revoked",
    )
    assert executor.calls == []
    assert not residue.exists()
    assert factory.lifecycle_events == [(101, "discard")]
    assert publisher.calls == []
    assert poll_calls == []


# -- revoked / unreadable token ---------------------------------------------

def test_revoked_connection_does_residue_only_cleanup(tmp_path):
    # Revoked between listing and reading the token: no login/poll/publish, but
    # any residue a prior crashed cycle left behind is still erased.
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
    # a profile was constructed only to erase residue — never logged in
    assert len(FakeProfile.instances) == 1
    profile = FakeProfile.instances[0]
    assert profile.discarded is True
    assert profile.logged_in_with is None  # never logs a revoked token in
    assert profile.cleaned is False  # no login/logout lifecycle, residue only


def test_revoked_residue_removal_failure_is_a_safe_failure(tmp_path):
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[{"user_id": 101, "workspace_label": "alpha", "token_epoch": 1}],
        tokens={101: None},
    )
    collector = Collector(
        config, store,
        profile_factory=factory_with({101: {"discard_fail": True}}),
        publish_fn=RecordingPublisher(), report_fn=None,
    )
    outcome = collector.collect_once()[0]
    assert outcome.status == "failed"  # unremovable revoked residue is not silent
    assert TOKEN_A not in outcome.detail
    assert "aistat-conn" not in outcome.detail
    assert str(tmp_path) not in outcome.detail


def test_replaced_token_is_used_not_the_stale_one(tmp_path):
    # After a token is replaced during a running worker, the next cycle logs in
    # with the current token from the store — the old value is never revived.
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[{"user_id": 101, "workspace_label": "alpha", "token_epoch": 5}],
        tokens={101: TOKEN_A},
    )
    collector = Collector(
        config, store, profile_factory=factory_with(),
        publish_fn=RecordingPublisher(), report_fn=None,
    )
    collector.collect_once()
    store._tokens[101] = TOKEN_B  # replaced while the worker runs
    collector.collect_once()
    logins = [p.logged_in_with for p in FakeProfile.instances]
    assert logins == [TOKEN_A, TOKEN_B]
    assert TOKEN_A not in logins[1:]  # the stale token is never reused


def test_cleanup_residue_failure_downgrades_to_safe_failure(tmp_path):
    # Data may have been collected, but a residue that cannot be erased must not
    # be silent: the connection outcome is a safe failure so reuse is blocked.
    config = make_config(tmp_path)
    store = FakeStore(
        connections=[
            {"user_id": 101, "workspace_label": "alpha", "token_epoch": 1},
            {"user_id": 202, "workspace_label": "beta", "token_epoch": 1},
        ],
        tokens={101: TOKEN_A, 202: TOKEN_B},
    )
    collector = Collector(
        config, store,
        profile_factory=factory_with({101: {"cleanup_fail": True}}),
        publish_fn=RecordingPublisher(), report_fn=None,
    )
    outcomes = {o.user_id: o for o in collector.collect_once()}
    assert outcomes[101].status == "failed"
    assert TOKEN_A not in outcomes[101].detail
    assert str(tmp_path) not in outcomes[101].detail
    # one connection's cleanup failure does not stop the other
    assert outcomes[202].status == "collected"


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
