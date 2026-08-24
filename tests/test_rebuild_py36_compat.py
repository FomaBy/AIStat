"""The isolated recovery CLI must run on the production Python 3.6.8 host."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import aistat


REPO_ROOT = Path(aistat.__file__).resolve().parent.parent


def _run_python(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def _rebuild_chain_files():
    result = _run_python(
        "import sys, aistat.rebuild, json;"
        "print(json.dumps(sorted("
        "m for m in sys.modules "
        "if m == 'aistat' or m.startswith('aistat.'))))"
    )
    assert result.returncode == 0, "importing aistat.rebuild failed:\\n" + result.stderr
    names = json.loads(result.stdout.strip().splitlines()[-1])
    return [
        path for path in (
            REPO_ROOT / Path(*name.split(".")).with_suffix(".py")
            for name in names
        )
        if path.is_file()
    ]


def test_rebuild_import_chain_avoids_dataclasses():
    """`python -m aistat.rebuild` cannot import a Python-3.7-only module."""
    result = _run_python(
        "import sys, aistat.rebuild;"
        "sys.exit(3 if 'dataclasses' in sys.modules else 0)"
    )
    assert result.returncode == 0, (
        "importing aistat.rebuild pulled in 'dataclasses' (Python 3.7+):\\n"
        + result.stderr
    )


def test_rebuild_chain_has_no_dataclasses_import():
    """The fresh recovery CLI import chain remains statically Python-3.6 clean."""
    offenders = []
    for path in _rebuild_chain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
                offenders.append((path.name, node.lineno))
            elif isinstance(node, ast.Import):
                if any(alias.name == "dataclasses" for alias in node.names):
                    offenders.append((path.name, node.lineno))
    assert not offenders, "dataclasses imports in rebuild chain: %s" % offenders
