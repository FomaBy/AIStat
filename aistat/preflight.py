"""Preflight validation for the trusted local AIStat runtime.

Before the installer stops the running runtime and swaps a new code copy into
place it must prove the new configuration is safe to start (acceptance
criterion: "До остановки старого runtime выполняется полный preflight").
Every check runs and is collected, so a single preflight reports every problem
instead of failing on the first. Secrets are compared and length-checked but
their values are never returned, printed or logged — only the variable name and
a pass/fail verdict leave this module.

The same report drives two callers: the installer runs it against a freshly
staged copy before touching the live runtime, and the supervisor runs it at
startup so a misconfigured runtime fails fast with a clear message instead of
crash-looping its children.
"""

import argparse
import hmac
import importlib
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config
from .endpoints import https_endpoint_error

logger = logging.getLogger("aistat.preflight")

# Long-poll network cadences that must never dip below one minute, so a
# misconfigured runtime cannot hammer the public host or the owner's Multica.
MIN_INTERVAL_SECONDS = 60

# The four contour entry modules must import cleanly (this also proves their
# third-party dependencies, e.g. ``cryptography`` for the worker store, resolve
# in the runtime interpreter).
CONTOUR_MODULES = (
    "aistat.poller",
    "aistat.publish",
    "aistat.worker_sync",
    "aistat.collector",
)

@dataclass
class Check:
    """One named preflight verdict; ``detail`` never carries a secret value."""

    name: str
    ok: bool
    detail: str


@dataclass
class PreflightReport:
    checks: List[Check]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if not c.ok]

    def render(self) -> str:
        lines = []
        for c in self.checks:
            lines.append("{} {}: {}".format("PASS" if c.ok else "FAIL",
                                            c.name, c.detail))
        lines.append("preflight {}".format("OK" if self.ok else "FAILED"))
        return "\n".join(lines)


def _is_owner_only(mode: int) -> bool:
    """True when a mode grants no group/other access (the 0o077 bits are clear)."""
    return (mode & 0o077) == 0


def _secret_ok(value: Optional[str]) -> bool:
    return bool(value) and len(value.encode("utf-8")) >= 32


def _same_secret(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def check_https_endpoint(name: str, url: Optional[str]) -> Check:
    """Validate one deployable runtime endpoint without exposing its value."""
    error = https_endpoint_error(name, url)
    if error is not None:
        return Check(name, False, error)
    return Check(name, True, "HTTPS endpoint configured")


def _check_path_permissions(config: Config) -> List[Check]:
    checks: List[Check] = []
    key_path = Path(config.worker_key_path)
    store_path = Path(config.worker_store_path)

    # The key must never share the store's directory: whoever can read the
    # store must not automatically be able to read the key that decrypts it.
    if key_path.parent.resolve() == store_path.parent.resolve():
        checks.append(Check(
            "worker_key_location", False,
            "AISTAT_WORKER_KEY_PATH must live outside the store directory",
        ))
    else:
        checks.append(Check(
            "worker_key_location", True, "key kept outside the store directory",
        ))

    key_parent = key_path.parent
    if key_parent.exists():
        mode = stat.S_IMODE(os.stat(key_parent).st_mode)
        checks.append(Check(
            "worker_key_dir_perms", _is_owner_only(mode),
            "key directory mode {:o} (must be 0700, owner-only)".format(mode),
        ))
    if key_path.exists():
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        checks.append(Check(
            "worker_key_perms", _is_owner_only(mode),
            "key file mode {:o} (must be 0600, owner-only)".format(mode),
        ))
    if store_path.exists():
        mode = stat.S_IMODE(os.stat(store_path).st_mode)
        checks.append(Check(
            "worker_store_perms", _is_owner_only(mode),
            "store file mode {:o} (must be 0600, owner-only)".format(mode),
        ))
    return checks


def check_env_file(env_file: Path) -> Check:
    """The persistent private env file must be an owner-only regular file.

    The launchd plist intentionally carries no secret values, so the installed
    supervisor can only reload its configuration from this file. A missing
    file therefore fails: secrets exported only in the invoking shell would
    not survive to the persistent runtime (criteria 2/4/6). Rejecting a
    world/group-readable or symlinked secrets file keeps the ingest/session/
    worker secrets out of reach of other local accounts (criterion 6).
    """
    env_file = Path(env_file)
    try:
        info = os.lstat(env_file)
    except OSError:
        return Check(
            "env_file", False,
            "private env file does not exist "
            "(a persistent owner-only 0600 file is required)")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return Check("env_file", False, "env file must be a regular file "
                                        "(not a symlink)")
    mode = stat.S_IMODE(info.st_mode)
    if not _is_owner_only(mode):
        return Check("env_file", False,
                     "env file mode {:o} (must be 0600, owner-only)".format(mode))
    return Check("env_file", True, "private env file is owner-only")


_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def load_env_file(path: Path) -> Dict[str, str]:
    """Parse an owner-only ``KEY=VALUE`` env file and inject it into os.environ.

    The private env file is the only place the runtime's secrets live; the
    plist never carries them. Its permissions are validated by the caller
    (see :func:`check_env_file`) before this runs. Values are injected into
    the process environment so ``Config`` and every child pick them up, but
    are never echoed. The file is parsed, never executed by a shell.

    Raises ``ValueError`` for a malformed line; the message names only the
    line number, never its content, so a mistyped secret cannot leak.
    """
    values: Dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise ValueError(
                "line {} is not a KEY=VALUE assignment".format(lineno))
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not _ENV_KEY_RE.match(key):
            raise ValueError(
                "line {} has an invalid variable name".format(lineno))
        if (len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"')):
            val = val[1:-1]
        os.environ[key] = val
        values[key] = val
    return values


def load_effective_env(env_file: Path) -> Optional[Check]:
    """Validate the persistent env file, then load it into the process env.

    Shared by the supervisor, the installer and the preflight CLI so all
    three apply identical semantics: the file must exist as an owner-only
    regular file, must parse as KEY=VALUE, and its values win over the
    ambient environment. Returns the failing :class:`Check`, or ``None``
    once the effective values are loaded.
    """
    verdict = check_env_file(env_file)
    if not verdict.ok:
        return verdict
    try:
        load_env_file(env_file)
    except ValueError as exc:
        return Check("env_file", False, "env file is malformed: {}".format(exc))
    except OSError as exc:
        return Check("env_file", False,
                     "could not read env file: {}".format(type(exc).__name__))
    return None


def _check_imports() -> List[Check]:
    checks: List[Check] = []
    for module in CONTOUR_MODULES:
        try:
            importlib.import_module(module)
            checks.append(Check("import:" + module, True, "imports cleanly"))
        except Exception as exc:  # pragma: no cover - defensive
            checks.append(Check("import:" + module, False,
                                "import failed: {}".format(type(exc).__name__)))
    try:
        importlib.import_module("cryptography.fernet")
        checks.append(Check("dependency:cryptography", True, "available"))
    except Exception as exc:  # pragma: no cover - defensive
        checks.append(Check("dependency:cryptography", False,
                            "missing: {}".format(type(exc).__name__)))
    return checks


def run_preflight(config: Config, *, check_imports: bool = True,
                  env_file: Optional[Path] = None) -> PreflightReport:
    """Validate a runtime configuration and return every verdict.

    ``check_imports`` is on by default (proves the contour modules and their
    dependencies load); tests that only exercise config validation turn it off.
    ``env_file`` is validated only when given (the supervisor passes its
    AISTAT_ENV_FILE so an unsafe secrets file is caught before startup).
    """
    checks: List[Check] = []

    # Owner publisher: tenant identity is required and its snapshot secret must
    # be present and long enough to be a real HMAC key.
    checks.append(Check(
        "tenant_id", config.publish_tenant_id is not None,
        "AISTAT_TENANT_ID configured" if config.publish_tenant_id is not None
        else "AISTAT_TENANT_ID is required",
    ))
    checks.append(check_https_endpoint(
        "AISTAT_PUBLISH_URL", config.publish_url
    ))
    checks.append(Check(
        "ingest_secret", _secret_ok(config.ingest_secret),
        "AISTAT_INGEST_SECRET present" if _secret_ok(config.ingest_secret)
        else "AISTAT_INGEST_SECRET must contain at least 32 bytes",
    ))

    # PAT worker: its pull endpoint and independent secret.
    checks.append(check_https_endpoint(
        "AISTAT_WORKER_SYNC_URL", config.worker_sync_url
    ))
    checks.append(Check(
        "worker_secret", _secret_ok(config.worker_secret),
        "AISTAT_WORKER_SECRET present" if _secret_ok(config.worker_secret)
        else "AISTAT_WORKER_SECRET must contain at least 32 bytes",
    ))

    # The runtime needs the hosted session key as well: without its real value
    # it cannot prove that the two transport keys are independent from it.
    checks.append(Check(
        "session_secret", _secret_ok(config.session_secret),
        "AISTAT_SESSION_SECRET present" if _secret_ok(config.session_secret)
        else "AISTAT_SESSION_SECRET must contain at least 32 bytes",
    ))

    # The three HMAC keys must be mutually independent so compromising one
    # transport cannot forge another.
    independent = not (
        _same_secret(config.ingest_secret, config.worker_secret)
        or _same_secret(config.ingest_secret, config.session_secret)
        or _same_secret(config.worker_secret, config.session_secret)
    )
    checks.append(Check(
        "secret_independence", independent,
        "ingest/worker/session secrets are independent" if independent
        else "ingest, worker and session secrets must all differ",
    ))

    # Host-facing cadences must stay at or above one minute.
    for name, value in (
        ("AISTAT_PUBLISH_INTERVAL_SECONDS", config.publish_interval_seconds),
        ("AISTAT_WORKER_PULL_INTERVAL_SECONDS", config.worker_pull_interval_seconds),
        ("AISTAT_WORKER_COLLECT_INTERVAL_SECONDS",
         config.worker_collect_interval_seconds),
    ):
        checks.append(Check(
            name.lower(), value >= MIN_INTERVAL_SECONDS,
            "{}s (>= {}s)".format(value, MIN_INTERVAL_SECONDS) if value >= MIN_INTERVAL_SECONDS
            else "{}s is below the {}s floor".format(value, MIN_INTERVAL_SECONDS),
        ))

    checks.extend(_check_path_permissions(config))

    if env_file is not None:
        checks.append(check_env_file(env_file))

    if check_imports:
        checks.extend(_check_imports())

    return PreflightReport(checks)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the local AIStat runtime configuration"
    )
    parser.add_argument("--no-imports", action="store_true",
                        help="skip contour import/dependency checks")
    parser.add_argument("--env-file", default=os.environ.get("AISTAT_ENV_FILE"),
                        help="persistent private env file to validate and load")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env_file = Path(args.env_file) if args.env_file else None
    if env_file is not None:
        # Load the file first (file values win over the ambient shell) so the
        # Config below validates what the persistent runtime would start with.
        failure = load_effective_env(env_file)
        if failure is not None:
            report = PreflightReport([failure])
            print(report.render())
            return 1
    report = run_preflight(
        Config(),
        check_imports=not args.no_imports,
        env_file=env_file,
    )
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
