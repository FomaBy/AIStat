"""Application configuration.

Every knob has an environment-variable override so the poller and server
can be tuned without code changes. Defaults are chosen for this workspace.
"""

import os
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

from . import handoff, oauth
from .tenant import canonical_tenant_id, tenant_db_path

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


def _env_optional_int(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return canonical_tenant_id(raw)


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


# Sentinel distinguishing "argument not supplied" (use the env-driven default)
# from an explicit ``None``. This reproduces ``dataclasses.field(default_factory)``
# semantics without importing ``dataclasses``.
_UNSET = object()


class Config:
    """Runtime configuration.

    Formerly a ``@dataclass``; rewritten as a plain class with an explicit
    ``__init__`` so the whole ``aistat.backup`` import chain stays importable on
    the production host's Python 3.6.8, which ships no ``dataclasses`` module
    (FAN-1435). Every field keeps its per-instance, environment-driven default
    and its keyword-argument override, so ``Config()`` and ``Config(db_path=...)``
    behave exactly as they did under the dataclass.
    """

    def __init__(
        self,
        db_path=_UNSET,
        poll_interval_seconds=_UNSET,
        usage_days=_UNSET,
        detail_budget=_UNSET,
        issue_page_limit=_UNSET,
        cli_bin=_UNSET,
        cli_timeout_seconds=_UNSET,
        pricing_path=_UNSET,
        pricing_overrides_path=_UNSET,
        credits_per_usd=_UNSET,
        auth_username=_UNSET,
        admin_email=_UNSET,
        auth_password_hash=_UNSET,
        session_secret=_UNSET,
        session_hours=_UNSET,
        session_cookie_secure=_UNSET,
        security_db_path=_UNSET,
        tenants_dir=_UNSET,
        allowed_hosts=_UNSET,
        oauth_providers=_UNSET,
        oauth_allowed_emails=_UNSET,
        force_https=_UNSET,
        ingest_secret=_UNSET,
        ingest_max_age_seconds=_UNSET,
        max_snapshot_bytes=_UNSET,
        publish_url=_UNSET,
        publish_interval_seconds=_UNSET,
        publish_timeout_seconds=_UNSET,
        publish_tenant_id=_UNSET,
        worker_secret=_UNSET,
        worker_sync_url=_UNSET,
        worker_pull_interval_seconds=_UNSET,
        worker_store_path=_UNSET,
        worker_key_path=_UNSET,
        multica_connect_enabled=_UNSET,
        multica_official_url=_UNSET,
        connection_pending_ttl_seconds=_UNSET,
        worker_readiness_ttl_seconds=_UNSET,
        cli_profiles_dir=_UNSET,
        worker_tenants_dir=_UNSET,
        worker_collect_interval_seconds=_UNSET,
        backup_dir=_UNSET,
        backup_retention=_UNSET,
    ):
        # Path to the SQLite database file.
        self.db_path = (
            Path(os.environ.get("AISTAT_DB_PATH", str(PROJECT_ROOT / "data" / "aistat.db")))
            if db_path is _UNSET
            else db_path
        )
        # Start-to-start cadence of local Multica polling.
        self.poll_interval_seconds = (
            _env_int("AISTAT_POLL_INTERVAL_SECONDS", 45)
            if poll_interval_seconds is _UNSET
            else poll_interval_seconds
        )
        # Window passed to `multica runtime usage --days N` (server caps at 365).
        self.usage_days = (
            _env_int("AISTAT_USAGE_DAYS", 90) if usage_days is _UNSET else usage_days
        )
        # Max issues whose per-issue details (usage + runs) are fetched per cycle.
        # Issues are prioritised by most recent `updated_at`, so active work is
        # refreshed first and the legacy archive drains in the background.
        self.detail_budget = (
            _env_int("AISTAT_DETAIL_BUDGET", 40)
            if detail_budget is _UNSET
            else detail_budget
        )
        # Page size for `multica issue list`.
        self.issue_page_limit = (
            _env_int("AISTAT_ISSUE_PAGE_LIMIT", 100)
            if issue_page_limit is _UNSET
            else issue_page_limit
        )
        # The multica CLI binary and per-call timeout.
        self.cli_bin = (
            os.environ.get("AISTAT_CLI_BIN", "multica") if cli_bin is _UNSET else cli_bin
        )
        self.cli_timeout_seconds = (
            _env_int("AISTAT_CLI_TIMEOUT_SECONDS", 120)
            if cli_timeout_seconds is _UNSET
            else cli_timeout_seconds
        )
        # Versioned pricing table (model -> official per-1M-token rates + source).
        self.pricing_path = (
            _env_path("AISTAT_PRICING_PATH", PROJECT_ROOT / "pricing.json")
            if pricing_path is _UNSET
            else pricing_path
        )
        # Optional second pricing file that overrides / extends the base one,
        # letting an operator add or re-rate models without editing pricing.json.
        self.pricing_overrides_path = (
            _env_path("AISTAT_PRICING_OVERRIDES", None)
            if pricing_overrides_path is _UNSET
            else pricing_overrides_path
        )
        # Credits per USD of cost. Multica publishes no official public credit
        # model, so this is a documented, configurable assumption (default 1.0 =>
        # 1 credit == $1). Override with AISTAT_CREDITS_PER_USD.
        self.credits_per_usd = (
            _env_float("AISTAT_CREDITS_PER_USD", 1.0)
            if credits_per_usd is _UNSET
            else credits_per_usd
        )
        # Public WSGI authentication. Secrets are environment-only and must never
        # be committed. The cPanel application validates them on startup.
        self.auth_username = (
            os.environ.get("AISTAT_ADMIN_USERNAME", "admin")
            if auth_username is _UNSET
            else auth_username
        )
        self.admin_email = (
            (os.environ.get("AISTAT_ADMIN_EMAIL") or None)
            if admin_email is _UNSET
            else admin_email
        )
        self.auth_password_hash = (
            (os.environ.get("AISTAT_PASSWORD_HASH") or None)
            if auth_password_hash is _UNSET
            else auth_password_hash
        )
        self.session_secret = (
            (os.environ.get("AISTAT_SESSION_SECRET") or None)
            if session_secret is _UNSET
            else session_secret
        )
        self.session_hours = (
            _env_int("AISTAT_SESSION_HOURS", 12)
            if session_hours is _UNSET
            else session_hours
        )
        self.session_cookie_secure = (
            _env_bool(
                "AISTAT_SESSION_COOKIE_SECURE",
                _env_bool("AISTAT_FORCE_HTTPS", False),
            )
            if session_cookie_secure is _UNSET
            else session_cookie_secure
        )
        self.security_db_path = (
            _env_path("AISTAT_SECURITY_DB_PATH", PROJECT_ROOT / "data" / "security.db")
            if security_db_path is _UNSET
            else security_db_path
        )
        self.tenants_dir = (
            _env_path("AISTAT_TENANTS_DIR", PROJECT_ROOT / "data" / "tenants")
            if tenants_dir is _UNSET
            else tenants_dir
        )
        self.allowed_hosts = (
            _env_csv("AISTAT_ALLOWED_HOSTS", ("localhost", "127.0.0.1", "testserver"))
            if allowed_hosts is _UNSET
            else allowed_hosts
        )
        # Enabled OAuth providers, keyed by name (built from the generic
        # AISTAT_OAUTH_* env schema; empty unless an operator configures one).
        self.oauth_providers = (
            oauth.providers_from_env(os.environ)
            if oauth_providers is _UNSET
            else oauth_providers
        )
        # Emails allowed to reach private statistics after an OAuth login.
        # Fail-closed: empty means a valid OAuth login grants no data access.
        self.oauth_allowed_emails = (
            oauth.allowed_emails_from_env(os.environ)
            if oauth_allowed_emails is _UNSET
            else oauth_allowed_emails
        )
        self.force_https = (
            _env_bool("AISTAT_FORCE_HTTPS", False)
            if force_https is _UNSET
            else force_https
        )
        # The hosted app receives only signed SQLite snapshots. The Multica CLI
        # token remains on the trusted local machine.
        self.ingest_secret = (
            (os.environ.get("AISTAT_INGEST_SECRET") or None)
            if ingest_secret is _UNSET
            else ingest_secret
        )
        self.ingest_max_age_seconds = (
            _env_int("AISTAT_INGEST_MAX_AGE_SECONDS", 300)
            if ingest_max_age_seconds is _UNSET
            else ingest_max_age_seconds
        )
        self.max_snapshot_bytes = (
            _env_int("AISTAT_MAX_SNAPSHOT_BYTES", 64 * 1024 * 1024)
            if max_snapshot_bytes is _UNSET
            else max_snapshot_bytes
        )
        # Optional local publisher. When configured, run.sh starts it alongside
        # the Multica poller and sends a fresh snapshot at most every five minutes.
        self.publish_url = (
            (os.environ.get("AISTAT_PUBLISH_URL") or None)
            if publish_url is _UNSET
            else publish_url
        )
        self.publish_interval_seconds = (
            _env_int("AISTAT_PUBLISH_INTERVAL_SECONDS", 300)
            if publish_interval_seconds is _UNSET
            else publish_interval_seconds
        )
        self.publish_timeout_seconds = (
            _env_int("AISTAT_PUBLISH_TIMEOUT_SECONDS", 60)
            if publish_timeout_seconds is _UNSET
            else publish_timeout_seconds
        )
        self.publish_tenant_id = (
            _env_optional_int("AISTAT_TENANT_ID")
            if publish_tenant_id is _UNSET
            else publish_tenant_id
        )
        # "Connect your Multica" secure token handoff. The host accepts a user
        # token only while this independent worker-channel secret is configured;
        # the same secret authenticates the trusted local worker's pull channel.
        self.worker_secret = (
            (os.environ.get("AISTAT_WORKER_SECRET") or None)
            if worker_secret is _UNSET
            else worker_secret
        )
        # Trusted local worker only: base URL of the public host whose worker
        # endpoints this machine pulls (e.g. https://aistat.app).
        self.worker_sync_url = (
            (os.environ.get("AISTAT_WORKER_SYNC_URL") or None)
            if worker_sync_url is _UNSET
            else worker_sync_url
        )
        self.worker_pull_interval_seconds = (
            _env_int("AISTAT_WORKER_PULL_INTERVAL_SECONDS", 300)
            if worker_pull_interval_seconds is _UNSET
            else worker_pull_interval_seconds
        )
        # Encrypted worker token store and its key. The key must live outside the
        # store's directory; neither ships in the cPanel package.
        self.worker_store_path = (
            _env_path(
                "AISTAT_WORKER_STORE_PATH",
                PROJECT_ROOT / "data" / "worker_connections.db",
            )
            if worker_store_path is _UNSET
            else worker_store_path
        )
        self.worker_key_path = (
            _env_path(
                "AISTAT_WORKER_KEY_PATH",
                Path.home() / ".config" / "aistat" / "worker.key",
            )
            if worker_key_path is _UNSET
            else worker_key_path
        )
        # Fail-closed master switch for the whole "connect your Multica" feature.
        # Without an explicit truthy value both the token intake and the worker
        # pull/ack channel are unavailable, so the feature ships dormant.
        self.multica_connect_enabled = (
            _env_bool("AISTAT_MULTICA_CONNECT_ENABLED", False)
            if multica_connect_enabled is _UNSET
            else multica_connect_enabled
        )
        # The single official Multica host every connection is pinned to. A
        # user-supplied server URL is never published; only this exact host is
        # ever stored, so a poisoned server_url cannot redirect the PAT.
        self.multica_official_url = (
            (
                os.environ.get("AISTAT_MULTICA_OFFICIAL_URL")
                or handoff.OFFICIAL_MULTICA_URL
            )
            if multica_official_url is _UNSET
            else multica_official_url
        )
        # Plaintext pending-token lifetime on the host (default 10 min, hard cap
        # 15 min). After it the host physically erases the token.
        self.connection_pending_ttl_seconds = (
            max(
                1,
                min(
                    _env_int(
                        "AISTAT_CONNECTION_PENDING_TTL_SECONDS",
                        handoff.PENDING_TTL_DEFAULT_SECONDS,
                    ),
                    handoff.PENDING_TTL_MAX_SECONDS,
                ),
            )
            if connection_pending_ttl_seconds is _UNSET
            else connection_pending_ttl_seconds
        )
        # How fresh the worker's last authenticated pull must be for intake to save
        # a token; otherwise intake fails closed with 503 and stores nothing.
        self.worker_readiness_ttl_seconds = (
            _env_int(
                "AISTAT_WORKER_READINESS_TTL_SECONDS",
                handoff.WORKER_READINESS_TTL_DEFAULT_SECONDS,
            )
            if worker_readiness_ttl_seconds is _UNSET
            else worker_readiness_ttl_seconds
        )
        # Task-owned HOME root for the per-connection official CLI. Every
        # connection logs in under its own ``--profile aistat-conn-<id>`` inside
        # this directory, so no per-user token ever touches the owner's real
        # ``~/.multica`` and the ambient MULTICA_* identity is scrubbed away.
        self.cli_profiles_dir = (
            _env_path("AISTAT_CLI_PROFILES_DIR", PROJECT_ROOT / "data" / "cli_profiles")
            if cli_profiles_dir is _UNSET
            else cli_profiles_dir
        )
        # Where the per-connection worker stages each user's freshly polled tenant
        # database before it is published. Distinct from ``tenants_dir`` (the
        # public host's install target) so a worker and host on one machine in
        # tests never collide.
        self.worker_tenants_dir = (
            _env_path("AISTAT_WORKER_TENANTS_DIR", PROJECT_ROOT / "data" / "worker_tenants")
            if worker_tenants_dir is _UNSET
            else worker_tenants_dir
        )
        # Start-to-start cadence of the per-connection collection loop.
        self.worker_collect_interval_seconds = (
            _env_int("AISTAT_WORKER_COLLECT_INTERVAL_SECONDS", 300)
            if worker_collect_interval_seconds is _UNSET
            else worker_collect_interval_seconds
        )
        # Local, at-rest backups of the durable SQLite stores (FAN-1185). Each run
        # of ``python -m aistat.backup create`` writes one integrity-checked,
        # compressed generation here; older generations beyond the retention count
        # are pruned. Lives under ``data/`` so it is gitignored and never shipped.
        self.backup_dir = (
            _env_path("AISTAT_BACKUP_DIR", PROJECT_ROOT / "data" / "backups")
            if backup_dir is _UNSET
            else backup_dir
        )
        # How many backup generations to keep (oldest pruned first). At least one.
        self.backup_retention = (
            max(1, _env_int("AISTAT_BACKUP_RETENTION", 14))
            if backup_retention is _UNSET
            else backup_retention
        )

    def ensure_db_dir(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def ensure_security_db_dir(self) -> None:
        self.security_db_path.parent.mkdir(parents=True, exist_ok=True)

    def ensure_tenants_dir(self) -> None:
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.tenants_dir.chmod(0o700)
        except OSError:
            pass

    def ensure_cli_profiles_dir(self) -> None:
        self.cli_profiles_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.cli_profiles_dir.chmod(0o700)
        except OSError:
            pass

    def ensure_worker_tenants_dir(self) -> None:
        self.worker_tenants_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.worker_tenants_dir.chmod(0o700)
        except OSError:
            pass

    def ensure_backup_dir(self) -> None:
        # Backups may contain the accounts and encrypted-token stores, so keep
        # the directory owner-only just like the tenants directory.
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.backup_dir.chmod(0o700)
        except OSError:
            pass

    def tenant_db_path(self, user_id: int) -> Path:
        """Resolve a tenant path only from a trusted internal numeric user id."""
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("tenant path requires an internal integer user id")
        return Path(tenant_db_path(self.tenants_dir, user_id))

    def worker_tenant_db_path(self, user_id: int) -> Path:
        """Worker-local staging path for one tenant's freshly polled data."""
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("tenant path requires an internal integer user id")
        return Path(tenant_db_path(self.worker_tenants_dir, user_id))
