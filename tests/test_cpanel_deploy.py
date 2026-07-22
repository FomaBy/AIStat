"""Isolated executable proof for exact and atomic cPanel deployment."""

import fcntl
import json
import os
import shutil
import subprocess
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

    def deploy(self, sha, tree, env=None):
        return subprocess.run(
            [
                "bash",
                str(self.host / "deploy" / "cpanel_deploy.sh"),
                "deploy",
                sha,
                tree,
            ],
            cwd=self.host,
            env=env or self.env,
            capture_output=True,
            text=True,
        )

    def rollback(self, target, env=None):
        return subprocess.run(
            [
                "bash",
                str(self.host / "deploy" / "cpanel_deploy.sh"),
                "rollback",
                str(target),
            ],
            cwd=self.host,
            env=env or self.env,
            capture_output=True,
            text=True,
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


def test_dangling_live_target_is_rejected_without_staging(harness):
    sha, tree = harness.identity()
    harness.releases.mkdir()
    harness.app.symlink_to(harness.releases / "missing")

    result = harness.deploy(sha, tree)

    assert result.returncode != 0
    assert "release target does not exist" in result.stderr
    assert os.readlink(str(harness.app)) == str(harness.releases / "missing")
    assert harness.managed_releases() == []


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
