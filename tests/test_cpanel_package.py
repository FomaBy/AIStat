"""The cPanel package must stay free of worker code, deps and secrets."""

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_package():
    env = dict(os.environ, AISTAT_SKIP_ZIP="1")
    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "build_cpanel_package.sh")],
        check=True,
        capture_output=True,
        env=env,
        cwd=REPO_ROOT,
    )
    return REPO_ROOT / "dist" / "aistat-cpanel"


def test_cpanel_package_keeps_worker_side_out(tmp_path):
    package = _build_package()
    # Both public contours and their shared protocol module ship...
    assert (package / "aistat" / "wsgi.py").is_file()
    assert (package / "aistat" / "legacy_wsgi.py").is_file()
    assert (package / "aistat" / "handoff.py").is_file()
    # ...while worker-only code, its crypto dependency, env files and any
    # key/store material never reach the shared host.
    assert not (package / "aistat" / "worker_store.py").exists()
    assert not (package / "aistat" / "worker_sync.py").exists()
    # The trusted-local runtime supervisor/installer/preflight never ship.
    assert not (package / "aistat" / "supervisor.py").exists()
    assert not (package / "aistat" / "runtime_install.py").exists()
    assert not (package / "aistat" / "preflight.py").exists()
    assert (package / "aistat" / "endpoints.py").is_file()
    deployable_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package.rglob("*.py")
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
    """The shared host runs only the WSGI contours; the local FastAPI/uvicorn
    app (aistat/server.py) and its dependency must never reach it."""
    package = _build_package()

    # The loopback-only FastAPI module does not ship at all.
    assert not (package / "aistat" / "server.py").exists()

    # No shipped module imports FastAPI/uvicorn/starlette.
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if re.search(
            r"\b(fastapi|uvicorn|starlette)\b",
            path.read_text(encoding="utf-8"),
        )
    ]
    assert offenders == []

    # The dependency-free requirements pin no ASGI stack.
    requirements = (
        (package / "requirements.txt").read_text(encoding="utf-8").lower()
    )
    for forbidden in ("fastapi", "uvicorn", "starlette"):
        assert forbidden not in requirements

    # The entry points run the dependency-free legacy WSGI app, not FastAPI.
    assert "legacy_wsgi" in (
        package / "passenger_wsgi.py"
    ).read_text(encoding="utf-8")
    assert "legacy_wsgi" in (package / "aistat.cgi").read_text(encoding="utf-8")
