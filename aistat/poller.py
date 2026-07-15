"""Background poller: full incremental sync cycle over the multica CLI.

One cycle:
  1. runtime list                          -> runtimes
  2. agent list                            -> agents
  3. project list                          -> projects
  4. runtime usage <id> --days N (each)    -> daily_usage
  5. runtime activity <id> (each)          -> runtime_activity
  6. issue list --project <id> (paginated) -> issues (with story_points)
  7. issue usage + issue runs, for up to `detail_budget` issues whose
     `updated_at` changed since their details were last fetched, most
     recently updated first                -> issue_usage, runs
  8. agent tasks <id> (each)               -> runs

Every source failure is logged, recorded in sync_state and reflected in
health; it never aborts the remaining sources and is never replaced with
empty/zero data.
"""

import argparse
import functools
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import normalize, pricing, store
from .cli import CliError, run_cli
from .config import Config
from .db import connect, init_db, utcnow_iso

logger = logging.getLogger("aistat.poller")

# runner(args: List[str]) -> parsed JSON. Injectable for tests.
Runner = Callable[[List[str]], Any]


@dataclass
class CycleResult:
    started_at: str = ""
    finished_at: str = ""
    sources_ok: int = 0
    sources_failed: int = 0
    errors: List[str] = field(default_factory=list)
    detail_synced: int = 0
    detail_failed: int = 0

    @property
    def ok(self) -> bool:
        return self.sources_failed == 0


class Poller:
    def __init__(self, config: Config, conn: sqlite3.Connection,
                 runner: Optional[Runner] = None):
        self.config = config
        self.conn = conn
        self.runner: Runner = runner or functools.partial(
            run_cli, binary=config.cli_bin, timeout=config.cli_timeout_seconds
        )

    # -- helpers ------------------------------------------------------------

    def _source(self, result: CycleResult, name: str,
                fn: Callable[[], Any]) -> Tuple[bool, Any]:
        """Run one source, record its outcome, keep the cycle going on error."""
        try:
            value = fn()
        except (CliError, normalize.NormalizationError) as exc:
            message = str(exc)
            logger.error("source %s failed: %s", name, message)
            store.record_source_attempt(self.conn, name, ok=False, error=message)
            self.conn.commit()
            result.sources_failed += 1
            result.errors.append(f"{name}: {message}")
            return False, None
        store.record_source_attempt(self.conn, name, ok=True)
        self.conn.commit()
        result.sources_ok += 1
        return True, value

    # -- individual sources ---------------------------------------------------

    def sync_runtimes(self) -> List[Dict[str, Any]]:
        data = self.runner(["runtime", "list"])
        rows = [normalize.normalize_runtime(item) for item in data]
        store.upsert_runtimes(self.conn, rows)
        return rows

    def sync_agents(self) -> List[Dict[str, Any]]:
        data = self.runner(["agent", "list"])
        rows = [normalize.normalize_agent(item) for item in data]
        store.upsert_agents(self.conn, rows)
        return rows

    def sync_projects(self) -> List[Dict[str, Any]]:
        data = self.runner(["project", "list"])
        rows = [normalize.normalize_project(item) for item in data]
        store.upsert_projects(self.conn, rows)
        return rows

    def sync_runtime_usage(self, runtime_id: str) -> int:
        data = self.runner(
            ["runtime", "usage", runtime_id, "--days", str(self.config.usage_days)]
        )
        rows = [normalize.normalize_daily_usage(item) for item in data]
        return store.upsert_daily_usage(self.conn, rows)

    def sync_runtime_activity(self, runtime_id: str) -> int:
        data = self.runner(["runtime", "activity", runtime_id])
        rows = normalize.normalize_activity(runtime_id, data)
        return store.replace_runtime_activity(self.conn, runtime_id, rows)

    def sync_project_issues(self, project_id: str) -> int:
        """Paginate through all issues of a project and upsert them."""
        limit = self.config.issue_page_limit
        offset = 0
        total = 0
        while True:
            data = self.runner(
                ["issue", "list", "--project", project_id,
                 "--limit", str(limit), "--offset", str(offset)]
            )
            issues = data.get("issues") or []
            rows = [normalize.normalize_issue(item) for item in issues]
            total += store.upsert_issues(self.conn, rows)
            if not data.get("has_more") or not issues:
                break
            offset += limit
        return total

    def pending_detail_issues(self, budget: int) -> List[sqlite3.Row]:
        """Issues whose usage/runs details are missing or stale, newest first."""
        return self.conn.execute(
            """
            SELECT id, identifier, updated_at FROM issues
            WHERE details_synced_for IS NULL OR details_synced_for != updated_at
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (budget,),
        ).fetchall()

    def sync_issue_details(self, result: CycleResult,
                           budget: Optional[int] = None) -> None:
        """Fetch `issue usage` + `issue runs` for up to `budget` stale issues.

        Reported as a single 'issue_details' source so sync_state stays
        readable; per-issue failures are collected, do not stop the batch,
        and leave that issue marked stale for retry next cycle.
        """
        budget = self.config.detail_budget if budget is None else budget
        pending = self.pending_detail_issues(budget)
        errors: List[str] = []
        for issue in pending:
            issue_id, updated_at = issue["id"], issue["updated_at"]
            try:
                usage = self.runner(["issue", "usage", issue_id])
                runs = self.runner(["issue", "runs", issue_id])
                store.upsert_issue_usage(
                    self.conn, normalize.normalize_issue_usage(issue_id, usage)
                )
                store.upsert_runs(
                    self.conn, [normalize.normalize_run(item) for item in runs]
                )
                store.mark_issue_details_synced(self.conn, issue_id, updated_at)
                self.conn.commit()
                result.detail_synced += 1
            except (CliError, normalize.NormalizationError) as exc:
                errors.append(f"{issue['identifier'] or issue_id}: {exc}")
                result.detail_failed += 1
                logger.error("issue details failed for %s: %s", issue_id, exc)

        if errors:
            summary = f"{len(errors)} of {len(pending)} issues failed: " + "; ".join(errors[:3])
            store.record_source_attempt(self.conn, "issue_details", ok=False, error=summary)
            result.sources_failed += 1
            result.errors.append(f"issue_details: {summary}")
        else:
            store.record_source_attempt(self.conn, "issue_details", ok=True)
            result.sources_ok += 1
        self.conn.commit()

    def sync_agent_tasks(self, agent_id: str) -> int:
        data = self.runner(["agent", "tasks", agent_id])
        rows = [normalize.normalize_run(item) for item in data]
        return store.upsert_runs(self.conn, rows)

    def sync_pricing(self, result: CycleResult) -> None:
        """Load pricing.json, mirror it into the DB and (re)compute daily costs.

        Reported as its own 'pricing' source so a broken/missing pricing file
        surfaces in health rather than silently leaving costs stale. Models
        present in usage but absent from the table are recorded as unpriced —
        that is a reported condition, not a source failure.
        """
        try:
            rates = pricing.load_pricing(
                self.config.pricing_path, self.config.pricing_overrides_path
            )
            pricing.upsert_model_pricing(self.conn, rates)
            pricing.recompute_daily_costs(
                self.conn, rates, self.config.credits_per_usd
            )
            unpriced = pricing.unpriced_models_in_usage(self.conn, rates)
            self.conn.commit()
        except pricing.PricingError as exc:
            message = str(exc)
            logger.error("source pricing failed: %s", message)
            store.record_source_attempt(self.conn, "pricing", ok=False, error=message)
            self.conn.commit()
            result.sources_failed += 1
            result.errors.append(f"pricing: {message}")
            return
        store.record_source_attempt(self.conn, "pricing", ok=True)
        self.conn.commit()
        result.sources_ok += 1
        if unpriced:
            logger.warning("unpriced models in usage: %s", ", ".join(unpriced))

    # -- the cycle ------------------------------------------------------------

    def run_cycle(self, detail_budget: Optional[int] = None) -> CycleResult:
        result = CycleResult(started_at=utcnow_iso())

        _, runtimes = self._source(result, "runtimes", self.sync_runtimes)
        _, agents = self._source(result, "agents", self.sync_agents)
        _, projects = self._source(result, "projects", self.sync_projects)

        for runtime in runtimes or []:
            rid = runtime["id"]
            self._source(result, f"runtime_usage:{rid}",
                         functools.partial(self.sync_runtime_usage, rid))
            self._source(result, f"runtime_activity:{rid}",
                         functools.partial(self.sync_runtime_activity, rid))

        # daily_usage is now fresh; price it and refresh the pricing table.
        self.sync_pricing(result)

        for project in projects or []:
            pid = project["id"]
            self._source(result, f"issues:{pid}",
                         functools.partial(self.sync_project_issues, pid))

        self.sync_issue_details(result, budget=detail_budget)

        for agent in agents or []:
            aid = agent["id"]
            self._source(result, f"agent_tasks:{aid}",
                         functools.partial(self.sync_agent_tasks, aid))

        result.finished_at = utcnow_iso()
        self.conn.execute(
            """
            INSERT INTO poll_cycles (started_at, finished_at, sources_ok, sources_failed, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                result.started_at,
                result.finished_at,
                result.sources_ok,
                result.sources_failed,
                "; ".join(result.errors)[:2000] if result.errors else None,
            ),
        )
        self.conn.commit()
        logger.info(
            "cycle done: %d ok, %d failed, details %d synced / %d failed",
            result.sources_ok, result.sources_failed,
            result.detail_synced, result.detail_failed,
        )
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AIStat Multica poller")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit")
    parser.add_argument("--interval", type=int, default=None,
                        help="seconds between cycles (default: config)")
    parser.add_argument("--detail-budget", type=int, default=None,
                        help="max issues to detail-sync per cycle (default: config)")
    parser.add_argument("--db", default=None, help="SQLite database path")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config()
    if args.db:
        from pathlib import Path

        config.db_path = Path(args.db)
    if args.interval is not None:
        config.poll_interval_seconds = args.interval

    config.ensure_db_dir()
    conn = connect(config.db_path)
    init_db(conn)
    poller = Poller(config, conn)

    try:
        while True:
            result = poller.run_cycle(detail_budget=args.detail_budget)
            if args.once:
                return 0 if result.ok else 1
            time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        logger.info("stopped")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
