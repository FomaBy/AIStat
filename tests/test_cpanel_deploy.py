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


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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
            # The production host runs CPython 3.6.8, which has no pycache
            # prefix and always writes bytecode next to the sources. Some 3.8+
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

    def managed_releases(self):
        if not self.releases.exists():
            return []
        return sorted(
            path for path in self.releases.iterdir() if path.name.startswith("release-")
        )


@pytest.fixture
def harness(tmp_path):
    return DeployHarness(tmp_path)


def _manifest(release):
    return json.loads((release / "PACKAGE-MANIFEST.json").read_text("utf-8"))


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
    assert not (harness.home / "data").exists()

    releases = harness.managed_releases()
    repeated = harness.deploy(sha, tree)
    assert repeated.returncode == 0, repeated.stderr
    assert "ALREADY LIVE" in repeated.stdout
    assert os.readlink(str(harness.app)) == str(release)
    assert harness.managed_releases() == releases


def test_remote_commit_and_tree_drift_leave_live_target_unchanged(harness):
    first_sha, first_tree = harness.identity()
    first = harness.deploy(first_sha, first_tree)
    assert first.returncode == 0, first.stderr
    live = os.readlink(str(harness.app))
    releases = harness.managed_releases()
    second_sha, second_tree = harness.commit("candidate two")

    stale = harness.deploy(first_sha, first_tree)
    assert stale.returncode != 0
    assert "commit drift" in stale.stderr
    assert os.readlink(str(harness.app)) == live
    assert harness.managed_releases() == releases

    wrong_tree = harness.deploy(second_sha, "0" * 40)
    assert wrong_tree.returncode != 0
    assert "tree drift" in wrong_tree.stderr
    assert os.readlink(str(harness.app)) == live
    assert harness.managed_releases() == releases


def test_second_fetch_detects_drift_and_removes_unpublished_stage(harness, tmp_path):
    expected_sha, expected_tree = harness.identity()
    next_sha, _next_tree = harness.commit("future candidate")
    _git(harness.source, "push", "origin", "HEAD:refs/heads/next")
    subprocess.run(
        ["git", "--git-dir", str(harness.origin), "update-ref", "refs/heads/main", expected_sha],
        check=True,
    )

    real_git = shutil.which("git")
    wrapper_dir = tmp_path / "fake-bin"
    wrapper_dir.mkdir()
    counter = tmp_path / "fetch-count"
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *" fetch "*)
    count=0
    [ ! -f "$FETCH_COUNT" ] || count="$(cat "$FETCH_COUNT")"
    count=$((count + 1))
    printf '%s\n' "$count" >"$FETCH_COUNT"
    if [ "$count" -eq 2 ]; then
      "$REAL_GIT" --git-dir="$DRIFT_ORIGIN" update-ref refs/heads/main "$DRIFT_SHA"
    fi
    ;;
esac
exec "$REAL_GIT" "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = dict(
        harness.env,
        PATH=str(wrapper_dir) + os.pathsep + harness.env["PATH"],
        REAL_GIT=real_git,
        FETCH_COUNT=str(counter),
        DRIFT_ORIGIN=str(harness.origin),
        DRIFT_SHA=next_sha,
    )

    result = harness.deploy(expected_sha, expected_tree, env)

    assert result.returncode != 0
    assert "pre-publish commit drift" in result.stderr
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


@pytest.mark.parametrize("relative", ["aistat.cgi", "passenger_wsgi.py"])
def test_forced_top_level_compile_blocks_invalid_candidate(harness, relative):
    target = harness.source / relative
    target.write_text("def broken(:\n", encoding="utf-8")
    _git(harness.source, "add", relative)
    _git(harness.source, "commit", "-m", "invalid entry point")
    _git(harness.source, "push", "origin", "HEAD:refs/heads/main")
    sha, tree = harness.identity()

    result = harness.deploy(sha, tree)

    assert result.returncode != 0
    assert "forced compile" in result.stderr
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
