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
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

import aistat.server as server_module
import aistat.wsgi as public_wsgi_module
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

        # Fold the Uvicorn thread's liveness into any CDP stall diagnostic, so
        # a long-lived-session timeout can tell a wedged browser apart from a
        # dead test server (FAN-1352).
        def _server_context(session=session):
            thread = session.thread
            return {
                "server_thread_alive": thread.is_alive() if thread else None,
                "server_should_exit": getattr(session.server, "should_exit", None),
            }

        session.cdp = launch_chrome(CHROME, context=_server_context)
        yield session.cdp, f"http://127.0.0.1:{port}"
    finally:
        session.close()


@pytest.fixture
def public_page():
    """The public Flask login page on a real HTTP port and headless Chrome."""
    with tempfile.TemporaryDirectory(prefix="aistat-public-browser-") as path:
        root = Path(path)
        config = Config(
            db_path=root / "public.db",
            security_db_path=root / "security.db",
            tenants_dir=root / "tenants",
            auth_username="browser",
            auth_password_hash=generate_password_hash(
                "not-used", method="pbkdf2:sha256:600000"
            ),
            session_secret="session-" + "s" * 48,
            ingest_secret="ingest-" + "i" * 48,
            allowed_hosts=("127.0.0.1",),
            force_https=False,
        )
        port = _free_port()
        server = make_server("127.0.0.1", port,
                             public_wsgi_module.create_app(config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cdp = launch_chrome(CHROME)
        try:
            yield cdp, f"http://127.0.0.1:{port}", config
        finally:
            cdp.close()
            server.shutdown()
            thread.join(timeout=10)


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


def _locale_preload(language):
    return (
        'Object.defineProperty(Navigator.prototype, "language", '
        f'{{configurable: true, get: () => {json.dumps(language)}}}); '
        'if (sessionStorage.getItem("aistat.test.locale-ready") !== "1") { '
        'localStorage.removeItem("aistat.locale"); '
        'sessionStorage.setItem("aistat.test.locale-ready", "1"); }'
    )


def _connection_preload(status="none", unsupported=False):
    """Keep the real dashboard while making the connection lifecycle explicit."""
    payload = {"status": status} if isinstance(status, str) else status
    response = "return new Response('', {status: 404});" if unsupported else (
        "return new Response(JSON.stringify(%s), {headers: "
        "{'Content-Type': 'application/json'}});" % json.dumps(payload)
    )
    return r'''(() => {
      const originalFetch = window.fetch.bind(window);
      window.__aistat_connection_gets = 0;
      window.__aistat_connection_posts = 0;
      window.fetch = (input, options = {}) => {
        const url = new URL(typeof input === "string" ? input : input.url, location.href);
        if (url.pathname === "/api/connection") {
          if ((options.method || "GET") !== "GET") {
            window.__aistat_connection_posts += 1;
            return Promise.resolve(new Response(JSON.stringify({status: "pending"}), {
              headers: {"Content-Type": "application/json"},
            }));
          }
          window.__aistat_connection_gets += 1;
          %s
        }
        if (url.pathname === "/api/connection/revoke") {
          return Promise.resolve(new Response(JSON.stringify({status: "revocation_pending"}), {
            headers: {"Content-Type": "application/json"},
          }));
        }
        return originalFetch(input, options);
      };
    })();''' % response


def _browser_key(cdp, key, code, key_code, modifiers=0):
    params = {
        "key": key, "code": code, "windowsVirtualKeyCode": key_code,
        "nativeVirtualKeyCode": key_code, "modifiers": modifiers,
    }
    cdp.call("Input.dispatchKeyEvent", dict(params, type="keyDown"))
    cdp.call("Input.dispatchKeyEvent", dict(params, type="keyUp"))


def _connection_focus(cdp):
    return cdp.eval('''(() => {
      const dialog = document.getElementById("connection-cabinet");
      const active = document.activeElement;
      const style = getComputedStyle(active);
      const rect = active.getBoundingClientRect();
      return {contained: dialog.contains(active), disabled: !!active.disabled,
              hidden: active.hidden || !!active.closest("[hidden]"),
              display: style.display, visibility: style.visibility,
              width: rect.width, height: rect.height};
    })()''')


def _visible_connection_elements(cdp):
    return cdp.eval('''(() => {
      const dialog = document.getElementById("connection-cabinet");
      const visible = (id) => {
        const element = document.getElementById(id);
        const rect = element.getBoundingClientRect();
        return getComputedStyle(element).display !== "none"
          && rect.width > 0 && rect.height > 0;
      };
      const hidden = [...dialog.querySelectorAll("[hidden]")];
      return {
        elements: Object.fromEntries([
          "connection-form", "connection-actions", "connection-empty",
          "connection-error", "connection-advice", "connection-workspace-detail",
          "connection-synced-detail"
        ].map((id) => [id, visible(id)])),
        hidden: hidden.map((element) => ({
          display: getComputedStyle(element).display,
          boxes: element.getClientRects().length,
        })),
        concealed: [...dialog.querySelectorAll("*")]
          .filter((element) => element.closest("[hidden]"))
          .map((element) => ({
            boxes: element.getClientRects().length,
          })),
      };
    })()''')


def test_open_page_navigates_the_attached_target(dashboard):
    """A fresh target must reach the requested page after its flat session
    is attached; creating a target alone is not sufficient evidence."""
    cdp, base = dashboard
    requested_url = base + "/?project=P1&agent=A1"
    cdp.open_page(requested_url)
    cdp.wait_for(BOOTED_JS)
    assert cdp.eval("location.href") == requested_url
    assert cdp.eval('document.getElementById("card-tokens") !== null') is True


def test_default_english_localizes_static_and_dynamic_dashboard_copy(dashboard):
    """A non-Russian browser starts in English without a page reload."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    assert cdp.eval("document.documentElement.lang") == "en"
    assert cdp.eval("document.title") == "AIStat — Multica token statistics"
    assert cdp.eval('document.getElementById("locale-switcher").getAttribute("aria-label")') == "Interface language: English"
    assert cdp.eval('document.querySelector(".filters").getAttribute("aria-label")') == "Filters"
    assert cdp.eval('document.querySelector(".connection-host").textContent') == "Official host: https://multica.ai"
    assert cdp.eval('document.querySelector(".connection-host a").href') == "https://multica.ai/"
    assert cdp.eval('document.body.innerText.match(/[А-Яа-яЁё]/g) || []') == []
    cdp.eval('document.getElementById("locale-switcher").click()')
    cdp.wait_for('document.documentElement.lang === "ru"')
    assert cdp.eval('document.querySelector(".filters").getAttribute("aria-label")') == "Фильтры"
    assert cdp.eval('document.querySelector(".connection-host").textContent') == "Официальный хост: https://multica.ai"
    assert cdp.eval('document.querySelector(".connection-host a").href') == "https://multica.ai/"
    cdp.eval('document.getElementById("locale-switcher").click()')
    cdp.wait_for('document.documentElement.lang === "en"')


def test_connection_dialog_is_accessible_and_resets_unsent_pat(dashboard):
    cdp, base = dashboard
    cdp.open_page(base + "/", preload_script=_connection_preload())
    cdp.wait_for(BOOTED_JS)
    cdp.wait_for('!document.getElementById("connection-trigger").hidden')
    assert cdp.eval('document.getElementById("connection-cabinet").open') is False

    cdp.eval('document.getElementById("connection-trigger").click()')
    cdp.wait_for('document.getElementById("connection-cabinet").open')
    assert cdp.eval('document.activeElement.id') == "connection-token"
    assert cdp.eval('document.getElementById("connection-cabinet").getAttribute("aria-modal")') == "true"
    _press_key(cdp, "Tab", shiftKey=True)
    assert cdp.eval('document.getElementById("connection-cabinet").contains(document.activeElement)') is True
    _press_key(cdp, "Tab")
    assert cdp.eval('document.activeElement.id') == "connection-token"

    cdp.eval('document.getElementById("connection-form").requestSubmit()')
    cdp.wait_for('!document.getElementById("connection-form-error").hidden')
    assert cdp.eval('window.__aistat_connection_posts') == 0

    cdp.eval('document.getElementById("connection-token").value = "unsent-test-pat"')
    _browser_key(cdp, "Escape", "Escape", 27)
    cdp.wait_for('!document.getElementById("connection-cabinet").open')
    assert cdp.eval('document.getElementById("connection-cabinet").hidden') is True
    assert cdp.eval('document.activeElement.id') == "connection-trigger"
    assert cdp.eval('document.getElementById("connection-token").value') == ""

    try:
        for width in (390, 900, 1440):
            cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": 900, "deviceScaleFactor": 1,
                "mobile": False,
            })
            cdp.eval('document.getElementById("connection-trigger").click()')
            box = cdp.eval('''(() => {
              const dialog = document.getElementById("connection-cabinet");
              const rect = dialog.getBoundingClientRect();
              return {left: rect.left, right: rect.right, width: rect.width,
                      scrollWidth: dialog.scrollWidth,
                      clientWidth: dialog.clientWidth,
                      viewportWidth: window.innerWidth};
            })()''')
            assert box["left"] >= 0
            assert box["right"] <= box["viewportWidth"]
            assert box["scrollWidth"] <= box["clientWidth"]
            cdp.eval('document.getElementById("connection-close").click()')
    finally:
        cdp.call("Emulation.clearDeviceMetricsOverride")


def test_connection_dialog_manages_active_and_pending_states(dashboard):
    cdp, base = dashboard
    cdp.open_page(base + "/", preload_script=_connection_preload("active"))
    cdp.wait_for(BOOTED_JS)
    cdp.wait_for('state.connection.status === "active"')
    cdp.wait_for('!document.getElementById("connection-trigger").hidden')
    cdp.eval('document.getElementById("connection-trigger").click()')
    cdp.wait_for('document.getElementById("connection-cabinet").open')
    assert cdp.eval('document.activeElement.id') == "connection-replace"

    cdp.eval('document.getElementById("connection-replace").click()')
    cdp.wait_for('!document.getElementById("connection-form").hidden')
    assert cdp.eval('document.activeElement.id') == "connection-token"
    cdp.eval('document.getElementById("connection-close").click()')
    cdp.wait_for('!document.getElementById("connection-cabinet").open')

    cdp.eval('document.getElementById("connection-trigger").click()')
    cdp.wait_for('!document.getElementById("connection-actions").hidden')
    cdp.eval('document.getElementById("connection-disconnect").click()')
    cdp.wait_for('!document.getElementById("connection-confirm").hidden')
    assert cdp.eval('document.activeElement.id') == "connection-confirm-no"
    cdp.eval('document.getElementById("connection-confirm-no").click()')
    assert cdp.eval('document.getElementById("connection-confirm").hidden') is True
    assert cdp.eval('document.activeElement.id') == "connection-disconnect"

    cdp.open_page(base + "/", preload_script=_connection_preload("pending"))
    cdp.wait_for(BOOTED_JS)
    cdp.wait_for('window.__aistat_connection_gets > 1')
    assert cdp.eval('document.getElementById("connection-cabinet").open') is False
    assert cdp.eval('state.connection.status') == "pending"


def test_connection_dialog_focus_and_visibility_cover_all_states(dashboard):
    cdp, base = dashboard
    states = (
        ("none", True, False, True, False, False, False, False),
        ("disabled", True, False, False, False, False, False, False),
        ("pending", False, False, False, False, False, False, False),
        ("replacement_pending", False, False, False, False, False, False, False),
        ({"status": "active", "workspace_label": "Main", "last_synced_at": 1},
         False, True, False, False, False, True, True),
        ("error", False, True, False, True, False, False, False),
        ("revocation_pending", False, False, False, False, True, False, False),
        ("revoked", True, False, True, False, True, False, False),
    )
    for status, form, actions, empty, error, advice, workspace, synced in states:
        cdp.open_page(base + "/", preload_script=_connection_preload(status))
        cdp.wait_for(BOOTED_JS)
        current = status if isinstance(status, str) else status["status"]
        cdp.wait_for('state.connection.status === %s' % json.dumps(current))
        cdp.eval('document.getElementById("connection-trigger").click()')
        cdp.wait_for('document.getElementById("connection-cabinet").open')
        focus = _connection_focus(cdp)
        assert focus["contained"] is True
        assert focus["disabled"] is False
        assert focus["hidden"] is False
        assert focus["display"] != "none"
        assert focus["visibility"] == "visible"
        assert focus["width"] > 0
        assert focus["height"] > 0
        visibility = _visible_connection_elements(cdp)
        assert visibility["elements"] == {
            "connection-form": form,
            "connection-actions": actions,
            "connection-empty": empty,
            "connection-error": error,
            "connection-advice": advice,
            "connection-workspace-detail": workspace,
            "connection-synced-detail": synced,
        }
        assert all(item["display"] == "none" and item["boxes"] == 0
                   for item in visibility["hidden"])
        assert all(item["boxes"] == 0 for item in visibility["concealed"])


def test_connection_dialog_keeps_focus_during_connection_operations(dashboard):
    cdp, base = dashboard
    operations = (
        ("none", "/api/connection", "pending", "connect"),
        ("active", "/api/connection", "replacement_pending", "replace"),
        ("active", "/api/connection/revoke", "revocation_pending", "revoke"),
    )
    for initial, path, pending, operation in operations:
        cdp.open_page(base + "/", preload_script=_connection_preload(initial))
        cdp.wait_for(BOOTED_JS)
        cdp.wait_for('state.connection.status === %s' % json.dumps(initial))
        cdp.eval('''(() => {
          const originalFetch = window.fetch;
          window.fetch = (input, options = {}) => {
            const url = new URL(typeof input === "string" ? input : input.url, location.href);
            if (url.pathname === "/api/connection" && window.__connection_post_status
                && (options.method || "GET") === "GET") {
              return Promise.resolve(new Response(JSON.stringify({status: window.__connection_post_status}), {
                headers: {"Content-Type": "application/json"},
              }));
            }
            if (url.pathname === %s && (options.method || "GET") === "POST") {
              return new Promise((resolve) => {
                window.__resolve_connection_post = () => {
                  window.__connection_post_status = %s;
                  resolve(new Response(JSON.stringify({status: %s}),
                    {headers: {"Content-Type": "application/json"}}));
                };
              });
            }
            return originalFetch(input, options);
          };
        })()''' % (json.dumps(path), json.dumps(pending), json.dumps(pending)))
        cdp.eval('document.getElementById("connection-trigger").click()')
        cdp.wait_for('document.getElementById("connection-cabinet").open')
        if operation == "replace":
            cdp.eval('document.getElementById("connection-replace").click()')
        if operation == "revoke":
            cdp.eval('document.getElementById("connection-disconnect").click()')
            cdp.eval('document.getElementById("connection-confirm-yes").click()')
        else:
            cdp.eval('document.getElementById("connection-token").value = "test-pat"')
            cdp.eval('document.getElementById("connection-form").requestSubmit()')
        cdp.wait_for('state.connectionBusy')
        assert _connection_focus(cdp)["contained"] is True
        assert cdp.eval('document.activeElement.id') == "connection-close"
        cdp.eval('window.__resolve_connection_post()')
        cdp.wait_for('!state.connectionBusy && state.connection.status === %s'
                     % json.dumps(pending))
        assert _connection_focus(cdp)["contained"] is True
        assert cdp.eval('document.activeElement.id') == "connection-close"


def test_connection_dialog_hides_unsupported_surface(dashboard):
    cdp, base = dashboard
    cdp.open_page(base + "/", preload_script=_connection_preload(unsupported=True))
    cdp.wait_for(BOOTED_JS)
    cdp.wait_for('state.connectionSupported === false')
    assert cdp.eval('document.getElementById("connection-trigger").hidden') is True
    assert cdp.eval('document.getElementById("connection-cabinet").hidden') is True


def test_filter_panel_resizes_multiselects_and_stacks_without_overflow(dashboard):
    """The sidebar stays beside content on desktop, then stacks cleanly.

    The synthetic meta payload deliberately gives every multi-select more than
    its former three rows; the second payload arrives through the production
    SSE handler.
    """
    cdp, base = dashboard
    preload_script = r'''(() => {
      const originalFetch = window.fetch;
      window.__aistat_meta_phase = 0;
      const meta = () => {
        const count = window.__aistat_meta_phase ? 5 : 4;
        const items = (prefix) => Array.from(
          {length: count}, (_, index) => `${prefix}-${index + 1}`);
        return {
          projects: items("project").map((id) => ({id, title: id})),
          agents: items("agent").map((id) => ({id, name: id})),
          models: items("model"),
          date_span: {first: "2026-01-01", last: "2026-01-02"},
        };
      };
      window.fetch = (...args) => {
        const url = String(args[0] && args[0].url || args[0]);
        if (new URL(url, location.href).pathname === "/api/meta") {
          return Promise.resolve(new Response(JSON.stringify(meta()), {
            headers: {"Content-Type": "application/json"},
          }));
        }
        return originalFetch(...args);
      };
      class TestEventSource {
        constructor() {
          this.listeners = {};
          window.__aistat_events = this;
          queueMicrotask(() => this.onopen && this.onopen());
        }
        addEventListener(type, listener) { this.listeners[type] = listener; }
        emit(type, data) { this.listeners[type]({data}); }
      }
      window.EventSource = TestEventSource;
    })();'''
    snapshot = '''(() => {
      const box = (node) => {
        const rect = node.getBoundingClientRect();
        return {left: rect.left, right: rect.right, top: rect.top,
                bottom: rect.bottom, width: rect.width, height: rect.height};
      };
      const filters = document.querySelector(".filters");
      return {
        filters: box(filters),
        content: box(document.querySelector(".dashboard-content")),
        scrollWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
        selects: [...filters.querySelectorAll("select[multiple]")].map((select) => ({
          options: select.options.length, size: select.size,
          scrollHeight: select.scrollHeight, clientHeight: select.clientHeight,
          scrollWidth: select.scrollWidth, clientWidth: select.clientWidth,
        })),
        controls: [...filters.querySelectorAll("select, input, button")].map(box),
        filterScrollHeight: filters.scrollHeight,
        filterClientHeight: filters.clientHeight,
      };
    })()'''
    try:
        for width in (1440, 900, 390):
            cdp.open_page(base + "/", preload_script=preload_script)
            cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": 900, "deviceScaleFactor": 1,
                "mobile": False,
            })
            cdp.wait_for(BOOTED_JS)
            layout = cdp.eval(snapshot)
            assert layout["scrollWidth"] <= layout["viewportWidth"]
            assert layout["filterScrollHeight"] <= layout["filterClientHeight"]
            assert all(item["options"] == item["size"] == 5
                       for item in layout["selects"])
            assert all(item["scrollHeight"] <= item["clientHeight"] and
                       item["scrollWidth"] <= item["clientWidth"]
                       for item in layout["selects"])
            assert all(item["left"] >= 0 and
                       item["right"] <= layout["viewportWidth"]
                       for item in layout["controls"])
            if width == 1440:
                assert layout["filters"]["right"] < layout["content"]["left"]
            else:
                assert layout["filters"]["bottom"] <= layout["content"]["top"]

        cdp.eval('''window.__aistat_meta_phase = 1;
          window.__aistat_events.emit("update", JSON.stringify({
            beat: {seq: 2, at: "2026-01-02T00:00:00Z"},
            cycle: {id: "meta-refresh"},
          }));''')
        cdp.wait_for(
            'state.lastSyncMarker === "2:meta-refresh" && '
            '[...document.querySelectorAll(".filters select[multiple]")]'
            '.every((select) => select.options.length === 6 && select.size === 6)'
        )
        layout = cdp.eval(snapshot)
        assert all(item["scrollHeight"] <= item["clientHeight"] and
                   item["scrollWidth"] <= item["clientWidth"]
                   for item in layout["selects"])
    finally:
        cdp.call("Emulation.clearDeviceMetricsOverride")


def test_public_login_localizes_and_persists_browser_locale(public_page):
    cdp, base, _ = public_page
    cdp.open_page(
        base + "/login",
        preload_script=_locale_preload("ru-RU"),
    )
    cdp.wait_for('document.getElementById("locale-switcher") !== null', timeout=10)
    assert cdp.eval("document.documentElement.lang") == "ru"
    assert cdp.eval("document.title") == "Вход — AIStat"
    assert cdp.eval('document.querySelectorAll("#locale-switcher").length') == 1

    cdp.eval('document.getElementById("locale-switcher").click()')
    cdp.wait_for('document.documentElement.lang === "en"')
    assert cdp.eval("document.title") == "Sign in — AIStat"
    assert cdp.eval('document.body.innerText.match(/[А-Яа-яЁё]/g) || []') == []

    cdp.eval('document.getElementById("locale-switcher").focus()')
    _press_key(cdp, "Enter")
    cdp.wait_for('document.documentElement.lang === "ru"')
    cdp.eval('document.getElementById("locale-switcher").focus()')
    _press_key(cdp, " ")
    cdp.wait_for('document.documentElement.lang === "en"')
    assert cdp.eval('localStorage.getItem("aistat.locale")') == "en"
    cdp.eval("window.__aistat_pre_reload = true; location.reload()")
    cdp.wait_for(
        'window.__aistat_pre_reload === undefined && '
        'document.getElementById("locale-switcher") !== null'
    )
    assert cdp.eval("document.documentElement.lang") == "en"
    assert cdp.eval('localStorage.getItem("aistat.locale")') == "en"

    cdp.open_page(
        base + "/login",
        preload_script=_locale_preload("en-US"),
    )
    cdp.wait_for('document.getElementById("locale-switcher") !== null')
    assert cdp.eval("document.documentElement.lang") == "en"
    assert cdp.eval("document.title") == "Sign in — AIStat"


def test_public_registration_closed_localizes_both_languages(
    public_page, monkeypatch
):
    cdp, base, config = public_page
    config.oauth_providers = {"google": object()}

    def reject_registration(*args, **kwargs):
        raise public_wsgi_module.oauth.RegistrationClosedError(
            "registration is closed"
        )

    monkeypatch.setattr(
        public_wsgi_module.oauth, "finish", reject_registration
    )
    cdp.open_page(
        base + "/auth/google/callback",
        preload_script=_locale_preload("ru-RU"),
    )
    cdp.wait_for(
        'typeof I18N === "object" && '
        'document.getElementById("locale-switcher") !== null'
    )
    assert cdp.eval(
        'performance.getEntriesByType("navigation")[0].responseStatus'
    ) == 403
    assert cdp.eval(
        'performance.getEntriesByType("resource").some('
        'entry => new URL(entry.name).pathname === "/i18n.js")'
    ) is True
    assert cdp.eval('document.querySelectorAll("#locale-switcher").length') == 1
    assert cdp.eval("document.documentElement.lang") == "ru"
    assert cdp.eval("document.title") == "Регистрация закрыта — AIStat"
    assert cdp.eval('document.querySelector(".subtitle").textContent') == (
        "Регистрация сейчас закрыта. Чтобы получить доступ, "
        "обратитесь к администратору."
    )
    assert cdp.eval('document.querySelector(".login-card p a").textContent') == (
        "Вернуться ко входу"
    )
    assert cdp.eval('document.querySelector(".login-card p a").href') == (
        base + "/login"
    )

    cdp.eval('document.getElementById("locale-switcher").click()')
    cdp.wait_for('document.documentElement.lang === "en"')
    assert cdp.eval("document.title") == "Registration closed — AIStat"
    assert cdp.eval('document.querySelector(".subtitle").textContent') == (
        "Registration is currently closed. Contact an administrator "
        "to get access."
    )
    assert cdp.eval('document.querySelector(".login-card p a").textContent') == (
        "Back to sign in"
    )
    assert cdp.eval('document.body.innerText.match(/[А-Яа-яЁё]/g) || []') == []


def _press_key(cdp, key, **options):
    event = {"key": key, "bubbles": True, "cancelable": True}
    event.update(options)
    cdp.eval("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', %s))" %
             json.dumps(event))


def _settled_card_tokens(cdp, lang, unit):
    """The token card text once a locale swap has fully landed.

    ``aistat:localechange`` flips ``html[lang]`` synchronously but rebuilds the
    cards asynchronously (``refreshMeta().then(refreshAll)``), and ``fmtTokens``
    formats through ``I18N.tag`` — so the localized unit is the signal that the
    dynamic re-render finished, and the returned text is compared against the
    exact representation expected for ``lang`` (never against another locale).
    """
    cdp.wait_for(f'document.documentElement.lang === "{lang}" && '
                 'document.getElementById("card-tokens").textContent'
                 f'.includes({json.dumps(unit)})')
    return cdp.eval('document.getElementById("card-tokens").textContent')


def test_language_switcher_keyboard_persistence_and_state(dashboard):
    """The native button keeps browser state while updating dynamic copy."""
    cdp, base = dashboard
    # A deterministic English start: the same seeded 3.4 M total for P1 reads
    # as "≈ 3.4 M" in English and "≈ 3,4 млн" in Russian.
    cdp.open_page(base + "/?project=P1",
                  preload_script=_locale_preload("en-US"))
    cdp.wait_for(BOOTED_JS)
    assert _settled_card_tokens(cdp, "en", " M") == "≈ 3.4 M"
    cdp.eval('document.querySelector(".chart-data").open = true; document.getElementById("locale-switcher").focus()')
    assert cdp.eval("document.activeElement.id") == "locale-switcher"

    _press_key(cdp, "Enter")
    assert _settled_card_tokens(cdp, "ru", "млн") == "≈ 3,4 млн"
    assert cdp.eval("document.title") == "AIStat — статистика токенов Multica"
    assert cdp.eval('localStorage.getItem("aistat.locale")') == "ru"
    assert _selected(cdp, "filter-project") == ["P1"]
    assert cdp.eval('document.querySelector(".chart-data").open') is True
    assert cdp.eval('document.getElementById("locale-switcher").getAttribute("aria-label")') == "Язык интерфейса: Русский"

    cdp.eval('document.getElementById("locale-switcher").focus()')
    _press_key(cdp, " ")
    assert _settled_card_tokens(cdp, "en", " M") == "≈ 3.4 M"
    cdp.wait_for('document.getElementById("efficiency-time-title").textContent.includes("Efficiency over time")')
    assert _selected(cdp, "filter-project") == ["P1"]

    cdp.eval('document.getElementById("locale-switcher").click()')
    cdp.wait_for('document.documentElement.lang === "ru"')
    # The marker dies with the old document, so the booted condition below can
    # only match the freshly reloaded page — plain BOOTED_JS also holds on the
    # pre-reload document and races the new boot (QA-FAN1938-01).
    cdp.eval("window.__pre_reload = true; location.reload()")
    cdp.wait_for(f'window.__pre_reload === undefined && ({BOOTED_JS})')
    # Then wait for the restored state itself before asserting exact values.
    cdp.wait_for('document.documentElement.lang === "ru" && '
                 '[...document.getElementById("filter-project").selectedOptions]'
                 '.map((o) => o.value).join(",") === "P1"')
    assert _settled_card_tokens(cdp, "ru", "млн") == "≈ 3,4 млн"
    assert cdp.eval("document.documentElement.lang") == "ru"
    assert _selected(cdp, "filter-project") == ["P1"]


def test_language_switcher_relocalizes_visible_filter_error(dashboard):
    """An existing URL-state warning changes language without clearing state."""
    cdp, base = dashboard
    cdp.open_page(base + "/?group=bogus")
    cdp.wait_for(BOOTED_JS)
    if cdp.eval("document.documentElement.lang") != "ru":
        cdp.eval('document.getElementById("locale-switcher").click()')
        cdp.wait_for('document.documentElement.lang === "ru"')
    assert "сброшены" in _filter_error(cdp)
    cdp.eval('document.getElementById("locale-switcher").click()')
    cdp.wait_for('document.documentElement.lang === "en"')
    cdp.wait_for('document.getElementById("filter-error").textContent.includes("Invalid filter parameters")')
    assert cdp.eval("location.search") == ""
    cdp.eval('document.getElementById("locale-switcher").click()')
    cdp.wait_for('document.documentElement.lang === "ru"')


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


def test_configurable_chart_catalog_selection_url_and_accessible_table(dashboard):
    cdp, base = dashboard
    cdp.open_page(base + "/?chart_x=agent&chart_y=agent_work_seconds")
    cdp.wait_for(BOOTED_JS)
    cdp.wait_for('Boolean(state.charts["chart-configurable"])')
    assert _element_value(cdp, "chart-dimension") == "agent"
    assert _element_value(cdp, "chart-measure") == "agent_work_seconds"
    catalog = cdp.eval('''(() => ({
      dimensions: [...document.getElementById("chart-dimension").options].map((o) => o.value),
      measures: [...document.getElementById("chart-measure").options].map((o) => [o.value, o.disabled]),
      type: state.charts["chart-configurable"].config.type,
      canvas: document.getElementById("chart-configurable").getAttribute("aria-label"),
    }))()''')
    assert catalog["dimensions"] == ["time", "project", "agent", "model", "issue"]
    assert len(catalog["measures"]) == 14
    assert dict(catalog["measures"])["task_count"] is True
    assert catalog["type"] == "bar"
    assert ("Агент" in catalog["canvas"] and "Время работы" in catalog["canvas"]) or (
        "Agent" in catalog["canvas"] and "Agent work time" in catalog["canvas"]
    )

    cdp.eval('''(() => {
      const select = document.getElementById("chart-dimension");
      select.value = "time";
      select.dispatchEvent(new Event("change"));
    })()''')
    cdp.wait_for('state.chartDimension === "time" && state.charts["chart-configurable"].config.type === "line"')
    cdp.eval('''(() => {
      const select = document.getElementById("chart-measure");
      select.value = "tokens_per_sp";
      select.dispatchEvent(new Event("change"));
    })()''')
    cdp.wait_for('!location.search.includes("chart_x") && location.search.includes("chart_y=tokens_per_sp")')
    cdp.eval('document.querySelector("#configurable-chart-panel details").open = true')
    assert cdp.eval('document.querySelectorAll("#table-configurable-chart tbody tr").length') > 0

    cdp.eval("window.__chart_reload = true; location.reload()")
    cdp.wait_for(f'window.__chart_reload === undefined && ({BOOTED_JS})')
    assert _element_value(cdp, "chart-dimension") == "time"
    assert _element_value(cdp, "chart-measure") == "tokens_per_sp"

    cdp.eval('''(() => {
      const select = document.getElementById("filter-project");
      for (const option of select.options) option.selected = option.value === "P2";
      select.dispatchEvent(new Event("change"));
    })()''')
    cdp.wait_for('!document.getElementById("empty-configurable-chart").hidden')

    cdp.eval('''(() => {
      const original = window.fetch.bind(window);
      window.fetch = (input, options) => {
        const url = new URL(typeof input === "string" ? input : input.url, location.href);
        if (url.pathname === "/api/chart") return Promise.resolve(new Response("", {status: 500}));
        return original(input, options);
      };
      const select = document.getElementById("chart-measure");
      select.value = "total_tokens";
      select.dispatchEvent(new Event("change"));
    })()''')
    cdp.wait_for('!document.getElementById("configurable-chart-status").hidden')
    assert any(word in cdp.eval('document.getElementById("configurable-chart-status").textContent').lower()
               for word in ("загруз", "load"))


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


def test_agent_count_and_worktime_cards_and_column(dashboard):
    """The two new summary cards and the per-agent duration column render from
    live /api/summary + /api/agents data (FAN-1228)."""
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    assert cdp.eval('document.getElementById("card-agent-count").textContent') == "3"
    # 21600 s of agent-time reads as a duration (6 hours), not raw seconds.
    assert cdp.eval('document.getElementById("card-agent-time").textContent') == "6 ч"
    header = cdp.eval(
        '[...document.querySelectorAll("#table-agents thead th")]'
        '.map((th) => th.textContent)')
    assert header[-1] == "Время работы"
    durations = cdp.eval(
        '[...document.querySelectorAll("#table-agents tbody tr")]'
        '.map((tr) => tr.lastElementChild.textContent)')
    # A1 1h, A3 2h, A2 3h render as readable durations in the last column.
    assert "1 ч" in durations and "2 ч" in durations and "3 ч" in durations


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
