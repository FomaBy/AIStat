"""Application configuration.

Every knob has an environment-variable override so the poller and server
can be tuned without code changes. Defaults are chosen for this workspace.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass
class Config:
    # Path to the SQLite database file.
    db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("AISTAT_DB_PATH", str(PROJECT_ROOT / "data" / "aistat.db"))
        )
    )
    # Seconds to sleep between the end of one poll cycle and the start of the next.
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

    def ensure_db_dir(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
