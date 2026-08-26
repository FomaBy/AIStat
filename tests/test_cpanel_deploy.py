"""Isolated executable proof for exact and atomic cPanel deployment."""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_INPUTS = (
    "aistat",
    "aistat.cgi",
    "passenger_wsgi.py",
    "pricing.json",
    "requirements-cpanel.txt",
    "deploy/cpanel_deploy.sh",
    "deploy/namecheap.htaccess",
    "scripts/build_cpanel_package.sh",
)

# Mirrors what aistat.cgi does on every request: put the live symlink on
# sys.path and import the application from it. Run without
# PYTHONDONTWRITEBYTECODE it makes CPython write __pycache__/*.pyc next to the
# sources, i.e. through the symlink and into the published release.
LIVE_IMPORT = "\n".join(
    (
        "import os, sys",
        "sys.path.insert(0, os.environ['AISTAT_APP_ROOT'])",
        "import passenger_wsgi",
        "from aistat import legacy_wsgi",
        "assert callable(passenger_wsgi.application)",
        "assert legacy_wsgi.application is passenger_wsgi.application",
    )
)

# Stands in for python3 during a deploy. It intercepts only the publish helper
# (`python3 - <next-link> <app-link>`), performs the real rename itself and then
# injects the configured fault immediately after that commit point. Every other
# python3 call passes through untouched.
FAULT_PYTHON3 = """#!/usr/bin/env bash
set -uo pipefail
case "${2-}" in
  */.*.next.*)
    "$REAL_PYTHON3" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \\
      "$2" "$3" || exit 70
    case "$FAULT_MODE" in
      exit)
        exit "$FAULT_EXIT"
        ;;
      pid)
        kill -"$FAULT_SIGNAL" "$PPID"
        ;;
      group)
        own_group="$(ps -o pgid= -p "$$" | tr -d ' ')"
        if [ "$own_group" = "$PROTECTED_PGID" ]; then
          printf 'refusing to signal the test runner process group\\n' >&2
          exit 71
        fi
        kill -"$FAULT_SIGNAL" 0
        ;;
    esac
    exit 0
    ;;
esac
exec "$REAL_PYTHON3" "$@"
"""


# Stands in for python3 during a deploy. The import smoke is the only call it
# treats specially: before passing it through, it records the DB/tenant path
# environment the script actually handed to the smoke. Everything the smoke
# creates lives in a throwaway root the script removes afterwards, so this
# capture is the only way a test can observe those overrides at all.
SMOKE_ENV_PYTHON3 = """#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
  case "$arg" in
    *"import passenger_wsgi"*)
      {
        printf 'HOME=%s\\n' "${HOME-<unset>}"
        printf 'AISTAT_DB_PATH=%s\\n' "${AISTAT_DB_PATH-<unset>}"
        printf 'AISTAT_SECURITY_DB_PATH=%s\\n' "${AISTAT_SECURITY_DB_PATH-<unset>}"
        printf 'AISTAT_TENANTS_DIR=%s\\n' "${AISTAT_TENANTS_DIR-<unset>}"
      } >"$SMOKE_ENV_FILE"
      ;;
  esac
done
exec "$REAL_PYTHON3" "$@"
"""


# Stands in for git during a deploy. Every call passes through untouched; the
# second `fetch` — the pre-publish gate — additionally rewrites the published
# `main` first, so the gate sees a branch that moved after the stage was built.
DRIFT_GIT = """#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *" fetch "*)
    count=0
    [ ! -f "$FETCH_COUNT" ] || count="$(cat "$FETCH_COUNT")"
    count=$((count + 1))
    printf '%s\\n' "$count" >"$FETCH_COUNT"
    if [ "$count" -eq 2 ]; then
      "$REAL_GIT" --git-dir="$DRIFT_ORIGIN" update-ref refs/heads/main "$DRIFT_SHA"
    fi
    ;;
esac
exec "$REAL_GIT" "$@"
"""


MERGE_BASE_FAILURE_GIT = """#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *" merge-base --is-ancestor "*)
    exit 128
    ;;
esac
exec "$REAL_GIT" "$@"
"""


NOOP_RESET_GIT = """#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *" reset --hard "*)
    exit 0
    ;;
esac
exec "$REAL_GIT" "$@"
"""


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_bytes(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    ).stdout


class DeployHarness:
    def __init__(self, tmp_path):
        self.source = tmp_path / "source"
        self.source.mkdir()
        for relative in FIXTURE_INPUTS:
            source = REPO_ROOT / relative
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        subprocess.run(
            ["git", "init", "-b", "main", str(self.source)],
            check=True,
            capture_output=True,
        )
        _git(self.source, "config", "user.email", "qa@example.invalid")
        _git(self.source, "config", "user.name", "AIStat QA")
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "candidate one")

        self.origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "clone", "--bare", str(self.source), str(self.origin)],
            check=True,
            capture_output=True,
        )
        _git(self.source, "remote", "add", "origin", str(self.origin))
        self.host = tmp_path / "host"
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.host)],
            check=True,
            capture_output=True,
        )

        self.home = tmp_path / "home"
        self.private = self.home / "aistat-private"
        self.private.mkdir(parents=True)
        self.runtime_data = tmp_path / "runtime-data"
        (self.runtime_data / "tenants").mkdir(parents=True)
        self.app = self.home / "aistat_app"
        self.releases = self.home / "aistat_releases"
        self.lock = self.private / "cpanel-deploy.lock"
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            AISTAT_APP_ROOT=str(self.app),
            AISTAT_RELEASES_DIR=str(self.releases),
            AISTAT_DEPLOY_LOCK_FILE=str(self.lock),
            AISTAT_KEEP_RELEASES="2",
        )

    def identity(self, repo=None, ref="HEAD"):
        repo = repo or self.source
        return _git(repo, "rev-parse", ref), _git(repo, "rev-parse", ref + "^{tree}")

    def _run_script(self, argv, env):
        return subprocess.run(
            ["bash", str(self.host / "deploy" / "cpanel_deploy.sh"), *argv],
            cwd=self.host,
            env=env or self.env,
            capture_output=True,
            text=True,
            # Own session, so a fault injected with `kill 0` reaches this deploy
            # and nothing else.
            start_new_session=True,
        )

    def deploy(self, sha, tree, env=None):
        return self._run_script(["deploy", sha, tree], env)

    def rollback(self, target, env=None):
        return self._run_script(["rollback", str(target)], env)

    def verify_manifest(self, root, sha, tree, mode):
        """Call the script's own manifest check without running a deploy."""
        return subprocess.run(
            [
                "bash",
                "-c",
                'AISTAT_DEPLOY_LIB_ONLY=1 . "$1"; verify_manifest "$2" "$3" "$4" "$5"',
                "aistat-verify-manifest",
                str(self.host / "deploy" / "cpanel_deploy.sh"),
                str(root),
                sha,
                tree,
                mode,
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def validate_keep_releases(self, value):
        """Call the script's own retention check without running a deploy."""
        return subprocess.run(
            [
                "bash",
                "-c",
                'AISTAT_DEPLOY_LIB_ONLY=1 . "$1"; validate_keep_releases',
                "aistat-validate-keep-releases",
                str(self.host / "deploy" / "cpanel_deploy.sh"),
            ],
            env=dict(self.env, AISTAT_KEEP_RELEASES=value),
            capture_output=True,
            text=True,
        )

    def import_live_app(self):
        """Import the application through the live symlink, like the CGI does."""
        env = {
            key: value
            for key, value in self.env.items()
            if key != "PYTHONDONTWRITEBYTECODE"
        }
        env.update(
            AISTAT_APP_ROOT=str(self.app),
            AISTAT_CGI_ENV_FILE=str(self.home / "missing.env"),
            AISTAT_DB_PATH=str(self.runtime_data / "aistat.db"),
            AISTAT_SECURITY_DB_PATH=str(self.runtime_data / "security.db"),
            AISTAT_TENANTS_DIR=str(self.runtime_data / "tenants"),
            AISTAT_SESSION_SECRET="harness-session-secret-000000000000001",
            AISTAT_INGEST_SECRET="harness-ingest-secret-0000000000000002",
            AISTAT_ADMIN_USERNAME="harness-admin",
            AISTAT_PASSWORD_HASH="harness-password-hash",
            AISTAT_ALLOWED_HOSTS="localhost",
        )
        argv = [sys.executable]
        if sys.version_info >= (3, 8):
            # Legacy host interpreters (historically CPython 3.6.8) have no
            # pycache prefix and always write bytecode next to the sources. Some 3.8+
            # builds (Apple's, for one) ship a non-empty default prefix, so
            # clear it to reproduce the host behaviour instead of the dev box's.
            argv.append("-X")
            argv.append("pycache_prefix=")
        argv.extend(("-c", LIVE_IMPORT))
        return subprocess.run(
            argv,
            cwd=str(self.app),
            env=env,
            capture_output=True,
            text=True,
        )

    def bytecode_in(self, release):
        return sorted(
            path.relative_to(release).as_posix()
            for path in release.rglob("__pycache__/*.pyc")
        )

    def commit(self, message):
        pricing = self.source / "pricing.json"
        pricing.write_text(pricing.read_text("utf-8") + "\n", encoding="utf-8")
        _git(self.source, "add", "pricing.json")
        _git(self.source, "commit", "-m", message)
        _git(self.source, "push", "origin", "HEAD:refs/heads/main")
        return self.identity()

    def unrelated_commit(self, message):
        """Push a commit on an orphan history — never contained in `main`."""
        _git(self.source, "checkout", "--quiet", "--orphan", "unrelated")
        _git(self.source, "commit", "-m", message)
        sha = _git(self.source, "rev-parse", "HEAD")
        _git(self.source, "push", "origin", "HEAD:refs/heads/unrelated")
        _git(self.source, "checkout", "--quiet", "main")
        return sha

    def set_origin_main(self, sha):
        """Rewrite the published branch, as a force-push or a revert would."""
        subprocess.run(
            ["git", "--git-dir", str(self.origin), "update-ref", "refs/heads/main", sha],
            check=True,
            capture_output=True,
        )

    def managed_releases(self):
        if not self.releases.exists():
            return []
        return sorted(
            path for path in self.releases.iterdir() if path.name.startswith("release-")
        )


@pytest.fixture
def harness(tmp_path):
    return DeployHarness(tmp_path)


@pytest.fixture
def symlinked_harness(tmp_path):
    """A harness whose every configured path runs through a symlinked parent.

    `tmp_path` alone cannot express this: pytest hands out an already resolved
    directory, so a deploy driven from it never exercises a host where the
    checkout, `$HOME`, the releases dir or the app root is reached via a link.
    """
    real = tmp_path / "real-workspace"
    real.mkdir()
    link = tmp_path / "linked-workspace"
    link.symlink_to(real, target_is_directory=True)
    return DeployHarness(link)


def _manifest(release):
    return json.loads((release / "PACKAGE-MANIFEST.json").read_text("utf-8"))


def _drift_env(harness, tmp_path, drift_sha):
    """Move the published `main` in between the pre-build and pre-publish gates."""
    wrapper_dir = tmp_path / "fake-bin"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / "git"
    wrapper.write_text(DRIFT_GIT, encoding="utf-8")
    wrapper.chmod(0o755)
    return dict(
        harness.env,
        PATH=str(wrapper_dir) + os.pathsep + harness.env["PATH"],
        REAL_GIT=shutil.which("git"),
        FETCH_COUNT=str(tmp_path / "fetch-count"),
        DRIFT_ORIGIN=str(harness.origin),
        DRIFT_SHA=drift_sha,
    )


def _merge_base_failure_env(harness, tmp_path):
    """Make only the approval-membership query fail as a broken repository."""
    wrapper_dir = tmp_path / "merge-base-failure-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(MERGE_BASE_FAILURE_GIT, encoding="utf-8")
    wrapper.chmod(0o755)
    return dict(
        harness.env,
        PATH=str(wrapper_dir) + os.pathsep + harness.env["PATH"],
        REAL_GIT=shutil.which("git"),
    )


def _noop_reset_env(harness, tmp_path):
    """Report only `reset --hard` as successful without changing the checkout."""
    wrapper_dir = tmp_path / "noop-reset-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(NOOP_RESET_GIT, encoding="utf-8")
    wrapper.chmod(0o755)
    return dict(
        harness.env,
        PATH=str(wrapper_dir) + os.pathsep + harness.env["PATH"],
        REAL_GIT=shutil.which("git"),
    )


def _smoke_env(harness, tmp_path):
    """Record the DB/tenant path environment of the real import smoke."""
    wrapper_dir = tmp_path / "smoke-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "python3"
    wrapper.write_text(SMOKE_ENV_PYTHON3, encoding="utf-8")
    wrapper.chmod(0o755)
    return dict(
        harness.env,
        PATH=str(wrapper_dir) + os.pathsep + harness.env["PATH"],
        REAL_PYTHON3=shutil.which("python3"),
        SMOKE_ENV_FILE=str(tmp_path / "smoke-env"),
    )


def _fault_env(harness, tmp_path, mode, signal="TERM", exit_code="0"):
    wrapper_dir = tmp_path / "fault-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "python3"
    wrapper.write_text(FAULT_PYTHON3, encoding="utf-8")
    wrapper.chmod(0o755)
    return dict(
        harness.env,
        PATH=str(wrapper_dir) + os.pathsep + harness.env["PATH"],
        REAL_PYTHON3=shutil.which("python3"),
        FAULT_MODE=mode,
        FAULT_SIGNAL=signal,
        FAULT_EXIT=exit_code,
        PROTECTED_PGID=str(os.getpgrp()),
    )


def test_exact_candidate_excludes_untracked_and_logs_evidence(harness):
    sha, tree = harness.identity()
    sentinel = harness.host / "aistat" / "qa_untracked_sentinel.py"
    sentinel.write_text("SECRET_SENTINEL = True\n", encoding="utf-8")

    result = harness.deploy(sha, tree)

    assert result.returncode == 0, result.stderr
    assert harness.app.is_symlink()
    release = Path(os.readlink(str(harness.app)))
    assert release.is_dir()
    assert not (release / "aistat" / sentinel.name).exists()
    manifest = _manifest(release)
    assert manifest["source_commit_sha"] == sha
    assert manifest["source_tree_sha"] == tree
    assert sha in result.stdout and tree in result.stdout
    assert "previous=none" in result.stdout
    assert "new=" + str(release) in result.stdout
    assert "manifest_sha256=" in result.stdout

    releases = harness.managed_releases()
    repeated = harness.deploy(sha, tree)
    assert repeated.returncode == 0, repeated.stderr
    assert "ALREADY LIVE" in repeated.stdout
    assert os.readlink(str(harness.app)) == str(release)
    assert harness.managed_releases() == releases


def test_import_smoke_overrides_every_db_and_tenant_path(harness, tmp_path):
    """The smoke must run with all three DB/tenant paths forced into the
    throwaway validation root — production paths and the deploy $HOME stay
    untouched. Captured from the real smoke invocation, so deleting any one of
    the AISTAT_DB_PATH / AISTAT_SECURITY_DB_PATH / AISTAT_TENANTS_DIR overrides
    in cpanel_deploy.sh turns this test red: the un-overridden variable falls
    back to `<package>/data` (aistat/config.py resolves defaults relative to
    the package, not $HOME) and is reported here as `<unset>`."""
    sha, tree = harness.identity()
    env = _smoke_env(harness, tmp_path)

    result = harness.deploy(sha, tree, env)

    assert result.returncode == 0, result.stderr
    captured = dict(
        line.split("=", 1)
        for line in (tmp_path / "smoke-env").read_text("utf-8").splitlines()
    )
    smoke_home = captured["HOME"]
    assert "aistat-cpanel-validate" in smoke_home
    assert smoke_home != str(harness.home)
    for key in ("AISTAT_DB_PATH", "AISTAT_SECURITY_DB_PATH", "AISTAT_TENANTS_DIR"):
        value = captured[key]
        assert value != "<unset>", key + " override missing from the smoke env"
        assert "aistat-cpanel-validate" in value, key + " must point into the validation root"
        assert not value.startswith(str(harness.home)), key
        assert not value.startswith(str(harness.runtime_data)), key


def test_approved_pin_resets_checkout_and_publishes_its_bytes_after_main_moves_on(
    harness,
):
    """The checkout and published bytes both come from the approved pin."""
    pinned_sha, pinned_tree = harness.identity()
    pinned_pricing = _git_bytes(
        harness.source, "cat-file", "blob", pinned_sha + ":pricing.json"
    )
    tip_sha, tip_tree = harness.commit("candidate two")
    tip_pricing = _git_bytes(
        harness.source, "cat-file", "blob", tip_sha + ":pricing.json"
    )
    assert pinned_pricing != tip_pricing
    _git(
        harness.host,
        "fetch",
        "--quiet",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    _git(harness.host, "reset", "--hard", tip_sha)
    assert harness.identity(harness.host) == (tip_sha, tip_tree)

    result = harness.deploy(pinned_sha, pinned_tree)

    assert result.returncode == 0, result.stderr
    assert "contained-in=origin/main" in result.stdout
    assert harness.identity(harness.host) == (pinned_sha, pinned_tree)
    release = Path(os.readlink(str(harness.app)))
    assert (release / "pricing.json").read_bytes() == pinned_pricing
    assert (release / "pricing.json").read_bytes() != tip_pricing
    manifest = _manifest(release)
    assert manifest["source_commit_sha"] == pinned_sha
    assert manifest["source_tree_sha"] == pinned_tree


def test_successful_reset_that_leaves_the_wrong_checkout_is_refused(
    harness, tmp_path
):
    """The post-reset identity guard must reject a false-successful reset."""
    pinned_sha, pinned_tree = harness.identity()
    tip_sha, tip_tree = harness.commit("candidate two")
    _git(
        harness.host,
        "fetch",
        "--quiet",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    _git(harness.host, "reset", "--hard", tip_sha)
    assert harness.identity(harness.host) == (tip_sha, tip_tree)
    env = _noop_reset_env(harness, tmp_path)

    result = harness.deploy(pinned_sha, pinned_tree, env)

    assert result.returncode != 0
    assert "checkout identity mismatch after reset" in result.stderr
    assert harness.identity(harness.host) == (tip_sha, tip_tree)
    assert not harness.app.exists() and not harness.app.is_symlink()
    assert harness.managed_releases() == []


def test_daily_cron_on_a_live_approved_candidate_stays_quiet(harness):
    """The unchanged cron line must not log an ERROR a day once `main` moves on."""
    pinned_sha, pinned_tree = harness.identity()
    first = harness.deploy(pinned_sha, pinned_tree)
    assert first.returncode == 0, first.stderr
    live = os.readlink(str(harness.app))
    releases = harness.managed_releases()
    harness.commit("candidate two")

    repeated = harness.deploy(pinned_sha, pinned_tree)

    assert repeated.returncode == 0, repeated.stderr
    assert "ALREADY LIVE" in repeated.stdout
    assert "ERROR" not in repeated.stderr
    assert os.readlink(str(harness.app)) == live
    assert harness.managed_releases() == releases


def test_candidate_no_longer_contained_in_main_is_refused(harness):
    """A rewritten `main` revokes approval even though the object is still local."""
    pinned_sha, pinned_tree = harness.identity()
    harness.set_origin_main(harness.unrelated_commit("unrelated history"))
    # The orphan carries the same tree as the pin, so the tree check cannot be
    # what rejects this: containment is isolated. And the host clone still holds
    # the object, so this is the "not approved" branch, not "missing after fetch".
    assert _git(harness.host, "cat-file", "-t", pinned_sha) == "commit"

    result = harness.deploy(pinned_sha, pinned_tree)

    assert result.returncode != 0
    assert "pre-build candidate is not approved" in result.stderr
    assert not harness.app.exists() and not harness.app.is_symlink()
    assert harness.managed_releases() == []


def test_candidate_absent_after_fetch_is_refused(harness):
    """`rev-parse --verify` alone would accept this; the object does not exist."""
    absent_sha = "0" * 39 + "1"

    result = harness.deploy(absent_sha, "0" * 40)

    assert result.returncode != 0
    assert "pre-build candidate commit missing after fetch" in result.stderr
    assert not harness.app.exists() and not harness.app.is_symlink()
    assert harness.managed_releases() == []


def test_candidate_tree_mismatch_is_refused(harness):
    pinned_sha, pinned_tree = harness.identity()
    _tip_sha, foreign_tree = harness.commit("candidate two")
    assert foreign_tree != pinned_tree

    result = harness.deploy(pinned_sha, foreign_tree)

    assert result.returncode != 0
    assert (
        "pre-build tree drift: expected "
        + foreign_tree
        + ", candidate "
        + pinned_tree
    ) in result.stderr
    assert not harness.app.exists() and not harness.app.is_symlink()
    assert harness.managed_releases() == []


def test_merge_base_exit_128_reports_broken_repository_not_revoked_approval(
    harness, tmp_path
):
    pinned_sha, pinned_tree = harness.identity()
    env = _merge_base_failure_env(harness, tmp_path)

    result = harness.deploy(pinned_sha, pinned_tree, env)

    assert result.returncode != 0
    assert (
        "pre-build could not test candidate against origin/main (git exit 128)"
        in result.stderr
    )
    assert "is not approved" not in result.stderr
    assert not harness.app.exists() and not harness.app.is_symlink()
    assert harness.managed_releases() == []


def test_main_advancing_between_the_two_fetches_still_publishes(harness, tmp_path):
    """The staged candidate stays approved when `main` merely gains a commit."""
    expected_sha, expected_tree = harness.identity()
    next_sha, _next_tree = harness.commit("future candidate")
    harness.set_origin_main(expected_sha)
    env = _drift_env(harness, tmp_path, next_sha)

    result = harness.deploy(expected_sha, expected_tree, env)

    assert result.returncode == 0, result.stderr
    release = Path(os.readlink(str(harness.app)))
    assert _manifest(release)["source_commit_sha"] == expected_sha
    assert list(harness.releases.glob(".incoming-*")) == []


def test_second_fetch_detects_revoked_approval_and_removes_unpublished_stage(
    harness, tmp_path
):
    expected_sha, expected_tree = harness.identity()
    unrelated_sha = harness.unrelated_commit("unrelated history")
    env = _drift_env(harness, tmp_path, unrelated_sha)

    result = harness.deploy(expected_sha, expected_tree, env)

    assert result.returncode != 0
    assert "pre-publish candidate is not approved" in result.stderr
    assert not harness.app.exists() and not harness.app.is_symlink()
    assert harness.managed_releases() == []
    assert list(harness.releases.glob(".incoming-*")) == []


def test_host_local_lock_refuses_concurrent_attempt_before_staging(harness):
    sha, tree = harness.identity()
    harness.lock.touch(mode=0o600)
    with harness.lock.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = harness.deploy(sha, tree)

    assert result.returncode != 0
    assert "holds the host-local lock" in result.stderr
    assert not harness.app.exists() and not harness.app.is_symlink()
    assert not harness.releases.exists()


def test_first_repeat_retention_and_atomic_rollback(harness):
    sha1, tree1 = harness.identity()
    first = harness.deploy(sha1, tree1)
    assert first.returncode == 0, first.stderr
    release1 = Path(os.readlink(str(harness.app)))

    sha2, tree2 = harness.commit("candidate two")
    second = harness.deploy(sha2, tree2)
    assert second.returncode == 0, second.stderr
    release2 = Path(os.readlink(str(harness.app)))
    assert release2 != release1 and release1.is_dir()
    assert "previous=" + str(release1) in second.stdout

    sha3, tree3 = harness.commit("candidate three")
    third = harness.deploy(sha3, tree3)
    assert third.returncode == 0, third.stderr
    release3 = Path(os.readlink(str(harness.app)))
    assert set(harness.managed_releases()) == {release2, release3}
    assert not release1.exists()

    rolled_back = harness.rollback(release2)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert os.readlink(str(harness.app)) == str(release2)
    assert "previous=" + str(release3) in rolled_back.stdout
    assert "new=" + str(release2) in rolled_back.stdout
    assert sha2 in rolled_back.stdout and tree2 in rolled_back.stdout
    assert not list(harness.home.glob(".aistat_app.next.*"))


def test_retention_zero_preserves_every_release(harness):
    env = dict(harness.env, AISTAT_KEEP_RELEASES="0")
    identities = [harness.identity()]
    assert harness.deploy(*identities[-1], env).returncode == 0
    identities.append(harness.commit("candidate two"))
    assert harness.deploy(*identities[-1], env).returncode == 0
    identities.append(harness.commit("candidate three"))
    assert harness.deploy(*identities[-1], env).returncode == 0
    assert len(harness.managed_releases()) == 3


@pytest.mark.parametrize("value", ["0", "2", "5", "9", "10", "12", "19", "20", "100"])
def test_canonical_retention_values_are_accepted(harness, value):
    """`0` plus every canonical integer >= 2, single- and multi-digit alike."""
    result = harness.validate_keep_releases(value)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["1", "-1", "x", "1.5", "02", "0x2", "2.0", " 2"])
def test_non_canonical_retention_values_are_rejected(harness, value):
    result = harness.validate_keep_releases(value)
    assert result.returncode != 0
    assert "must be 0" in result.stderr


def test_multi_digit_retention_is_accepted_end_to_end(harness):
    env = dict(harness.env, AISTAT_KEEP_RELEASES="10")
    identities = [harness.identity()]
    assert harness.deploy(*identities[-1], env).returncode == 0
    identities.append(harness.commit("candidate two"))
    assert harness.deploy(*identities[-1], env).returncode == 0
    identities.append(harness.commit("candidate three"))
    assert harness.deploy(*identities[-1], env).returncode == 0
    assert len(harness.managed_releases()) == 3


@pytest.mark.parametrize("value", ["1", "-1", "x", "1.5", "02"])
def test_invalid_retention_fails_before_shared_mutation(harness, value):
    sha, tree = harness.identity()
    result = harness.deploy(sha, tree, dict(harness.env, AISTAT_KEEP_RELEASES=value))
    assert result.returncode != 0
    assert "must be 0" in result.stderr
    assert not harness.lock.exists()
    assert not harness.releases.exists()


def test_existing_manual_directory_requires_separate_maintenance(harness):
    sha, tree = harness.identity()
    harness.app.mkdir()
    marker = harness.app / "manual.txt"
    marker.write_text("still live\n", encoding="utf-8")

    result = harness.deploy(sha, tree)

    assert result.returncode != 0
    assert "first-migration maintenance" in result.stderr
    assert harness.app.is_dir() and not harness.app.is_symlink()
    assert marker.read_text("utf-8") == "still live\n"
    assert harness.managed_releases() == []


@pytest.mark.parametrize(
    ("relative", "expected_error"),
    [
        # A broken .py is stopped by the package-wide compileall gate: it walks
        # every *.py, so it always fires before the dedicated entry-point gate.
        ("passenger_wsgi.py", "package failed forced compileall"),
        # A broken .cgi is invisible to compileall — only the dedicated
        # top-level entry-point gate can reject it. This is the one candidate
        # shape that proves that gate exists at all.
        ("aistat.cgi", "top-level entry point failed forced compile"),
    ],
)
def test_forced_top_level_compile_blocks_invalid_candidate(
    harness, relative, expected_error
):
    target = harness.source / relative
    target.write_text("def broken(:\n", encoding="utf-8")
    _git(harness.source, "add", relative)
    _git(harness.source, "commit", "-m", "invalid entry point")
    _git(harness.source, "push", "origin", "HEAD:refs/heads/main")
    sha, tree = harness.identity()

    result = harness.deploy(sha, tree)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not harness.app.exists() and not harness.app.is_symlink()
    assert harness.managed_releases() == []


def test_deploy_recovers_from_a_dangling_live_symlink(harness):
    sha, tree = harness.identity()
    harness.releases.mkdir()
    missing = harness.releases / "release-missing"
    harness.app.symlink_to(missing)

    result = harness.deploy(sha, tree)

    assert result.returncode == 0, result.stderr
    assert "points at a missing release" in result.stdout
    release = Path(os.readlink(str(harness.app)))
    assert release.is_dir() and release != missing
    assert "previous=" + str(missing) in result.stdout


def test_live_symlink_outside_the_releases_dir_is_still_rejected(harness):
    sha, tree = harness.identity()
    harness.releases.mkdir()
    harness.app.symlink_to(harness.home / "elsewhere")

    result = harness.deploy(sha, tree)

    assert result.returncode != 0
    assert "direct child of" in result.stderr
    assert os.readlink(str(harness.app)) == str(harness.home / "elsewhere")
    assert harness.managed_releases() == []


def test_rollback_recovers_from_a_dangling_live_symlink(harness):
    sha1, tree1 = harness.identity()
    assert harness.deploy(sha1, tree1).returncode == 0
    release1 = Path(os.readlink(str(harness.app)))
    sha2, tree2 = harness.commit("candidate two")
    assert harness.deploy(sha2, tree2).returncode == 0
    release2 = Path(os.readlink(str(harness.app)))
    shutil.rmtree(release2)
    assert harness.app.is_symlink() and not release2.exists()

    result = harness.rollback(release1)

    assert result.returncode == 0, result.stderr
    assert "points at a missing release" in result.stdout
    assert os.readlink(str(harness.app)) == str(release1)
    assert release1.is_dir()


def test_rollback_still_refuses_a_target_that_does_not_exist(harness):
    sha, tree = harness.identity()
    assert harness.deploy(sha, tree).returncode == 0
    live = os.readlink(str(harness.app))

    result = harness.rollback(harness.releases / "release-never-existed")

    assert result.returncode != 0
    assert "release target does not exist" in result.stderr
    assert os.readlink(str(harness.app)) == live


def test_served_release_survives_repeat_deploy_next_deploy_and_rollback(harness):
    sha1, tree1 = harness.identity()
    assert harness.deploy(sha1, tree1).returncode == 0
    release1 = Path(os.readlink(str(harness.app)))

    served = harness.import_live_app()
    assert served.returncode == 0, served.stderr
    bytecode = harness.bytecode_in(release1)
    assert bytecode, "the runtime wrote no bytecode into the live release"

    repeated = harness.deploy(sha1, tree1)
    assert repeated.returncode == 0, repeated.stderr
    assert "ALREADY LIVE" in repeated.stdout
    assert os.readlink(str(harness.app)) == str(release1)

    sha2, tree2 = harness.commit("candidate two")
    second = harness.deploy(sha2, tree2)
    assert second.returncode == 0, second.stderr
    release2 = Path(os.readlink(str(harness.app)))
    assert release2 != release1 and release1.is_dir()

    rolled_back = harness.rollback(release1)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert os.readlink(str(harness.app)) == str(release1)
    # The allowlist tolerates the bytecode; it never rewrites or removes it.
    assert harness.bytecode_in(release1) == bytecode


def test_runtime_allowlist_covers_bytecode_only(harness, tmp_path):
    sha, tree = harness.identity()
    assert harness.deploy(sha, tree).returncode == 0
    release = Path(os.readlink(str(harness.app)))
    manifest = _manifest(release)
    sha, tree = manifest["source_commit_sha"], manifest["source_tree_sha"]
    assert harness.verify_manifest(release, sha, tree, "strict").returncode == 0
    assert harness.verify_manifest(release, sha, tree, "runtime").returncode == 0

    served = tmp_path / "served-release"
    shutil.copytree(release, served)
    cache = served / "aistat" / "__pycache__"
    cache.mkdir()
    (cache / "legacy_wsgi.cpython-36.pyc").write_bytes(b"\x00fake bytecode")

    strict = harness.verify_manifest(served, sha, tree, "strict")
    assert strict.returncode != 0
    assert "aistat/__pycache__/legacy_wsgi.cpython-36.pyc" in strict.stderr
    assert harness.verify_manifest(served, sha, tree, "runtime").returncode == 0

    (cache / "notes.txt").write_text("not bytecode\n", encoding="utf-8")
    intruder = harness.verify_manifest(served, sha, tree, "runtime")
    assert intruder.returncode != 0
    assert "aistat/__pycache__/notes.txt" in intruder.stderr
    (cache / "notes.txt").unlink()

    (served / "data").mkdir()
    (served / "data" / "aistat.db").write_text("runtime state\n", encoding="utf-8")
    runtime_state = harness.verify_manifest(served, sha, tree, "runtime")
    assert runtime_state.returncode != 0
    assert "data/aistat.db" in runtime_state.stderr
    shutil.rmtree(served / "data")

    pricing = served / "pricing.json"
    pricing.write_bytes(b"#" * len(pricing.read_bytes()))
    tampered = harness.verify_manifest(served, sha, tree, "runtime")
    assert tampered.returncode != 0
    assert "manifest digest mismatch: pricing.json" in tampered.stderr


def test_runtime_state_in_the_live_release_blocks_the_next_deploy_by_name(harness):
    sha1, tree1 = harness.identity()
    assert harness.deploy(sha1, tree1).returncode == 0
    release1 = Path(os.readlink(str(harness.app)))
    (release1 / "data").mkdir()
    (release1 / "data" / "aistat.db").write_text("runtime state\n", encoding="utf-8")
    sha2, tree2 = harness.commit("candidate two")

    result = harness.deploy(sha2, tree2)

    assert result.returncode != 0
    assert "data/aistat.db" in result.stderr
    assert os.readlink(str(harness.app)) == str(release1)
    assert harness.managed_releases() == [release1]


def test_deploy_and_rollback_work_through_a_symlinked_path(symlinked_harness):
    harness = symlinked_harness
    # The whole point of this fixture: an unresolved path with a real link in it.
    assert os.path.realpath(str(harness.home)) != str(harness.home)

    sha1, tree1 = harness.identity()
    first = harness.deploy(sha1, tree1)
    assert first.returncode == 0, first.stderr
    release1 = Path(os.readlink(str(harness.app)))
    assert _manifest(release1)["source_commit_sha"] == sha1

    sha2, tree2 = harness.commit("candidate two")
    second = harness.deploy(sha2, tree2)
    assert second.returncode == 0, second.stderr
    release2 = Path(os.readlink(str(harness.app)))
    assert _manifest(release2)["source_commit_sha"] == sha2

    rolled_back = harness.rollback(release1)
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert os.readlink(str(harness.app)) == str(release1)


def test_symlinked_payload_file_is_still_refused(harness, tmp_path):
    sha, tree = harness.identity()
    assert harness.deploy(sha, tree).returncode == 0
    package = tmp_path / "linked-file-package"
    shutil.copytree(Path(os.readlink(str(harness.app))), package)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = package / "pricing.json"
    stolen = outside / "pricing.json"
    stolen.write_text(victim.read_text("utf-8"), encoding="utf-8")
    victim.unlink()
    victim.symlink_to(stolen)

    result = harness.verify_manifest(package, sha, tree, "strict")

    assert result.returncode != 0
    assert "manifest path is not a regular file: pricing.json" in result.stderr


@pytest.mark.parametrize("harness_fixture", ("harness", "symlinked_harness"))
def test_payload_resolving_outside_the_package_is_still_refused(
    request, harness_fixture
):
    """A directory alias cannot smuggle content in from outside the root."""
    harness = request.getfixturevalue(harness_fixture)
    sha, tree = harness.identity()
    assert harness.deploy(sha, tree).returncode == 0
    workspace = harness.source.parent
    package = workspace / "aliased-package"
    shutil.copytree(Path(os.readlink(str(harness.app))), package)
    outside = workspace / "outside"
    outside.mkdir()
    shutil.move(str(package / "aistat"), str(outside / "aistat"))
    (package / "aistat").symlink_to(outside / "aistat", target_is_directory=True)

    result = harness.verify_manifest(package, sha, tree, "strict")

    assert result.returncode != 0
    assert "manifest path escapes package: aistat/" in result.stderr


def test_directory_alias_inside_the_package_is_still_refused(harness, tmp_path):
    """Resolving inside the root is not enough — no symlink may live in a package."""
    sha, tree = harness.identity()
    assert harness.deploy(sha, tree).returncode == 0
    package = tmp_path / "internal-alias-package"
    shutil.copytree(Path(os.readlink(str(harness.app))), package)
    (package / "aistat-alias").symlink_to(package / "aistat", target_is_directory=True)

    result = harness.verify_manifest(package, sha, tree, "strict")

    assert result.returncode != 0
    assert "package contains symlink" in result.stderr


@pytest.mark.parametrize("harness_fixture", ("harness", "symlinked_harness"))
def test_manifest_entry_through_an_internal_alias_is_still_refused(
    request, harness_fixture
):
    """The one layout whose rejection *path* moved when the escape check was cut.

    A manifest path traversing an in-package directory alias used to be caught by
    the removed dirname comparison; it resolves back inside the root, so only the
    symlink walk can reject it now. Nothing else in the suite pins that, and a
    refactor of the walk would silently reopen the hole.
    """
    harness = request.getfixturevalue(harness_fixture)
    sha, tree = harness.identity()
    assert harness.deploy(sha, tree).returncode == 0
    package = harness.source.parent / "entry-through-alias-package"
    shutil.copytree(Path(os.readlink(str(harness.app))), package)
    # The manifest still lists `aistat/...`; those paths now reach their files
    # through the alias, and each one resolves back inside the package root.
    shutil.move(str(package / "aistat"), str(package / "real"))
    (package / "aistat").symlink_to(package / "real", target_is_directory=True)

    result = harness.verify_manifest(package, sha, tree, "strict")

    assert result.returncode != 0
    assert "package contains symlink" in result.stderr


def test_only_already_published_releases_relax_to_the_runtime_allowlist():
    script = (REPO_ROOT / "deploy" / "cpanel_deploy.sh").read_text("utf-8")
    calls = re.findall(r"^\s*verify_manifest (\S+)(.*)$", script, re.MULTILINE)
    assert {
        target: "runtime" if "runtime" in rest else "strict" for target, rest in calls
    } == {
        '"$package"': "strict",  # freshly built package
        '"$incoming"': "strict",  # staged release, before publication
        '"$PREVIOUS_TARGET"': "runtime",  # live release, already served traffic
        '"$target"': "runtime",  # rollback target, already served traffic
    }


@pytest.mark.parametrize("delivery", ["pid", "group"])
@pytest.mark.parametrize("signal_name", ["TERM", "HUP", "INT"])
def test_signal_at_the_commit_point_keeps_the_live_release(
    harness, tmp_path, delivery, signal_name
):
    sha, tree = harness.identity()
    env = _fault_env(harness, tmp_path, delivery, signal=signal_name)

    result = harness.deploy(sha, tree, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PUBLISHED" in result.stdout
    release = Path(os.readlink(str(harness.app)))
    assert release.is_dir()
    assert (release / "PACKAGE-MANIFEST.json").is_file()
    assert not list(harness.home.glob(".aistat_app.next.*"))


def test_nonzero_publish_helper_after_the_commit_point_keeps_the_live_release(
    harness, tmp_path
):
    sha, tree = harness.identity()
    env = _fault_env(harness, tmp_path, "exit", exit_code="3")

    result = harness.deploy(sha, tree, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "publish helper exited with status 3" in result.stdout
    assert "PUBLISHED" in result.stdout
    release = Path(os.readlink(str(harness.app)))
    assert release.is_dir()
    assert (release / "PACKAGE-MANIFEST.json").is_file()


def test_signal_at_the_rollback_commit_point_keeps_both_releases(harness, tmp_path):
    sha1, tree1 = harness.identity()
    assert harness.deploy(sha1, tree1).returncode == 0
    release1 = Path(os.readlink(str(harness.app)))
    sha2, tree2 = harness.commit("candidate two")
    assert harness.deploy(sha2, tree2).returncode == 0
    release2 = Path(os.readlink(str(harness.app)))
    env = _fault_env(harness, tmp_path, "group", signal="TERM")

    result = harness.rollback(release1, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ROLLED BACK" in result.stdout
    assert os.readlink(str(harness.app)) == str(release1)
    assert release1.is_dir() and release2.is_dir()


def test_tampered_manifest_blocks_rollback_and_preserves_live(harness):
    sha1, tree1 = harness.identity()
    assert harness.deploy(sha1, tree1).returncode == 0
    release1 = Path(os.readlink(str(harness.app)))
    sha2, tree2 = harness.commit("candidate two")
    assert harness.deploy(sha2, tree2).returncode == 0
    release2 = Path(os.readlink(str(harness.app)))
    (release1 / "pricing.json").write_text("tampered\n", encoding="utf-8")

    result = harness.rollback(release1)

    assert result.returncode != 0
    assert "manifest verification failed" in result.stderr
    assert os.readlink(str(harness.app)) == str(release2)
