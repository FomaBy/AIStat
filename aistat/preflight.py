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
import ipaddress
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from .config import Config

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

_HOST_LABEL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
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


def _hostname_ok(hostname: Optional[str]) -> bool:
    """Validate an IP literal or DNS/IDNA hostname without resolving it."""
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass

    # A trailing dot is valid for a fully-qualified DNS name, but it is not a
    # label.  IDNA conversion rejects malformed Unicode before the ASCII label
    # grammar and total-length limits are applied.
    dns_name = hostname[:-1] if hostname.endswith(".") else hostname
    if not dns_name:
        return False
    try:
        ascii_name = dns_name.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return (
        len(ascii_name) <= 253
        and all(_HOST_LABEL_RE.match(label) for label in ascii_name.split("."))
    )


def _check_https(name: str, url: Optional[str], allow_insecure: bool) -> Check:
    if not isinstance(url, str) or not url:
        return Check(name, False, "{} is not configured".format(name))

    malformed = Check(
        name,
        False,
        "must be an absolute URL with a valid host and port",
    )
    if "\\" in url or any(
        ch.isspace() or ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f
        for ch in url
    ):
        return malformed
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        return malformed

    # urllib.parse is a component splitter, not a validator.  In particular,
    # it accepts empty authorities, userinfo and an empty port delimiter.  The
    # runtime endpoints must be network locations, never credential-bearing
    # URLs; a configured port must be usable as a destination port.
    authority = parsed.netloc
    invalid_bracket_suffix = False
    if authority.startswith("["):
        closing = authority.find("]")
        suffix = authority[closing + 1:] if closing >= 0 else ""
        invalid_bracket_suffix = (
            closing < 0
            or (suffix and not suffix.startswith(":"))
            or suffix == ":"
        )
    if (
        not parsed.netloc
        or "@" in parsed.netloc
        or parsed.netloc.endswith(":")
        or invalid_bracket_suffix
        or not _hostname_ok(hostname)
        or port == 0
    ):
        return malformed

    scheme = parsed.scheme.lower()
    if scheme == "https":
        return Check(name, True, "HTTPS endpoint configured")
    if allow_insecure and scheme == "http":
        return Check(name, True, "HTTP endpoint allowed (insecure test mode)")
    return Check(name, False, "must use HTTPS")


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
    """A private env file, if present, must be an owner-only regular file.

    Rejecting a world/group-readable secrets file keeps the ingest/session/
    worker secrets out of reach of other local accounts (criterion 6).
    """
    env_file = Path(env_file)
    if not env_file.exists():
        return Check("env_file", True, "no private env file (using process env)")
    info = os.lstat(env_file)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return Check("env_file", False, "env file must be a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if not _is_owner_only(mode):
        return Check("env_file", False,
                     "env file mode {:o} (must be 0600, owner-only)".format(mode))
    return Check("env_file", True, "private env file is owner-only")


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
    checks.append(_check_https("AISTAT_PUBLISH_URL", config.publish_url,
                               config.allow_insecure_publish))
    checks.append(Check(
        "ingest_secret", _secret_ok(config.ingest_secret),
        "AISTAT_INGEST_SECRET present" if _secret_ok(config.ingest_secret)
        else "AISTAT_INGEST_SECRET must contain at least 32 bytes",
    ))

    # PAT worker: its pull endpoint and independent secret.
    checks.append(_check_https("AISTAT_WORKER_SYNC_URL", config.worker_sync_url,
                               config.allow_insecure_publish))
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
                        help="private env file to permission-check")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = run_preflight(
        Config(),
        check_imports=not args.no_imports,
        env_file=Path(args.env_file) if args.env_file else None,
    )
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
