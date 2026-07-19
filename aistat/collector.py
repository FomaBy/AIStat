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
install is atomic. A separate per-tenant credential-version fence suppresses a
cycle as soon as replace/revoke makes its epoch stale and linearizes the final
publish/report against changes that arrive at that boundary.
"""

import argparse
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import handoff
from .cli_profile import (
    CliProfileError,
    ConnectionCliProfile,
    assert_safe_profile_storage,
)
from .config import Config
from .db import connect, init_db
from .poller import Poller
from .publish import PublishError, publish_snapshot
from .tenant import canonical_tenant_id
from .worker_store import WorkerStoreError, WorkerTokenStore
from .worker_sync import report_sync

logger = logging.getLogger("aistat.collector")

PROFILE_CREATE_FAILURE = "the connection profile could not be created"
CONNECTION_FAILURE = "connection collection failed"


def _safe_status(detail: str) -> str:
    """Keep outcomes inside the finite worker-visible error vocabulary."""
    if not detail:
        return ""
    return handoff.safe_sync_error(detail, default=CONNECTION_FAILURE)


@dataclass
class ConnectionOutcome:
    user_id: int
    status: str  # "collected" | "skipped" | "failed"
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"collected", "skipped", "failed"}:
            self.status = "failed"
        self.detail = _safe_status(self.detail)

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

    def force_release(self) -> None:
        """Best-effort descriptor cleanup for a faulting release hook."""
        fd = self._fd
        if fd is None:
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        except OSError:
            pass
        finally:
            self._fd = None


def _safe_lock_release(lock: _TenantLock, user_id: int) -> None:
    try:
        lock.release()
        return
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        logger.error("connection %s: profile lock release failed", user_id)
    try:
        force_release = getattr(lock, "force_release", None)
        if callable(force_release):
            force_release()
            return
        delegate = getattr(lock, "_delegate", None)
        force_release = getattr(delegate, "force_release", None)
        if callable(force_release):
            force_release()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        logger.error("connection %s: profile lock cleanup failed", user_id)


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
            try:
                outcomes.append(self._collect_one(meta))
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                # Keep a malformed/unexpected tenant record from aborting the
                # rest of the cycle.  The id is trusted only if it can be
                # canonicalized; otherwise this is a local diagnostic outcome.
                try:
                    user_id = canonical_tenant_id(meta.get("user_id", 0))
                except Exception:
                    user_id = 0
                outcomes.append(ConnectionOutcome(user_id, "failed", CONNECTION_FAILURE))
        return outcomes

    # -- per-connection ------------------------------------------------------

    def _collect_one(self, meta: Dict[str, Any]) -> ConnectionOutcome:
        user_id = canonical_tenant_id(meta["user_id"])
        listed_epoch = int(meta.get("token_epoch") or 0)
        try:
            # Validate both config and encrypted-store metadata before lock,
            # token decryption, profile construction or any CLI lifecycle.
            # Missing/empty metadata is the only supported legacy shape.
            handoff.normalize_official_server_url(
                meta.get("server_url"), self.config.multica_official_url
            )
        except ValueError:
            return self._fail(
                user_id, listed_epoch, handoff.UNSUPPORTED_MULTICA_SERVER
            )
        except Exception:
            return self._fail(user_id, listed_epoch, CONNECTION_FAILURE)
        try:
            # This must precede even the per-tenant lock: its pathname lives in
            # the same root and would otherwise follow a symlink or fail on a
            # non-directory before the profile's login guard can run.
            assert_safe_profile_storage(self.config.cli_profiles_dir, user_id)
        except CliProfileError as exc:
            return self._fail(user_id, listed_epoch, str(exc))
        except Exception:
            return self._fail(user_id, listed_epoch, CONNECTION_FAILURE)
        try:
            lock = _TenantLock(self.config.cli_profiles_dir, user_id)
        except Exception:
            return self._fail(
                user_id, listed_epoch, "the connection profile lock could not be acquired"
            )
        try:
            acquired = lock.acquire()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _safe_lock_release(lock, user_id)
            return self._fail(
                user_id,
                listed_epoch,
                "the connection profile lock could not be acquired",
            )
        if not acquired:
            return ConnectionOutcome(
                user_id, "skipped",
                "another poll of this tenant is already in progress",
            )
        try:
            try:
                with self.store.credential_fence(user_id) as fence:
                    credential = fence.get_credential()
            except WorkerStoreError:
                return self._fail(
                    user_id,
                    listed_epoch,
                    "the stored credential version could not be read",
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return self._fail(
                    user_id,
                    listed_epoch,
                    "the stored credential version could not be read",
                )
            if credential is None:
                # Revoked/erased between listing and reading. Do a residue-only
                # local cleanup so a prior crashed cycle's stale token config is
                # never left behind, but perform no login/poll/publish.
                return self._discard_revoked(user_id)
            try:
                handoff.normalize_official_server_url(
                    credential.server_url, self.config.multica_official_url
                )
            except ValueError:
                return self._fail(
                    user_id,
                    credential.token_epoch,
                    handoff.UNSUPPORTED_MULTICA_SERVER,
                )
            except Exception:
                return self._fail(
                    user_id, credential.token_epoch, CONNECTION_FAILURE
                )
            return self._collect_with_token(
                user_id,
                credential.token_epoch,
                credential.workspace_label,
                credential.token,
            )
        finally:
            _safe_lock_release(lock, user_id)

    def _discard_revoked(self, user_id: int) -> ConnectionOutcome:
        try:
            profile = self.profile_factory(self.config, user_id)
            profile.discard_residue()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.error(
                "connection %s: revoked-connection residue could not be removed",
                user_id,
            )
            return ConnectionOutcome(
                user_id, "failed", "the connection profile could not be cleaned up"
            )
        return ConnectionOutcome(user_id, "skipped", "connection was revoked")

    def _collect_with_token(
        self, user_id: int, epoch: int, label: Optional[str], token: str
    ) -> ConnectionOutcome:
        try:
            profile = self.profile_factory(self.config, user_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return self._record_outcome(
                user_id,
                epoch,
                ConnectionOutcome(user_id, "failed", PROFILE_CREATE_FAILURE),
            )

        entered = False
        cleanup_done = False

        def finish(outcome: ConnectionOutcome, *, report: bool = True):
            nonlocal cleanup_done
            if not cleanup_done:
                cleanup_failure = (
                    self._safe_cleanup(profile, user_id)
                    if entered
                    else self._safe_discard(profile, user_id)
                )
                cleanup_done = True
                # A secondary cleanup failure must not hide the primary
                # authentication/poll/publish failure.  A successful primary
                # outcome is downgraded because residue must not be reusable.
                if outcome.ok and cleanup_failure is not None:
                    outcome = cleanup_failure
            if outcome.status == "skipped":
                logger.info(
                    "connection %s: credential changed during collection",
                    user_id,
                )
                return outcome
            return self._record_outcome(user_id, epoch, outcome) if report else outcome

        try:
            try:
                if not self._credential_is_current(user_id, epoch):
                    return finish(self._stale_credential(user_id), report=False)
            except WorkerStoreError:
                return finish(
                    ConnectionOutcome(
                        user_id,
                        "failed",
                        "the credential version could not be verified",
                    )
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return finish(
                    ConnectionOutcome(user_id, "failed", CONNECTION_FAILURE)
                )

            try:
                enter_fn = getattr(profile, "__enter__", None)
                if callable(enter_fn):
                    entered_profile = enter_fn()
                    if entered_profile is not None:
                        profile = entered_profile
                entered = True
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return finish(
                    ConnectionOutcome(user_id, "failed", PROFILE_CREATE_FAILURE)
                )

            try:
                outcome, _lifecycle_started = self._drive_profile(
                    profile, user_id, epoch, label, token
                )
            except WorkerStoreError:
                outcome = ConnectionOutcome(
                    user_id,
                    "failed",
                    "the credential version could not be verified",
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                logger.error(
                    "connection %s lifecycle failed (%s)",
                    user_id,
                    "unexpected exception",
                )
                outcome = ConnectionOutcome(user_id, "failed", CONNECTION_FAILURE)

            if not outcome.ok:
                return finish(outcome, report=outcome.status != "skipped")

            try:
                # Linearize freshness immediately before outbound publish. A
                # replace/revoke that already completed makes this false; one
                # that starts after the check waits until publish, cleanup and
                # the success report finish for the still-current version.
                with self.store.credential_fence(user_id) as fence:
                    try:
                        if not fence.is_current(epoch):
                            outcome = ConnectionOutcome(
                                user_id,
                                "skipped",
                                "connection credential changed during collection",
                            )
                        else:
                            outcome = self._publish(user_id)
                    finally:
                        cleanup_failure = self._safe_cleanup(profile, user_id)
                        cleanup_done = True
                    if outcome.ok and cleanup_failure is not None:
                        outcome = cleanup_failure
                    return finish(
                        outcome,
                        report=outcome.status != "skipped",
                    )
            except WorkerStoreError:
                return finish(
                    ConnectionOutcome(
                        user_id,
                        "failed",
                        "the credential version could not be verified before publish",
                    )
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return finish(
                    ConnectionOutcome(user_id, "failed", CONNECTION_FAILURE)
                )
        except (KeyboardInterrupt, SystemExit):
            if not cleanup_done:
                if entered:
                    self._safe_cleanup(profile, user_id)
                else:
                    self._safe_discard(profile, user_id)
            cleanup_done = True
            raise

    def _drive_profile(
        self,
        profile: ConnectionCliProfile,
        user_id: int,
        epoch: int,
        label: Optional[str],
        token: str,
    ) -> Tuple[ConnectionOutcome, bool]:
        """Login, select workspace and poll; publish is separately fenced."""
        if not self._credential_is_current(user_id, epoch):
            return self._stale_credential(user_id), False
        try:
            profile.login(token)
        except CliProfileError as exc:
            # Safe, path-free message (auth, symlinked storage or unremovable
            # residue) — never the CLI's raw stderr or a token.
            return ConnectionOutcome(
                user_id,
                "failed",
                handoff.safe_sync_error(
                    str(exc), default="official CLI login failed for the connection"
                ),
            ), True
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.error("connection %s login failed", user_id)
            return ConnectionOutcome(
                user_id, "failed", "official CLI login failed for the connection"
            ), True
        if not self._credential_is_current(user_id, epoch):
            return self._stale_credential(user_id), True
        try:
            profile.select_workspace(label)
        except CliProfileError as exc:
            return ConnectionOutcome(
                user_id,
                "failed",
                handoff.safe_sync_error(
                    str(exc), default="the connection's workspace could not be resolved"
                ),
            ), True
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.error("connection %s workspace selection failed", user_id)
            return ConnectionOutcome(
                user_id, "failed", "the connection's workspace could not be resolved"
            ), True
        if not self._credential_is_current(user_id, epoch):
            return self._stale_credential(user_id), True
        db_path = self.config.worker_tenant_db_path(user_id)
        try:
            self._poll_into(db_path, profile.runner)
        except Exception as exc:  # defensive: never leak a token via a trace
            logger.error(
                "polling connection %s failed (%s)", user_id, type(exc).__name__
            )
            return (
                ConnectionOutcome(
                    user_id, "failed", "polling the connection's data failed"
                ),
                True,
            )
        if not self._credential_is_current(user_id, epoch):
            return self._stale_credential(user_id), True
        return ConnectionOutcome(user_id, "collected", ""), True

    def _credential_is_current(self, user_id: int, epoch: int) -> bool:
        with self.store.credential_fence(user_id) as fence:
            return fence.is_current(epoch)

    @staticmethod
    def _stale_credential(user_id: int) -> ConnectionOutcome:
        return ConnectionOutcome(
            user_id,
            "skipped",
            "connection credential changed during collection",
        )

    def _publish(self, user_id: int) -> ConnectionOutcome:
        db_path = self.config.worker_tenant_db_path(user_id)
        try:
            self.publish_fn(self.config, db_path, user_id)
        except (PublishError, ValueError):
            return ConnectionOutcome(
                user_id, "failed", "publishing the connection's snapshot failed"
            )
        except Exception as exc:
            logger.error(
                "publishing connection %s failed (%s)",
                user_id,
                type(exc).__name__,
            )
            return ConnectionOutcome(
                user_id, "failed", "publishing the connection's snapshot failed"
            )
        return ConnectionOutcome(user_id, "collected", "")

    def _record_outcome(
        self, user_id: int, epoch: int, outcome: ConnectionOutcome
    ) -> ConnectionOutcome:
        if outcome.ok:
            self._report(user_id, epoch, True, None)
            logger.info("collected connection %s", user_id)
        else:
            self._report(user_id, epoch, False, outcome.detail)
            logger.error("connection %s: %s", user_id, outcome.detail)
        return outcome

    def _safe_cleanup(
        self, profile: ConnectionCliProfile, user_id: int
    ) -> Optional[ConnectionOutcome]:
        """Log out and erase residue; a removal failure becomes a safe failure."""
        try:
            exit_fn = getattr(profile, "__exit__", None)
            if callable(exit_fn):
                exit_fn(None, None, None)
            else:
                profile.cleanup()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.error(
                "connection %s: profile residue could not be removed on cleanup",
                user_id,
            )
            return ConnectionOutcome(
                user_id, "failed", "the connection profile could not be cleaned up"
            )
        return None

    def _safe_discard(
        self, profile: ConnectionCliProfile, user_id: int
    ) -> Optional[ConnectionOutcome]:
        """Erase residue without logout when no login was attempted."""
        try:
            profile.discard_residue()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.error(
                "connection %s: profile residue could not be discarded",
                user_id,
            )
            return ConnectionOutcome(
                user_id, "failed", "the connection profile could not be cleaned up"
            )
        return None

    def _poll_into(self, db_path: Path, runner: Callable[[List[str]], Any]) -> None:
        self.config.ensure_worker_tenants_dir()
        conn = connect(db_path)
        try:
            init_db(conn)
            self.poll_fn(self.config, conn, runner)
        finally:
            conn.close()

    def _fail(self, user_id: int, epoch: int, message: str) -> ConnectionOutcome:
        safe_message = _safe_status(message) or CONNECTION_FAILURE
        logger.error("connection %s: %s", user_id, safe_message)
        self._report(user_id, epoch, False, safe_message)
        return ConnectionOutcome(user_id, "failed", safe_message)

    def _report(
        self, user_id: int, epoch: int, ok: bool, error: Optional[str]
    ) -> None:
        if self.report_fn is None:
            return
        safe_error = None if error is None else _safe_status(error)
        try:
            self.report_fn(self.config, user_id, epoch, ok, safe_error)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.warning(
                "could not report connection %s outcome (unexpected exception)",
                user_id,
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
