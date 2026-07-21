"""The ``python -m aistat.backup`` chain must stay Python 3.6.8 compatible.

FAN-1435. The production host (Namecheap shared, ``server386``) ships only
Python 3.6.8 — no ``python3.7+`` and no ``dataclasses`` module. The public CGI
contour is deliberately kept 3.6-clean, but the FAN-1185 backup/restore CLI and
every module it imports must be too, or ``python -m aistat.backup <cmd>`` dies
on the host. Blockers already hit and fixed:

* import-time ``ModuleNotFoundError: No module named 'dataclasses'`` (3.7+);
* ``Path.unlink(missing_ok=...)`` (3.8+) and ``subprocess(..., text=...)`` (3.7+);
* ``argparse ...add_subparsers(required=True)`` (3.7+) — ``TypeError`` on 3.6;
* ``sqlite3.Connection.backup()`` (3.7+) — ``AttributeError`` on 3.6.

These guards catch a regression on a dev machine that only has 3.7+. The
``sqlite3.Connection.backup`` case can't be caught by a static kwarg scan (it is
a type-blind instance method), so it is guarded at run time instead by forcing
the fallback path and driving a full backup cycle.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import aistat
from aistat import backup as backup_mod
from aistat import snapshot as snapshot_mod
from aistat.config import Config
from aistat.db import connect, init_db
from conftest import seed_aggregate_fixture

REPO_ROOT = Path(aistat.__file__).resolve().parent.parent

# Attribute/keyword names introduced after Python 3.6 that would break on the host.
_SUBPROCESS_CALLERS = {"run", "Popen", "call", "check_call", "check_output"}


def _run_python(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def _backup_chain_files():
    """Source files of every ``aistat.*`` module imported by ``aistat.backup``.

    Discovered by importing in a *fresh* interpreter, so the set is exactly the
    backup chain and not whatever the pytest process already imported.
    """
    result = _run_python(
        "import sys, aistat.backup, json;"
        "print(json.dumps(sorted("
        "m for m in sys.modules "
        "if m == 'aistat' or m.startswith('aistat.'))))"
    )
    assert result.returncode == 0, "importing aistat.backup failed:\n" + result.stderr
    names = json.loads(result.stdout.strip().splitlines()[-1])
    files = []
    for name in names:
        path = REPO_ROOT / Path(*name.split(".")).with_suffix(".py")
        if path.is_file():
            files.append(path)
    path_names = [p.name for p in files]
    assert path_names, "no chain modules discovered"
    # Sanity: the two modules that used to import dataclasses are in the chain.
    assert "config.py" in path_names
    assert "snapshot.py" in path_names
    return files


def test_backup_import_chain_avoids_dataclasses():
    """Importing the backup CLI must not pull in the 3.7+ ``dataclasses`` module."""
    result = _run_python(
        "import sys, aistat.backup;"
        "sys.exit(3 if 'dataclasses' in sys.modules else 0)"
    )
    assert result.returncode == 0, (
        "importing aistat.backup pulled in 'dataclasses' (a Python 3.7+ module); "
        "the production host runs Python 3.6.8 where it does not exist.\n"
        + result.stderr
    )


def test_backup_chain_has_no_post_36_constructs():
    """No chain module may use a statically-detectable feature newer than 3.6."""
    offenders = []
    for path in _backup_chain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
                offenders.append((path.name, node.lineno, "imports dataclasses (3.7+)"))
            elif isinstance(node, ast.Import):
                if any(alias.name == "dataclasses" for alias in node.names):
                    offenders.append((path.name, node.lineno, "imports dataclasses (3.7+)"))
            elif isinstance(node, ast.Attribute) and node.attr == "fromisoformat":
                offenders.append((path.name, node.lineno, "datetime.fromisoformat (3.7+)"))
            elif isinstance(node, ast.Call):
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else None
                is_subprocess = attr in _SUBPROCESS_CALLERS
                for kw in node.keywords:
                    if kw.arg == "missing_ok":
                        offenders.append((path.name, node.lineno, "Path.unlink(missing_ok=) (3.8+)"))
                    if kw.arg == "text" and is_subprocess:
                        offenders.append((path.name, node.lineno, "subprocess(text=) (3.7+)"))
                    if kw.arg == "required" and attr == "add_subparsers":
                        offenders.append((path.name, node.lineno, "add_subparsers(required=) (3.7+)"))
    assert not offenders, (
        "Python 3.6-incompatible constructs in the aistat.backup chain "
        "(host runs 3.6.8): " + "; ".join("%s:%d %s" % o for o in offenders)
    )


def test_backup_main_requires_a_subcommand():
    """No subcommand exits 2 cleanly — not a 3.7-only ``required=`` TypeError."""
    assert backup_mod.main([]) == 2


def _cfg_with_seeded_db(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conn = connect(data / "aistat.db")
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()
    return Config(
        db_path=data / "aistat.db",
        security_db_path=data / "security.db",
        worker_store_path=data / "worker_connections.db",
        tenants_dir=data / "tenants",
        backup_dir=data / "backups",
        backup_retention=14,
    )


def test_backup_works_without_sqlite_backup_api(tmp_path, monkeypatch):
    """Force the Python 3.6 fallback (no ``Connection.backup``) and prove a full
    create -> restore -> verify self-test still succeeds coherently."""
    monkeypatch.setattr(snapshot_mod, "_HAS_SQLITE_BACKUP", False)
    assert snapshot_mod._HAS_SQLITE_BACKUP is False
    cfg = _cfg_with_seeded_db(tmp_path)

    report = backup_mod.self_test(cfg)
    assert report["ok"] is True
    assert any(m["label"] == "aistat.db" for m in report["members"])

    generation = backup_mod.create_backup(cfg)
    verified = backup_mod.verify_backup(cfg, generation.name)
    assert "aistat.db" in verified["verified_members"]
