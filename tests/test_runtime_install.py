"""Transactional runtime install/rollback and plist rendering (FAN-1404)."""

import logging
import os
from pathlib import Path

import plistlib
import pytest

from aistat import runtime_install as ri
from aistat.config import Config
from aistat.preflight import Check, PreflightReport, run_preflight


# ---- fakes ---------------------------------------------------------------

class FakeController(ri.LaunchController):
    def __init__(self, fail_bootstraps=0):
        self.loaded = False
        self.calls = []
        self.fail_bootstraps = fail_bootstraps

    def bootstrap(self, plist_path):
        self.calls.append(("bootstrap", str(plist_path)))
        if self.fail_bootstraps > 0:
            self.fail_bootstraps -= 1
            raise ri.LaunchError("bootstrap refused")
        self.loaded = True

    def bootout(self, label, plist_path=None):
        self.calls.append(("bootout", label))
        self.loaded = False

    def is_loaded(self, label):
        return self.loaded

    def kickstart(self, label):
        self.calls.append(("kickstart", label))


def ok_preflight():
    return PreflightReport([Check("stub", True, "ok")])


def make_stage(tmp_path, name, marker):
    stage = tmp_path / name
    (stage / "aistat").mkdir(parents=True)
    (stage / "aistat" / "__init__.py").write_text(
        "MARKER = {!r}\n".format(marker), encoding="utf-8"
    )
    (stage / "requirements.txt").write_text("cryptography\n", encoding="utf-8")
    return stage


def make_installer(tmp_path, controller, preflight_fn=ok_preflight):
    return ri.Installer(
        tmp_path / "runtime",
        "/runtime/.venv/bin/python",
        tmp_path / "production.env",
        controller,
        plist_dir=tmp_path / "LaunchAgents",
        preflight_fn=preflight_fn,
    )


def active_marker(installer):
    text = (installer.paths.code / "aistat" / "__init__.py").read_text()
    return text.split("=", 1)[1].strip().strip("'\"")


def valid_preflight_config(tmp_path):
    config = Config()
    config.publish_tenant_id = 7
    config.publish_url = "https://aistat.app/api/ingest/snapshot"
    config.worker_sync_url = "https://aistat.app"
    config.ingest_secret = "i" * 40
    config.worker_secret = "w" * 40
    config.session_secret = "s" * 40
    config.publish_interval_seconds = 300
    config.worker_pull_interval_seconds = 300
    config.worker_collect_interval_seconds = 300
    config.worker_key_path = tmp_path / "key" / "worker.key"
    config.worker_store_path = tmp_path / "store" / "connections.db"
    return config


def invalidate_secret_config(config, case):
    if case == "session-missing":
        config.session_secret = None
    elif case == "session-empty":
        config.session_secret = ""
    elif case == "session-31-bytes":
        config.session_secret = "s" * 31
    elif case == "session-ingest":
        config.session_secret = config.ingest_secret
    elif case == "session-worker":
        config.session_secret = config.worker_secret
    elif case == "ingest-worker":
        config.worker_secret = config.ingest_secret
    else:  # pragma: no cover - keeps new matrix entries honest
        raise AssertionError("unknown secret case: {}".format(case))


INVALID_ENDPOINT_CASES = (
    "missing",
    "empty",
    "relative",
    "http",
    "empty-authority",
    "query-without-authority",
    "triple-slash-relative",
    "leading-whitespace",
    "trailing-whitespace",
    "internal-whitespace",
    "tab-whitespace",
    "backslash",
    "userinfo",
    "credentials-only-authority",
    "empty-port",
    "zero-port",
    "non-numeric-port",
    "negative-port",
    "double-port",
    "out-of-range-port",
    "unterminated-ipv6",
    "ipv6-bracket-suffix",
    "empty-dns-label",
    "leading-hyphen-label",
)


def invalid_endpoint(case):
    values = {
        "missing": None,
        "empty": "",
        "relative": "/relative",
        "http": "http://host.example/path",
        "empty-authority": "https://",
        "query-without-authority": "https://?query=1",
        "triple-slash-relative": "https:///relative",
        "leading-whitespace": " https://host.example/path",
        "trailing-whitespace": "https://host.example/path ",
        "internal-whitespace": "https://bad host.example/path",
        "tab-whitespace": "https://host.example/pa\tth",
        "backslash": "https://host.example/path\\segment",
        "userinfo": (
            "https://synthetic-url-user-never-log:"
            "synthetic-url-password-never-log@host.example/path"
        ),
        "credentials-only-authority": (
            "https://synthetic-url-user-never-log:"
            "synthetic-url-password-never-log@"
        ),
        "empty-port": "https://host.example:/path",
        "zero-port": "https://host.example:0/path",
        "non-numeric-port": "https://host.example:notaport/path",
        "negative-port": "https://host.example:-1/path",
        "double-port": "https://host.example:443:444/path",
        "out-of-range-port": "https://host.example:65536/path",
        "unterminated-ipv6": "https://[2001:db8::1/path",
        "ipv6-bracket-suffix": "https://[2001:db8::1]suffix/path",
        "empty-dns-label": "https://bad..example/path",
        "leading-hyphen-label": "https://-bad.example/path",
    }
    return values[case]


# ---- plist rendering -----------------------------------------------------

def test_render_plist_is_valid_and_derives_from_root():
    root = Path("/Users/whoever/Library/Application Support/AIStat")
    text = ri.render_plist(root, "/rt/.venv/bin/python",
                           root.parent.parent / ".config/aistat/production.env")
    doc = plistlib.loads(text.encode())
    assert doc["Label"] == ri.LABEL
    assert doc["ProgramArguments"] == [
        "/rt/.venv/bin/python", "-m", "aistat.supervisor"]
    assert doc["WorkingDirectory"] == str(root / "code")
    env = doc["EnvironmentVariables"]
    assert env["AISTAT_RUNTIME_ROOT"] == str(root)
    assert env["AISTAT_DB_PATH"] == str(root / "data" / "aistat.db")
    assert env["AISTAT_WORKER_STORE_PATH"] == str(
        root / "data" / "worker_connections.db")


def test_render_plist_changes_with_root():
    a = ri.render_plist(Path("/a/AIStat"), "/p", Path("/a/env"))
    b = ri.render_plist(Path("/b/AIStat"), "/p", Path("/b/env"))
    assert a != b
    assert "/a/AIStat" in a and "/b/AIStat" not in a


def test_render_plist_carries_no_secret_env_keys():
    text = ri.render_plist(Path("/x/AIStat"), "/p", Path("/x/env"))
    for key in ri._SECRET_ENV_KEYS:
        assert key not in text


def test_extra_env_secret_is_rejected():
    with pytest.raises(ri.RuntimeInstallError):
        ri.render_plist(Path("/x/AIStat"), "/p", Path("/x/env"),
                        extra_env={"AISTAT_INGEST_SECRET": "leak"})


def test_new_runtime_files_have_no_hardcoded_username():
    repo = Path(__file__).resolve().parent.parent
    for rel in ("aistat/runtime_install.py", "aistat/supervisor.py",
                "aistat/preflight.py", "deploy/aistat_runtime.sh",
                "deploy/com.aistat.runtime.plist.template"):
        assert "/Users/" not in (repo / rel).read_text(encoding="utf-8"), rel


# ---- install / reinstall -------------------------------------------------

def test_install_lays_down_code_and_bootstraps(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    status = installer.install(make_stage(tmp_path, "stage1", "v1"))
    assert active_marker(installer) == "v1"
    assert controller.loaded and status["loaded"]
    assert installer.paths.plist.exists()
    assert ("bootstrap", str(installer.paths.plist)) in controller.calls


def test_reinstall_is_idempotent_and_keeps_previous(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    installer.install(make_stage(tmp_path, "stage2", "v2"))
    assert active_marker(installer) == "v2"
    assert installer.paths.code_prev.exists()
    prev = (installer.paths.code_prev / "aistat" / "__init__.py").read_text()
    assert "v1" in prev


def test_preflight_failure_leaves_old_runtime_untouched(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    calls_before = len(controller.calls)

    bad = make_installer(
        tmp_path, controller,
        preflight_fn=lambda: PreflightReport([Check("x", False, "nope")]),
    )
    with pytest.raises(ri.PreflightFailed):
        bad.install(make_stage(tmp_path, "stage2", "v2"))
    # Old code still active, no new launchctl activity, no partial swap.
    assert active_marker(installer) == "v1"
    assert not installer.paths.code_prev.exists()
    assert len(controller.calls) == calls_before


@pytest.mark.parametrize(
    "case",
    [
        "session-missing",
        "session-empty",
        "session-31-bytes",
        "session-ingest",
        "session-worker",
        "ingest-worker",
    ],
)
def test_invalid_secrets_never_reach_runtime_control(tmp_path, case):
    config = valid_preflight_config(tmp_path)
    invalidate_secret_config(config, case)
    controller = FakeController()
    installer = make_installer(
        tmp_path,
        controller,
        preflight_fn=lambda: run_preflight(config, check_imports=False),
    )

    with pytest.raises(ri.PreflightFailed):
        installer.install(make_stage(tmp_path, "stage", "candidate"))

    # Preflight must fail before bootout/bootstrap/kickstart can start or alter
    # the supervisor job, and before any live-code/plist mutation.
    assert controller.calls == []
    assert not controller.loaded
    assert not installer.paths.code.exists()
    assert not installer.paths.code_prev.exists()
    assert not installer.paths.plist.exists()


@pytest.mark.parametrize("field", ["publish_url", "worker_sync_url"])
@pytest.mark.parametrize("case", INVALID_ENDPOINT_CASES)
def test_invalid_endpoints_never_reach_runtime_control(
    tmp_path, monkeypatch, field, case
):
    monkeypatch.setenv("AISTAT_ALLOW_INSECURE_PUBLISH", "1")
    config = valid_preflight_config(tmp_path)
    endpoint = invalid_endpoint(case)
    setattr(config, field, endpoint)
    controller = FakeController()
    installer = make_installer(
        tmp_path,
        controller,
        preflight_fn=lambda: run_preflight(config, check_imports=False),
    )
    stage = make_stage(tmp_path, "stage", "candidate")

    with pytest.raises(ri.PreflightFailed) as exc_info:
        installer.install(stage)

    # Every invalid URL fails before launchctl, plist creation, live-code swap
    # or persistent runtime-directory creation.
    assert controller.calls == []
    assert not controller.loaded
    assert stage.exists()
    assert not installer.paths.code.exists()
    assert not installer.paths.code_prev.exists()
    assert not installer.paths.data.exists()
    assert not installer.paths.plist.exists()
    error = str(exc_info.value)
    if endpoint:
        assert endpoint not in error
    assert "synthetic-url-user-never-log" not in error
    assert "synthetic-url-password-never-log" not in error


def test_bootstrap_failure_rolls_back_to_previous(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))

    # The next install's bootstrap fails once; the restore re-bootstraps.
    controller.fail_bootstraps = 1
    with pytest.raises(ri.LaunchError):
        installer.install(make_stage(tmp_path, "stage2", "v2"))
    # Previous code is back in place and the job is loaded again.
    assert active_marker(installer) == "v1"
    assert controller.loaded


def test_update_preserves_persistent_data(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    owner_db = installer.paths.data / "aistat.db"
    owner_db.write_bytes(b"owner-data")
    (installer.paths.data / "worker_tenants").mkdir()

    installer.install(make_stage(tmp_path, "stage2", "v2"))
    assert owner_db.read_bytes() == b"owner-data"
    assert (installer.paths.data / "worker_tenants").exists()


# ---- uninstall / rollback / restart --------------------------------------

def test_uninstall_keeps_data_by_default(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    (installer.paths.data / "aistat.db").write_bytes(b"keep")

    result = installer.uninstall()
    assert not installer.paths.code.exists()
    assert not installer.paths.plist.exists()
    assert not controller.loaded
    assert result["data_preserved"]
    assert (installer.paths.data / "aistat.db").read_bytes() == b"keep"


def test_uninstall_purge_removes_data(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    (installer.paths.data / "aistat.db").write_bytes(b"gone")

    result = installer.uninstall(purge=True)
    assert not installer.paths.data.exists()
    assert not result["data_preserved"]


def test_rollback_restores_previous(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    installer.install(make_stage(tmp_path, "stage2", "v2"))
    assert active_marker(installer) == "v2"

    installer.rollback()
    assert active_marker(installer) == "v1"
    assert controller.loaded


def test_rollback_without_previous_errors(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    with pytest.raises(ri.RuntimeInstallError):
        installer.rollback()


def test_restart_kickstarts_when_loaded(tmp_path):
    controller = FakeController()
    installer = make_installer(tmp_path, controller)
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    installer.restart()
    assert ("kickstart", ri.LABEL) in controller.calls


# ---- effective env-file preflight for direct restart/rollback (FAN-1425) --

SENTINEL_INGEST = "synthetic-ingest-secret-never-log-1425"
SENTINEL_WORKER = "synthetic-worker-secret-never-log-1425"
SENTINEL_SESSION = "synthetic-session-secret-never-log-1425"
SENTINELS = (SENTINEL_INGEST, SENTINEL_WORKER, SENTINEL_SESSION)


@pytest.fixture
def clean_env():
    """Scrub ambient AISTAT_* vars and undo what load_env_file injected."""
    saved = os.environ.copy()
    for key in list(os.environ):
        if key.startswith("AISTAT_"):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(saved)


class RecordingController(ri.LaunchController):
    """Fake launchd surface that records every call, including is_loaded."""

    def __init__(self, loaded=False, fail_bootstraps=0, events=None):
        self.loaded = loaded
        self.fail_bootstraps = fail_bootstraps
        self.calls = []
        self.events = events

    def _record(self, name, detail):
        self.calls.append((name, detail))
        if self.events is not None:
            self.events.append(name)

    def bootstrap(self, plist_path):
        self._record("bootstrap", str(plist_path))
        if self.fail_bootstraps > 0:
            self.fail_bootstraps -= 1
            raise ri.LaunchError("bootstrap refused")
        self.loaded = True

    def bootout(self, label, plist_path=None):
        self._record("bootout", label)
        self.loaded = False

    def is_loaded(self, label):
        self._record("is_loaded", label)
        return self.loaded

    def kickstart(self, label):
        self._record("kickstart", label)


def patch_cli_controller(monkeypatch, *, loaded=False, fail_bootstraps=0,
                         events=None):
    controllers = []

    def factory():
        controller = RecordingController(
            loaded=loaded, fail_bootstraps=fail_bootstraps, events=events)
        controllers.append(controller)
        return controller

    monkeypatch.setattr(ri, "LaunchctlController", factory)
    return controllers


def env_file_values(tmp_path):
    return {
        "AISTAT_TENANT_ID": "7",
        "AISTAT_PUBLISH_URL": "https://aistat.app/api/ingest/snapshot",
        "AISTAT_WORKER_SYNC_URL": "https://aistat.app",
        "AISTAT_INGEST_SECRET": SENTINEL_INGEST,
        "AISTAT_WORKER_SECRET": SENTINEL_WORKER,
        "AISTAT_SESSION_SECRET": SENTINEL_SESSION,
        "AISTAT_PUBLISH_INTERVAL_SECONDS": "300",
        "AISTAT_WORKER_PULL_INTERVAL_SECONDS": "300",
        "AISTAT_WORKER_COLLECT_INTERVAL_SECONDS": "300",
        "AISTAT_WORKER_KEY_PATH": str(tmp_path / "keys" / "worker.key"),
        "AISTAT_WORKER_STORE_PATH": str(tmp_path / "store" / "connections.db"),
    }


def write_env_file(path, values, mode=0o600):
    # Double quotes survive load_env_file's strip, so whitespace-bearing
    # invalid endpoints reach Config exactly as written here.
    lines = ['{}="{}"'.format(k, v) for k, v in values.items() if v is not None]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(mode)


def make_installed_runtime(tmp_path, *, with_previous=True):
    """A fake installed runtime: active code, previous code, data, plist."""
    root = tmp_path / "runtime"
    (root / "code" / "aistat").mkdir(parents=True)
    (root / "code" / "aistat" / "__init__.py").write_text(
        "MARKER = 'active'\n", encoding="utf-8")
    if with_previous:
        (root / "code.prev" / "aistat").mkdir(parents=True)
        (root / "code.prev" / "aistat" / "__init__.py").write_text(
            "MARKER = 'previous'\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "aistat.db").write_bytes(b"owner-data")
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    (plist_dir / (ri.LABEL + ".plist")).write_bytes(b"installed-plist")
    return root, plist_dir


def snapshot_state(root, plist_dir):
    """Byte-for-byte view of code, code.prev, data and the plist."""
    state = {}
    for base in (Path(root), Path(plist_dir)):
        if not base.exists():
            state[str(base)] = None
            continue
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                state[str(path)] = ("symlink", os.readlink(str(path)))
            elif path.is_file():
                state[str(path)] = path.read_bytes()
            else:
                state[str(path)] = "dir"
    return state


def run_cli(command, root, plist_dir, env_file=None):
    argv = [command, "--runtime-root", str(root), "--plist-dir", str(plist_dir)]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    return ri.main(argv)


@pytest.mark.parametrize("command", ["restart", "rollback"])
@pytest.mark.parametrize("field", ["AISTAT_PUBLISH_URL",
                                   "AISTAT_WORKER_SYNC_URL"])
@pytest.mark.parametrize("case", INVALID_ENDPOINT_CASES)
def test_cli_invalid_env_file_endpoint_fails_closed(
    tmp_path, monkeypatch, caplog, capsys, clean_env, command, field, case
):
    monkeypatch.setenv("AISTAT_ALLOW_INSECURE_PUBLISH", "1")
    root, plist_dir = make_installed_runtime(tmp_path)
    values = env_file_values(tmp_path)
    endpoint = invalid_endpoint(case)
    values[field] = endpoint  # None ("missing") drops the key entirely
    env_file = tmp_path / "production.env"
    write_env_file(env_file, values)
    controllers = patch_cli_controller(monkeypatch, loaded=True)
    before = snapshot_state(root, plist_dir)

    with caplog.at_level(logging.DEBUG):
        rc = run_cli(command, root, plist_dir, env_file)

    assert rc == 2
    assert all(controller.calls == [] for controller in controllers)
    assert snapshot_state(root, plist_dir) == before
    captured = capsys.readouterr()
    for output in (captured.out, captured.err, caplog.text):
        if endpoint:
            assert endpoint not in output
        assert "synthetic-url-user-never-log" not in output
        assert "synthetic-url-password-never-log" not in output
        for sentinel in SENTINELS:
            assert sentinel not in output


def test_cli_restart_loaded_preflights_once_then_kickstarts(
    tmp_path, monkeypatch, clean_env
):
    root, plist_dir = make_installed_runtime(tmp_path)
    env_file = tmp_path / "production.env"
    write_env_file(env_file, env_file_values(tmp_path))
    events = []
    real_run_preflight = ri.preflight.run_preflight

    def counting_run_preflight(*args, **kwargs):
        events.append("preflight")
        return real_run_preflight(*args, **kwargs)

    monkeypatch.setattr(ri.preflight, "run_preflight", counting_run_preflight)
    patch_cli_controller(monkeypatch, loaded=True, events=events)

    rc = run_cli("restart", root, plist_dir, env_file)

    assert rc == 0
    # Full preflight runs exactly once, before every controller call —
    # including the read-only is_loaded probe.
    assert events == ["preflight", "is_loaded", "kickstart", "is_loaded"]


def test_cli_restart_unloaded_preflights_once_then_bootstraps(
    tmp_path, monkeypatch, clean_env
):
    root, plist_dir = make_installed_runtime(tmp_path)
    env_file = tmp_path / "production.env"
    write_env_file(env_file, env_file_values(tmp_path))
    events = []
    real_run_preflight = ri.preflight.run_preflight

    def counting_run_preflight(*args, **kwargs):
        events.append("preflight")
        return real_run_preflight(*args, **kwargs)

    monkeypatch.setattr(ri.preflight, "run_preflight", counting_run_preflight)
    controllers = patch_cli_controller(monkeypatch, loaded=False, events=events)

    rc = run_cli("restart", root, plist_dir, env_file)

    assert rc == 0
    assert events == ["preflight", "is_loaded", "bootstrap", "is_loaded"]
    assert ("bootstrap", str(plist_dir / (ri.LABEL + ".plist"))) in \
        controllers[0].calls


def test_cli_rollback_valid_env_preflights_once_then_restores(
    tmp_path, monkeypatch, clean_env
):
    root, plist_dir = make_installed_runtime(tmp_path)
    env_file = tmp_path / "production.env"
    write_env_file(env_file, env_file_values(tmp_path))
    events = []
    real_run_preflight = ri.preflight.run_preflight

    def counting_run_preflight(*args, **kwargs):
        events.append("preflight")
        return real_run_preflight(*args, **kwargs)

    monkeypatch.setattr(ri.preflight, "run_preflight", counting_run_preflight)
    patch_cli_controller(monkeypatch, loaded=True, events=events)

    rc = run_cli("rollback", root, plist_dir, env_file)

    assert rc == 0
    assert events == ["preflight", "bootout", "bootstrap", "is_loaded"]
    marker = (root / "code" / "aistat" / "__init__.py").read_text()
    assert "previous" in marker
    assert not (root / "code.prev").exists()


def test_cli_rollback_without_previous_stays_side_effect_free(
    tmp_path, monkeypatch, capsys, clean_env
):
    root, plist_dir = make_installed_runtime(tmp_path, with_previous=False)
    values = env_file_values(tmp_path)
    values["AISTAT_PUBLISH_URL"] = invalid_endpoint("http")
    env_file = tmp_path / "production.env"
    write_env_file(env_file, values)
    controllers = patch_cli_controller(monkeypatch, loaded=True)
    before = snapshot_state(root, plist_dir)

    rc = run_cli("rollback", root, plist_dir, env_file)

    # The early no-previous rejection stays first and side-effect free.
    assert rc == 1
    assert all(controller.calls == [] for controller in controllers)
    assert snapshot_state(root, plist_dir) == before
    captured = capsys.readouterr()
    assert "no previous code copy" in captured.err
    for output in (captured.out, captured.err):
        assert invalid_endpoint("http") not in output
        for sentinel in SENTINELS:
            assert sentinel not in output


def test_cli_restart_env_file_overrides_invalid_ambient(
    tmp_path, monkeypatch, clean_env
):
    # Ambient environment carries an invalid endpoint; the owner-only env
    # file is fully valid. Supervisor precedence: the file wins -> restart ok.
    monkeypatch.setenv("AISTAT_PUBLISH_URL", "http://ambient-invalid.example/x")
    root, plist_dir = make_installed_runtime(tmp_path)
    env_file = tmp_path / "production.env"
    write_env_file(env_file, env_file_values(tmp_path))
    controllers = patch_cli_controller(monkeypatch, loaded=True)

    rc = run_cli("restart", root, plist_dir, env_file)

    assert rc == 0
    assert ("kickstart", ri.LABEL) in controllers[0].calls


def test_cli_restart_invalid_env_file_overrides_valid_ambient(
    tmp_path, monkeypatch, clean_env
):
    # The QA reproduction: ambient env is fully valid, but the effective env
    # file carries an invalid endpoint. The file value must win and block.
    for key, value in env_file_values(tmp_path).items():
        monkeypatch.setenv(key, value)
    root, plist_dir = make_installed_runtime(tmp_path)
    env_file = tmp_path / "production.env"
    write_env_file(env_file, {
        "AISTAT_PUBLISH_URL": "http://file-invalid.example/path"})
    controllers = patch_cli_controller(monkeypatch, loaded=True)
    before = snapshot_state(root, plist_dir)

    rc = run_cli("restart", root, plist_dir, env_file)

    assert rc == 2
    assert all(controller.calls == [] for controller in controllers)
    assert snapshot_state(root, plist_dir) == before


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_cli_restart_rejects_readable_env_file_without_loading(
    tmp_path, monkeypatch, capsys, clean_env, mode
):
    root, plist_dir = make_installed_runtime(tmp_path)
    env_file = tmp_path / "production.env"
    write_env_file(env_file, env_file_values(tmp_path), mode=mode)
    controllers = patch_cli_controller(monkeypatch, loaded=True)
    before = snapshot_state(root, plist_dir)

    rc = run_cli("restart", root, plist_dir, env_file)

    assert rc == 2
    assert all(controller.calls == [] for controller in controllers)
    assert snapshot_state(root, plist_dir) == before
    # The unsafe file was never loaded: its values stayed out of the process.
    assert os.environ.get("AISTAT_INGEST_SECRET") is None
    captured = capsys.readouterr()
    for sentinel in SENTINELS:
        assert sentinel not in captured.out + captured.err


def test_cli_restart_rejects_symlinked_env_file(
    tmp_path, monkeypatch, clean_env
):
    root, plist_dir = make_installed_runtime(tmp_path)
    target = tmp_path / "real.env"
    write_env_file(target, env_file_values(tmp_path))
    env_file = tmp_path / "production.env"
    env_file.symlink_to(target)
    controllers = patch_cli_controller(monkeypatch, loaded=True)

    rc = run_cli("restart", root, plist_dir, env_file)

    assert rc == 2
    assert all(controller.calls == [] for controller in controllers)


@pytest.mark.parametrize("via", ["flag", "env-var"])
def test_cli_restart_missing_explicit_env_file_fails_closed(
    tmp_path, monkeypatch, capsys, clean_env, via
):
    root, plist_dir = make_installed_runtime(tmp_path)
    missing = tmp_path / "missing.env"
    controllers = patch_cli_controller(monkeypatch, loaded=True)
    before = snapshot_state(root, plist_dir)

    if via == "flag":
        rc = run_cli("restart", root, plist_dir, missing)
    else:
        monkeypatch.setenv("AISTAT_ENV_FILE", str(missing))
        rc = run_cli("restart", root, plist_dir)

    assert rc == 2
    assert all(controller.calls == [] for controller in controllers)
    assert snapshot_state(root, plist_dir) == before
    assert "configured env file does not exist" in capsys.readouterr().err


def test_cli_restart_absent_default_env_path_uses_ambient(
    tmp_path, monkeypatch, clean_env
):
    # No --env-file and no AISTAT_ENV_FILE: like the supervisor, an absent
    # default path means the ambient process environment is the effective
    # configuration.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key, value in env_file_values(tmp_path).items():
        monkeypatch.setenv(key, value)
    root, plist_dir = make_installed_runtime(tmp_path)
    controllers = patch_cli_controller(monkeypatch, loaded=True)

    rc = run_cli("restart", root, plist_dir)

    assert rc == 0
    assert ("kickstart", ri.LABEL) in controllers[0].calls


def test_restart_preflight_exception_text_is_sanitized(
    tmp_path, clean_env
):
    values = env_file_values(tmp_path)
    values["AISTAT_PUBLISH_URL"] = invalid_endpoint("userinfo")
    env_file = tmp_path / "production.env"
    write_env_file(env_file, values)
    controller = RecordingController(loaded=True)
    installer = ri.Installer(
        tmp_path / "runtime", "/rt/python", env_file, controller,
        plist_dir=tmp_path / "LaunchAgents", env_file_explicit=True,
    )

    with pytest.raises(ri.PreflightFailed) as exc_info:
        installer.restart()

    text = str(exc_info.value)
    assert invalid_endpoint("userinfo") not in text
    assert "synthetic-url-user-never-log" not in text
    assert "synthetic-url-password-never-log" not in text
    for sentinel in SENTINELS:
        assert sentinel not in text
    assert controller.calls == []


@pytest.mark.parametrize("method", ["restart", "rollback"])
def test_injected_preflight_failure_blocks_every_controller_call(
    tmp_path, method
):
    controller = RecordingController(loaded=True)
    installer = ri.Installer(
        tmp_path / "runtime", "/rt/python", tmp_path / "production.env",
        controller,
        plist_dir=tmp_path / "LaunchAgents",
        preflight_fn=lambda: PreflightReport([Check("x", False, "nope")]),
    )
    if method == "rollback":
        (installer.paths.code_prev / "aistat").mkdir(parents=True)

    with pytest.raises(ri.PreflightFailed):
        getattr(installer, method)()

    assert controller.calls == []


def test_install_recovery_after_swap_is_not_blocked_by_gate(
    tmp_path, monkeypatch, clean_env
):
    env_file = tmp_path / "production.env"
    write_env_file(env_file, env_file_values(tmp_path))
    controller = RecordingController()
    installer = ri.Installer(
        tmp_path / "runtime", "/rt/python", env_file, controller,
        plist_dir=tmp_path / "LaunchAgents", env_file_explicit=True,
    )
    installer.install(make_stage(tmp_path, "stage1", "v1"))
    assert active_marker(installer) == "v1"

    events = []
    real_run_preflight = ri.preflight.run_preflight

    def counting_run_preflight(*args, **kwargs):
        events.append("preflight")
        return real_run_preflight(*args, **kwargs)

    monkeypatch.setattr(ri.preflight, "run_preflight", counting_run_preflight)
    controller.fail_bootstraps = 1

    with pytest.raises(ri.LaunchError):
        installer.install(make_stage(tmp_path, "stage2", "v2"))

    # One preflight for the whole failed install; the internal transactional
    # recovery restored and re-bootstrapped the previous code without a
    # second gate.
    assert events == ["preflight"]
    assert active_marker(installer) == "v1"
    assert controller.loaded
