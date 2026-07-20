"""Transactional install / uninstall of the local AIStat runtime supervisor.

The runtime lives under a *runtime root* (``$HOME/Library/Application Support/
AIStat`` by default) with a strict split:

    <root>/code/        active code copy (the ``aistat`` package + manifests)
    <root>/code.prev/   previous code copy, kept for rollback
    <root>/data/        persistent state — dbs, encrypted store, tenants, logs
    <root>/.venv/       runtime virtualenv (created by the shell wrapper)

An update stages a fresh copy, renders the launchd plist from the real ``$HOME``
and runtime root (no hard-coded username), then atomically swaps ``code/`` and
(re)bootstraps a single supervisor job. Any failure after the swap restores the
previous ``code/`` and re-bootstraps it, so a broken update can never leave the
machine without a working runtime. ``data/`` — the worker key, encrypted store,
tenant databases, owner database and logs — is on a separate path and is never
touched by an update; only ``--purge`` uninstall removes it.

The plist never carries a secret: it points at an owner-only env file
(``AISTAT_ENV_FILE``) that the supervisor loads at startup. This module only
ever writes non-secret paths into the plist.

Every runtime-activating command — ``install``, ``restart`` and ``rollback`` —
requires the *persistent* private env file and validates the effective
configuration first: the file must exist as an owner-only regular file, is
parsed (never shell-executed) and loaded with the same source/precedence
semantics as the supervisor before any ``launchctl`` call or code/plist
mutation. Secrets exported only in the invoking shell can never satisfy this
gate — the launchd job carries no secrets, so a runtime without the file
would have no configuration to reload. Only ``uninstall`` (and ``status``/
``render``) stays available as a fail-safe path when the configuration is
invalid or absent.
"""

import argparse
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import Config
from . import preflight

logger = logging.getLogger("aistat.runtime_install")

LABEL = "com.aistat.runtime"

# Env keys that must never appear in the plist (criterion 6). Rendering builds
# the plist from non-secret paths only; this list backs an explicit guard.
_SECRET_ENV_KEYS = (
    "AISTAT_INGEST_SECRET",
    "AISTAT_SESSION_SECRET",
    "AISTAT_WORKER_SECRET",
    "AISTAT_PASSWORD_HASH",
)

DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class RuntimeInstallError(RuntimeError):
    """Raised for an install/uninstall failure that leaves state consistent."""


class PreflightFailed(RuntimeInstallError):
    def __init__(self, report: "preflight.PreflightReport"):
        self.report = report
        super().__init__("preflight failed:\n" + report.render())


class LaunchError(RuntimeInstallError):
    pass


# --------------------------------------------------------------------------
# plist rendering
# --------------------------------------------------------------------------

def runtime_env(runtime_root: Path, env_file: Path) -> Dict[str, str]:
    """Non-secret environment baked into the plist.

    Data paths are pinned under ``<root>/data`` (never ``<root>/code/data``)
    so a code swap can never disturb the owner database, encrypted store or
    tenant snapshots.
    """
    root = Path(runtime_root)
    data = root / "data"
    return {
        "PATH": DEFAULT_PATH,
        "AISTAT_RUNTIME_ROOT": str(root),
        "AISTAT_ENV_FILE": str(env_file),
        "AISTAT_DB_PATH": str(data / "aistat.db"),
        "AISTAT_WORKER_STORE_PATH": str(data / "worker_connections.db"),
        "AISTAT_WORKER_TENANTS_DIR": str(data / "worker_tenants"),
        "AISTAT_CLI_PROFILES_DIR": str(data / "cli_profiles"),
    }


def render_plist(runtime_root: Path, python: str, env_file: Path,
                 *, label: str = LABEL,
                 extra_env: Optional[Dict[str, str]] = None) -> str:
    """Build a valid launchd plist for the supervisor job.

    Built programmatically (via :mod:`plistlib`) so the output is always valid
    and every path derives from the caller-supplied ``$HOME``-based runtime
    root — there is no hard-coded username anywhere in the result.
    """
    root = Path(runtime_root)
    env = runtime_env(root, Path(env_file))
    if extra_env:
        env.update(extra_env)
    data = root / "data"
    document = {
        "Label": label,
        "ProgramArguments": [str(python), "-m", "aistat.supervisor"],
        "WorkingDirectory": str(root / "code"),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(data / "runtime.stdout.log"),
        "StandardErrorPath": str(data / "runtime.stderr.log"),
    }
    assert_no_secrets_in_plist(document)
    return plistlib.dumps(document).decode("utf-8")


def assert_no_secrets_in_plist(document: Dict) -> None:
    env = document.get("EnvironmentVariables", {}) or {}
    leaked = [k for k in _SECRET_ENV_KEYS if k in env]
    if leaked:
        raise RuntimeInstallError(
            "refusing to write secrets into the plist: {}".format(
                ", ".join(sorted(leaked))
            )
        )


def validate_plist(text: str) -> Dict:
    """Parse a rendered plist, proving it is well-formed (installer lint)."""
    try:
        document = plistlib.loads(text.encode("utf-8"))
    except Exception as exc:  # plistlib raises various parse errors
        raise RuntimeInstallError("rendered plist is invalid: {}".format(exc))
    assert_no_secrets_in_plist(document)
    return document


# --------------------------------------------------------------------------
# launchctl control (injectable so the whole flow is unit-testable)
# --------------------------------------------------------------------------

class LaunchController:
    """Abstract launchd control surface."""

    def bootstrap(self, plist_path: Path) -> None:
        raise NotImplementedError

    def bootout(self, label: str, plist_path: Optional[Path] = None) -> None:
        raise NotImplementedError

    def is_loaded(self, label: str) -> bool:
        raise NotImplementedError

    def kickstart(self, label: str) -> None:
        raise NotImplementedError


class LaunchctlController(LaunchController):
    def __init__(self, domain: Optional[str] = None):
        self.domain = domain or "gui/{}".format(os.getuid())

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["launchctl", *args], capture_output=True, text=True
        )

    def bootstrap(self, plist_path: Path) -> None:
        result = self._run("bootstrap", self.domain, str(plist_path))
        if result.returncode != 0:
            raise LaunchError(
                "launchctl bootstrap failed: {}".format(
                    (result.stderr or "").strip()[-300:]
                )
            )

    def bootout(self, label: str, plist_path: Optional[Path] = None) -> None:
        # Idempotent: booting out a job that is not loaded is not an error.
        target = str(plist_path) if plist_path else "{}/{}".format(
            self.domain, label)
        self._run("bootout", self.domain, target)

    def is_loaded(self, label: str) -> bool:
        result = self._run("print", "{}/{}".format(self.domain, label))
        return result.returncode == 0

    def kickstart(self, label: str) -> None:
        self._run("kickstart", "-k", "{}/{}".format(self.domain, label))


# --------------------------------------------------------------------------
# installer
# --------------------------------------------------------------------------

@dataclass
class InstallPaths:
    runtime_root: Path
    plist_dir: Path

    @property
    def code(self) -> Path:
        return self.runtime_root / "code"

    @property
    def code_prev(self) -> Path:
        return self.runtime_root / "code.prev"

    @property
    def data(self) -> Path:
        return self.runtime_root / "data"

    @property
    def plist(self) -> Path:
        return self.plist_dir / (LABEL + ".plist")


class Installer:
    def __init__(
        self,
        runtime_root: Path,
        python: str,
        env_file: Path,
        controller: LaunchController,
        *,
        plist_dir: Optional[Path] = None,
        preflight_fn: Optional[Callable[[], "preflight.PreflightReport"]] = None,
    ):
        self.python = str(python)
        self.env_file = Path(env_file)
        self.controller = controller
        home_agents = Path.home() / "Library" / "LaunchAgents"
        self.paths = InstallPaths(
            Path(runtime_root), Path(plist_dir) if plist_dir else home_agents
        )
        self._preflight_fn = preflight_fn or self._default_preflight

    def _load_effective_env(self) -> Optional["preflight.Check"]:
        """Validate and load the required persistent env file, supervisor-style.

        The launchd job intentionally carries no secret values, so the
        installed supervisor can only reload configuration from this file.
        Missing (explicit *or* default path), symlinked, group/world-readable
        and malformed files all fail here — before any ``launchctl`` call or
        code/plist mutation — and ambient shell secrets alone can never
        satisfy the gate. On success the file's values are injected into the
        process environment (file values win over ambient environment) so the
        ``Config`` built by preflight sees the configuration the runtime
        would actually start with.
        """
        return preflight.load_effective_env(self.env_file)

    def _default_preflight(self) -> "preflight.PreflightReport":
        # Effective-config guard: load the private env file first so the
        # Config below validates the values the supervisor would start with,
        # not just the ambient process environment. Imports are validated by
        # the staged run in the shell wrapper against the runtime venv.
        failure = self._load_effective_env()
        if failure is not None:
            return preflight.PreflightReport([failure])
        return preflight.run_preflight(
            Config(), check_imports=False, env_file=self.env_file
        )

    def _require_preflight(self) -> None:
        """Fail closed before any launchd control or runtime-state mutation."""
        report = self._preflight_fn()
        if not report.ok:
            raise PreflightFailed(report)

    # ---- public commands ------------------------------------------------

    def install(self, stage_dir: Path) -> Dict:
        """Preflight, then atomically swap in the staged code and bootstrap.

        On any failure after the swap the previous code copy is restored and
        re-bootstrapped; data is never touched.
        """
        stage_dir = Path(stage_dir)
        if not (stage_dir / "aistat").is_dir():
            raise RuntimeInstallError(
                "stage dir {} has no aistat package".format(stage_dir)
            )
        self._require_preflight()

        plist_text = render_plist(self.paths.runtime_root, self.python,
                                  self.env_file)
        validate_plist(plist_text)

        self.paths.data.mkdir(parents=True, exist_ok=True)
        self.paths.plist.parent.mkdir(parents=True, exist_ok=True)

        had_previous_code = self.paths.code.exists()
        self._swap_code(stage_dir)
        try:
            self._write_plist(plist_text)
            self.controller.bootout(LABEL, self.paths.plist)
            self.controller.bootstrap(self.paths.plist)
            self._postflight()
        except Exception as exc:
            logger.error("install failed after swap (%s); rolling back",
                         type(exc).__name__)
            self._restore_previous(had_previous_code)
            raise
        return self.status()

    def uninstall(self, purge: bool = False) -> Dict:
        """Stop the supervisor and remove code; keep data unless ``purge``."""
        self.controller.bootout(LABEL, self.paths.plist)
        if self.paths.plist.exists():
            self.paths.plist.unlink()
        for path in (self.paths.code, self.paths.code_prev):
            if path.exists():
                shutil.rmtree(path)
        if purge and self.paths.data.exists():
            shutil.rmtree(self.paths.data)
        return {"uninstalled": True, "purged": purge,
                "data_preserved": self.paths.data.exists()}

    def rollback(self) -> Dict:
        """Preflight, then restore the previous code copy and re-bootstrap it.

        The gate covers only this public/manual entry point; the internal
        post-swap recovery in :meth:`install` calls ``_restore_previous``
        directly after an already-passed preflight and must keep restoring.
        """
        if not self.paths.code_prev.exists():
            raise RuntimeInstallError("no previous code copy to roll back to")
        self._require_preflight()
        self._restore_previous(True)
        return self.status()

    def restart(self) -> Dict:
        """Preflight the effective configuration, then kickstart/bootstrap."""
        self._require_preflight()
        if self.controller.is_loaded(LABEL):
            self.controller.kickstart(LABEL)
        else:
            self.controller.bootstrap(self.paths.plist)
        return self.status()

    def status(self) -> Dict:
        status_file = self.paths.data.parent / "run" / "supervisor.status.json"
        supervisor = None
        if status_file.exists():
            try:
                supervisor = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                supervisor = None
        return {
            "label": LABEL,
            "loaded": self.controller.is_loaded(LABEL),
            "runtime_root": str(self.paths.runtime_root),
            "code_installed": self.paths.code.exists(),
            "previous_available": self.paths.code_prev.exists(),
            "supervisor": supervisor,
        }

    # ---- internals ------------------------------------------------------

    def _swap_code(self, stage_dir: Path) -> None:
        if self.paths.code_prev.exists():
            shutil.rmtree(self.paths.code_prev)
        if self.paths.code.exists():
            os.replace(str(self.paths.code), str(self.paths.code_prev))
        shutil.move(str(stage_dir), str(self.paths.code))

    def _restore_previous(self, had_previous_code: bool) -> None:
        # Remove the half-installed code and put the previous copy back.
        if self.paths.code.exists():
            shutil.rmtree(self.paths.code)
        if had_previous_code and self.paths.code_prev.exists():
            os.replace(str(self.paths.code_prev), str(self.paths.code))
            try:
                self._write_plist(render_plist(
                    self.paths.runtime_root, self.python, self.env_file))
                self.controller.bootout(LABEL, self.paths.plist)
                self.controller.bootstrap(self.paths.plist)
            except Exception:
                logger.error("could not re-bootstrap the restored runtime")

    def _write_plist(self, text: str) -> None:
        self.paths.plist.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.plist.with_suffix(".plist.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(str(tmp), str(self.paths.plist))

    def _postflight(self) -> None:
        if not self.controller.is_loaded(LABEL):
            raise LaunchError("supervisor job did not load after bootstrap")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _default_env_file() -> Path:
    raw = os.environ.get("AISTAT_ENV_FILE")
    if raw:
        return Path(raw)
    return Path.home() / ".config" / "aistat" / "production.env"


def _build_installer(args) -> Installer:
    runtime_root = Path(
        args.runtime_root or os.environ.get("AISTAT_RUNTIME_ROOT")
        or (Path.home() / "Library" / "Application Support" / "AIStat")
    )
    return Installer(
        runtime_root,
        args.python or sys.executable,
        Path(args.env_file) if args.env_file else _default_env_file(),
        LaunchctlController(),
        plist_dir=Path(args.plist_dir) if args.plist_dir else None,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install / manage the local AIStat runtime supervisor"
    )
    parser.add_argument("command",
                        choices=["install", "uninstall", "rollback",
                                 "restart", "status", "render"])
    parser.add_argument("--stage", help="staged code directory (install)")
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--python", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--plist-dir", default=None)
    parser.add_argument("--purge", action="store_true",
                        help="uninstall: also remove persistent data")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    installer = _build_installer(args)
    try:
        if args.command == "install":
            if not args.stage:
                parser.error("install requires --stage")
            result = installer.install(Path(args.stage))
        elif args.command == "uninstall":
            result = installer.uninstall(purge=args.purge)
        elif args.command == "rollback":
            result = installer.rollback()
        elif args.command == "restart":
            result = installer.restart()
        elif args.command == "render":
            print(render_plist(installer.paths.runtime_root, installer.python,
                               installer.env_file))
            return 0
        else:
            result = installer.status()
    except PreflightFailed as exc:
        print(exc.report.render(), file=sys.stderr)
        return 2
    except RuntimeInstallError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
