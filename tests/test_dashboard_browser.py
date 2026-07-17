"""Real-browser dashboard regression (FAN-1255): URL filter-state restore,
recovery from invalid links, valid-empty ranges and the full reset.

Runs the actual dashboard (FastAPI + static files + Chart.js) in headless
Chrome, driven over the DevTools protocol through ``--remote-debugging-pipe``
— stdlib only, no webdriver/playwright dependency. The suite skips cleanly
on machines without a Chrome/Chromium binary; everything else in the test
run stays unaffected.
"""

import json
import os
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

import aistat.server as server_module
from aistat.config import Config
from aistat.db import connect, init_db
from conftest import seed_aggregate_fixture

CHROME_CANDIDATES = (
    os.environ.get("AISTAT_CHROME"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome"),
    shutil.which("chromium"),
    shutil.which("chromium-browser"),
)
CHROME = next((c for c in CHROME_CANDIDATES if c and Path(c).exists()), None)

pytestmark = pytest.mark.skipif(
    CHROME is None, reason="no Chrome/Chromium binary for browser regression")

BOOT_TIMEOUT = 15.0


class Cdp:
    """Minimal DevTools-protocol client over Chrome's --remote-debugging-pipe.

    Chrome reads \\0-separated JSON commands from fd 3 and writes responses
    and events, likewise \\0-separated, to fd 4.
    """

    def __init__(self, chrome, user_data_dir):
        cmd_read, self._cmd_write = os.pipe()      # we write commands
        self._resp_read, resp_write = os.pipe()    # we read responses
        os.set_inheritable(cmd_read, True)
        os.set_inheritable(resp_write, True)

        def place_pipe_fds():
            # Chrome expects the CDP pipes exactly at fds 3 (its input) and
            # 4 (its output). The command pipe is created first, so it owns
            # the lowest free fds and dup2 in this order cannot clobber the
            # response pipe. dup2(fd, fd) is a no-op that keeps CLOEXEC
            # (Python pipe fds are CLOEXEC per PEP 446), so inheritability
            # must be forced explicitly or fd 3/4 vanish at execve.
            os.dup2(cmd_read, 3)
            os.dup2(resp_write, 4)
            os.set_inheritable(3, True)
            os.set_inheritable(4, True)

        self._proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--remote-debugging-pipe",
             "--user-data-dir=" + str(user_data_dir), "about:blank"],
            # close_fds must stay off: the default close pass runs after
            # preexec_fn and would destroy the freshly placed fds 3/4;
            # CLOEXEC already keeps every other Python fd out of Chrome.
            close_fds=False,
            preexec_fn=place_pipe_fds,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(cmd_read)
        os.close(resp_write)
        self._buffer = b""
        self._next_id = 0
        self.session_id = None
        self._target_id = None

    def _read_message(self, timeout=BOOT_TIMEOUT):
        deadline = time.monotonic() + timeout
        while b"\0" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CDP read timed out")
            ready, _, _ = select.select([self._resp_read], [], [], remaining)
            if not ready:
                raise TimeoutError("CDP read timed out")
            chunk = os.read(self._resp_read, 65536)
            if not chunk:
                raise ConnectionError("CDP pipe closed")
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method, params=None, session=True):
        self._next_id += 1
        message = {"id": self._next_id, "method": method,
                   "params": params or {}}
        if session and self.session_id:
            message["sessionId"] = self.session_id
        os.write(self._cmd_write, json.dumps(message).encode() + b"\0")
        while True:  # events arrive interleaved; wait for our reply
            reply = self._read_message()
            if reply.get("id") == self._next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def open_page(self, url):
        # One fresh tab per page: closing the previous target first means a
        # booted-page condition can never match a stale document.
        if self._target_id:
            self.call("Target.closeTarget", {"targetId": self._target_id},
                      session=False)
            self.session_id = None
        target = self.call("Target.createTarget", {"url": url}, session=False)
        self._target_id = target["targetId"]
        attached = self.call(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True}, session=False)
        self.session_id = attached["sessionId"]

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
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
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
                           f"page state: {page_state}")

    def close(self):
        try:
            self.call("Browser.close", session=False)
        except Exception:
            self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=10)
        os.close(self._cmd_write)
        os.close(self._resp_read)


DEBUG_STATE_JS = """JSON.stringify({
  search: location.search,
  tokens: document.getElementById("card-tokens").textContent,
  live: document.getElementById("live-label").textContent,
  error: document.getElementById("filter-error").textContent,
})"""

# Boot finished successfully once the summary card holds a real value.
BOOTED_JS = 'document.getElementById("card-tokens").textContent !== "—"'


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def dashboard():
    """The dashboard on a real HTTP port over a seeded database, plus one
    headless Chrome the tests navigate page-by-page."""
    import uvicorn

    tmp = tempfile.TemporaryDirectory(prefix="aistat-browser-")
    config = Config()
    config.db_path = Path(tmp.name) / "browser.db"
    config.credits_per_usd = 2.0
    conn = connect(config.db_path)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        server_module.create_app(config), host="127.0.0.1", port=port,
        log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)

    cdp = Cdp(CHROME, Path(tmp.name) / "chrome-profile")
    try:
        yield cdp, f"http://127.0.0.1:{port}"
    finally:
        cdp.close()
        server.should_exit = True
        thread.join(timeout=10)
        tmp.cleanup()


def _element_value(cdp, element_id):
    return cdp.eval(f'document.getElementById("{element_id}").value')


def _selected(cdp, element_id):
    return cdp.eval(
        f'[...document.getElementById("{element_id}").selectedOptions]'
        '.map((o) => o.value)')


def _filter_error(cdp):
    return cdp.eval('''(() => {
      const note = document.getElementById("filter-error");
      return note.hidden ? null : note.textContent;
    })()''')


def _search_params(cdp):
    return cdp.eval(
        "[...new URLSearchParams(location.search).entries()]")


def test_restores_valid_url_state_and_survives_reload(dashboard):
    """Happy path: repeated dimension params, a custom range and a group
    restore into the controls, no error note, and a reload after an
    interactive change keeps the new state (FAN-1188 behaviour intact)."""
    cdp, base = dashboard
    cdp.open_page(base + "/?project=P1&project=P2&agent=A2"
                  "&from=2026-01-01T10:00&to=2026-01-01T12:00&group=agent")
    cdp.wait_for(BOOTED_JS)
    assert _filter_error(cdp) is None
    assert _selected(cdp, "filter-project") == ["P1", "P2"]
    assert _selected(cdp, "filter-agent") == ["A2"]
    assert _element_value(cdp, "filter-period") == "custom"
    assert _element_value(cdp, "filter-from") == "2026-01-01T10:00"
    assert _element_value(cdp, "filter-to") == "2026-01-01T12:00"
    assert _element_value(cdp, "filter-group") == "agent"
    # The valid URL is preserved verbatim — no rewrite without a reason.
    assert dict(_search_params(cdp))["from"] == "2026-01-01T10:00"

    # Interactive change → URL updated → reload restores the new state.
    cdp.eval('''(() => {
      const input = document.getElementById("filter-from");
      input.value = "2026-01-01T10:15";
      input.dispatchEvent(new Event("change"));
    })()''')
    cdp.wait_for('location.search.includes("from=2026-01-01T10%3A15")')
    # The marker dies with the old document, so the booted condition below
    # can only match the freshly reloaded page.
    cdp.eval("window.__pre_reload = true; location.reload()")
    cdp.wait_for(f'window.__pre_reload === undefined && ({BOOTED_JS})')
    assert _element_value(cdp, "filter-from") == "2026-01-01T10:15"
    assert _selected(cdp, "filter-project") == ["P1", "P2"]


def test_recovers_from_malformed_url_state(dashboard):
    """The QA reproduction: bogus from/group plus an unknown agent and an
    out-of-range days must not strand the dashboard — invalid parts are
    dropped, the URL is normalized, data loads, the note explains."""
    cdp, base = dashboard
    cdp.open_page(base + "/?from=bogus&to=2026-01-01T10%3A00"
                  "&group=bogus&days=999&agent=ghost")
    cdp.wait_for(BOOTED_JS)  # boot completes instead of dying on HTTP 422
    error = _filter_error(cdp)
    assert error is not None and "сброшены" in error
    for param in ("from", "group", "agent", "days"):
        assert param in error
    # Only the valid remainder survives in the URL and the controls.
    params = dict(_search_params(cdp))
    assert params == {"days": "custom", "to": "2026-01-01T10:00"}
    assert _element_value(cdp, "filter-group") == "model"
    assert _selected(cdp, "filter-agent") == [""]  # "Все агенты"
    assert _element_value(cdp, "filter-from") == ""
    assert _element_value(cdp, "filter-to") == "2026-01-01T10:00"


def test_recovers_from_calendar_invalid_from(dashboard):
    """The QA reproduction for FAN-1269: a calendar-impossible ``from``
    (February 30) must be dropped like any other invalid value even though
    Chrome's lenient Date.parse rolls it over to March 2 — the dashboard
    boots on the surviving state instead of dying on HTTP 422."""
    cdp, base = dashboard
    cdp.open_page(base + "/?from=2026-02-30T00:00&to=2026-03-03T00:00"
                  "&group=agent")
    cdp.wait_for(BOOTED_JS)
    error = _filter_error(cdp)
    assert error is not None and "сброшены: from." in error
    assert dict(_search_params(cdp)) == {
        "days": "custom", "to": "2026-03-03T00:00", "group": "agent"}
    assert _element_value(cdp, "filter-from") == ""
    assert _element_value(cdp, "filter-to") == "2026-03-03T00:00"
    assert _element_value(cdp, "filter-group") == "agent"


def test_recovers_from_calendar_invalid_to(dashboard):
    """The second FAN-1269 QA reproduction: April 31 in ``to`` is dropped,
    the valid ``from`` survives and the dashboard loads."""
    cdp, base = dashboard
    cdp.open_page(base + "/?from=2026-04-01T00:00&to=2026-04-31T00:00"
                  "&group=agent")
    cdp.wait_for(BOOTED_JS)
    error = _filter_error(cdp)
    assert error is not None and "сброшены: to." in error
    assert dict(_search_params(cdp)) == {
        "days": "custom", "from": "2026-04-01T00:00", "group": "agent"}
    assert _element_value(cdp, "filter-from") == "2026-04-01T00:00"
    assert _element_value(cdp, "filter-to") == ""


def test_calendar_validation_holds_in_real_chrome(dashboard):
    """isValidDateTimeLocal must judge calendar reality itself (FAN-1269):
    the bug lived exactly in real Chrome, whose Date.parse normalizes
    impossible dates instead of returning NaN, so the validator is probed
    directly in the page against impossible days, non-leap February 29 and
    impossible times."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    invalid = ["2026-02-29T00:00", "2027-02-29T00:00", "2026-02-30T12:00",
               "2026-04-31T00:00", "2026-06-31T23:59", "2026-01-32T00:00",
               "2026-13-01T00:00", "2026-00-01T00:00", "2026-01-00T00:00",
               "2026-01-01T24:00", "2026-01-01T10:60", "2026-01-01T10:00:60"]
    valid = ["2028-02-29T00:00", "2026-02-28T23:59", "2026-04-30T00:00",
             "2026-12-31T23:59:59"]
    results = cdp.eval(
        json.dumps(invalid + valid) + ".map(isValidDateTimeLocal)")
    assert results == [False] * len(invalid) + [True] * len(valid)


def test_recovers_from_reverse_range_url(dashboard):
    """A reverse (and equal) from/to range never becomes active state: the
    range is reset, the URL returns to canonical /, data loads."""
    cdp, base = dashboard
    for query in ("/?from=2026-01-01T11:00&to=2026-01-01T10:00",
                  "/?from=2026-01-01T10:00&to=2026-01-01T10:00"):
        cdp.open_page(base + query)
        cdp.wait_for(BOOTED_JS)
        error = _filter_error(cdp)
        assert error is not None and "раньше" in error
        assert cdp.eval("location.search") == ""
        assert _element_value(cdp, "filter-period") == "30"
        assert _element_value(cdp, "filter-from") == ""
        assert _element_value(cdp, "filter-to") == ""


def test_interactive_reverse_range_is_not_committed(dashboard):
    """Typing a reverse range into the inputs shows the error and keeps the
    last valid state out of both the URL and the API queries."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)

    set_and_change = '''((id, value) => {
      const input = document.getElementById(id);
      input.value = value;
      input.dispatchEvent(new Event("change"));
    })'''
    cdp.eval(f'{set_and_change}("filter-from", "2026-01-01T11:00")')
    cdp.wait_for('location.search.includes("from=")')  # half-open commits
    cdp.eval(f'{set_and_change}("filter-to", "2026-01-01T10:00")')
    error = _filter_error(cdp)
    assert error is not None and "не применён" in error
    assert "to=" not in cdp.eval("location.search")

    cdp.eval(f'{set_and_change}("filter-to", "2026-01-01T12:00")')
    cdp.wait_for('location.search.includes("to=")')  # ordered range commits
    assert _filter_error(cdp) is None


def test_valid_empty_range_shows_zeros_not_failure(dashboard):
    """A well-formed range with no data is a normal result: zeros on the
    cards, no error note, no boot failure."""
    cdp, base = dashboard
    cdp.open_page(base + "/?from=2030-01-01T00:00&to=2030-01-02T00:00")
    cdp.wait_for(BOOTED_JS)
    assert _filter_error(cdp) is None
    tokens = cdp.eval('document.getElementById("card-tokens").textContent')
    assert tokens.rstrip().endswith("0")


def test_clear_button_returns_to_canonical_dashboard(dashboard):
    """One unambiguous reset: every filter back to its default, the URL back
    to bare /, data reloaded for the default period."""
    cdp, base = dashboard
    cdp.open_page(base + "/?project=P1&agent=A2&model=m-shared"
                  "&from=2026-01-01T10:00&to=2026-01-01T12:00&group=project")
    cdp.wait_for(BOOTED_JS)
    cdp.eval('document.getElementById("filter-reset").click()')
    cdp.wait_for('location.search === ""')
    assert _element_value(cdp, "filter-period") == "30"
    assert _element_value(cdp, "filter-group") == "model"
    assert _element_value(cdp, "filter-from") == ""
    assert _element_value(cdp, "filter-to") == ""
    assert _selected(cdp, "filter-project") == [""]
    assert _selected(cdp, "filter-agent") == [""]
    assert _selected(cdp, "filter-model") == [""]
    assert _filter_error(cdp) is None
    # The default 30-day window sees the whole fixture again.
    cdp.wait_for('document.getElementById("card-tokens").textContent'
                 '.includes("млн")')
