"""Per-connection official-CLI profile: isolation, stdin token, host pinning."""

import os

import pytest

import aistat.cli_profile as cli_profile
from aistat.cli_profile import (
    CliProfileError,
    ConnectionCliProfile,
    ExecResult,
    resolve_workspace,
    scrubbed_env,
)
from aistat.config import Config

TOKEN = "mul_super_secret_user_pat_value"


def make_config(tmp_path, **overrides):
    config = Config()
    config.cli_profiles_dir = tmp_path / "cli_profiles"
    config.multica_official_url = "https://multica.ai"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class FakeExecutor:
    """Records every invocation so tests can assert argv/env/stdin discipline."""

    def __init__(self, workspaces=None, login_rc=0, logout_rc=0, ws_error=False):
        self.workspaces = (
            workspaces
            if workspaces is not None
            else [{"id": "ws-alpha", "name": "Alpha", "slug": "alpha"}]
        )
        self.login_rc = login_rc
        self.logout_rc = logout_rc
        self.ws_error = ws_error
        self.calls = []

    def raw(self, args, *, prepend, env, stdin=None):
        self.calls.append(
            {"kind": "raw", "args": list(args), "prepend": list(prepend),
             "env": dict(env), "stdin": stdin}
        )
        if list(args)[:2] == ["auth", "logout"]:
            return ExecResult(self.logout_rc, "", "")
        return ExecResult(self.login_rc, "", "" if self.login_rc == 0 else "denied")

    def json(self, args, *, prepend, env):
        self.calls.append(
            {"kind": "json", "args": list(args), "prepend": list(prepend),
             "env": dict(env), "stdin": None}
        )
        if list(args)[:2] == ["workspace", "list"]:
            if self.ws_error:
                from aistat.cli import CliError
                raise CliError(args, "boom")
            return list(self.workspaces)
        return []


def all_argv_text(executor):
    """Flatten every argv/prepend token the executor ever saw."""
    text = []
    for call in executor.calls:
        text.extend(call["args"])
        text.extend(call["prepend"])
    return " ".join(text)


# -- environment isolation ---------------------------------------------------

def test_scrubbed_env_drops_owner_identity_and_pins_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICA_TOKEN", "owner-secret")
    monkeypatch.setenv("MULTICA_WORKSPACE_ID", "owner-ws")
    monkeypatch.setenv("MULTICA_SERVER_URL", "https://api.multica.ai")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = scrubbed_env(tmp_path / "home")
    assert not any(k.startswith("MULTICA_") for k in env)
    assert env["HOME"] == str(tmp_path / "home")
    assert env["PATH"] == "/usr/bin"  # non-identity vars survive


def test_profile_name_is_deterministic_from_internal_id(tmp_path):
    config = make_config(tmp_path)
    assert ConnectionCliProfile(config, 42).profile == "aistat-conn-42"
    # Non-canonical ids are rejected before any path/name is derived.
    with pytest.raises(ValueError):
        ConnectionCliProfile(config, -1)
    with pytest.raises(ValueError):
        ConnectionCliProfile(config, "42; rm -rf")


# -- login: token only via stdin, never argv/env -----------------------------

def test_login_passes_token_only_through_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICA_TOKEN", "owner-secret-token")
    executor = FakeExecutor()
    profile = ConnectionCliProfile(make_config(tmp_path), 7, executor=executor)
    profile.login(TOKEN)

    login_calls = [c for c in executor.calls if c["args"] == ["login", "--token"]]
    assert len(login_calls) == 1
    call = login_calls[0]
    # token reaches the CLI only on stdin
    assert call["stdin"] == TOKEN + "\n"
    # token never appears in argv/prepend of any call
    assert TOKEN not in all_argv_text(executor)
    # the owner's ambient token is scrubbed from the child env
    assert "MULTICA_TOKEN" not in call["env"]
    assert not any(k.startswith("MULTICA_") for k in call["env"])
    # every call is pinned to the isolated profile + official host
    assert call["prepend"] == [
        "--profile", "aistat-conn-7", "--server-url", "https://multica.ai",
    ]


def test_login_failure_raises_safe_error_without_token(tmp_path):
    executor = FakeExecutor(login_rc=1)
    profile = ConnectionCliProfile(make_config(tmp_path), 7, executor=executor)
    with pytest.raises(CliProfileError) as exc_info:
        profile.login(TOKEN)
    message = str(exc_info.value)
    assert TOKEN not in message
    assert "aistat-conn" not in message


def test_login_clears_stale_profile_residue_first(tmp_path):
    config = make_config(tmp_path)
    profile = ConnectionCliProfile(config, 7, executor=FakeExecutor())
    residue = config.cli_profiles_dir / ".multica" / "profiles" / "aistat-conn-7"
    residue.mkdir(parents=True)
    (residue / "config.json").write_text('{"token": "stale_old_pat"}')
    profile.login(TOKEN)
    assert not (residue / "config.json").exists()


# -- host pinning: never trust a stored server_url ---------------------------

def test_host_is_always_the_configured_official_url(tmp_path):
    config = make_config(tmp_path, multica_official_url="https://multica.ai")
    executor = FakeExecutor()
    profile = ConnectionCliProfile(config, 7, executor=executor)
    profile.login(TOKEN)
    profile.select_workspace("alpha")
    profile.runner(["issue", "list", "--project", "P1"])
    for call in executor.calls:
        prep = call["prepend"]
        assert "--server-url" in prep
        assert prep[prep.index("--server-url") + 1] == "https://multica.ai"
        # a user/store supplied host never appears
        assert "https://api.multica.ai" not in prep


# -- workspace selection -----------------------------------------------------

def test_resolve_workspace_matches_and_refuses_ambiguity():
    spaces = [
        {"id": "aaaa1111", "name": "Alpha", "slug": "alpha"},
        {"id": "bbbb2222", "name": "Beta", "slug": "beta"},
    ]
    assert resolve_workspace(spaces, "Alpha")["id"] == "aaaa1111"
    assert resolve_workspace(spaces, "beta")["id"] == "bbbb2222"
    assert resolve_workspace(spaces, "bbbb")["id"] == "bbbb2222"  # id prefix
    assert resolve_workspace([spaces[0]], None)["id"] == "aaaa1111"  # sole ws
    with pytest.raises(CliProfileError):
        resolve_workspace(spaces, None)  # multiple, none chosen
    with pytest.raises(CliProfileError):
        resolve_workspace(spaces, "nonesuch")
    with pytest.raises(CliProfileError):
        resolve_workspace([], "alpha")  # PAT has no workspace


def test_runner_pins_selected_workspace(tmp_path):
    executor = FakeExecutor(
        workspaces=[
            {"id": "ws-alpha", "name": "Alpha", "slug": "alpha"},
            {"id": "ws-beta", "name": "Beta", "slug": "beta"},
        ]
    )
    profile = ConnectionCliProfile(make_config(tmp_path), 7, executor=executor)
    profile.login(TOKEN)
    chosen = profile.select_workspace("beta")
    assert chosen["id"] == "ws-beta"
    profile.runner(["agent", "list"])
    data_call = [c for c in executor.calls if c["args"] == ["agent", "list"]][0]
    prep = data_call["prepend"]
    assert prep[prep.index("--workspace-id") + 1] == "ws-beta"


def test_runner_requires_workspace_selection_first(tmp_path):
    profile = ConnectionCliProfile(make_config(tmp_path), 7, executor=FakeExecutor())
    profile.login(TOKEN)
    with pytest.raises(CliProfileError):
        profile.runner(["agent", "list"])


# -- cleanup: logout + residue erased ----------------------------------------

def test_cleanup_logs_out_and_erases_residue(tmp_path):
    config = make_config(tmp_path)
    executor = FakeExecutor()
    profile = ConnectionCliProfile(config, 7, executor=executor)
    profile.login(TOKEN)
    residue = config.cli_profiles_dir / ".multica" / "profiles" / "aistat-conn-7"
    residue.mkdir(parents=True, exist_ok=True)
    (residue / "config.json").write_text('{"token": "live_pat"}')
    profile.cleanup()
    assert not residue.exists()
    assert any(c["args"] == ["auth", "logout"] for c in executor.calls)


def test_context_manager_cleans_up_on_exit(tmp_path):
    config = make_config(tmp_path)
    executor = FakeExecutor()
    residue = config.cli_profiles_dir / ".multica" / "profiles" / "aistat-conn-7"
    with ConnectionCliProfile(config, 7, executor=executor) as profile:
        profile.login(TOKEN)
        residue.mkdir(parents=True, exist_ok=True)
        (residue / "config.json").write_text('{"token": "live_pat"}')
    assert not residue.exists()
    assert any(c["args"] == ["auth", "logout"] for c in executor.calls)


# -- fail-closed storage: reject a symlinked/non-directory profile root -------

def test_login_rejects_symlinked_profile_root_without_touching_target(tmp_path):
    # A synthetic attacker points the task-owned HOME at a foreign directory.
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "sentinel").write_text("do-not-touch")
    profiles = tmp_path / "cli_profiles"
    os.symlink(foreign, profiles)  # cli_profiles_dir is now a symlink

    config = make_config(tmp_path, cli_profiles_dir=profiles)
    executor = FakeExecutor()
    profile = ConnectionCliProfile(config, 7, executor=executor)
    with pytest.raises(CliProfileError) as exc_info:
        profile.login(TOKEN)

    # fail closed *before* the token is handed to the CLI: no login attempt
    assert executor.calls == []
    # the linked/foreign target is left completely unchanged, no fallback write
    assert (foreign / "sentinel").read_text() == "do-not-touch"
    assert list(foreign.iterdir()) == [foreign / "sentinel"]
    # the error carries neither the token nor a path
    message = str(exc_info.value)
    assert TOKEN not in message
    assert str(tmp_path) not in message


def test_login_rejects_symlinked_dot_multica(tmp_path):
    # A nested credential-bearing component (.multica) is the symlink.
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    config = make_config(tmp_path)
    config.cli_profiles_dir.mkdir(parents=True)
    os.symlink(foreign, config.cli_profiles_dir / ".multica")

    executor = FakeExecutor()
    profile = ConnectionCliProfile(config, 7, executor=executor)
    with pytest.raises(CliProfileError):
        profile.login(TOKEN)
    assert executor.calls == []


def test_login_rejects_non_directory_profile_root(tmp_path):
    profiles = tmp_path / "cli_profiles"
    profiles.write_text("i am a file, not a directory")
    config = make_config(tmp_path, cli_profiles_dir=profiles)
    executor = FakeExecutor()
    profile = ConnectionCliProfile(config, 7, executor=executor)
    with pytest.raises(CliProfileError):
        profile.login(TOKEN)
    assert executor.calls == []


def test_discard_residue_rejects_symlinked_root(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "sentinel").write_text("keep")
    profiles = tmp_path / "cli_profiles"
    os.symlink(foreign, profiles)
    config = make_config(tmp_path, cli_profiles_dir=profiles)
    profile = ConnectionCliProfile(config, 7, executor=FakeExecutor())
    with pytest.raises(CliProfileError):
        profile.discard_residue()
    assert (foreign / "sentinel").read_text() == "keep"


# -- logout re-pins the official host (recorder proof) -----------------------

def test_logout_pins_profile_and_official_host(tmp_path):
    config = make_config(tmp_path, multica_official_url="https://multica.ai")
    executor = FakeExecutor()
    profile = ConnectionCliProfile(config, 7, executor=executor)
    profile.login(TOKEN)
    profile.logout()
    logout_calls = [c for c in executor.calls if c["args"] == ["auth", "logout"]]
    assert len(logout_calls) == 1
    prep = logout_calls[0]["prepend"]
    # every lifecycle call — logout included — pins the isolated profile and host
    assert prep == [
        "--profile", "aistat-conn-7", "--server-url", "https://multica.ai",
    ]
    assert "https://api.multica.ai" not in prep  # never a poisoned/ambient host
    # and the child env is still scrubbed of the owner identity
    assert not any(k.startswith("MULTICA_") for k in logout_calls[0]["env"])


# -- cleanup runs even when logout fails -------------------------------------

def test_cleanup_erases_residue_even_when_logout_fails(tmp_path):
    config = make_config(tmp_path)
    executor = FakeExecutor(logout_rc=1)  # logout reports failure
    profile = ConnectionCliProfile(config, 7, executor=executor)
    profile.login(TOKEN)
    residue = config.cli_profiles_dir / ".multica" / "profiles" / "aistat-conn-7"
    residue.mkdir(parents=True, exist_ok=True)
    (residue / "config.json").write_text('{"token": "live_pat"}')
    profile.cleanup()  # must not raise; residue removal still runs
    assert not residue.exists()
    assert any(c["args"] == ["auth", "logout"] for c in executor.calls)


# -- residue removal is never silent -----------------------------------------

def test_unremovable_residue_fails_closed(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    profile = ConnectionCliProfile(config, 7, executor=FakeExecutor())

    def boom(_path):
        raise OSError(13, "Permission denied", str(_path))

    monkeypatch.setattr(cli_profile.shutil, "rmtree", boom)
    with pytest.raises(CliProfileError) as exc_info:
        profile.cleanup()
    message = str(exc_info.value)
    assert TOKEN not in message
    assert str(tmp_path) not in message  # the OS error path never surfaces


def test_login_fails_closed_when_stale_residue_cannot_be_removed(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    executor = FakeExecutor()
    profile = ConnectionCliProfile(config, 7, executor=executor)

    def boom(_path):
        raise OSError(13, "Permission denied", str(_path))

    monkeypatch.setattr(cli_profile.shutil, "rmtree", boom)
    # login must fail closed and never hand the token to the CLI when the stale
    # residue of a revoked/replaced credential cannot be erased first.
    with pytest.raises(CliProfileError):
        profile.login(TOKEN)
    assert executor.calls == []


# -- crash/restart: a crashed cycle's residue is erased before the next login -

def test_restart_erases_crashed_residue_before_logging_in(tmp_path):
    config = make_config(tmp_path)
    # Simulate a prior cycle that crashed mid-flight, leaving a stale token file.
    residue = config.cli_profiles_dir / ".multica" / "profiles" / "aistat-conn-7"
    residue.mkdir(parents=True)
    (residue / "config.json").write_text('{"token": "revoked_old_pat"}')

    # A fresh profile (worker restart) logs in with the current token.
    executor = FakeExecutor()
    profile = ConnectionCliProfile(config, 7, executor=executor)
    profile.login("mul_new_current_pat")

    # the crashed cycle's stale token config is gone, not resurrected
    assert not (residue / "config.json").exists()
    # and the new login used the current token via stdin only
    login_calls = [c for c in executor.calls if c["args"] == ["login", "--token"]]
    assert len(login_calls) == 1
    assert login_calls[0]["stdin"] == "mul_new_current_pat\n"
    assert "revoked_old_pat" not in all_argv_text(executor)
