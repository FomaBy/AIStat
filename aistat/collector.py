"""Per-user data collection: poll every active connection, publish per tenant.

The trusted local worker walks the encrypted ``WorkerTokenStore`` of active
connections and, for each one independently:

1. decrypts that user's Multica API token;
2. logs the *official* CLI into a task-owned, per-connection profile using the
   token via the CLI's stdin prompt (never argv/env), pinned to the official
   host, with the connection's own workspace explicitly selected;
3. polls that workspace into a worker-local tenant database
   (``worker_tenants/<internal_user_id>.db``) with the existing ``Poller``;
4. publishes a signed, tenant-scoped snapshot of only that tenant;
5. logs out and erases the profile residue.

A per-tenant advisory lock provides backpressure so two cycles (or a restarted
worker) never poll the same tenant concurrently. One connection's auth, CLI,
poll or publish failure is isolated: it is recorded with a safe status that
never contains the token, a profile path or raw CLI detail, and the remaining
connections are still collected. Writes are idempotent upserts and the host
install is atomic, so a crash/restart neither duplicates data nor resurrects a
revoked credential.
"""

import argparse
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .cli_profile import CliProfileError, ConnectionCliProfile
from .config import Config
from .db import connect, init_db
from .poller import Poller
from .publish import PublishError, publish_snapshot
from .tenant import canonical_tenant_id
from .worker_store import WorkerStoreError, WorkerTokenStore
from .worker_sync import WorkerSyncError, report_sync

logger = logging.getLogger("aistat.collector")


@dataclass
class ConnectionOutcome:
    user_id: int
    status: str  # "collected" | "skipped" | "failed"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "collected"


def _default_poll(config: Config, conn, runner) -> None:
    """Run one full poll cycle of a connection's workspace into ``conn``."""
    Poller(config, conn, runner=runner).run_cycle(deadline=None)


class _TenantLock:
    """Non-blocking per-tenant advisory lock; released on close or crash."""

    def __init__(self, root: Path, user_id: int):
        self._path = Path(root) / "conn-{}.lock".format(int(user_id))
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class Collector:
    """Collect every active connection, isolating each connection's failures."""

    def __init__(
        self,
        config: Config,
        store: WorkerTokenStore,
        *,
        profile_factory: Callable[..., ConnectionCliProfile] = ConnectionCliProfile,
        publish_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        report_fn: Optional[Callable[..., Any]] = report_sync,
        poll_fn: Callable[..., None] = _default_poll,
    ):
        self.config = config
        self.store = store
        self.profile_factory = profile_factory
        self.publish_fn = publish_fn or publish_snapshot
        self.report_fn = report_fn
        self.poll_fn = poll_fn

    # -- public API ----------------------------------------------------------

    def collect_once(self) -> List[ConnectionOutcome]:
        outcomes: List[ConnectionOutcome] = []
        for meta in self.store.list_connections():
            outcomes.append(self._collect_one(meta))
        return outcomes

    # -- per-connection ------------------------------------------------------

    def _collect_one(self, meta: Dict[str, Any]) -> ConnectionOutcome:
        user_id = canonical_tenant_id(meta["user_id"])
        epoch = int(meta.get("token_epoch") or 0)
        label = meta.get("workspace_label")
        lock = _TenantLock(self.config.cli_profiles_dir, user_id)
        if not lock.acquire():
            return ConnectionOutcome(
                user_id, "skipped",
                "another poll of this tenant is already in progress",
            )
        try:
            try:
                token = self.store.get_token(user_id)
            except WorkerStoreError:
                return self._fail(user_id, epoch, "the stored token could not be read")
            if not token:
                # Revoked/erased between listing and reading — nothing to do.
                return ConnectionOutcome(user_id, "skipped", "connection was revoked")
            return self._collect_with_token(user_id, epoch, label, token)
        finally:
            lock.release()

    def _collect_with_token(
        self, user_id: int, epoch: int, label: Optional[str], token: str
    ) -> ConnectionOutcome:
        with self.profile_factory(self.config, user_id) as profile:
            try:
                profile.login(token)
            except CliProfileError:
                return self._fail(
                    user_id, epoch,
                    "authentication with the connection's token failed",
                )
            try:
                profile.select_workspace(label)
            except CliProfileError as exc:
                return self._fail(user_id, epoch, str(exc))
            db_path = self.config.worker_tenant_db_path(user_id)
            try:
                self._poll_into(db_path, profile.runner)
            except Exception as exc:  # defensive: never leak a token via a trace
                logger.error(
                    "polling connection %s failed (%s)", user_id, type(exc).__name__
                )
                return self._fail(user_id, epoch, "polling the connection's data failed")
            try:
                self.publish_fn(self.config, db_path, user_id)
            except (PublishError, ValueError):
                return self._fail(
                    user_id, epoch, "publishing the connection's snapshot failed"
                )
        # Profile logged out and residue erased by the context manager.
        self._report(user_id, epoch, True, None)
        logger.info("collected connection %s", user_id)
        return ConnectionOutcome(user_id, "collected", "")

    def _poll_into(self, db_path: Path, runner: Callable[[List[str]], Any]) -> None:
        self.config.ensure_worker_tenants_dir()
        conn = connect(db_path)
        try:
            init_db(conn)
            self.poll_fn(self.config, conn, runner)
        finally:
            conn.close()

    def _fail(self, user_id: int, epoch: int, message: str) -> ConnectionOutcome:
        logger.error("connection %s: %s", user_id, message)
        self._report(user_id, epoch, False, message)
        return ConnectionOutcome(user_id, "failed", message)

    def _report(
        self, user_id: int, epoch: int, ok: bool, error: Optional[str]
    ) -> None:
        if self.report_fn is None:
            return
        try:
            self.report_fn(self.config, user_id, epoch, ok, error)
        except (WorkerSyncError, WorkerStoreError, OSError) as exc:
            logger.warning(
                "could not report connection %s outcome (%s)",
                user_id, type(exc).__name__,
            )


def watch(config: Config) -> int:
    store = WorkerTokenStore(config.worker_store_path, config.worker_key_path)
    collector = Collector(config, store)
    interval = max(1, config.worker_collect_interval_seconds)
    while True:
        started = time.monotonic()
        try:
            outcomes = collector.collect_once()
            collected = sum(1 for o in outcomes if o.status == "collected")
            failed = sum(1 for o in outcomes if o.status == "failed")
            skipped = sum(1 for o in outcomes if o.status == "skipped")
            if outcomes:
                logger.info(
                    "collection cycle done: %d collected, %d failed, %d skipped",
                    collected, failed, skipped,
                )
        except (WorkerStoreError, OSError) as exc:
            logger.error("collection cycle failed (%s)", type(exc).__name__)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll every connected user's workspace and publish per-tenant"
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single collection cycle and exit"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config()
    try:
        if not args.once:
            return watch(config)
        store = WorkerTokenStore(config.worker_store_path, config.worker_key_path)
        outcomes = Collector(config, store).collect_once()
    except (WorkerStoreError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            return 0
        logger.error("%s", type(exc).__name__)
        return 1
    summary = {
        "collected": [o.user_id for o in outcomes if o.status == "collected"],
        "failed": [
            {"user_id": o.user_id, "detail": o.detail}
            for o in outcomes if o.status == "failed"
        ],
        "skipped": [
            {"user_id": o.user_id, "detail": o.detail}
            for o in outcomes if o.status == "skipped"
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not any(o.status == "failed" for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
