"""The cPanel package must stay free of worker code, deps and secrets."""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cpanel_package_keeps_worker_side_out(tmp_path):
    env = dict(os.environ, AISTAT_SKIP_ZIP="1")
    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "build_cpanel_package.sh")],
        check=True,
        capture_output=True,
        env=env,
        cwd=REPO_ROOT,
    )
    package = REPO_ROOT / "dist" / "aistat-cpanel"
    # Both public contours and their shared protocol module ship...
    assert (package / "aistat" / "wsgi.py").is_file()
    assert (package / "aistat" / "legacy_wsgi.py").is_file()
    assert (package / "aistat" / "handoff.py").is_file()
    # ...while worker-only code, its crypto dependency, env files and any
    # key/store material never reach the shared host.
    assert not (package / "aistat" / "worker_store.py").exists()
    assert not (package / "aistat" / "worker_sync.py").exists()
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
