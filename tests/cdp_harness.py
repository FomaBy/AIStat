"""DevTools-protocol harness for the real-browser dashboard tests (FAN-1346).

Split out of ``test_dashboard_browser.py`` so the protocol/lifecycle logic can
be driven deterministically — a fake transport plus an injected clock, no real
Chrome and no sleeps — and so the pure protocol/deadline/cleanup tests keep
running on machines with no browser binary.

The client talks the Chrome DevTools protocol over ``--remote-debugging-pipe``
(stdlib only, no webdriver/playwright). Chrome reads ``\\0``-separated JSON
commands from fd 3 and writes replies and events, likewise ``\\0``-separated,
to fd 4.

Two environmental hardenings live here and nowhere else:

* Chrome runs against a task-owned, throwaway ``HOME``/``TMPDIR``/profile so a
  browser test never reads or writes the developer's real Chrome state.
* ``--use-mock-keychain`` keeps Chrome off the macOS login Keychain. On a fresh
  ``HOME`` (no ``~/Library/Keychains``) the OSCrypt keychain probe made during
  the first network request blocks indefinitely, so ``Page.navigate`` issued
  its lifecycle events and then never returned a reply — the exact clean-HOME
  timeout this module fixes. The mock keychain is in-memory and HOME-independent.
"""

import json
import os
import select
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# Every CDP call is bounded by this single absolute deadline (see ``Cdp.call``),
# which covers send, receive and buffered-frame parsing alike.
#
# The deadline exists to catch a genuinely hung transport (a lost reply, a dead
# Chrome), not to enforce a latency budget. It therefore has to clear the
# slowest *legitimate* operation, and two of those are far slower than a warm
# protocol round-trip: a cold Chrome answering its first command on a throwaway
# profile, and a ``Runtime.evaluate`` that awaits a compound page refresh
# (``refreshMeta().then(refreshAll)`` — a meta fetch plus eight parallel API
# fetches plus a full Chart.js render). On an idle machine both finish in a few
# seconds, but on a cold, contended CI runner their tail crosses 15s and the
# read fired a false ``CDP read timed out`` (the FAN-1347 preload flake). 60s
# gives an order of magnitude of headroom over the observed warm cost while
# still bounding a real hang.
BOOT_TIMEOUT = 60.0

CHROME_CANDIDATES = (
    os.environ.get("AISTAT_CHROME"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome"),
    shutil.which("chromium"),
    shutil.which("chromium-browser"),
)
CHROME = next((c for c in CHROME_CANDIDATES if c and Path(c).exists()), None)

# The reason string the real-browser tests skip with when no binary is present.
NO_CHROME_REASON = "no Chrome/Chromium binary for browser regression"

DEBUG_STATE_JS = """JSON.stringify({
  search: location.search,
  tokens: document.getElementById("card-tokens").textContent,
  live: document.getElementById("live-label").textContent,
  error: document.getElementById("filter-error").textContent,
})"""

# Boot finished successfully once the summary card holds a real value.
BOOTED_JS = 'document.getElementById("card-tokens").textContent !== "—"'

_TARGET_TEARDOWN_EVENTS = frozenset((
    "Target.detachedFromTarget",
    "Target.targetDestroyed",
))


class PipeTransport:
    """Raw ``\\0``-framed CDP transport over Chrome's debugging-pipe fds.

    ``recv`` returns the next chunk of bytes, ``b""`` at end-of-pipe, or ``None``
    when the poll times out — the three outcomes ``Cdp`` distinguishes. ``send``
    is bounded by the same absolute deadline: the command fd is non-blocking and
    each write waits for writability only until the deadline, so a full pipe (a
    Chrome that has stopped reading) raises a bounded ``TimeoutError`` instead of
    blocking forever.
    """

    def __init__(self, cmd_write, resp_read, *, clock=time.monotonic):
        self._cmd_write = cmd_write
        self._resp_read = resp_read
        self._clock = clock
        self._closed = False
        # A blocked send must be bounded by the deadline, not hang on a full
        # pipe; select on the fd only reports writability, os.write on a
        # blocking fd could still stall on a partial write. Non-blocking + a
        # select/deadline loop bounds the whole send. This is our write end of
        # the command pipe; Chrome reads the other end, unaffected.
        os.set_blocking(cmd_write, False)

    def send(self, data, deadline=None):
        view = memoryview(data)
        while view:
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError("CDP send timed out")
                _, writable, _ = select.select([], [self._cmd_write], [],
                                               remaining)
                if not writable:
                    raise TimeoutError("CDP send timed out")
            try:
                written = os.write(self._cmd_write, view)
            except BlockingIOError:
                continue
            view = view[written:]

    def recv(self, timeout):
        ready, _, _ = select.select([self._resp_read], [], [], max(0.0, timeout))
        if not ready:
            return None
        return os.read(self._resp_read, 65536)

    def close(self):
        if self._closed:
            return
        self._closed = True
        for fd in (self._cmd_write, self._resp_read):
            try:
                os.close(fd)
            except OSError:
                pass


class ChromeProcess:
    """Force-reaps one Chrome process; idempotent, bounded, never raises."""

    def __init__(self, proc):
        self._proc = proc
        self._reaped = False

    def status(self):
        """A non-blocking, never-raising snapshot of the Chrome process for a
        stall diagnostic: ``running(pid=N)`` while it is alive, ``exited(rc=N)``
        once it has gone. ``poll()`` reaps nothing and cannot block, so it is
        safe on the timeout path where a hung call is being reported."""
        try:
            rc = self._proc.poll()
        except Exception:
            return "unknown"
        if rc is None:
            return "running(pid=%s)" % getattr(self._proc, "pid", "?")
        return "exited(rc=%s)" % rc

    def reap(self):
        if self._reaped:
            return
        self._reaped = True
        proc = self._proc
        try:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        except Exception:
            # Teardown must never surface a reap failure; the fds and workdir
            # are still released by the caller.
            pass


class Cdp:
    """Minimal DevTools-protocol client over an injectable transport.

    The transport and clock are parameters so the whole protocol — lifecycle
    ordering, session routing, the single-deadline timeout and cleanup — can be
    exercised without a real browser. Use :func:`launch_chrome` for the real one.
    """

    def __init__(self, transport, *, process=None, workdir=None,
                 clock=time.monotonic, context=None):
        self._transport = transport
        self._process = process
        self._workdir = workdir
        self._clock = clock
        self._buffer = b""
        self._next_id = 0
        self.session_id = None
        self._target_id = None
        self._requested_url = None
        self._closed = False
        self._target_discovery_enabled = False
        # ``Page.close`` is asynchronous: its reply acknowledges the request,
        # while target/session teardown arrives as later Target-domain events.
        # Keep only those bounded lifecycle facts; retaining every Page event
        # from a long-lived browser would grow without bound.
        self._target_teardown_events = []
        # Optional caller-supplied probe (e.g. the dashboard fixture reporting
        # its Uvicorn thread's liveness). Read only while building a stall
        # diagnostic, never on the happy path, and never allowed to raise.
        self._context = context
        # Per-call stall accounting: the method in flight, its one absolute
        # deadline, when it started and how many transport bytes have arrived
        # since. These turn a bare ``CDP read timed out`` into a classifiable
        # transport-vs-browser-vs-server stall (see ``_diagnostics``).
        self._call_method = None
        self._call_deadline = None
        self._call_start = None
        self._call_recv_bytes = 0

    def _buffer_state(self):
        """(complete framed messages waiting, trailing partial-frame bytes) in
        the receive buffer — the pending-frame state a stall needs."""
        complete = self._buffer.count(b"\0")
        partial = len(self._buffer.rsplit(b"\0", 1)[-1]) if self._buffer else 0
        return complete, partial

    def _process_status(self):
        """Chrome process state for a diagnostic, or ``None`` when unknown —
        never raises, tolerant of a stand-in process without ``status()``."""
        if self._process is None:
            return None
        status = getattr(self._process, "status", None)
        if not callable(status):
            return "unknown"
        try:
            return status()
        except Exception:
            return "unknown"

    def _context_snapshot(self):
        """Read the optional caller probe once, without ever masking a stall."""
        if self._context is None:
            return None
        try:
            return self._context()
        except Exception as exc:
            return "context-probe-failed: %s" % exc

    @staticmethod
    def _format_context(context):
        if isinstance(context, dict):
            return ", ".join("%s=%s" % (key, context[key])
                             for key in sorted(context))
        return str(context)

    def _stall_class(self, context=None):
        """Name the most likely stall owner from the evidence gathered so far.

        * ``browser-dead`` — the Chrome process has exited.
        * ``transport-recv`` — bytes are still arriving (or sit buffered) yet no
          matching reply landed: the pipe is live, the browser is streaming, the
          reply itself is missing/late.
        * ``server-dead`` — the fixture's server thread has stopped, so a
          browser waiting on its HTTP/SSE work is not blamed as a pipe stall.
        * ``browser-silent`` — the process is alive but not a single byte came
          back within the deadline: the browser (or its pipe) is wedged.
        * ``idle`` — no call was in flight (diagnostic built off the call path).
        """
        proc_status = self._process_status()
        if proc_status is not None and proc_status.startswith("exited"):
            return "browser-dead"
        if (isinstance(context, dict) and
                context.get("server_thread_alive") is False):
            return "server-dead"
        if self._call_start is None:
            return "idle"
        complete, partial = self._buffer_state()
        if self._call_recv_bytes > 0 or complete or partial:
            return "transport-recv"
        return "browser-silent"

    def _diagnostics(self):
        # The original url/target/session core stays verbatim and first so
        # existing callers and assertions keep matching on it; the stall
        # classifier and its evidence are appended.
        core = ("requested_url=%r, target_id=%r, session_id=%r" %
                (self._requested_url, self._target_id, self.session_id))
        context = self._context_snapshot()
        parts = [core, "stall=%s" % self._stall_class(context)]
        if self._call_start is not None:
            elapsed = self._clock() - self._call_start
            budget = ((self._call_deadline - self._call_start)
                      if self._call_deadline is not None else float("nan"))
            parts.append("method=%r, elapsed=%.3fs/deadline=%.3fs" %
                         (self._call_method, elapsed, budget))
            complete, partial = self._buffer_state()
            parts.append("recv_bytes=%d, buffered=%dframes+%dB" %
                         (self._call_recv_bytes, complete, partial))
        proc_status = self._process_status()
        if proc_status is not None:
            parts.append("chrome=%s" % proc_status)
        if context:
            parts.append(self._format_context(context))
        return ", ".join(parts)

    def _read_message(self, deadline):
        """Read one framed message, bounded by an absolute monotonic deadline
        supplied by the caller — never a fresh per-message window.

        The deadline is authoritative for buffered/coalesced frames too: when a
        single packet carries several events and then the reply, time spent
        consuming the earlier frames still counts, so a matching reply that only
        becomes readable after the deadline must not slip through. Without the
        re-check below the ``while`` loop is skipped entirely for an
        already-buffered frame and an expired call could accept a late reply.
        """
        while b"\0" not in self._buffer:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("CDP read timed out")
            chunk = self._transport.recv(remaining)
            if chunk is None:
                raise TimeoutError("CDP read timed out")
            if chunk == b"":
                raise ConnectionError("CDP pipe closed")
            self._call_recv_bytes += len(chunk)
            self._buffer += chunk
        if deadline - self._clock() <= 0:
            raise TimeoutError("CDP read timed out")
        raw, self._buffer = self._buffer.split(b"\0", 1)
        message = json.loads(raw)
        if message.get("method") in _TARGET_TEARDOWN_EVENTS:
            self._target_teardown_events.append(message)
        return message

    def call(self, method, params=None, session=True, timeout=BOOT_TIMEOUT,
             deadline=None):
        self._next_id += 1
        message = {"id": self._next_id, "method": method,
                   "params": params or {}}
        expected_session = self.session_id if session and self.session_id else None
        if expected_session:
            message["sessionId"] = expected_session
        # One absolute deadline for the whole call — send, receive and buffered
        # frame parsing share it. Interleaved events consume the budget instead
        # of resetting it, so an event stream with no matching reply (or a send
        # that blocks on a full pipe) still terminates in bounded time.
        if deadline is None:
            deadline = self._clock() + timeout
        # Arm this call's stall accounting so a timeout can name what stalled.
        self._call_method = method
        self._call_deadline = deadline
        self._call_start = self._clock()
        self._call_recv_bytes = 0
        try:
            self._transport.send(json.dumps(message).encode() + b"\0", deadline)
            while True:  # events arrive interleaved; wait for our reply
                reply = self._read_message(deadline)
                if reply.get("id") == self._next_id:
                    if (expected_session and
                            reply.get("sessionId") != expected_session):
                        raise RuntimeError(
                            "%s: reply for unexpected session %r (%s)" %
                            (method, reply.get("sessionId"), self._diagnostics()))
                    if "error" in reply:
                        raise RuntimeError("%s: %s (%s)" %
                                           (method, reply["error"], self._diagnostics()))
                    return reply.get("result", {})
        except (ConnectionError, TimeoutError) as exc:
            raise type(exc)("%s: %s (%s)" %
                            (method, exc, self._diagnostics())) from exc
        finally:
            # Every terminal reply path, including send, protocol and
            # wrong-session failures, is no longer an in-flight call.
            self._call_start = None

    def _ensure_target_discovery(self):
        """Enable the two Target events that prove page teardown exactly once."""
        if not self._target_discovery_enabled:
            self.call("Target.setDiscoverTargets", {"discover": True},
                      session=False)
            self._target_discovery_enabled = True

    def _consume_target_teardown_events(self, target_id, session_id):
        """Return the exact destruction/detach facts received so far.

        Target events for unrelated browser targets are intentionally dropped:
        this single-threaded harness owns exactly one page session at a time and
        must not retain extension/background-target noise across a 20-test run.
        """
        destroyed = detached = False
        events, self._target_teardown_events = self._target_teardown_events, []
        for event in events:
            params = event.get("params", {})
            if (event.get("method") == "Target.targetDestroyed" and
                    params.get("targetId") == target_id):
                destroyed = True
            elif (event.get("method") == "Target.detachedFromTarget" and
                  params.get("sessionId") == session_id):
                detached = True
        return destroyed, detached

    def _wait_for_target_teardown(self, target_id, session_id, deadline,
                                  started):
        """Wait under the close operation's one deadline for exact teardown.

        ``Page.close`` only asks Chrome to close the active page.  The old
        document's SSE/timers are known to be gone only after both the flattened
        session detaches and Target discovery reports the page destroyed.
        """
        destroyed = detached = False
        self._call_method = "Page.close teardown"
        self._call_deadline = deadline
        self._call_start = started
        self._call_recv_bytes = 0
        try:
            while not (destroyed and detached):
                just_destroyed, just_detached = self._consume_target_teardown_events(
                    target_id, session_id)
                destroyed = destroyed or just_destroyed
                detached = detached or just_detached
                if destroyed and detached:
                    return
                try:
                    self._read_message(deadline)
                except (ConnectionError, TimeoutError) as exc:
                    raise type(exc)(
                        "Page.close teardown: target_id=%r, session_id=%r, "
                        "target_destroyed=%s, session_detached=%s (%s)" %
                        (target_id, session_id, destroyed, detached,
                         self._diagnostics())) from exc
        finally:
            self._call_start = None

    def _close_current_target(self):
        """Destroy and detach the current page before creating another one.

        Chrome 150 acknowledges ``Target.closeTarget`` before an attached page
        is gone.  That left prior dashboard tabs (and their EventSource/timers)
        alive in this module-scoped browser.  ``Page.close`` plus the exact
        Target events gives a causal teardown barrier without retries or sleeps.
        """
        target_id, session_id = self._target_id, self.session_id
        started = self._clock()
        deadline = started + BOOT_TIMEOUT
        self.call("Page.close", deadline=deadline)
        self._wait_for_target_teardown(target_id, session_id, deadline, started)
        self.session_id = None
        self._target_id = None

    def open_page(self, url, preload_script=None):
        self._ensure_target_discovery()
        # One fresh tab per page: wait for the previous target's real
        # destruction/detachment before a new session can be created.
        if self._target_id:
            self._close_current_target()
        self._requested_url = url
        # A target created with a non-blank URL may finish navigation before
        # Target.attachToTarget returns on newer Chrome versions.  Always
        # attach to a blank target first, then navigate through that exact
        # flattened session so Page/Runtime commands cannot land on a stale
        # or launch-created about:blank target.
        target = self.call(
            "Target.createTarget",
            {"url": "about:blank"},
            session=False)
        self._target_id = target["targetId"]
        attached = self.call(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True}, session=False)
        self.session_id = attached["sessionId"]
        self.call("Page.enable")
        if preload_script:
            self.call("Page.addScriptToEvaluateOnNewDocument",
                      {"source": preload_script})
        self.call("Page.navigate", {"url": url})

    def eval(self, expression):
        """Evaluate JS in the page; returns the JSON-serialized value."""
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True})
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get(
                "text", "JS exception") + ": " + str(result["exceptionDetails"]))
        return result["result"].get("value")

    def wait_for(self, condition_js, timeout=BOOT_TIMEOUT):
        """Poll a JS boolean expression until it holds; evaluation errors
        while a navigation destroys the execution context just poll again."""
        deadline = self._clock() + timeout
        last_error = None
        while self._clock() < deadline:
            try:
                if self.eval(condition_js):
                    return
                last_error = None
            except RuntimeError as exc:
                last_error = exc
            time.sleep(0.1)
        try:
            page_state = self.eval(DEBUG_STATE_JS)
        except RuntimeError as exc:
            page_state = f"unavailable: {exc}"
        raise TimeoutError(f"condition never held: {condition_js}\n"
                           f"last eval error: {last_error}\n"
                           f"page state: {page_state}\n"
                           f"{self._diagnostics()}")

    def close(self):
        """Reap Chrome, close the pipe fds and delete the task-owned workdir.

        Idempotent and defensive: a graceful ``Browser.close`` that times out or
        raises still force-reaps the process, and the fds and workdir are always
        released.
        """
        if self._closed:
            return
        self._closed = True
        try:
            try:
                self.call("Browser.close", session=False)
            except Exception:
                pass
            if self._process is not None:
                self._process.reap()
        finally:
            self._transport.close()
            if self._workdir is not None:
                shutil.rmtree(self._workdir, ignore_errors=True)


def _place_pipe_fds(cmd_read, resp_write):
    # Chrome expects the CDP pipes exactly at fds 3 (its input) and 4 (its
    # output). The command pipe is created first, so it owns the lowest free
    # fds and dup2 in this order cannot clobber the response pipe. dup2(fd, fd)
    # is a no-op that keeps CLOEXEC (Python pipe fds are CLOEXEC per PEP 446),
    # so inheritability must be forced explicitly or fd 3/4 vanish at execve.
    os.dup2(cmd_read, 3)
    os.dup2(resp_write, 4)
    os.set_inheritable(3, True)
    os.set_inheritable(4, True)


def launch_chrome(chrome=CHROME, *, clock=time.monotonic, context=None):
    """Start headless Chrome on a fresh, task-owned HOME/TMPDIR/profile and
    return a :class:`Cdp` bound to it. ``close()`` reaps the process and removes
    the whole throwaway tree. ``context`` is an optional caller probe (e.g. the
    server thread's liveness) folded into a stall diagnostic."""
    workdir = Path(tempfile.mkdtemp(prefix="aistat-browser-"))
    home = workdir / "home"
    tmp = workdir / "tmp"
    profile = workdir / "profile"
    for path in (home, tmp, profile):
        path.mkdir()

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)

    cmd_read, cmd_write = os.pipe()      # we write commands
    resp_read, resp_write = os.pipe()    # we read responses
    os.set_inheritable(cmd_read, True)
    os.set_inheritable(resp_write, True)

    try:
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             # HOME-independent keychain: the fix for the clean-HOME
             # Page.navigate stall (FAN-1346).
             "--use-mock-keychain", "--password-store=basic",
             "--remote-debugging-pipe",
             "--user-data-dir=" + str(profile), "about:blank"],
            # close_fds must stay off: the default close pass runs after
            # preexec_fn and would destroy the freshly placed fds 3/4;
            # CLOEXEC already keeps every other Python fd out of Chrome.
            close_fds=False,
            preexec_fn=lambda: _place_pipe_fds(cmd_read, resp_write),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        for fd in (cmd_read, cmd_write, resp_read, resp_write):
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    os.close(cmd_read)
    os.close(resp_write)
    return Cdp(PipeTransport(cmd_write, resp_read, clock=clock),
               process=ChromeProcess(proc), workdir=workdir, clock=clock,
               context=context)


class DashboardSession:
    """Owns the resources of the dashboard browser fixture and tears them all
    down defensively.

    ``close()`` releases every resource even if an earlier one raises, so a
    failure before ``yield`` (only some resources built) or a broken
    ``cdp.close()`` never leaks Chrome, the server thread or the temp DB. It is
    idempotent — a second call is a no-op — and records swallowed teardown
    errors on ``cleanup_errors`` for assertions.

    After the bounded join the server thread must be provably gone, not merely
    asked to stop: ``thread_alive_after_join`` records ``thread.is_alive()`` so
    a thread that outlived its join (a hung Uvicorn) is surfaced instead of
    silently leaking.
    """

    def __init__(self, cdp=None, server=None, thread=None, tmp=None):
        self.cdp = cdp
        self.server = server
        self.thread = thread
        self.tmp = tmp
        self._closed = False
        self.cleanup_errors = []
        self.thread_alive_after_join = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        errors = []
        if self.cdp is not None:
            try:
                self.cdp.close()
            except Exception as exc:  # a broken browser teardown must not leak the rest
                errors.append(exc)
        if self.server is not None:
            try:
                self.server.should_exit = True
            except Exception as exc:
                errors.append(exc)
        if self.thread is not None:
            try:
                self.thread.join(timeout=10)
                self.thread_alive_after_join = self.thread.is_alive()
                if self.thread_alive_after_join:
                    errors.append(RuntimeError(
                        "server thread still alive after join"))
            except Exception as exc:
                errors.append(exc)
        if self.tmp is not None:
            try:
                self.tmp.cleanup()
            except Exception as exc:
                errors.append(exc)
        self.cleanup_errors = errors
