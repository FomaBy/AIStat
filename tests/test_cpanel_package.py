"""The cPanel package is immutable, verifiable and free of local-only code."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_INPUTS = (
    "aistat",
    "aistat.cgi",
    "passenger_wsgi.py",
    "pricing.json",
    "requirements-cpanel.txt",
    "deploy/namecheap.htaccess",
    "scripts/build_cpanel_package.sh",
)


def _run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _candidate_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in PACKAGE_INPUTS:
        source = REPO_ROOT / relative
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    _run_git(repo, "config", "user.email", "qa@example.invalid")
    _run_git(repo, "config", "user.name", "AIStat QA")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "candidate")
    sha = _run_git(repo, "rev-parse", "HEAD")
    tree = _run_git(repo, "rev-parse", "HEAD^{tree}")
    return repo, sha, tree


def _build_package(tmp_path):
    repo, sha, tree = _candidate_repo(tmp_path)
    env = dict(os.environ, AISTAT_SKIP_ZIP="1")
    subprocess.run(
        ["bash", str(repo / "scripts" / "build_cpanel_package.sh"), sha, tree],
        check=True,
        capture_output=True,
        env=env,
        cwd=repo,
    )
    return repo / "dist" / "aistat-cpanel", repo, sha, tree


def _load_manifest(package):
    return json.loads((package / "PACKAGE-MANIFEST.json").read_text("utf-8"))


def _payload_paths(package):
    return {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file() and path.name != "PACKAGE-MANIFEST.json"
    }


def test_cpanel_package_keeps_worker_side_out(tmp_path):
    package, _repo, _sha, _tree = _build_package(tmp_path)
    assert (package / "aistat" / "wsgi.py").is_file()
    assert (package / "aistat" / "legacy_wsgi.py").is_file()
    assert (package / "aistat" / "handoff.py").is_file()
    assert not (package / "aistat" / "worker_store.py").exists()
    assert not (package / "aistat" / "worker_sync.py").exists()
    assert not (package / "aistat" / "supervisor.py").exists()
    assert not (package / "aistat" / "runtime_install.py").exists()
    assert not (package / "aistat" / "preflight.py").exists()
    assert (package / "aistat" / "endpoints.py").is_file()
    deployable_source = "\n".join(
        path.read_text(encoding="utf-8") for path in package.rglob("*.py")
    )
    assert "AISTAT_ALLOW_INSECURE_PUBLISH" not in deployable_source
    assert "allow_insecure_publish" not in deployable_source
    requirements = (package / "requirements.txt").read_text(encoding="utf-8")
    assert "cryptography" not in requirements
    leftovers = [
        str(path)
        for path in package.rglob("*")
        if path.name.startswith(".env")
        or path.name.endswith((".key", ".db"))
        or "worker_connections" in path.name
    ]
    assert leftovers == []


def test_cpanel_package_excludes_local_fastapi_contour(tmp_path):
    package, _repo, _sha, _tree = _build_package(tmp_path)
    assert not (package / "aistat" / "server.py").exists()
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if re.search(
            r"\b(fastapi|uvicorn|starlette)\b",
            path.read_text(encoding="utf-8"),
        )
    ]
    assert offenders == []
    requirements = (package / "requirements.txt").read_text("utf-8").lower()
    for forbidden in ("fastapi", "uvicorn", "starlette"):
        assert forbidden not in requirements
    assert "legacy_wsgi" in (package / "passenger_wsgi.py").read_text("utf-8")
    assert "legacy_wsgi" in (package / "aistat.cgi").read_text("utf-8")


def test_package_uses_only_exact_commit_and_rejects_wrong_tree(tmp_path):
    package, repo, sha, tree = _build_package(tmp_path)
    original_manifest = (package / "PACKAGE-MANIFEST.json").read_bytes()
    (repo / "aistat" / "qa_untracked_sentinel.py").write_text(
        "SECRET_SENTINEL = True\n", encoding="utf-8"
    )
    (repo / "pricing.json").write_text("dirty tracked input\n", encoding="utf-8")
    env = dict(os.environ, AISTAT_SKIP_ZIP="1")
    subprocess.run(
        ["bash", "scripts/build_cpanel_package.sh", sha, tree],
        check=True,
        capture_output=True,
        env=env,
        cwd=repo,
    )
    assert not (package / "aistat" / "qa_untracked_sentinel.py").exists()
    assert (package / "pricing.json").read_text("utf-8") != "dirty tracked input\n"
    assert (package / "PACKAGE-MANIFEST.json").read_bytes() == original_manifest

    failed = subprocess.run(
        ["bash", "scripts/build_cpanel_package.sh", sha, "0" * 40],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo,
    )
    assert failed.returncode != 0
    assert "tree mismatch" in failed.stderr
    assert (package / "PACKAGE-MANIFEST.json").read_bytes() == original_manifest


def test_manifest_covers_exact_payload_hash_size_and_mode(tmp_path):
    package, _repo, sha, tree = _build_package(tmp_path)
    manifest = _load_manifest(package)
    assert manifest["format"] == "aistat-cpanel-package"
    assert manifest["format_version"] == 1
    assert manifest["hash_algorithm"] == "sha256"
    assert manifest["source_commit_sha"] == sha
    assert manifest["source_tree_sha"] == tree
    paths = [entry["path"] for entry in manifest["files"]]
    assert paths == sorted(paths)
    assert len(paths) == len(set(paths))
    assert set(paths) == _payload_paths(package)
    for entry in manifest["files"]:
        path = package / entry["path"]
        info = path.stat()
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["size_bytes"] == info.st_size
        assert entry["mode"] == "%04o" % stat.S_IMODE(info.st_mode)
        assert not path.is_symlink()


def test_archived_builder_accepts_external_object_source(tmp_path):
    repo, sha, tree = _candidate_repo(tmp_path)
    archive = tmp_path / "candidate.tar"
    with archive.open("wb") as stream:
        subprocess.run(
            ["git", "-C", str(repo), "archive", sha], check=True, stdout=stream
        )
    stage = tmp_path / "stage"
    stage.mkdir()
    with tarfile.open(str(archive)) as tar:
        tar.extractall(str(stage))
    env = dict(
        os.environ,
        AISTAT_SKIP_ZIP="1",
        AISTAT_SOURCE_REPOSITORY=str(repo),
    )
    subprocess.run(
        ["bash", "scripts/build_cpanel_package.sh", sha, tree],
        check=True,
        capture_output=True,
        env=env,
        cwd=stage,
    )
    manifest = _load_manifest(stage / "dist" / "aistat-cpanel")
    assert manifest["source_commit_sha"] == sha
    assert manifest["source_tree_sha"] == tree


@pytest.mark.parametrize("value", ["", "abc1234", "A" * 40, "0" * 39])
def test_builder_requires_full_lowercase_identifiers(tmp_path, value):
    repo, sha, tree = _candidate_repo(tmp_path)
    args = [value or sha, tree] if value else []
    failed = subprocess.run(
        ["bash", "scripts/build_cpanel_package.sh", *args],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert failed.returncode != 0
    assert "usage:" in failed.stderr
