"""The ``python -m aistat.backup`` chain must stay Python 3.6.8 compatible.

FAN-1435. The production host (Namecheap shared, ``server386``) ships only
Python 3.6.8 — no ``python3.7+`` and no ``dataclasses`` module. The public CGI
contour is deliberately kept 3.6-clean, but the FAN-1185 backup/restore CLI and
every module it imports must be too, or ``python -m aistat.backup <cmd>`` dies
on the host (``ModuleNotFoundError: No module named 'dataclasses'`` at import,
and later ``Path.unlink(missing_ok=...)`` / ``subprocess(..., text=...)`` at
run time). These guards catch a regression on a dev machine that only has 3.7+.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import aistat

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
    """No chain module may use a syntax/API feature unavailable on Python 3.6."""
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
                is_subprocess = isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_CALLERS
                for kw in node.keywords:
                    if kw.arg == "missing_ok":
                        offenders.append((path.name, node.lineno, "Path.unlink(missing_ok=) (3.8+)"))
                    if kw.arg == "text" and is_subprocess:
                        offenders.append((path.name, node.lineno, "subprocess(text=) (3.7+)"))
    assert not offenders, (
        "Python 3.6-incompatible constructs in the aistat.backup chain "
        "(host runs 3.6.8): " + "; ".join("%s:%d %s" % o for o in offenders)
    )
