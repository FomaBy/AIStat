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

# Chrome takes a beat to answer the first command in a cold profile; every CDP
# call is bounded by this single absolute deadline (see ``Cdp.call``).
BOOT_TIMEOUT = 15.0

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


class PipeTransport:
    """Raw ``\\0``-framed CDP transport over Chrome's debugging-pipe fds.

    ``recv`` returns the next chunk of bytes, ``b""`` at end-of-pipe, or ``None``
    when the poll times out — the three outcomes ``Cdp`` distinguishes.
    """

    def __init__(self, cmd_write, resp_read):
        self._cmd_write = cmd_write
        self._resp_read = resp_read
        self._closed = False

    def send(self, data):
        os.write(self._cmd_write, data)

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
                 clock=time.monotonic):
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

    def _diagnostics(self):
        return ("requested_url=%r, target_id=%r, session_id=%r" %
                (self._requested_url, self._target_id, self.session_id))

    def _read_message(self, deadline):
        """Read one framed message, bounded by an absolute monotonic deadline
        supplied by the caller — never a fresh per-message window."""
        while b"\0" not in self._buffer:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("CDP read timed out")
            chunk = self._transport.recv(remaining)
            if chunk is None:
                raise TimeoutError("CDP read timed out")
            if chunk == b"":
                raise ConnectionError("CDP pipe closed")
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method, params=None, session=True, timeout=BOOT_TIMEOUT):
        self._next_id += 1
        message = {"id": self._next_id, "method": method,
                   "params": params or {}}
        expected_session = self.session_id if session and self.session_id else None
        if expected_session:
            message["sessionId"] = expected_session
        # One absolute deadline for the whole call: interleaved events consume
        # the budget instead of resetting it, so an event stream with no
        # matching reply still terminates in bounded time.
        deadline = self._clock() + timeout
        self._transport.send(json.dumps(message).encode() + b"\0")
        while True:  # events arrive interleaved; wait for our reply
            try:
                reply = self._read_message(deadline)
            except (ConnectionError, TimeoutError) as exc:
                raise type(exc)("%s: %s (%s)" %
                                (method, exc, self._diagnostics())) from exc
            if reply.get("id") == self._next_id:
                if expected_session and reply.get("sessionId") != expected_session:
                    raise RuntimeError(
                        "%s: reply for unexpected session %r (%s)" %
                        (method, reply.get("sessionId"), self._diagnostics()))
                if "error" in reply:
                    raise RuntimeError("%s: %s (%s)" %
                                       (method, reply["error"], self._diagnostics()))
                return reply.get("result", {})

    def open_page(self, url, preload_script=None):
        # One fresh tab per page: closing the previous target first means a
        # booted-page condition can never match a stale document.
        if self._target_id:
            self.call("Target.closeTarget", {"targetId": self._target_id},
                      session=False)
            self.session_id = None
            self._target_id = None
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


def launch_chrome(chrome=CHROME, *, clock=time.monotonic):
    """Start headless Chrome on a fresh, task-owned HOME/TMPDIR/profile and
    return a :class:`Cdp` bound to it. ``close()`` reaps the process and removes
    the whole throwaway tree."""
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
    return Cdp(PipeTransport(cmd_write, resp_read),
               process=ChromeProcess(proc), workdir=workdir, clock=clock)


class DashboardSession:
    """Owns the resources of the dashboard browser fixture and tears them all
    down defensively.

    ``close()`` releases every resource even if an earlier one raises, so a
    failure before ``yield`` (only some resources built) or a broken
    ``cdp.close()`` never leaks Chrome, the server thread or the temp DB. It is
    idempotent — a second call is a no-op — and records swallowed teardown
    errors on ``cleanup_errors`` for assertions.
    """

    def __init__(self, cdp=None, server=None, thread=None, tmp=None):
        self.cdp = cdp
        self.server = server
        self.thread = thread
        self.tmp = tmp
        self._closed = False
        self.cleanup_errors = []

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
            except Exception as exc:
                errors.append(exc)
        if self.tmp is not None:
            try:
                self.tmp.cleanup()
            except Exception as exc:
                errors.append(exc)
        self.cleanup_errors = errors
