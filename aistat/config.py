"""Application configuration.

Every knob has an environment-variable override so the poller and server
can be tuned without code changes. Defaults are chosen for this workspace.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return Path(raw)


def _env_csv(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


@dataclass
class Config:
    # Path to the SQLite database file.
    db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("AISTAT_DB_PATH", str(PROJECT_ROOT / "data" / "aistat.db"))
        )
    )
    # Start-to-start cadence of local Multica polling.
    poll_interval_seconds: int = field(
        default_factory=lambda: _env_int("AISTAT_POLL_INTERVAL_SECONDS", 45)
    )
    # Window passed to `multica runtime usage --days N` (server caps at 365).
    usage_days: int = field(default_factory=lambda: _env_int("AISTAT_USAGE_DAYS", 90))
    # Max issues whose per-issue details (usage + runs) are fetched per cycle.
    # Issues are prioritised by most recent `updated_at`, so active work is
    # refreshed first and the legacy archive drains in the background.
    detail_budget: int = field(default_factory=lambda: _env_int("AISTAT_DETAIL_BUDGET", 40))
    # Page size for `multica issue list`.
    issue_page_limit: int = field(default_factory=lambda: _env_int("AISTAT_ISSUE_PAGE_LIMIT", 100))
    # The multica CLI binary and per-call timeout.
    cli_bin: str = field(default_factory=lambda: os.environ.get("AISTAT_CLI_BIN", "multica"))
    cli_timeout_seconds: int = field(
        default_factory=lambda: _env_int("AISTAT_CLI_TIMEOUT_SECONDS", 120)
    )
    # Versioned pricing table (model -> official per-1M-token rates + source).
    pricing_path: Path = field(
        default_factory=lambda: _env_path("AISTAT_PRICING_PATH", PROJECT_ROOT / "pricing.json")
    )
    # Optional second pricing file that overrides / extends the base one,
    # letting an operator add or re-rate models without editing pricing.json.
    pricing_overrides_path: Optional[Path] = field(
        default_factory=lambda: _env_path("AISTAT_PRICING_OVERRIDES", None)
    )
    # Credits per USD of cost. Multica publishes no official public credit
    # model, so this is a documented, configurable assumption (default 1.0 =>
    # 1 credit == $1). Override with AISTAT_CREDITS_PER_USD.
    credits_per_usd: float = field(
        default_factory=lambda: _env_float("AISTAT_CREDITS_PER_USD", 1.0)
    )
    # Public WSGI authentication. Secrets are environment-only and must never
    # be committed. The cPanel application validates them on startup.
    auth_username: str = field(
        default_factory=lambda: os.environ.get("AISTAT_ADMIN_USERNAME", "admin")
    )
    auth_password_hash: Optional[str] = field(
        default_factory=lambda: os.environ.get("AISTAT_PASSWORD_HASH") or None
    )
    session_secret: Optional[str] = field(
        default_factory=lambda: os.environ.get("AISTAT_SESSION_SECRET") or None
    )
    session_hours: int = field(
        default_factory=lambda: _env_int("AISTAT_SESSION_HOURS", 12)
    )
    session_cookie_secure: bool = field(
        default_factory=lambda: _env_bool(
            "AISTAT_SESSION_COOKIE_SECURE",
            _env_bool("AISTAT_FORCE_HTTPS", False),
        )
    )
    security_db_path: Path = field(
        default_factory=lambda: _env_path(
            "AISTAT_SECURITY_DB_PATH", PROJECT_ROOT / "data" / "security.db"
        )
    )
    allowed_hosts: Tuple[str, ...] = field(
        default_factory=lambda: _env_csv(
            "AISTAT_ALLOWED_HOSTS", ("localhost", "127.0.0.1", "testserver")
        )
    )
    force_https: bool = field(
        default_factory=lambda: _env_bool("AISTAT_FORCE_HTTPS", False)
    )
    # The hosted app receives only signed SQLite snapshots. The Multica CLI
    # token remains on the trusted local machine.
    ingest_secret: Optional[str] = field(
        default_factory=lambda: os.environ.get("AISTAT_INGEST_SECRET") or None
    )
    ingest_max_age_seconds: int = field(
        default_factory=lambda: _env_int("AISTAT_INGEST_MAX_AGE_SECONDS", 300)
    )
    max_snapshot_bytes: int = field(
        default_factory=lambda: _env_int(
            "AISTAT_MAX_SNAPSHOT_BYTES", 64 * 1024 * 1024
        )
    )
    # Optional local publisher. When configured, run.sh starts it alongside
    # the Multica poller and sends a fresh snapshot at most every five minutes.
    publish_url: Optional[str] = field(
        default_factory=lambda: os.environ.get("AISTAT_PUBLISH_URL") or None
    )
    publish_interval_seconds: int = field(
        default_factory=lambda: _env_int("AISTAT_PUBLISH_INTERVAL_SECONDS", 300)
    )
    publish_timeout_seconds: int = field(
        default_factory=lambda: _env_int("AISTAT_PUBLISH_TIMEOUT_SECONDS", 60)
    )
    allow_insecure_publish: bool = field(
        default_factory=lambda: _env_bool("AISTAT_ALLOW_INSECURE_PUBLISH", False)
    )

    def ensure_db_dir(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def ensure_security_db_dir(self) -> None:
        self.security_db_path.parent.mkdir(parents=True, exist_ok=True)
