"""Fail-fast supervisor: single-instance, restart, crash-loop, clean kill.

Deterministic behaviour is proven with fake processes and a fake clock; the
process-group teardown and restart are also proven against real short-lived
subprocesses (FAN-1404, criteria 1/2/9).
"""

import os
import subprocess
import sys
import time

import pytest

from aistat import supervisor as sv
from aistat.supervisor import (
    AlreadyRunning,
    Contour,
    CrashLoop,
    SingleInstanceLock,
    Supervisor,
)


# ---- fakes ---------------------------------------------------------------

class FakeProc:
    def __init__(self, name, pid, hang=False):
        self.name = name
        self.pid = pid
        self._rc = None
        self.hang = hang
        self.terminated = 0
        self.killed = 0
        self.closed = False

    def poll(self):
        return self._rc

    def exit(self, rc=0):
        self._rc = rc

    def terminate(self):
        self.terminated += 1
        if not self.hang:
            self._rc = -15

    def kill(self):
        self.killed += 1
        self._rc = -9

    def wait(self, timeout=None):
        if self._rc is None:
            raise subprocess.TimeoutExpired(self.name, timeout)
        return self._rc

    def close(self):
        self.closed = True


class FakeSpawner:
    def __init__(self, hang=False):
        self.spawned = []
        self.hang = hang
        self._n = 0

    def __call__(self, contour, env, log_dir, cwd):
        self._n += 1
        proc = FakeProc(contour.name, 9000 + self._n, hang=self.hang)
        self.spawned.append(proc)
        return proc

    def by_name(self, name):
        return [p for p in self.spawned if p.name == name]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def contours(*names):
    return [Contour(n, (sys.executable, "-c", "pass")) for n in names]


def make_supervisor(tmp_path, spawner, clock, **kw):
    kw.setdefault("sleep", lambda _d: None)
    return Supervisor(
        contours("poller", "publisher", "worker_sync", "collector"),
        runtime_root=tmp_path,
        spawn=spawner,
        clock=clock,
        **kw,
    )


# ---- single-instance lock ------------------------------------------------

def test_single_instance_lock_excludes_second(tmp_path):
    path = tmp_path / "supervisor.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    second = SingleInstanceLock(path)
    with pytest.raises(AlreadyRunning):
        second.acquire()
    first.release()
    # After release a fresh supervisor can take the lock.
    second.acquire()
    second.release()


def test_start_refuses_when_already_running(tmp_path):
    spawner = FakeSpawner()
    clock = Clock()
    a = make_supervisor(tmp_path, spawner, clock)
    b = make_supervisor(tmp_path, FakeSpawner(), Clock())
    a.start()
    try:
        with pytest.raises(AlreadyRunning):
            b.start()
    finally:
        a.stop()


# ---- start / status ------------------------------------------------------

def test_start_spawns_every_contour(tmp_path):
    spawner = FakeSpawner()
    sup = make_supervisor(tmp_path, spawner, Clock())
    sup.start()
    try:
        assert len(spawner.spawned) == 4
        status_file = tmp_path / "run" / "supervisor.status.json"
        assert status_file.exists()
        # Status file is owner-only and lists every contour by name only.
        import json
        payload = json.loads(status_file.read_text())
        assert {c["name"] for c in payload["contours"]} == {
            "poller", "publisher", "worker_sync", "collector"}
        assert (os.stat(status_file).st_mode & 0o077) == 0
    finally:
        sup.stop()


# ---- restart / backoff / crash loop --------------------------------------

def test_dead_child_is_restarted_after_backoff(tmp_path):
    spawner = FakeSpawner()
    clock = Clock()
    sup = make_supervisor(tmp_path, spawner, clock,
                          backoff_base=1.0, backoff_cap=30.0)
    sup.start()
    try:
        poller = spawner.by_name("poller")[0]
        poller.exit(1)
        sup.poll_once()  # notices exit, schedules restart
        assert len(spawner.by_name("poller")) == 1  # not yet restarted
        clock.advance(2.0)
        sup.poll_once()  # backoff elapsed -> respawn
        assert len(spawner.by_name("poller")) == 2
    finally:
        sup.stop()


def test_crash_loop_raises_after_max_restarts(tmp_path):
    spawner = FakeSpawner()
    clock = Clock()
    sup = Supervisor(
        contours("poller"),
        runtime_root=tmp_path,
        spawn=spawner,
        clock=clock,
        sleep=lambda _d: None,
        max_restarts=3,
        restart_window=100.0,
        backoff_base=0.0,
        backoff_cap=0.0,
    )
    sup.start()
    try:
        spawner.spawned[0].exit(1)
        with pytest.raises(CrashLoop):
            for _ in range(20):
                clock.advance(0.001)
                for p in spawner.spawned:
                    if p.poll() is None:
                        p.exit(1)
                sup.poll_once()
    finally:
        sup.stop()


class DeadSpawner(FakeSpawner):
    """Every spawned proc is already dead, forcing an immediate crash loop."""

    def __call__(self, contour, env, log_dir, cwd):
        proc = super().__call__(contour, env, log_dir, cwd)
        proc.exit(1)
        return proc


def test_run_returns_nonzero_on_crash_loop(tmp_path, monkeypatch):
    spawner = DeadSpawner()
    sup = Supervisor(
        contours("poller"),
        runtime_root=tmp_path,
        spawn=spawner,
        clock=Clock(),
        sleep=lambda _d: None,
        max_restarts=2,
        restart_window=100.0,
        backoff_base=0.0,
        backoff_cap=0.0,
    )
    monkeypatch.setattr(sup, "_install_signal_handlers", lambda: None)
    assert sup.run() == 3


# ---- clean shutdown ------------------------------------------------------

def test_stop_terminates_all_and_releases_lock(tmp_path):
    spawner = FakeSpawner()
    sup = make_supervisor(tmp_path, spawner, Clock())
    sup.start()
    sup.stop()
    assert all(p.terminated == 1 for p in spawner.spawned)
    assert all(p.closed for p in spawner.spawned)
    # Lock was released: a new supervisor can start.
    other = make_supervisor(tmp_path, FakeSpawner(), Clock())
    other.start()
    other.stop()


def test_hanging_child_is_force_killed(tmp_path):
    spawner = FakeSpawner(hang=True)
    sup = make_supervisor(tmp_path, spawner, Clock(), grace_seconds=0.01)
    sup.start()
    sup.stop()
    for p in spawner.spawned:
        assert p.terminated == 1
        assert p.killed == 1


def test_run_stops_when_signal_flag_set(tmp_path, monkeypatch):
    spawner = FakeSpawner()
    sup = make_supervisor(tmp_path, spawner, Clock())
    monkeypatch.setattr(sup, "_install_signal_handlers", lambda: None)

    calls = {"n": 0}

    def fake_sleep(_d):
        calls["n"] += 1
        if calls["n"] >= 2:
            sup._stopping = True

    monkeypatch.setattr(sup, "_sleep", fake_sleep)
    assert sup.run() == 0
    assert all(p.terminated == 1 for p in spawner.spawned)


# ---- real subprocess lifecycle ------------------------------------------

def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for(path, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError("timed out waiting for {}".format(path))


def test_real_sigterm_leaves_no_orphan_grandchild(tmp_path):
    child_pidfile = tmp_path / "child.pid"
    grand_pidfile = tmp_path / "grand.pid"
    program = (
        "import os,sys,time\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        "    open(sys.argv[2],'w').write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "open(sys.argv[1],'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    contour = Contour("poller", (sys.executable, "-c", program,
                                 str(child_pidfile), str(grand_pidfile)))
    sup = Supervisor([contour], runtime_root=tmp_path, grace_seconds=5.0)
    sup.start()
    try:
        child_pid = int(_wait_for(child_pidfile))
        grand_pid = int(_wait_for(grand_pidfile))
        assert _alive(child_pid) and _alive(grand_pid)
    finally:
        sup.stop()
    # The whole process group is gone — no orphaned grandchild survives.
    deadline = time.time() + 5.0
    while time.time() < deadline and (_alive(child_pid) or _alive(grand_pid)):
        time.sleep(0.05)
    assert not _alive(child_pid)
    assert not _alive(grand_pid)


def test_real_child_is_restarted(tmp_path):
    counter = tmp_path / "starts.txt"
    program = (
        "import sys\n"
        "open(sys.argv[1],'a').write('x')\n"
    )
    contour = Contour("poller", (sys.executable, "-c", program, str(counter)))
    sup = Supervisor(
        [contour], runtime_root=tmp_path,
        poll_interval=0.02, backoff_base=0.01, backoff_cap=0.05,
        max_restarts=100, restart_window=60.0,
    )
    sup.start()
    try:
        deadline = time.time() + 8.0
        while time.time() < deadline:
            sup.poll_once()
            if counter.exists() and len(counter.read_text()) >= 3:
                break
            time.sleep(0.03)
        assert counter.exists() and len(counter.read_text()) >= 3
    finally:
        sup.stop()
