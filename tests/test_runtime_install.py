"""Transactional runtime install/rollback and plist rendering (FAN-1404)."""

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
    config.allow_insecure_publish = False
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
