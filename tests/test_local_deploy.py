"""Hermetic coverage for the local deployment helpers and release guard."""

import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "deploy" / "local_deploy.sh"

PACKAGE_BUILDER = """#!/usr/bin/env bash
set -euo pipefail
mkdir -p dist/aistat-cpanel
printf 'application = None\\n' >dist/aistat-cpanel/aistat.cgi
printf 'application = None\\n' >dist/aistat-cpanel/passenger_wsgi.py
"""

GIT_SHIM = """#!/usr/bin/env bash
set -euo pipefail
if [ "${AISTAT_TEST_FAIL_TREE:-0}" = 1 ]; then
  for arg in "$@"; do
    case "$arg" in
      *'^{tree}') exit 1;;
    esac
  done
fi
exec "$REAL_GIT" "$@"
"""

LAUNCHCTL_SHIM = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$AISTAT_TEST_LAUNCHCTL_LOG"
"""


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ReleaseHarness:
    def __init__(self, tmp_path, *, unrelated_dev=False):
        source = tmp_path / "source"
        source.mkdir()
        (source / "requirements.txt").write_text("", encoding="utf-8")
        builder = source / "scripts" / "build_cpanel_package.sh"
        builder.parent.mkdir()
        builder.write_text(PACKAGE_BUILDER, encoding="utf-8")

        subprocess.run(
            ["git", "init", "-b", "main", str(source)],
            check=True,
            capture_output=True,
        )
        _git(source, "config", "user.email", "qa@example.invalid")
        _git(source, "config", "user.name", "AIStat QA")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "main candidate")
        if unrelated_dev:
            _git(source, "checkout", "--orphan", "dev")
        else:
            _git(source, "checkout", "-b", "dev")
        (source / "candidate.txt").write_text("dev\n", encoding="utf-8")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "dev candidate")
        self.sha = _git(source, "rev-parse", "HEAD")
        self.tree = _git(source, "rev-parse", "HEAD^{tree}")

        self.origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "clone", "--bare", str(source), str(self.origin)],
            check=True,
            capture_output=True,
        )
        self.local_root = tmp_path / "local root"
        main = self.local_root / "main"
        main.parent.mkdir()
        subprocess.run(
            ["git", "clone", "--branch", "main", str(self.origin), str(main)],
            check=True,
            capture_output=True,
        )

        venv = main / ".venv"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o755)
        stamp = subprocess.run(
            ["cksum", str(main / "requirements.txt")],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        (venv / ".requirements.cksum").write_text(stamp, encoding="utf-8")

        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        git_shim = shim_dir / "git"
        git_shim.write_text(GIT_SHIM, encoding="utf-8")
        git_shim.chmod(0o755)
        launchctl = shim_dir / "launchctl"
        launchctl.write_text(LAUNCHCTL_SHIM, encoding="utf-8")
        launchctl.chmod(0o755)

        self.launchctl_log = tmp_path / "launchctl.log"
        self.env = dict(
            os.environ,
            HOME=str(tmp_path / "home"),
            PATH=f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
            REAL_GIT=shutil.which("git"),
            AISTAT_LOCAL_ROOT=str(self.local_root),
            AISTAT_REPO_URL=str(self.origin),
            AISTAT_DEV_PORT="19788",
            AISTAT_MAIN_PORT="19789",
            AISTAT_TEST_LAUNCHCTL_LOG=str(self.launchctl_log),
        )

    def release(self, *args, **env_overrides):
        return subprocess.run(
            ["bash", str(SCRIPT), "release", *args],
            cwd=REPO_ROOT,
            env=dict(self.env, **env_overrides),
            capture_output=True,
            text=True,
        )


def test_helpers_load_without_side_effects(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    local_root = tmp_path / "local root"
    repo_url = str(tmp_path / "origin.git")
    command = """
source "$1"
branch_guard dev
branch_guard main
printf '%s\\n' "$(label_for dev)"
printf '%s\\n' "$(port_for dev)"
printf '%s\\n' "$(port_for main)"
printf '%s\\n' "$(deploy_dir main)"
printf '%s\\n' "$(resolve_repo_url)"
"""
    result = subprocess.run(
        ["bash", "-c", command, "test-local-deploy", str(SCRIPT)],
        env=dict(
            os.environ,
            HOME=str(home),
            AISTAT_LOCAL_ROOT=str(local_root),
            AISTAT_DEV_PORT="19788",
            AISTAT_MAIN_PORT="19789",
            AISTAT_REPO_URL=repo_url,
        ),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "com.aistat.local.dev",
        "19788",
        "19789",
        str(local_root / "main"),
        repo_url,
    ]
    assert list(home.iterdir()) == []
    assert not local_root.exists()


def test_branch_guard_rejects_unknown_deployment(tmp_path):
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; branch_guard preview',
            "test-local-deploy",
            str(SCRIPT),
        ],
        env=dict(os.environ, HOME=str(tmp_path)),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unknown deployment 'preview' (expected: dev | main)" in result.stderr


def test_dashboard_plist_runs_serve_and_never_the_checkout_run_sh(tmp_path):
    home = tmp_path / "home"
    local_root = tmp_path / "local"
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; write_server_plist dev; cat "$HOME/Library/LaunchAgents/com.aistat.local.dev.plist"',
            "test-local-deploy", str(SCRIPT),
        ],
        env=dict(
            os.environ,
            HOME=str(home),
            AISTAT_LOCAL_ROOT=str(local_root),
            AISTAT_DEV_PORT="19788",
        ),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(result.stdout.encode("utf-8"))
    # The launched command belongs to this script, not to the tracked branch:
    # an older branch's own run.sh starts a poller next to the dashboard.
    assert plist["ProgramArguments"] == [
        "/bin/bash", str(local_root / "local_deploy.sh"), "serve", "dev",
    ]
    assert "run.sh" not in result.stdout
    assert "AISTAT_POLL_INTERVAL_SECONDS" not in result.stdout
    assert "AISTAT_CLI_BIN" not in result.stdout


LEGACY_RUN_SH = """#!/usr/bin/env bash
# The pre-cutover run.sh every deployment on an older branch still carries.
"$(dirname "$0")/.venv/bin/python" -m aistat.poller &
exec "$(dirname "$0")/.venv/bin/uvicorn" aistat.server:app --port 8787
"""

FAKE_UVICORN = """#!/usr/bin/env bash
printf 'uvicorn %s\\n' "$*"
"""


def _serve_deployment(tmp_path, branch, **env):
    """Run `local_deploy.sh serve <branch>` against a fake checkout."""
    local_root = tmp_path / "local root"
    dir_ = local_root / branch
    (dir_ / ".git").mkdir(parents=True)
    (dir_ / "requirements.txt").write_text("", encoding="utf-8")
    run_sh = dir_ / "run.sh"
    run_sh.write_text(LEGACY_RUN_SH, encoding="utf-8")
    run_sh.chmod(0o755)
    bin_dir = dir_ / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python", "uvicorn"):
        (bin_dir / name).write_text(FAKE_UVICORN, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    stamp = subprocess.run(
        ["cksum", str(dir_ / "requirements.txt")],
        check=True, capture_output=True, text=True,
    ).stdout
    (dir_ / ".venv" / ".requirements.cksum").write_text(stamp, encoding="utf-8")
    return subprocess.run(
        ["bash", "-c", 'source "$1"; cmd_serve "$2"',
         "test-local-deploy", str(SCRIPT), branch],
        env=dict(
            os.environ,
            HOME=str(tmp_path / "home"),
            AISTAT_LOCAL_ROOT=str(local_root),
            AISTAT_DEV_PORT="19788",
            AISTAT_MAIN_PORT="19789",
            **env,
        ),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "branch,port", [("dev", "19788"), ("main", "19789")]
)
def test_serve_starts_only_the_dashboard(tmp_path, branch, port):
    result = _serve_deployment(tmp_path, branch)

    assert result.returncode == 0, result.stderr
    # The effective command, not just the absence of env keys: exactly one
    # dashboard process and no collection contour, even though the checkout
    # still ships a run.sh that would start `aistat.poller`.
    assert "uvicorn aistat.server:app --host 127.0.0.1 --port %s" % port \
        in result.stdout
    assert "aistat.poller" not in result.stdout
    assert "aistat.publish" not in result.stdout


def test_serve_never_reads_collection_settings_from_the_checkout_env(tmp_path):
    """A deployment .env may configure a poller; serve must still not run one."""
    local_root = tmp_path / "local root"
    (local_root / "dev").mkdir(parents=True)
    (local_root / "dev" / ".env").write_text(
        "AISTAT_MULTICA_TOKEN=synthetic-token-never-log\n"
        "AISTAT_PUBLISH_URL=https://aistat.app/api/ingest/snapshot\n",
        encoding="utf-8",
    )
    result = _serve_deployment(tmp_path, "dev")

    assert result.returncode == 0, result.stderr
    assert "aistat.poller" not in result.stdout
    assert "synthetic-token-never-log" not in result.stdout


def test_release_logs_full_commit_and_tree(tmp_path):
    harness = ReleaseHarness(tmp_path)
    result = harness.release()

    assert result.returncode == 0, result.stderr
    pairs = re.findall(r"commit=([0-9a-f]+) tree=([0-9a-f]+)", result.stdout)
    assert pairs == [(harness.sha, harness.tree)] * 4
    assert _git(harness.origin, "rev-parse", "refs/heads/main") == harness.sha
    assert "kickstart -k" in harness.launchctl_log.read_text(encoding="utf-8")


def test_release_rejects_non_descendant_candidate(tmp_path):
    harness = ReleaseHarness(tmp_path, unrelated_dev=True)
    result = harness.release()

    assert result.returncode != 0
    assert "origin/main is not an ancestor of 'origin/dev'" in result.stderr
    assert _git(harness.origin, "rev-parse", "refs/heads/main") != harness.sha
    assert not harness.launchctl_log.exists()


def test_release_distinguishes_candidate_and_tree_failures(tmp_path):
    harness = ReleaseHarness(tmp_path)
    candidate_failure = harness.release("--from", "origin/missing")
    tree_failure = harness.release(AISTAT_TEST_FAIL_TREE="1")

    assert candidate_failure.returncode != 0
    assert tree_failure.returncode != 0
    candidate_message = "cannot resolve release candidate 'origin/missing'"
    tree_message = "cannot resolve release candidate tree 'origin/dev'"
    assert candidate_message in candidate_failure.stderr
    assert tree_message not in candidate_failure.stderr
    assert tree_message in tree_failure.stderr
    assert candidate_message not in tree_failure.stderr
