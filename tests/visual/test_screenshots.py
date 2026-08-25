import base64
import hashlib
import json
import os
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
from cdp_harness import BOOTED_JS, CHROME, DashboardSession, launch_chrome
from visual_regression import _decode_png, assert_png_matches


pytestmark = pytest.mark.skipif(
    CHROME is None, reason="no Chrome/Chromium binary for browser regression")

_BASELINES = Path(__file__).with_name("baselines")
_EXPECTED_CHROME_VERSION = os.environ.get(
    "AISTAT_CHROME_VERSION", "151.0.7922.170")
_VIEWPORT = {"width": 1440, "height": 1000, "deviceScaleFactor": 1,
             "mobile": False}
_PAINT_BUDGET_SECONDS = 15
_DASHBOARD_READY_JS = '''(() => {
  const card = document.getElementById("card-tokens");
  const live = document.getElementById("live-label");
  return document.readyState === "complete" && card && card.textContent !== "—"
    && live && live.textContent === "live";
})()'''
_DASHBOARD_RU_READY_JS = '''(() => {
  const card = document.getElementById("card-tokens");
  const live = document.getElementById("live-label");
  return document.readyState === "complete"
    && document.documentElement.lang === "ru"
    && card && card.textContent.includes("млн")
    && live && live.textContent === "live";
})()'''
_LOGIN_READY_JS = '''document.readyState === "complete" &&
  document.getElementById("locale-switcher") !== null'''
_LAYOUT_STABLE_JS = '''new Promise(resolve => {
  let previous = null;
  let stableFrames = 0;
  const sample = () => {
    const root = document.documentElement;
    const canvases = [...document.querySelectorAll("canvas")]
      .map(canvas => `${canvas.width}x${canvas.height}`);
    const current = JSON.stringify([
      root.scrollWidth, root.scrollHeight, document.body.scrollWidth,
      document.body.scrollHeight, canvases
    ]);
    stableFrames = current === previous ? stableFrames + 1 : 0;
    previous = current;
    if (stableFrames >= 2) resolve(true);
    else requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
})'''


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _locale_preload(language):
    return (
        'Object.defineProperty(Navigator.prototype, "language", '
        f'{{configurable: true, get: () => {json.dumps(language)}}}); '
        'localStorage.removeItem("aistat.locale");'
    )


def _wait_server(server):
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("visual test server did not start")
        time.sleep(0.05)


def _assert_browser_version(cdp, expected):
    product = cdp.call("Browser.getVersion", session=False).get("product", "")
    prefix = "Chrome/"
    actual = product[len(prefix):] if product.startswith(prefix) else product
    if actual != expected:
        raise RuntimeError(
            "Chrome version mismatch: expected %s, got %s" % (expected, actual))


def _disable_transient_rendering(cdp):
    """Freeze only test-rendering state; product CSS and layout stay untouched."""
    cdp.eval('''(() => {
      const id = "aistat-visual-transient-freeze";
      let style = document.getElementById(id);
      if (!style) {
        style = document.createElement("style");
        style.id = id;
        style.textContent = `*, *::before, *::after {
          animation: none !important;
          transition: none !important;
          caret-color: transparent !important;
        }
        :root { color-scheme: light !important; }`;
        document.head.appendChild(style);
      }
      document.querySelectorAll(
        "input, textarea, select, button, [contenteditable='true']"
      ).forEach((element) => element.blur());
      if (document.activeElement && document.activeElement.blur) {
        document.activeElement.blur();
      }
      if (window.Chart && Chart.instances) {
        Chart.defaults.animation = false;
        Chart.defaults.animations = {};
        Object.values(Chart.instances).forEach((chart) => {
          chart.options.animation = false;
          chart.options.animations = {};
          chart.stop();
          chart.update("none");
        });
      }
      return true;
    })()''')


def _settle(cdp, ready_js):
    """Wait for data, fonts and two consecutive identical layout frames."""
    cdp.wait_for(ready_js)
    cdp.wait_for('document.fonts.status === "loaded"')
    _disable_transient_rendering(cdp)
    cdp.eval(_LAYOUT_STABLE_JS)
    _disable_transient_rendering(cdp)


def _apply_css_shift_probe(cdp):
    """Apply the one-shot negative probe in the browser, never in product CSS."""
    cdp.eval('''(() => {
      const style = document.createElement("style");
      style.textContent = ".dashboard-layout { transform: translateX(1px) !important; }";
      document.head.appendChild(style);
      return true;
    })()''')


def _set_viewport(cdp):
    cdp.call("Emulation.setDeviceMetricsOverride", _VIEWPORT)


def _eval_before(cdp, expression, deadline):
    result = cdp.call("Runtime.evaluate", {
        "expression": expression, "returnByValue": True,
        "awaitPromise": True}, deadline=deadline)
    if "exceptionDetails" in result:
        raise RuntimeError(result["exceptionDetails"].get(
            "text", "JS exception") + ": " + str(result["exceptionDetails"]))
    return result["result"].get("value")


def _wait_for_stable_screenshot(cdp):
    """Capture only after the restored panel paint is observed by Chromium."""
    deadline = time.monotonic() + _PAINT_BUDGET_SECONDS
    layers = []
    original_read = cdp._read_message

    def record_layer_events(read_deadline):
        message = original_read(read_deadline)
        if message.get("method") == "LayerTree.layerTreeDidChange":
            layers.extend(message.get("params", {}).get("layers", ()))
        return message

    def paint_counts():
        return {layer["layerId"]: layer.get("paintCount", 0)
                for layer in layers if layer.get("drawsContent")}

    cdp._read_message = record_layer_events
    try:
        cdp.call("LayerTree.enable", deadline=deadline)
        _eval_before(cdp, '''new Promise(resolve => {
          const targets = [...document.querySelectorAll(".panel")];
          if (!targets.length) targets.push(document.body);
          window.__aistatVisualPaintOwners = targets.map(
            element => [element, element.style.willChange]);
          targets.forEach(element => element.style.willChange = "transform");
          requestAnimationFrame(() => requestAnimationFrame(resolve));
        })''', deadline)
        before = paint_counts()
        while not before:
            _eval_before(cdp, "new Promise(requestAnimationFrame)", deadline)
            before = paint_counts()

        layers.clear()
        _eval_before(cdp, '''new Promise(resolve => {
          (window.__aistatVisualPaintOwners || []).forEach(([element, value]) => {
            if (value) element.style.willChange = value;
            else element.style.removeProperty("will-change");
          });
          delete window.__aistatVisualPaintOwners;
          requestAnimationFrame(() => requestAnimationFrame(resolve));
        })''', deadline)
        while not any(
                layer.get("layerId") in before and
                layer.get("paintCount", 0) > before[layer["layerId"]]
                for layer in layers):
            _eval_before(cdp, "new Promise(requestAnimationFrame)", deadline)

        result = cdp.call("Page.captureScreenshot", deadline=deadline)
        cdp.call("LayerTree.disable", deadline=deadline)
        return base64.b64decode(result["data"])
    except TimeoutError as exc:
        raise AssertionError(
            "screenshot paint completion was not observed within %s seconds" %
            _PAINT_BUDGET_SECONDS) from exc
    finally:
        cdp._read_message = original_read


def _capture(cdp, name):
    if (name == "metrics" and
            os.environ.get("AISTAT_VISUAL_CSS_PROBE") == "1"):
        _apply_css_shift_probe(cdp)
    actual = _wait_for_stable_screenshot(cdp)
    if os.environ.get("AISTAT_VISUAL_HASHES") == "1":
        _, _, pixels = _decode_png(actual)
        print("%s visual-sha256=%s" %
              (name, hashlib.sha256(bytes(pixels)).hexdigest()))
    baseline = _BASELINES / (name + ".png")
    if os.environ.get("AISTAT_UPDATE_VISUAL_BASELINES") == "1":
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(actual)
        return
    if not baseline.exists():
        raise AssertionError(
            "missing visual baseline %s; set AISTAT_UPDATE_VISUAL_BASELINES=1 "
            "after reviewing the rendered state" % baseline)
    diff_dir = Path(os.environ.get("AISTAT_VISUAL_DIFF_DIR", tempfile.gettempdir()))
    assert_png_matches(actual, baseline.read_bytes(),
                       diff_dir / (name + ".diff.png"))


@pytest.fixture(scope="module")
def visual_browser():
    import uvicorn

    session = DashboardSession()
    public_server = public_thread = None
    try:
        session.tmp = tempfile.TemporaryDirectory(prefix="aistat-visual-")
        root = Path(session.tmp.name)
        dashboard_config = Config()
        dashboard_config.db_path = root / "dashboard.db"
        dashboard_config.credits_per_usd = 2.0
        conn = connect(dashboard_config.db_path)
        init_db(conn)
        seed_aggregate_fixture(conn)
        conn.close()

        dashboard_port = _free_port()
        session.server = uvicorn.Server(uvicorn.Config(
            server_module.create_app(dashboard_config), host="127.0.0.1",
            port=dashboard_port, log_level="warning"))
        session.thread = threading.Thread(
            target=session.server.run, daemon=True)
        session.thread.start()
        _wait_server(session.server)

        public_config = Config(
            db_path=root / "public.db",
            security_db_path=root / "security.db",
            tenants_dir=root / "tenants",
            auth_username="visual-test",
            auth_password_hash=generate_password_hash(
                "visual-test-password", method="pbkdf2:sha256:600000"
            ),
            session_secret="session-" + "s" * 48,
            ingest_secret="ingest-" + "i" * 48,
            allowed_hosts=("127.0.0.1",),
            force_https=False,
        )
        public_port = _free_port()
        public_server = make_server(
            "127.0.0.1", public_port, public_wsgi_module.create_app(public_config))
        public_thread = threading.Thread(
            target=public_server.serve_forever, daemon=True)
        public_thread.start()

        session.cdp = launch_chrome(
            CHROME, extra_args=("--run-all-compositor-stages-before-draw",))
        _assert_browser_version(session.cdp, _EXPECTED_CHROME_VERSION)
        yield (session.cdp, f"http://127.0.0.1:{dashboard_port}",
               f"http://127.0.0.1:{public_port}")
    finally:
        session.close()
        if public_server is not None:
            public_server.shutdown()
        if public_thread is not None:
            public_thread.join(timeout=10)


def test_metrics_page_matches_visual_baseline(visual_browser):
    cdp, dashboard_base, _ = visual_browser
    cdp.open_page(dashboard_base + "/", preload_script=_locale_preload("en-US"))
    _set_viewport(cdp)
    _settle(cdp, _DASHBOARD_READY_JS)
    _capture(cdp, "metrics")


def test_login_page_matches_visual_baseline(visual_browser):
    cdp, _, public_base = visual_browser
    cdp.open_page(public_base + "/login", preload_script=_locale_preload("en-US"))
    _set_viewport(cdp)
    _settle(cdp, _LOGIN_READY_JS)
    _capture(cdp, "login")


def test_i18n_switch_matches_visual_baseline(visual_browser):
    cdp, dashboard_base, _ = visual_browser
    cdp.open_page(dashboard_base + "/?project=P1",
                 preload_script=_locale_preload("en-US"))
    _set_viewport(cdp)
    _settle(cdp, _DASHBOARD_READY_JS)
    cdp.eval('document.getElementById("locale-switcher").click()')
    _settle(cdp, _DASHBOARD_RU_READY_JS)
    _capture(cdp, "i18n-switch")
