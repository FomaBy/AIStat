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

    def open_page(self, url, preload_script=None):
        # One fresh tab per page: closing the previous target first means a
        # booted-page condition can never match a stale document.
        if self._target_id:
            self.call("Target.closeTarget", {"targetId": self._target_id},
                      session=False)
            self.session_id = None
        target = self.call(
            "Target.createTarget",
            {"url": "about:blank" if preload_script else url},
            session=False)
        self._target_id = target["targetId"]
        attached = self.call(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True}, session=False)
        self.session_id = attached["sessionId"]
        if preload_script:
            self.call("Page.enable")
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


def _chart_colors(cdp, canvas_id):
    """label -> backgroundColor for a bar chart's datasets, read live from the
    rendered Chart.js instance."""
    return cdp.eval(
        '(() => { const c = state.charts["%s"]; const o = {};'
        ' c.data.datasets.forEach((d) => { o[d.label] = d.backgroundColor; });'
        ' return o; })()' % canvas_id)


def _reset_color_registry(cdp):
    """Make the shipped registry a fresh browser-session registry.

    The dashboard fixture already boots with its own entities. Clearing the
    live map lets capacity tests exercise the finite canonical universe
    without accidentally counting fixture identities as prior allocations.
    """
    assert cdp.eval(
        'typeof colorRegistry === "object" && '
        'typeof registerEntityColors === "function"') is True
    cdp.eval("colorRegistry.byKey.clear()")


def _register_colors(cdp, entity_type, ids):
    """Register one complete typed set through the production batch API."""
    return cdp.eval("registerEntityColors(%s, %s)" % (
        json.dumps(entity_type), json.dumps(ids)))


def _register_identity_batches(cdp, identities):
    by_type = {}
    for entity_type, entity_id in identities:
        if entity_id is not None and str(entity_id).strip():
            by_type.setdefault(entity_type, []).append(entity_id)
    for entity_type, ids in by_type.items():
        _register_colors(cdp, entity_type, ids)


def _color_map(cdp, entity_type, ids):
    return cdp.eval("Object.fromEntries(%s.map((id) => [id, entityColor(%s, id)]))" % (
        json.dumps(ids), json.dumps(entity_type)))


def _registry_map(cdp, typed_ids):
    return cdp.eval("Object.fromEntries(%s.map(([type, id]) => "
                    "[type + '\\u0000' + id, entityColor(type, id)]))" %
                    json.dumps(typed_ids))


def test_fallback_batch_has_exact_model_capacity_and_late_repeat(dashboard):
    """FAN-1318: canonical model batches fill 14 non-Fable slots exactly.

    A 15th identity may repeat deterministically, but never before the
    Fable-adjusted capacity. Forward and reverse input order must produce the
    same mapping, including the collision regression identities.
    """
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    _reset_color_registry(cdp)

    model_ids = ["collision-6", "collision-9"] + [
        "model-capacity-%02d" % i for i in range(12)]
    _register_colors(cdp, "model", model_ids)
    at_capacity = _color_map(cdp, "model", model_ids)
    fallback_palette = cdp.eval(
        'PALETTE.filter((color) => color !== '
        'ENTITY_ANCHORS.model["claude-fable-5"])')
    assert len(fallback_palette) == 14
    assert len(set(at_capacity.values())) == 14
    assert set(at_capacity.values()) == set(fallback_palette)
    assert at_capacity["collision-6"] != at_capacity["collision-9"]
    assert cdp.eval('entityColor("model", "claude-fable-5")') == "#ef4444"

    fifteenth = model_ids + ["model-after-capacity"]
    _register_colors(cdp, "model", fifteenth)
    after_capacity = _color_map(cdp, "model", fifteenth)
    assert {key: after_capacity[key] for key in model_ids} == at_capacity
    assert len(set(after_capacity.values())) == 14
    assert after_capacity["model-after-capacity"] in set(fallback_palette)

    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    _reset_color_registry(cdp)
    _register_colors(cdp, "model", list(reversed(fifteenth)))
    fresh_reverse = _color_map(cdp, "model", fifteenth)

    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    _reset_color_registry(cdp)
    _register_colors(cdp, "model", fifteenth)
    assert _color_map(cdp, "model", fifteenth) == fresh_reverse


def test_agent_and_project_batches_have_exact_fifteen_capacity(dashboard):
    """Agent/project spaces have 15 fallback colors and repeat on identity 16."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    _reset_color_registry(cdp)

    _register_colors(cdp, "agent", ["typed-space-shared"])
    _register_colors(cdp, "project", ["typed-space-shared"])
    assert cdp.eval('colorRegistry.byKey.has("model\\u0000typed-space-shared")') is False
    assert cdp.eval('colorRegistry.byKey.has("agent\\u0000typed-space-shared")') is True
    assert cdp.eval('colorRegistry.byKey.has("project\\u0000typed-space-shared")') is True
    assert cdp.eval('entityColor("agent", null)') == "#cbd5e1"
    assert cdp.eval('entityColor("project", "")') == "#cbd5e1"

    for entity_type in ("agent", "project"):
        _reset_color_registry(cdp)
        ids = ["typed-space-shared"] + [
            "%s-capacity-%02d" % (entity_type, i) for i in range(14)]
        _register_colors(cdp, entity_type, ids)
        at_capacity = _color_map(cdp, entity_type, ids)
        assert len(set(at_capacity.values())) == 15
        assert set(at_capacity.values()) == set(cdp.eval("PALETTE"))

        all_ids = ids + ["%s-after-capacity" % entity_type]
        _register_colors(cdp, entity_type, all_ids)
        after_capacity = _color_map(cdp, entity_type, all_ids)
        assert {key: after_capacity[key] for key in ids} == at_capacity
        assert len(set(after_capacity.values())) == 15

        cdp.open_page(base + "/")
        cdp.wait_for(BOOTED_JS)
        _reset_color_registry(cdp)
        _register_colors(cdp, entity_type, list(reversed(all_ids)))
        fresh_reverse = _color_map(cdp, entity_type, all_ids)

        cdp.open_page(base + "/")
        cdp.wait_for(BOOTED_JS)
        _reset_color_registry(cdp)
        _register_colors(cdp, entity_type, all_ids)
        assert _color_map(cdp, entity_type, all_ids) == fresh_reverse


def test_meta_boot_registers_canonical_sets_before_chart_render(dashboard):
    """Meta registration completes before the first chart and survives refresh."""
    cdp, base = dashboard
    trace_script = r'''(() => {
      window.__aistat_boot_trace = [];
      const trace = window.__aistat_boot_trace;
      const fetchImpl = window.fetch;
      window.fetch = (...args) => {
        const url = String(args[0] && args[0].url || args[0]);
        if (!url.includes("/api/meta")) return fetchImpl(...args);
        trace.push("meta:start");
        return fetchImpl(...args).then((response) => {
          trace.push("meta:end");
          return response;
        });
      };
      const getContext = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function(...args) {
        trace.push("chart:canvas");
        return getContext.apply(this, args);
      };
    })();'''
    cdp.open_page(base + "/?model=m-claude&agent=A1&project=P1",
                  preload_script=trace_script)
    cdp.wait_for(BOOTED_JS)

    typed_ids = cdp.eval('''(() => [
      ...[...document.querySelectorAll("#filter-model option")]
        .map((o) => ["model", o.value]).filter(([, id]) => id),
      ...[...document.querySelectorAll("#filter-agent option")]
        .map((o) => ["agent", o.value]).filter(([, id]) => id),
      ...[...document.querySelectorAll("#filter-project option")]
        .map((o) => ["project", o.value]).filter(([, id]) => id),
    ])()''')
    registered = cdp.eval('''(%s).map(([type, id]) => ({
      type, id, registered: colorRegistry.byKey.has(type + "\\u0000" + id),
    }))''' % json.dumps(typed_ids))
    assert registered and all(item["registered"] for item in registered)

    trace = cdp.eval("window.__aistat_boot_trace")
    assert trace.index("meta:end") < trace.index("chart:canvas")

    before = _registry_map(cdp, typed_ids)
    cdp.eval('''(() => {
      const select = document.getElementById("filter-model");
      for (const option of select.options) option.selected = option.value === "m-shared";
      select.dispatchEvent(new Event("change"));
    })()''')
    cdp.wait_for('state.models.length === 1 && state.models[0] === "m-shared"')
    cdp.eval('''(() => {
      const select = document.getElementById("filter-model");
      for (const option of select.options) option.selected = option.value === "m-claude";
      select.dispatchEvent(new Event("change"));
    })()''')
    cdp.wait_for('state.models.length === 1 && state.models[0] === "m-claude"')
    assert _registry_map(cdp, typed_ids) == before
    cdp.eval("refreshMeta().then(refreshAll)")
    cdp.wait_for(BOOTED_JS)
    assert _registry_map(cdp, typed_ids) == before
    cdp.eval("location.reload()")
    cdp.wait_for(BOOTED_JS)
    assert _registry_map(cdp, typed_ids) == before


def test_color_ledger_survives_live_meta_expansion_and_same_tab_reload(dashboard):
    """FAN-1320: append-only meta expansion keeps colors through reloads.

    The preload keeps the production ``refreshMeta().then(refreshAll)`` path
    intact while supplying the QA reproduction's first-seen and expanded meta
    sets. The fake EventSource only provides a deterministic real-browser
    ``update`` event; allocation, persistence and reload are production code.
    """
    cdp, base = dashboard
    preload_script = r'''(() => {
      const realFetch = window.fetch;
      window.fetch = (...args) => {
        const url = String(args[0] && args[0].url || args[0]);
        if (!url.includes("/api/meta")) return realFetch(...args);
        return realFetch(...args).then(async (response) => {
          const meta = await response.json();
          const expanded = sessionStorage.getItem("qa-meta-expanded") === "1";
          const reversed = sessionStorage.getItem("qa-meta-reversed") === "1";
          if (!expanded) meta.models = ["qa-sse-002"];
          else meta.models = reversed
            ? ["qa-sse-002", "qa-sse-000"]
            : ["qa-sse-000", "qa-sse-002"];
          return new Response(JSON.stringify(meta), {
            status: 200, headers: {"Content-Type": "application/json"},
          });
        });
      };
      class TestEventSource {
        constructor() {
          this.listeners = {};
          window.__qa_event_source = this;
        }
        addEventListener(name, callback) {
          (this.listeners[name] ||= []).push(callback);
        }
        emit(name, data) {
          for (const callback of this.listeners[name] || []) callback({data});
        }
      }
      window.EventSource = TestEventSource;
    })();'''
    cdp.open_page(base + "/", preload_script=preload_script)
    cdp.wait_for(BOOTED_JS)

    before = _registry_map(cdp, [["model", "qa-sse-002"]])
    payload = cdp.eval("JSON.parse(sessionStorage.getItem(COLOR_LEDGER_STORAGE_KEY))")
    assert payload["scope"] == "local"
    assert payload["assignments"]["model"]["qa-sse-002"] == before["model\u0000qa-sse-002"]

    cdp.eval('''(() => {
      sessionStorage.setItem("qa-meta-expanded", "1");
      window.__qa_event_source.emit("update", JSON.stringify({
        beat: {seq: 1, at: "2026-01-02T00:00:00Z"}, cycle: {id: "qa"},
      }));
    })()''')
    cdp.wait_for('colorRegistry.byKey.has("model\\u0000qa-sse-000")')
    expanded = _registry_map(cdp, [["model", "qa-sse-000"], ["model", "qa-sse-002"]])
    assert expanded["model\u0000qa-sse-002"] == before["model\u0000qa-sse-002"]
    assert expanded["model\u0000qa-sse-000"] != expanded["model\u0000qa-sse-002"]

    cdp.eval("window.__pre_reload = true; location.reload()")
    cdp.wait_for(f'window.__pre_reload === undefined && ({BOOTED_JS})')
    assert _registry_map(cdp, [["model", "qa-sse-000"], ["model", "qa-sse-002"]]) == expanded

    cdp.eval('sessionStorage.setItem("qa-meta-reversed", "1"); '
             'window.__pre_reload = true; location.reload()')
    cdp.wait_for(f'window.__pre_reload === undefined && ({BOOTED_JS})')
    assert _registry_map(cdp, [["model", "qa-sse-000"], ["model", "qa-sse-002"]]) == expanded


def test_color_ledger_clean_history_is_order_independent_and_scope_safe(dashboard):
    """A cleared ledger is deterministic, while wrong-scope data is dropped."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    ids = ["collision-6", "collision-9", "clean-history"]

    cdp.eval("clearColorLedger(); initializeColorLedger('local')")
    _register_colors(cdp, "model", ids)
    forward = _color_map(cdp, "model", ids)

    cdp.eval("clearColorLedger(); initializeColorLedger('local')")
    _register_colors(cdp, "model", list(reversed(ids)))
    reverse = _color_map(cdp, "model", ids)
    assert forward == reverse

    foreign = {
        "version": 1,
        "scope": "user:other",
        "assignments": {"model": {"foreign": "#4f6df5"}},
    }
    cdp.eval("sessionStorage.setItem(COLOR_LEDGER_STORAGE_KEY, %s)" % json.dumps(
        json.dumps(foreign)))
    cdp.eval("initializeColorLedger('local')")
    assert cdp.eval("sessionStorage.getItem(COLOR_LEDGER_STORAGE_KEY)") is None
    assert cdp.eval('colorRegistry.byKey.has("model\\u0000foreign")') is False


def test_color_ledger_rejects_corrupt_payload_without_trusting_anchors(dashboard):
    """Corrupt state cannot restore sentinel/reserved colors or Fable drift."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    corrupt = {
        "version": 99,
        "scope": "local",
        "assignments": {"model": {
            "claude-fable-5": "#0ea5e9",
            "bad-sentinel": "#cbd5e1",
        }},
    }
    cdp.eval("sessionStorage.setItem(COLOR_LEDGER_STORAGE_KEY, %s)" % json.dumps(
        json.dumps(corrupt)))
    cdp.eval("initializeColorLedger('local')")
    assert cdp.eval("sessionStorage.getItem(COLOR_LEDGER_STORAGE_KEY)") is None
    assert cdp.eval('entityColor("model", "claude-fable-5")') == "#ef4444"
    assert cdp.eval('entityColor("agent", null)') == "#cbd5e1"
    assert cdp.eval('colorRegistry.byKey.has("model\\u0000bad-sentinel")') is False


def test_color_ledger_storage_denial_uses_memory_fallback(dashboard):
    """A denied sessionStorage must warn but never prevent dashboard boot."""
    cdp, base = dashboard
    preload_script = r'''Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      get() { throw new Error("storage denied by QA"); },
    });'''
    cdp.open_page(base + "/", preload_script=preload_script)
    cdp.wait_for(BOOTED_JS)
    assert cdp.eval("colorRegistry.storage === null && colorRegistry.storageWarningShown") is True
    _register_colors(cdp, "model", ["storage-denied"])
    assert cdp.eval('entityColor("model", "storage-denied")') != "#cbd5e1"


def _probe_entity_colors(cdp, identities):
    """Return ``type:id -> color`` from the live page registry."""
    return cdp.eval(
        '(ids => Object.fromEntries(ids.map(([type, id]) => ['
        'type + ":" + (id === null ? "<null>" : id), '
        'entityColor(type, id)])))(' + json.dumps(identities) + ')')


def _fresh_entity_colors(cdp, base, identities):
    """Probe a new document with an empty result so only the fresh registry
    and the explicit probe identities can claim fallback slots."""
    cdp.open_page(base + "/?from=2030-01-01T00:00&to=2030-01-02T00:00")
    cdp.wait_for(BOOTED_JS)
    _reset_color_registry(cdp)
    _register_identity_batches(cdp, identities)
    return _probe_entity_colors(cdp, identities)


def _fresh_entity_color_snapshot(cdp, base, identities):
    """Return colors plus palette cardinality after probing a fresh registry."""
    cdp.open_page(base + "/?from=2030-01-01T00:00&to=2030-01-02T00:00")
    cdp.wait_for(BOOTED_JS)
    _reset_color_registry(cdp)
    _register_identity_batches(cdp, identities)
    return cdp.eval(
        '(ids => { const colors = Object.fromEntries(ids.map(([type, id]) => ['
        'type + ":" + (id === null ? "<null>" : id), '
        'entityColor(type, id)])); '
        'return {colors, unique: new Set(Object.values(colors)).size, '
        'paletteSize: PALETTE.length}; })(' + json.dumps(identities) + ')')


def test_fallback_colors_are_order_independent_across_fresh_pages(dashboard):
    """FAN-1315: collision-6/9, anchors, typed spaces and sentinels do not
    depend on which identity first touched a fresh browser registry."""
    cdp, base = dashboard
    identities = [
        ["model", "claude-fable-5"],
        ["model", "collision-6"],
        ["model", "collision-9"],
        ["model", "typed-shared"],
        ["agent", "typed-shared"],
        ["project", "typed-shared"],
        ["agent", None],
        ["project", ""],
    ]
    forward = _fresh_entity_colors(cdp, base, identities)
    reverse = _fresh_entity_colors(cdp, base, list(reversed(identities)))

    assert forward == reverse
    assert forward["model:claude-fable-5"] == "#ef4444"
    assert forward["model:collision-6"] != forward["model:collision-9"]
    assert "#ef4444" not in {
        forward["model:collision-6"], forward["model:collision-9"]}
    assert forward["agent:<null>"] == "#cbd5e1"
    assert forward["project:"] == "#cbd5e1"


def test_fallback_mapping_is_deterministic_after_palette_exhaustion(dashboard):
    """Once every non-anchor palette slot has been claimed, repeats remain
    stable and the full mapping is still independent of encounter order."""
    cdp, base = dashboard
    stable_ids = ["collision-6", "collision-9"] + [
        f"exhaustion-{index}" for index in range(14)]
    identities = [["model", stable_id] for stable_id in stable_ids]
    forward = _fresh_entity_color_snapshot(cdp, base, identities)
    reverse = _fresh_entity_color_snapshot(cdp, base, list(reversed(identities)))

    assert forward["colors"] == reverse["colors"]
    assert forward["colors"]["model:collision-6"] != \
        forward["colors"]["model:collision-9"]
    # More identities than palette slots exercise the repeated-color path;
    # the fixed palette bounds the number of distinct fallback colors.
    assert len(stable_ids) > forward["paletteSize"]
    assert forward["unique"] <= forward["paletteSize"]
    assert forward["unique"] < len(stable_ids)


def test_fallback_mapping_survives_filter_and_reload(dashboard):
    """A filter change keeps cached identity colors, and a reload with the
    filtered URL reconstructs the same mapping in a fresh registry."""
    cdp, base = dashboard
    cdp.open_page(base + "/?project=P1")
    cdp.wait_for(BOOTED_JS)
    identities = [["model", "collision-6"], ["model", "collision-9"]]
    before = _probe_entity_colors(cdp, identities)

    cdp.eval('''(() => {
      const select = document.getElementById("filter-project");
      for (const option of select.options) option.selected = option.value === "P2";
      select.dispatchEvent(new Event("change"));
    })()''')
    cdp.wait_for('state.projects.length === 1 && state.projects[0] === "P2"')
    assert _probe_entity_colors(cdp, identities) == before
    assert "project=P2" in cdp.eval("location.search")

    cdp.eval("window.__pre_reload = true; location.reload()")
    cdp.wait_for(f'window.__pre_reload === undefined && ({BOOTED_JS})')
    assert _probe_entity_colors(cdp, identities) == before


def test_entity_colors_follow_typed_identity(dashboard):
    """FAN-1237: color is a function of typed stable identity, not array
    position. Probed directly in real Chrome against the shipped registry."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    # Fable is anchored red in the model identity space, whatever the data order.
    assert cdp.eval('entityColor("model", "claude-fable-5")') == "#ef4444"
    # That red is reserved: no other model may be assigned it, and distinct
    # models get distinct colors (deterministic collision control).
    others = cdp.eval(
        '["claude-opus-4-8","gpt-5.6-sol","gpt-5.6-terra","m-claude","m-shared"]'
        '.map((m) => entityColor("model", m))')
    assert "#ef4444" not in others
    assert len(set(others)) == len(others)
    # A stable id keeps its color no matter when it is asked or what is assigned
    # around it — position/order never enters into it.
    stable = cdp.eval(
        '(() => { const a = entityColor("model", "probe-x");'
        ' entityColor("model", "probe-y"); entityColor("model", "probe-z");'
        ' return a === entityColor("model", "probe-x"); })()')
    assert stable is True
    # Unknown / unattributed identity gets the explicit sentinel, never a
    # palette slot.
    assert cdp.eval('entityColor("agent", null)') == "#cbd5e1"
    assert cdp.eval('entityColor("project", "")') == "#cbd5e1"


def test_daily_model_colors_match_across_metric_charts(dashboard):
    """The exact reported bug (Fable red in the tokens chart, green in the cost
    chart): a model must be one color on both daily charts, and that color must
    be the identity registry's — not derived from its position in each chart's
    own metric sort."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    tokens = _chart_colors(cdp, "chart-daily-tokens")
    cost = _chart_colors(cdp, "chart-daily-cost")
    assert tokens and set(tokens) == set(cost)
    for model, color in tokens.items():
        assert cost[model] == color
        assert cdp.eval('entityColor("model", %s)' % json.dumps(model)) == color


def test_model_colors_survive_group_switch(dashboard):
    """Switching the daily grouping to agent and back to model leaves every
    model's color untouched — the registry caches by identity, not position."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    before = _chart_colors(cdp, "chart-daily-tokens")
    assert "m-shared" in before
    change_group = '''((v) => {
      const s = document.getElementById("filter-group");
      s.value = v; s.dispatchEvent(new Event("change"));
    })'''
    cdp.eval(f'{change_group}("agent")')
    cdp.wait_for('state.group === "agent" && state.charts["chart-daily-tokens"]'
                 '.data.datasets.some((d) => d.label === "Solo Claude")')
    cdp.eval(f'{change_group}("model")')
    cdp.wait_for('state.group === "model" && state.charts["chart-daily-tokens"]'
                 '.data.datasets.some((d) => d.label === "m-shared")')
    assert _chart_colors(cdp, "chart-daily-tokens") == before


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
