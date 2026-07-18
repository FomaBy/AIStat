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

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from cdp_harness import (
    BOOT_TIMEOUT, Cdp, ChromeProcess, DashboardSession, PipeTransport)


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
                 stall_events=40, wrong_session_methods=(), clock=None,
                 close_events=True):
        self.session_id = session_id
        self.commands = []
        self._outbox = []
        self._target_seq = 0
        self._stall = set(stall_methods)
        self._stall_events = stall_events
        self._wrong = set(wrong_session_methods)
        self._clock = clock
        self._close_events = close_events
        self.events_delivered = 0
        self.closed = False
        self.destroyed_targets = []
        self.detached_sessions = []
        self._session_targets = {}

    # --- transport interface -------------------------------------------------
    def send(self, data, deadline=None):
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
        if self._clock is not None:
            self._clock.advance()
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

    def _event(self, method, session=None, params=None):
        event = {"method": method, "params": params or {}}
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
            self._session_targets[self.session_id] = msg["params"]["targetId"]
            self._event("Target.attachedToTarget")  # skipped, matched by id
            self._reply(msg, {"sessionId": self.session_id})
        elif method == "Page.close":
            self._reply(msg, {}, session=msg.get("sessionId"))
            if self._close_events:
                session = msg.get("sessionId")
                target = self._session_targets.pop(session)
                self.detached_sessions.append(session)
                self.destroyed_targets.append(target)
                self._event("Target.detachedFromTarget",
                            params={"sessionId": session, "targetId": target})
                self._event("Target.targetDestroyed", params={"targetId": target})
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
        ("Target.setDiscoverTargets", None),
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


def test_reopen_waits_for_previous_target_destruction_and_session_detach():
    """FAN-1352 causal red→green: a close acknowledgement alone is not enough.

    The old harness sent ``Target.closeTarget`` then immediately created another
    target.  This oracle requires the candidate's ``Page.close`` barrier to
    receive both the old flattened-session detach and the target-destroyed event
    before the next target lifecycle begins.
    """
    fake = FakeChrome(session_id="S-EXACT")
    cdp = Cdp(fake, clock=lambda: 0.0)
    cdp.open_page("http://127.0.0.1:9/one")
    old_target, old_session = cdp._target_id, cdp.session_id
    fake.commands.clear()
    cdp.open_page("http://127.0.0.1:9/two")
    assert [method for method, _ in fake.commands] == [
        "Page.close", "Target.createTarget", "Target.attachToTarget",
        "Page.enable", "Page.navigate"]
    assert fake.destroyed_targets == [old_target]
    assert fake.detached_sessions == [old_session]


def test_reopen_refuses_to_create_a_target_without_teardown_events():
    """A failed close cannot be masked by creating a second tab or retrying.

    The fake acknowledges ``Page.close`` but never emits the evidence that the
    prior target/session vanished.  The one monotonic deadline must raise a
    bounded timeout and leave the browser with only its original created target.
    """
    clock = FakeClock(step=1.0)
    fake = FakeChrome(session_id="S-EXACT", clock=clock, close_events=False)
    cdp = Cdp(fake, clock=clock)
    cdp.open_page("http://127.0.0.1:9/one")

    with pytest.raises(TimeoutError) as failure:
        cdp.open_page("http://127.0.0.1:9/two")

    message = str(failure.value)
    assert "Page.close teardown" in message
    assert "target_destroyed=False" in message
    assert "session_detached=False" in message
    assert [method for method, _ in fake.commands].count("Target.createTarget") == 1


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

        def send(self, data, deadline=None):
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


class _SettableClock:
    """A monotonic clock whose value is set explicitly, so a transport can model
    a packet becoming readable at a precise instant."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class CoalescedTransport:
    """Delivers several frames — interleaved events then (optionally) the
    matching reply — in ONE buffered packet on a single ``recv``, and advances
    the injected clock to ``arrive_at`` as that packet becomes readable. Models
    Chrome coalescing an event burst and the reply into one pipe read.
    """

    def __init__(self, clock, frames, arrive_at):
        self._clock = clock
        self._packet = b"".join(_dumps(frame) for frame in frames)
        self._arrive_at = arrive_at
        self._delivered = False
        self.sent = []

    def send(self, data, deadline=None):
        self.sent.append(data)

    def recv(self, timeout):
        if self._delivered:
            return None  # poll expiry — nothing more arrives
        self._delivered = True
        self._clock.t = self._arrive_at
        return self._packet

    def close(self):
        pass


def _lifecycle_events(session, count):
    return [{"method": "Page.lifecycleEvent", "params": {}, "sessionId": session}
            for _ in range(count)]


def test_call_rejects_coalesced_reply_that_arrives_after_deadline():
    """FAN-1347 negative probe: eight interleaved events and the matching reply
    delivered in one buffered packet that only becomes readable after the
    injected monotonic deadline. The buffered frames must not be parsed past the
    deadline, so the call ends in a bounded TimeoutError with method/URL/target/
    session diagnostics — never accepting the late reply."""
    clock = _SettableClock(0.0)
    reply = {"id": 1, "result": {"frameId": "F"}, "sessionId": "S-EXACT"}
    transport = CoalescedTransport(
        clock, _lifecycle_events("S-EXACT", 8) + [reply], arrive_at=6.0)
    cdp = Cdp(transport, clock=clock)
    cdp._requested_url = "http://127.0.0.1:9/dash"
    cdp._target_id = "T-1"
    cdp.session_id = "S-EXACT"

    with pytest.raises(TimeoutError) as failure:
        cdp.call("Page.navigate", {"url": cdp._requested_url}, timeout=5.0)

    message = str(failure.value)
    assert "Page.navigate" in message
    assert "requested_url='http://127.0.0.1:9/dash'" in message
    assert "target_id='T-1'" in message
    assert "session_id='S-EXACT'" in message


def test_call_accepts_coalesced_reply_that_arrives_before_deadline():
    """The mirror case: the same coalesced packet, readable before the deadline,
    is parsed frame by frame and the matching reply is accepted."""
    clock = _SettableClock(0.0)
    reply = {"id": 1, "result": {"frameId": "F"}, "sessionId": "S-EXACT"}
    transport = CoalescedTransport(
        clock, _lifecycle_events("S-EXACT", 8) + [reply], arrive_at=3.0)
    cdp = Cdp(transport, clock=clock)
    cdp.session_id = "S-EXACT"

    result = cdp.call("Page.navigate", {"url": "http://127.0.0.1:9/dash"},
                      timeout=5.0)
    assert result == {"frameId": "F"}


def test_call_times_out_on_coalesced_events_without_matching_reply():
    """A coalesced burst of events with no matching reply still terminates in
    bounded time: the frames are consumed, then the next poll expires."""
    clock = _SettableClock(0.0)
    transport = CoalescedTransport(
        clock, _lifecycle_events("S-EXACT", 8), arrive_at=3.0)
    cdp = Cdp(transport, clock=clock)
    cdp._requested_url = "http://127.0.0.1:9/x"
    cdp.session_id = "S-EXACT"

    with pytest.raises(TimeoutError) as failure:
        cdp.call("Page.navigate", {"url": cdp._requested_url}, timeout=5.0)
    assert "Page.navigate" in str(failure.value)
    assert "requested_url='http://127.0.0.1:9/x'" in str(failure.value)


# --- FAN-1352: a long-lived-session stall names what stalled ----------------
#
# The observed failure was a bounded `Runtime.evaluate: CDP read timed out`
# after 60s in one long-lived Chrome/CDP session — Chrome accepting the command
# and never replying, with the transport, buffer and process otherwise healthy.
# A bare timeout cannot tell that apart from a wedged pipe, a dead browser or a
# dead test server, so the timeout now classifies the stall and carries the
# evidence (monotonic elapsed/deadline, pending method, buffered/pending-frame
# state, recv activity, Chrome process state, optional server-liveness). These
# probe the classifier deterministically — no real Chrome, no sleeps.


class _FakeProc:
    """A subprocess stand-in for ``ChromeProcess.status()``: ``poll()`` returns
    ``None`` while running or an exit code once gone."""

    def __init__(self, rc=None, pid=4321):
        self._rc = rc
        self.pid = pid

    def poll(self):
        return self._rc


class _SilentBrowserTransport:
    """Accepts the command, then never delivers a byte — Chrome taking a
    ``Runtime.evaluate`` and never answering. Advances the injected clock to the
    deadline as the read blocks so the single-deadline call ends bounded."""

    def __init__(self, clock, stall_to):
        self._clock = clock
        self._stall_to = stall_to
        self.sent = []

    def send(self, data, deadline=None):
        self.sent.append(data)

    def recv(self, timeout):
        self._clock.t = self._stall_to
        return None

    def close(self):
        pass


class _DeadBrowserTransport:
    """The pipe EOFs (a Chrome that has exited): recv returns ``b""``."""

    def send(self, data, deadline=None):
        pass

    def recv(self, timeout):
        return b""

    def close(self):
        pass


def test_stall_names_a_silent_browser_and_keeps_the_single_deadline():
    """The exact FAN-1352 signature: the command was sent, the process is alive
    and the pipe delivered nothing before the one absolute deadline. The bounded
    timeout classifies it ``browser-silent`` and carries method, monotonic
    elapsed/deadline, zero recv, empty buffer and the running Chrome — and the
    clock never runs past the deadline (the single deadline is not weakened)."""
    clock = _SettableClock(0.0)
    cdp = Cdp(_SilentBrowserTransport(clock, stall_to=5.0),
              process=ChromeProcess(_FakeProc(rc=None)), clock=clock)
    cdp._requested_url = "http://127.0.0.1:9/dash"
    cdp._target_id = "T-1"
    cdp.session_id = "S-EXACT"

    with pytest.raises(TimeoutError) as failure:
        cdp.call("Runtime.evaluate", {"expression": "1"}, timeout=5.0)

    message = str(failure.value)
    assert "Runtime.evaluate" in message
    assert "stall=browser-silent" in message
    assert "method='Runtime.evaluate'" in message
    assert "elapsed=5.000s/deadline=5.000s" in message
    assert "recv_bytes=0, buffered=0frames+0B" in message
    assert "chrome=running(pid=4321)" in message
    assert "requested_url='http://127.0.0.1:9/dash'" in message
    assert clock.t == 5.0  # bounded exactly by the one deadline, no overrun


def test_stall_names_a_dead_browser_from_the_process_state():
    """A pipe EOF while the process has exited is a dead browser, not a mere
    transport hiccup — the classifier reads the process state and says so."""
    cdp = Cdp(_DeadBrowserTransport(),
              process=ChromeProcess(_FakeProc(rc=137)), clock=lambda: 0.0)
    cdp._requested_url = "http://127.0.0.1:9/dash"
    with pytest.raises(ConnectionError) as failure:
        cdp.call("Runtime.evaluate", {"expression": "1"}, timeout=5.0)
    message = str(failure.value)
    assert "stall=browser-dead" in message
    assert "chrome=exited(rc=137)" in message


def test_stall_names_a_transport_recv_when_events_arrive_without_a_reply():
    """Events keep arriving but the matching reply never does: the pipe and
    browser are live, the reply itself is missing. That is ``transport-recv``,
    distinct from a browser that went silent."""
    clock = _SettableClock(0.0)
    transport = CoalescedTransport(
        clock, _lifecycle_events("S-EXACT", 8), arrive_at=3.0)
    cdp = Cdp(transport, process=ChromeProcess(_FakeProc(rc=None)), clock=clock)
    cdp._requested_url = "http://127.0.0.1:9/dash"
    cdp.session_id = "S-EXACT"

    with pytest.raises(TimeoutError) as failure:
        cdp.call("Runtime.evaluate", {"expression": "1"}, timeout=5.0)
    message = str(failure.value)
    assert "stall=transport-recv" in message
    assert "chrome=running(pid=4321)" in message
    assert "recv_bytes=0, buffered=0frames+0B" not in message  # bytes did arrive


def test_stall_folds_in_the_caller_server_liveness_context():
    """A server-liveness probe (the dashboard fixture's Uvicorn thread) is
    folded into the stall message, so a wedged browser is told apart from a
    dead test server."""
    clock = _SettableClock(0.0)
    cdp = Cdp(_SilentBrowserTransport(clock, stall_to=5.0),
              process=ChromeProcess(_FakeProc(rc=None)), clock=clock,
              context=lambda: {"server_thread_alive": True,
                               "server_should_exit": False})
    with pytest.raises(TimeoutError) as failure:
        cdp.call("Runtime.evaluate", {"expression": "1"}, timeout=5.0)
    message = str(failure.value)
    assert "server_thread_alive=True" in message
    assert "server_should_exit=False" in message


def test_stall_names_a_dead_server_separately_from_a_silent_browser():
    """A dead Uvicorn thread is a server stall, not an opaque browser timeout."""
    clock = _SettableClock(0.0)
    cdp = Cdp(_SilentBrowserTransport(clock, stall_to=5.0),
              process=ChromeProcess(_FakeProc(rc=None)), clock=clock,
              context=lambda: {"server_thread_alive": False,
                               "server_should_exit": True})
    with pytest.raises(TimeoutError) as failure:
        cdp.call("Runtime.evaluate", {"expression": "1"}, timeout=5.0)
    message = str(failure.value)
    assert "stall=server-dead" in message
    assert "server_thread_alive=False" in message


def test_failed_session_reply_disarms_the_next_idle_diagnostic():
    """A completed error reply cannot be reported later as an in-flight call."""
    fake = FakeChrome(session_id="S-EXACT", wrong_session_methods={"Page.enable"})
    cdp = Cdp(fake, clock=lambda: 0.0)
    cdp.session_id = "S-EXACT"
    with pytest.raises(RuntimeError):
        cdp.call("Page.enable", session=True)
    assert "stall=idle" in cdp._diagnostics()


def test_a_context_probe_that_raises_never_masks_the_real_stall():
    """The optional probe is defensive: if it raises, the stall is still
    reported, with the probe failure noted rather than swallowing the timeout."""
    clock = _SettableClock(0.0)

    def _boom():
        raise RuntimeError("probe blew up")

    cdp = Cdp(_SilentBrowserTransport(clock, stall_to=5.0),
              process=ChromeProcess(_FakeProc(rc=None)), clock=clock,
              context=_boom)
    with pytest.raises(TimeoutError) as failure:
        cdp.call("Runtime.evaluate", {"expression": "1"}, timeout=5.0)
    message = str(failure.value)
    assert "stall=browser-silent" in message
    assert "context-probe-failed" in message


def test_diagnostics_report_idle_off_the_call_path():
    """A diagnostic built when no call is in flight (a wait_for whose condition
    never held, after its probes all returned) reports ``idle`` and omits the
    per-call fields — it must not attribute a finished call as the stall."""
    fake = FakeChrome(session_id="S-EXACT")
    cdp = Cdp(fake, clock=lambda: 0.0)
    cdp.open_page("http://127.0.0.1:9/dash")  # several calls, all complete
    diag = cdp._diagnostics()
    assert "stall=idle" in diag
    assert "method=" not in diag
    assert "elapsed=" not in diag


# --- AC#5: bounded send + failure-safe, idempotent cleanup ------------------


def test_send_is_bounded_when_the_command_pipe_is_full():
    """A Chrome that has stopped reading fills the command pipe. The send must
    fail with a bounded TimeoutError instead of blocking forever on the write."""
    read_fd, write_fd = os.pipe()
    transport = PipeTransport(write_fd, read_fd)
    try:
        payload = b"x" * (8 * 1024 * 1024)  # far exceeds the pipe buffer
        deadline = time.monotonic() + 0.3
        started = time.monotonic()
        with pytest.raises(TimeoutError) as failure:
            transport.send(payload, deadline)
        assert "send timed out" in str(failure.value)
        assert time.monotonic() - started < 5  # returned promptly, not a hang
    finally:
        transport.close()


def test_send_on_a_dead_pipe_fails_fast_rather_than_hanging():
    """When the read end is gone (a dead Chrome) the write fails immediately;
    the error is surfaced, bounded, not swallowed into a hang."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    transport = PipeTransport(write_fd, read_fd)
    try:
        started = time.monotonic()
        with pytest.raises(OSError):
            transport.send(b"probe\0", deadline=time.monotonic() + 5)
        assert time.monotonic() - started < 5
    finally:
        transport.close()


# --- AC#5: failure-safe, idempotent cleanup ---------------------------------

class _Server:
    def __init__(self):
        self.should_exit = False


class _Thread:
    def __init__(self, alive_after_join=False):
        self.joined = 0
        self._alive_after_join = alive_after_join

    def join(self, timeout=None):
        self.joined += 1

    def is_alive(self):
        return self._alive_after_join


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


def test_cleanup_joins_a_real_server_thread_to_completion():
    """The Uvicorn stand-in really runs and really stops: after close() the
    thread is provably gone (``is_alive() is False``), not just asked to exit."""
    class _StoppableServer:
        def __init__(self):
            self.should_exit = False

        def run(self):
            while not self.should_exit:
                time.sleep(0.005)

    server = _StoppableServer()
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    session = DashboardSession(server=server, thread=thread)

    session.close()
    assert session.thread_alive_after_join is False
    assert thread.is_alive() is False
    assert session.cleanup_errors == []


def test_cleanup_surfaces_a_hung_server_thread_after_bounded_join():
    """A thread that outlives its bounded join (a hung Uvicorn) is recorded and
    surfaced as a cleanup error instead of leaking silently."""
    server, thread = _Server(), _Thread(alive_after_join=True)
    session = DashboardSession(server=server, thread=thread)

    session.close()
    assert thread.joined == 1
    assert session.thread_alive_after_join is True
    assert any(isinstance(e, RuntimeError) and "still alive" in str(e)
               for e in session.cleanup_errors)


def test_cdp_close_bounded_on_graceful_timeout_then_reaps_and_cleans():
    """Failure path (b): the graceful Browser.close never replies (a hang the
    deadline turns into a bounded timeout); Chrome is still force-reaped, the
    transport closed and the task-owned workdir removed. Idempotent."""
    class _SilentTransport:
        def __init__(self):
            self.closed = 0

        def send(self, data, deadline=None):
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
    # Provisioned for a cold Chrome's first command and an awaitPromise compound
    # refresh under CI contention, not a warm round-trip (FAN-1348); still finite
    # so a genuine hang is bounded.
    assert BOOT_TIMEOUT == 60.0
