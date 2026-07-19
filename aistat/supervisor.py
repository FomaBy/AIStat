"""Fail-fast supervisor for the trusted local AIStat runtime.

One launchd job runs ``python -m aistat.supervisor``. The supervisor keeps
exactly one instance of each long-running contour alive:

* owner poller        — ``python -m aistat.poller``
* owner publisher     — ``python -m aistat.publish --watch``
* PAT worker sync     — ``python -m aistat.worker_sync --watch``
* per-user collector  — ``python -m aistat.collector``

A contour that dies is restarted with bounded exponential backoff. A contour
that crash-loops (too many restarts inside a short window) makes the supervisor
fail fast — it stops every child and exits non-zero, so launchd's ``KeepAlive``
restarts the whole runtime and the crash loop stays visible instead of hidden.

Each child is spawned in its own session/process group (``start_new_session``),
so on SIGTERM/SIGINT — and on reinstall/uninstall via ``launchctl bootout`` —
the supervisor signals the whole group and leaves no orphaned grandchild
(e.g. a ``multica`` CLI subprocess or a per-connection collector CLI). A single
-instance ``flock`` guarantees a second supervisor can never double-run the
contours.

Secrets are read from the process environment (loaded from an owner-only
private env file, never from the plist) and passed to children as environment,
never as argv and never written to the status file or logs.
"""

import argparse
import errno
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

from .config import Config
from . import preflight

logger = logging.getLogger("aistat.supervisor")

# Root of the runtime code tree (the directory that contains the ``aistat``
# package) so children launched with ``python -m aistat.X`` resolve the import.
CODE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Contour:
    """One supervised long-running process: a stable name and its argv."""

    name: str
    argv: Tuple[str, ...]


def default_contours(python: Optional[str] = None) -> List[Contour]:
    py = python or sys.executable
    return [
        Contour("poller", (py, "-m", "aistat.poller")),
        Contour("publisher", (py, "-m", "aistat.publish", "--watch")),
        Contour("worker_sync", (py, "-m", "aistat.worker_sync", "--watch")),
        Contour("collector", (py, "-m", "aistat.collector")),
    ]


def default_runtime_root() -> Path:
    root = os.environ.get("AISTAT_RUNTIME_ROOT")
    if root:
        return Path(root)
    return Path.home() / "Library" / "Application Support" / "AIStat"


class AlreadyRunning(RuntimeError):
    """Raised when another supervisor already holds the single-instance lock."""


class CrashLoop(RuntimeError):
    """Raised when a contour restarts too often; the supervisor fails fast."""


class SingleInstanceLock:
    """Exclusive ``flock`` proving exactly one supervisor runs at a time."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunning(
                    "another supervisor already holds {}".format(self.path)
                )
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


class _RealProcess:
    """A spawned contour: group signalling plus log-handle cleanup."""

    def __init__(self, popen: subprocess.Popen, log_fh):
        self._p = popen
        self._log = log_fh
        self.pid = popen.pid

    def poll(self) -> Optional[int]:
        return self._p.poll()

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(os.getpgid(self._p.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            # The group may be gone already; fall back to the direct child.
            try:
                self._p.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def terminate(self) -> None:
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        self._signal_group(signal.SIGKILL)

    def wait(self, timeout: Optional[float] = None) -> int:
        return self._p.wait(timeout=timeout)

    def close(self) -> None:
        try:
            if self._log is not None:
                self._log.close()
        except OSError:
            pass


def _default_spawn(contour: Contour, env: Dict[str, str], log_dir: Path,
                   cwd: Path):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / (contour.name + ".log")
    log_fh = open(log_path, "ab", buffering=0)
    popen = subprocess.Popen(
        list(contour.argv),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
        cwd=str(cwd),
    )
    return _RealProcess(popen, log_fh)


@dataclass
class _ChildState:
    contour: Contour
    proc: object = None
    restart_at: float = 0.0
    restarts: Deque[float] = field(default_factory=deque)
    start_count: int = 0
    last_exit: Optional[int] = None


class Supervisor:
    """Keeps every contour alive; fails fast on a crash loop; kills cleanly."""

    def __init__(
        self,
        contours: Sequence[Contour],
        *,
        runtime_root: Path,
        env: Optional[Dict[str, str]] = None,
        spawn: Callable = _default_spawn,
        cwd: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = 0.5,
        grace_seconds: float = 10.0,
        max_restarts: int = 5,
        restart_window: float = 60.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
    ):
        self.runtime_root = Path(runtime_root)
        self._children = [_ChildState(c) for c in contours]
        self._env = dict(env if env is not None else os.environ)
        self._spawn = spawn
        self._cwd = Path(cwd) if cwd is not None else CODE_ROOT
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._grace = grace_seconds
        self._max_restarts = max_restarts
        self._restart_window = restart_window
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._log_dir = self.runtime_root / "data"
        self._run_dir = self.runtime_root / "run"
        self._lock = SingleInstanceLock(self._run_dir / "supervisor.lock")
        self._status_path = self._run_dir / "supervisor.status.json"
        self._stopping = False
        self._started = False

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._lock.acquire()
        self._started = True
        for state in self._children:
            self._spawn_child(state, count_restart=False)
        self._write_status()

    def poll_once(self) -> None:
        """One monitoring pass: restart the dead, fail fast on a crash loop."""
        now = self._clock()
        changed = False
        for state in self._children:
            if state.proc is None:
                if not self._stopping and now >= state.restart_at:
                    self._respawn(state, now)
                    changed = True
                continue
            rc = state.proc.poll()
            if rc is None:
                continue
            state.last_exit = rc
            logger.warning("contour %s exited (rc=%s)", state.contour.name, rc)
            try:
                state.proc.close()
            except Exception:
                pass
            state.proc = None
            changed = True
            if self._stopping:
                continue
            delay = self._backoff_for(state)
            state.restart_at = now + delay
            logger.info("contour %s restart scheduled in %.1fs",
                        state.contour.name, delay)
        if changed:
            self._write_status()

    def stop(self) -> None:
        self._stopping = True
        for state in self._children:
            if state.proc is not None:
                self._terminate(state)
        if self._started:
            self._write_status()
            self._lock.release()
            self._started = False

    def run(self) -> int:
        self._install_signal_handlers()
        try:
            self.start()
            while not self._stopping:
                self.poll_once()
                self._sleep(self._poll_interval)
        except CrashLoop as exc:
            logger.error("%s", exc)
            self.stop()
            return 3
        self.stop()
        return 0

    # ---- helpers --------------------------------------------------------

    def _backoff_for(self, state: _ChildState) -> float:
        n = len(state.restarts)
        return min(self._backoff_cap, self._backoff_base * (2 ** n))

    def _respawn(self, state: _ChildState, now: float) -> None:
        window_start = now - self._restart_window
        while state.restarts and state.restarts[0] < window_start:
            state.restarts.popleft()
        state.restarts.append(now)
        if len(state.restarts) > self._max_restarts:
            raise CrashLoop(
                "contour {} restarted {} times within {:.0f}s; failing fast"
                .format(state.contour.name, len(state.restarts),
                        self._restart_window)
            )
        self._spawn_child(state, count_restart=True)

    def _spawn_child(self, state: _ChildState, *, count_restart: bool) -> None:
        state.proc = self._spawn(state.contour, self._env, self._log_dir,
                                 self._cwd)
        state.start_count += 1
        logger.info("started contour %s (pid=%s)", state.contour.name,
                    getattr(state.proc, "pid", "?"))

    def _terminate(self, state: _ChildState) -> None:
        proc = state.proc
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=self._grace)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=self._grace)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                proc.close()
            except Exception:
                pass
            state.proc = None

    def _write_status(self) -> None:
        payload = {
            "pid": os.getpid(),
            "stopping": self._stopping,
            "contours": [
                {
                    "name": s.contour.name,
                    "pid": getattr(s.proc, "pid", None) if s.proc else None,
                    "running": s.proc is not None,
                    "starts": s.start_count,
                    "restarts_in_window": len(s.restarts),
                    "last_exit": s.last_exit,
                }
                for s in self._children
            ],
        }
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._status_path.with_suffix(".json.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(str(tmp), str(self._status_path))
        except OSError:
            logger.debug("could not write supervisor status")

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            logger.info("received signal %s; stopping", signum)
            self._stopping = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass


def load_env_file(path: Path) -> Dict[str, str]:
    """Parse an owner-only ``KEY=VALUE`` env file and inject it into os.environ.

    The private env file is the only place the runtime's secrets live; the
    plist never carries them. Its permissions are validated by the caller
    (see :func:`aistat.preflight.check_env_file`) before this runs. Values are
    injected into the process environment so ``Config`` and every child pick
    them up, but are never echoed.
    """
    values: Dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"')):
            val = val[1:-1]
        if key:
            os.environ[key] = val
            values[key] = val
    return values


def _resolve_env_file() -> Optional[Path]:
    raw = os.environ.get("AISTAT_ENV_FILE")
    if raw:
        return Path(raw)
    default = Path.home() / ".config" / "aistat" / "production.env"
    return default if default.exists() else None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Supervise the local AIStat runtime contours"
    )
    parser.add_argument("--skip-preflight", action="store_true",
                        help="start contours without the startup preflight")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    env_file = _resolve_env_file()
    if env_file is not None:
        verdict = preflight.check_env_file(env_file)
        if not verdict.ok:
            logger.error("refusing to start: %s", verdict.detail)
            return 2
        try:
            load_env_file(env_file)
        except OSError as exc:
            logger.error("could not read env file: %s", type(exc).__name__)
            return 2

    if not args.skip_preflight:
        report = preflight.run_preflight(Config(), env_file=env_file)
        if not report.ok:
            for check in report.failures:
                logger.error("preflight FAIL %s: %s", check.name, check.detail)
            logger.error("refusing to start a misconfigured runtime")
            return 2

    supervisor = Supervisor(
        default_contours(sys.executable),
        runtime_root=default_runtime_root(),
    )
    try:
        return supervisor.run()
    except AlreadyRunning as exc:
        logger.error("%s", exc)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
