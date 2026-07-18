"""Real-browser dashboard regression (FAN-1255): URL filter-state restore,
recovery from invalid links, valid-empty ranges and the full reset.

Runs the actual dashboard (FastAPI + static files + Chart.js) in headless
Chrome, driven over the DevTools protocol through ``--remote-debugging-pipe``
— stdlib only, no webdriver/playwright dependency. The DevTools client, its
task-owned HOME/TMPDIR/profile isolation and the ``--use-mock-keychain``
clean-HOME fix (FAN-1346) live in ``cdp_harness``; the pure protocol/deadline/
cleanup tests that need no browser live in ``test_cdp_protocol``. The suite
skips cleanly on machines without a Chrome/Chromium binary; everything else in
the test run stays unaffected.
"""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

import aistat.server as server_module
from aistat.config import Config
from aistat.db import connect, init_db
from conftest import seed_aggregate_fixture
from cdp_harness import (
    BOOTED_JS, CHROME, DashboardSession, NO_CHROME_REASON, launch_chrome)

pytestmark = pytest.mark.skipif(CHROME is None, reason=NO_CHROME_REASON)


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def dashboard():
    """The dashboard on a real HTTP port over a seeded database, plus one
    headless Chrome the tests navigate page-by-page. Every resource is owned by
    a :class:`DashboardSession` so teardown stays failure-safe and idempotent."""
    import uvicorn

    session = DashboardSession()
    try:
        session.tmp = tempfile.TemporaryDirectory(prefix="aistat-browser-")
        config = Config()
        config.db_path = Path(session.tmp.name) / "browser.db"
        config.credits_per_usd = 2.0
        conn = connect(config.db_path)
        init_db(conn)
        seed_aggregate_fixture(conn)
        conn.close()

        port = _free_port()
        session.server = uvicorn.Server(uvicorn.Config(
            server_module.create_app(config), host="127.0.0.1", port=port,
            log_level="warning"))
        session.thread = threading.Thread(target=session.server.run, daemon=True)
        session.thread.start()
        deadline = time.monotonic() + 15
        while not session.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)

        session.cdp = launch_chrome(CHROME)
        yield session.cdp, f"http://127.0.0.1:{port}"
    finally:
        session.close()


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


def test_open_page_navigates_the_attached_target(dashboard):
    """A fresh target must reach the requested page after its flat session
    is attached; creating a target alone is not sufficient evidence."""
    cdp, base = dashboard
    requested_url = base + "/?project=P1&agent=A1"
    cdp.open_page(requested_url)
    cdp.wait_for(BOOTED_JS)
    assert cdp.eval("location.href") == requested_url
    assert cdp.eval('document.getElementById("card-tokens") !== null') is True


def test_navigation_timeout_includes_target_diagnostics(dashboard):
    """A bounded adapter failure identifies the page and flat session that
    failed instead of only reporting a generic CDP timeout."""
    cdp, base = dashboard
    requested_url = base + "/?project=P1"
    cdp.open_page(requested_url)
    cdp.wait_for(BOOTED_JS)
    with pytest.raises(TimeoutError) as failure:
        cdp.wait_for("false", timeout=0)
    message = str(failure.value)
    assert "requested_url=%r" % requested_url in message
    assert "target_id=%r" % cdp._target_id in message
    assert "session_id=%r" % cdp.session_id in message


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
    requested_url = base + "/?model=m-claude&agent=A1&project=P1"
    cdp.open_page(requested_url, preload_script=trace_script)
    cdp.wait_for(BOOTED_JS)
    assert cdp.eval("location.href") == requested_url
    assert cdp.eval('document.getElementById("card-tokens") !== null') is True

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
    cdp.eval("window.__aistat_pre_reload = true; location.reload()")
    cdp.wait_for(f"window.__aistat_pre_reload === undefined && ({BOOTED_JS})")
    assert _registry_map(cdp, typed_ids) == before


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
