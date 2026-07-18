"""Deterministic protocol/deadline/cleanup tests for the CDP harness (FAN-1346).

These drive the real ``cdp_harness`` code against an in-process fake transport
and an injected clock — no real Chrome, no sleeps, no wall-clock waits — so they
keep running (and passing) on machines with no browser binary, where the
real-browser suite skips. They pin three properties that a single green browser
run cannot prove:

* the exact create-target → attach-flattened-session → enable/preload →
  navigate lifecycle, with every session-scoped command and its reply bound to
  one exact session;
* ``Cdp.call`` bounding a call by one monotonic absolute deadline that
  interleaved events consume instead of reset, with method/URL/target/session
  diagnostics;
* failure-safe, idempotent cleanup across the three teardown failure paths.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from cdp_harness import (
    BOOT_TIMEOUT, Cdp, ChromeProcess, DashboardSession)


class FakeClock:
    """A monotonic clock advanced only by explicit ticks — never wall time."""

    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        return self.t

    def advance(self):
        self.t += self.step


class FakeChrome:
    """In-process CDP responder implementing the transport interface.

    Records every command's ``(method, sessionId)`` in send order and answers
    the lifecycle deterministically. Session-scoped replies carry ``session_id``;
    events are interleaved before replies so the client must skip them and match
    by id. ``stall_methods`` emit a burst of reply-less events (to exercise the
    call deadline); ``wrong_session_methods`` reply with the wrong session (to
    exercise reply/session correlation).
    """

    def __init__(self, session_id="S-EXACT", *, stall_methods=(),
                 stall_events=40, wrong_session_methods=(), clock=None):
        self.session_id = session_id
        self.commands = []
        self._outbox = []
        self._target_seq = 0
        self._stall = set(stall_methods)
        self._stall_events = stall_events
        self._wrong = set(wrong_session_methods)
        self._clock = clock
        self.events_delivered = 0
        self.closed = False

    # --- transport interface -------------------------------------------------
    def send(self, data):
        for raw in data.split(b"\0"):
            if raw:
                self._handle(_loads(raw))

    def recv(self, timeout):
        if self._outbox:
            chunk = self._outbox.pop(0)
            self.events_delivered += 1
            if self._clock is not None:
                self._clock.advance()
            return chunk
        return b"" if self.closed else None

    def close(self):
        self.closed = True

    # --- scripting -----------------------------------------------------------
    def _emit(self, message):
        self._outbox.append(_dumps(message))

    def _reply(self, msg, result, session=None):
        reply = {"id": msg["id"], "result": result}
        if session:
            reply["sessionId"] = session
        self._emit(reply)

    def _event(self, method, session=None):
        event = {"method": method, "params": {}}
        if session:
            event["sessionId"] = session
        self._emit(event)

    def _handle(self, msg):
        method = msg["method"]
        self.commands.append((method, msg.get("sessionId")))
        if method in self._stall:
            self._event("Page.frameStartedNavigating", session=self.session_id)
            self._event("Page.frameStartedLoading", session=self.session_id)
            for _ in range(self._stall_events):
                self._event("Page.lifecycleEvent", session=self.session_id)
            return  # no matching reply — the call must still terminate
        if method == "Target.createTarget":
            self._target_seq += 1
            self._reply(msg, {"targetId": "T-%d" % self._target_seq})
        elif method == "Target.attachToTarget":
            self._event("Target.attachedToTarget")  # skipped, matched by id
            self._reply(msg, {"sessionId": self.session_id})
        elif method == "Page.navigate":
            self._event("Page.frameStartedNavigating", session=self.session_id)
            self._event("Page.frameStartedLoading", session=self.session_id)
            self._reply(msg, {"frameId": "F", "loaderId": "L",
                              "isDownload": False}, session=self.session_id)
        else:
            bad = method in self._wrong
            self._reply(msg, {"identifier": "1"},
                        session="S-WRONG" if bad else msg.get("sessionId"))


def _dumps(message):
    import json
    return json.dumps(message).encode() + b"\0"


def _loads(raw):
    import json
    return json.loads(raw)


# --- AC#3: lifecycle order + single exact session ---------------------------

def test_open_page_lifecycle_order_and_single_session():
    fake = FakeChrome(session_id="S-EXACT")
    cdp = Cdp(fake, clock=lambda: 0.0)
    cdp.open_page("http://127.0.0.1:9/dash", preload_script="void 0")

    assert fake.commands == [
        ("Target.createTarget", None),
        ("Target.attachToTarget", None),
        ("Page.enable", "S-EXACT"),
        ("Page.addScriptToEvaluateOnNewDocument", "S-EXACT"),
        ("Page.navigate", "S-EXACT"),
    ]
    assert cdp.session_id == "S-EXACT"
    assert cdp._target_id == "T-1"
    # Preload is registered before the document navigates (before → after).
    order = [method for method, _ in fake.commands]
    assert order.index("Page.addScriptToEvaluateOnNewDocument") < \
        order.index("Page.navigate")


def test_reopen_closes_previous_target_before_relifecycle():
    fake = FakeChrome(session_id="S-EXACT")
    cdp = Cdp(fake, clock=lambda: 0.0)
    cdp.open_page("http://127.0.0.1:9/one")
    fake.commands.clear()
    cdp.open_page("http://127.0.0.1:9/two")
    assert [method for method, _ in fake.commands] == [
        "Target.closeTarget", "Target.createTarget", "Target.attachToTarget",
        "Page.enable", "Page.navigate"]


def test_session_scoped_reply_for_wrong_session_is_rejected():
    """A session-scoped reply that carries a different session is a routing
    error, proving command and reply are correlated to one exact session."""
    fake = FakeChrome(session_id="S-EXACT",
                      wrong_session_methods={"Page.enable"})
    cdp = Cdp(fake, clock=lambda: 0.0)
    with pytest.raises(RuntimeError) as failure:
        cdp.open_page("http://127.0.0.1:9/dash")
    message = str(failure.value)
    assert "unexpected session" in message
    assert "S-WRONG" in message


# --- AC#4: single absolute deadline + bounded transport timeout -------------

def test_call_is_bounded_by_one_absolute_deadline_not_reset_by_events():
    clock = FakeClock(step=1.0)
    fake = FakeChrome(session_id="S-EXACT", stall_methods={"Page.navigate"},
                      stall_events=40, clock=clock)
    cdp = Cdp(fake, clock=clock)
    # The three fields open_page sets before navigating, so diagnostics are real.
    cdp._requested_url = "http://127.0.0.1:9/dash"
    cdp._target_id = "T-1"
    cdp.session_id = "S-EXACT"

    with pytest.raises(TimeoutError) as failure:
        cdp.call("Page.navigate", {"url": cdp._requested_url}, timeout=5.0)

    # A single deadline (start + 5, step 1) spends the budget on interleaved
    # events and stops; it never drains the whole 40-event stream, which a
    # per-message deadline reset would.
    assert cdp._clock() <= 6.0
    assert 1 <= fake.events_delivered <= 6
    assert fake.events_delivered < fake._stall_events

    message = str(failure.value)
    assert "Page.navigate" in message
    assert "requested_url='http://127.0.0.1:9/dash'" in message
    assert "target_id='T-1'" in message
    assert "session_id='S-EXACT'" in message


def test_pipe_close_midcall_raises_bounded_connection_error():
    """An EOF on the response pipe ends the call at once with diagnostics,
    never an unbounded spin."""
    class ClosingTransport:
        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data)

        def recv(self, timeout):
            return b""  # end-of-pipe

        def close(self):
            pass

    cdp = Cdp(ClosingTransport(), clock=lambda: 0.0)
    cdp._requested_url = "http://127.0.0.1:9/x"
    with pytest.raises(ConnectionError) as failure:
        cdp.call("Page.enable", session=False)
    assert "Page.enable" in str(failure.value)
    assert "requested_url='http://127.0.0.1:9/x'" in str(failure.value)


# --- AC#5: failure-safe, idempotent cleanup ---------------------------------

class _Server:
    def __init__(self):
        self.should_exit = False


class _Thread:
    def __init__(self):
        self.joined = 0

    def join(self, timeout=None):
        self.joined += 1


def test_cleanup_setup_failure_before_yield_releases_partial_resources():
    """Failure path (a): Chrome never launched (cdp is None), the server thread
    and temp dir still tear down."""
    tmp = tempfile.TemporaryDirectory()
    path = tmp.name
    server, thread = _Server(), _Thread()
    session = DashboardSession(cdp=None, server=server, thread=thread, tmp=tmp)

    session.close()
    assert server.should_exit is True
    assert thread.joined == 1
    assert not Path(path).exists()
    assert session.cleanup_errors == []

    session.close()  # idempotent — no second teardown, no error
    assert thread.joined == 1


def test_cleanup_survives_exception_in_cdp_close():
    """Failure path (c): a raising cdp.close() never leaks the server or temp."""
    class _Exploding:
        def close(self):
            raise RuntimeError("boom")

    tmp = tempfile.TemporaryDirectory()
    path = tmp.name
    server, thread = _Server(), _Thread()
    session = DashboardSession(cdp=_Exploding(), server=server, thread=thread,
                               tmp=tmp)

    session.close()
    assert any(isinstance(e, RuntimeError) for e in session.cleanup_errors)
    assert server.should_exit is True
    assert thread.joined == 1
    assert not Path(path).exists()

    session.close()  # idempotent
    assert thread.joined == 1


def test_cdp_close_bounded_on_graceful_timeout_then_reaps_and_cleans():
    """Failure path (b): the graceful Browser.close never replies (a hang the
    deadline turns into a bounded timeout); Chrome is still force-reaped, the
    transport closed and the task-owned workdir removed. Idempotent."""
    class _SilentTransport:
        def __init__(self):
            self.closed = 0

        def send(self, data):
            pass

        def recv(self, timeout):
            return None  # never a reply → bounded TimeoutError

        def close(self):
            self.closed += 1

    class _Reaper:
        def __init__(self):
            self.reaped = 0

        def reap(self):
            self.reaped += 1

    workdir = Path(tempfile.mkdtemp())
    transport, reaper = _SilentTransport(), _Reaper()
    cdp = Cdp(transport, process=reaper, workdir=workdir, clock=lambda: 0.0)

    cdp.close()
    assert reaper.reaped == 1
    assert transport.closed == 1
    assert not workdir.exists()

    cdp.close()  # idempotent — no double reap or double close
    assert reaper.reaped == 1
    assert transport.closed == 1


def test_chrome_process_reap_force_kills_unresponsive_chrome_idempotently():
    """A Chrome that ignores terminate (a hang) is force-killed, reaped once."""
    class _HangingProc:
        def __init__(self):
            self.terminated = 0
            self.killed = 0
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1
            self._alive = False

        def wait(self, timeout=None):
            if self.killed:
                return 0
            raise subprocess.TimeoutExpired("chrome", timeout)

    proc = _HangingProc()
    reaper = ChromeProcess(proc)
    reaper.reap()
    assert proc.terminated == 1 and proc.killed == 1

    reaper.reap()  # idempotent
    assert proc.terminated == 1 and proc.killed == 1


def test_boot_timeout_is_the_documented_default():
    assert BOOT_TIMEOUT == 15.0
