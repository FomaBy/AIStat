import base64
import hashlib
import json
import math
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
from visual_regression import _decode_png, _encode_png, assert_png_matches


pytestmark = pytest.mark.skipif(
    CHROME is None, reason="no Chrome/Chromium binary for browser regression")

_BASELINES = Path(__file__).with_name("baselines")
_EXPECTED_CHROME_VERSION = os.environ.get(
    "AISTAT_CHROME_VERSION", "151.0.7922.138")
_VIEWPORT = {"width": 1440, "height": 1000, "deviceScaleFactor": 1,
             "mobile": False}
_CAPTURE_TIMEZONE = "Europe/Warsaw"
_CAPTURE_CLIP = {"x": 0, "y": 0, "width": 1440, "height": 1000,
                 "scale": 1}
_PAINT_BUDGET_SECONDS = 15
_NATIVE_SELECT_IDS = ("filter-project", "filter-agent", "filter-model")
_NATIVE_DATETIME_IDS = ("filter-from", "filter-to")
_NATIVE_SELECTS = {
    "metrics": [
        {"id": "filter-project", "matches": 1, "id_matches": 1,
         "rect": [45, 107.234375, 291, 172.234375], "value": "",
         "multiple": True, "disabled": False, "hidden": False,
         "display": "block", "visibility": "visible",
         "options": [["", "All projects", True, False],
                     ["P1", "Alpha", False, False],
                     ["P2", "Beta", False, False]]},
        {"id": "filter-agent", "matches": 1, "id_matches": 1,
         "rect": [45, 207.078125, 291, 289.078125], "value": "",
         "multiple": True, "disabled": False, "hidden": False,
         "display": "block", "visibility": "visible",
         "options": [["", "All agents", True, False],
                     ["A2", "Dev Shared", False, False],
                     ["A3", "QA Shared", False, False],
                     ["A1", "Solo Claude", False, False]]},
        {"id": "filter-model", "matches": 1, "id_matches": 1,
         "rect": [45, 323.921875, 291, 405.921875], "value": "",
         "multiple": True, "disabled": False, "hidden": False,
         "display": "block", "visibility": "visible",
         "options": [["", "All models", True, False],
                     ["m-claude", "m-claude", False, False],
                     ["m-mystery", "m-mystery", False, False],
                     ["m-shared", "m-shared", False, False]]},
    ],
    "i18n-switch": [
        {"id": "filter-project", "matches": 1, "id_matches": 1,
         "rect": [45, 107.234375, 291, 172.234375], "value": "P1",
         "multiple": True, "disabled": False, "hidden": False,
         "display": "block", "visibility": "visible",
         "options": [["", "Все проекты", False, False],
                     ["P1", "Alpha", True, False],
                     ["P2", "Beta", False, False]]},
        {"id": "filter-agent", "matches": 1, "id_matches": 1,
         "rect": [45, 207.078125, 291, 289.078125], "value": "",
         "multiple": True, "disabled": False, "hidden": False,
         "display": "block", "visibility": "visible",
         "options": [["", "Все агенты", True, False],
                     ["A2", "Dev Shared", False, False],
                     ["A3", "QA Shared", False, False],
                     ["A1", "Solo Claude", False, False]]},
        {"id": "filter-model", "matches": 1, "id_matches": 1,
         "rect": [45, 323.921875, 291, 405.921875], "value": "",
         "multiple": True, "disabled": False, "hidden": False,
         "display": "block", "visibility": "visible",
         "options": [["", "Все модели", True, False],
                     ["m-claude", "m-claude", False, False],
                     ["m-mystery", "m-mystery", False, False],
                     ["m-shared", "m-shared", False, False]]},
    ],
}
_NATIVE_DATETIMES = {
    "metrics": [
        {"id": "filter-from", "matches": 1, "id_matches": 1,
         "rect": [45, 507.609375, 291, 542.453125], "value": "",
         "type": "datetime-local", "step": "60", "disabled": False,
         "hidden": False, "display": "block", "visibility": "visible"},
        {"id": "filter-to", "matches": 1, "id_matches": 1,
         "rect": [45, 577.296875, 291, 612.140625], "value": "",
         "type": "datetime-local", "step": "60", "disabled": False,
         "hidden": False, "display": "block", "visibility": "visible"},
    ],
    "i18n-switch": [
        {"id": "filter-from", "matches": 1, "id_matches": 1,
         "rect": [45, 507.609375, 291, 542.453125], "value": "",
         "type": "datetime-local", "step": "60", "disabled": False,
         "hidden": False, "display": "block", "visibility": "visible"},
        {"id": "filter-to", "matches": 1, "id_matches": 1,
         "rect": [45, 577.296875, 291, 612.140625], "value": "",
         "type": "datetime-local", "step": "60", "disabled": False,
         "hidden": False, "display": "block", "visibility": "visible"},
    ],
}
_NATIVE_CONTROL_EXCLUDED_PIXELS = 70272
_NATIVE_SELECT_JS = '''(() => {
  const ids = ["filter-project", "filter-agent", "filter-model"];
  const controls = ids.map(id => {
    const matches = [...document.querySelectorAll(`select[multiple][id="${id}"]`)];
    const select = matches[0];
    const idMatches = document.querySelectorAll(`[id="${id}"]`).length;
    if (!select) return {id, matches: matches.length, id_matches: idMatches};
    const rect = select.getBoundingClientRect();
    const style = getComputedStyle(select);
    return {
      id, matches: matches.length, id_matches: idMatches,
      rect: [rect.left, rect.top, rect.right, rect.bottom],
      value: select.value, multiple: select.multiple,
      disabled: select.disabled, hidden: select.hidden,
      display: style.display, visibility: style.visibility,
      options: [...select.options].map(option => [
        option.value, option.text, option.selected, option.disabled
      ]),
    };
  });
  return {
    controls,
    datetime_controls: ["filter-from", "filter-to"].map(id => {
      const matches = [...document.querySelectorAll(
        `input[type="datetime-local"][id="${id}"]`
      )];
      const input = matches[0];
      const idMatches = document.querySelectorAll(`[id="${id}"]`).length;
      if (!input) return {id, matches: matches.length, id_matches: idMatches};
      const rect = input.getBoundingClientRect();
      const style = getComputedStyle(input);
      return {
        id, matches: matches.length, id_matches: idMatches,
        rect: [rect.left, rect.top, rect.right, rect.bottom],
        value: input.value, type: input.type, step: input.step,
        disabled: input.disabled, hidden: input.hidden,
        display: style.display, visibility: style.visibility,
      };
    }),
    datetime_ids: [...document.querySelectorAll(
      'input[type="datetime-local"]'
    )].map(input => input.id),
    multiple_ids: [...document.querySelectorAll("select[multiple]")]
      .map(select => select.id),
  };
})()'''
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


def _set_viewport(cdp, scrollbar_width=0):
    viewport = dict(_VIEWPORT)
    viewport["width"] += scrollbar_width
    cdp.call("Emulation.setDeviceMetricsOverride", viewport)


def _set_capture_timezone(cdp):
    cdp.call("Emulation.setTimezoneOverride", {
        "timezoneId": _CAPTURE_TIMEZONE,
    })


def _capture_geometry(cdp):
    return cdp.eval('''(() => {
      const root = document.documentElement;
      const body = document.body;
      return {
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        clientWidth: root.clientWidth,
        clientHeight: root.clientHeight,
        rootScrollWidth: root.scrollWidth,
        bodyScrollWidth: body.scrollWidth,
        scrollbarWidth: window.innerWidth - root.clientWidth,
      };
    })()''')


def _assert_capture_geometry(geometry, scrollbar_width):
    expected = {
        "viewportWidth": _VIEWPORT["width"] + scrollbar_width,
        "viewportHeight": _VIEWPORT["height"],
        "clientWidth": _VIEWPORT["width"],
        "clientHeight": _VIEWPORT["height"],
        "scrollbarWidth": scrollbar_width,
    }
    if any(geometry.get(key) != value for key, value in expected.items()):
        raise AssertionError("capture surface geometry is inconsistent: %s" %
                             geometry)
    if (geometry.get("rootScrollWidth", 0) > _VIEWPORT["width"] or
            geometry.get("bodyScrollWidth", 0) > _VIEWPORT["width"]):
        raise AssertionError("capture surface geometry has horizontal overflow: %s" %
                             geometry)


def _normalize_capture_surface(cdp, ready_js):
    scrollbar_width = _capture_geometry(cdp).get("scrollbarWidth")
    if scrollbar_width not in (0, 15):
        raise AssertionError("unsupported capture scrollbar width: %s" %
                             scrollbar_width)
    _set_viewport(cdp, scrollbar_width)
    _settle(cdp, ready_js)
    geometry = _capture_geometry(cdp)
    _assert_capture_geometry(geometry, scrollbar_width)
    return geometry


def _native_control_masks(controls):
    masks = []
    for control in controls:
        left, top, right, bottom = control["rect"]
        # Keep the one-CSS-pixel product-owned border and layout outside the
        # native-control interior; pixel bounds are right/bottom exclusive.
        mask = (math.ceil(left + 0.5), math.ceil(top + 0.5),
                math.floor(right - 0.5), math.floor(bottom - 0.5))
        if (mask[0] < 0 or mask[1] < 0 or mask[2] > _VIEWPORT["width"] or
                mask[3] > _VIEWPORT["height"] or
                mask[0] >= mask[2] or mask[1] >= mask[3]):
            raise AssertionError("native control interior is invalid: %s" %
                                 control)
        masks.append(mask)
    for index, mask in enumerate(masks):
        for other in masks[index + 1:]:
            if (mask[0] < other[2] and other[0] < mask[2] and
                    mask[1] < other[3] and other[1] < mask[3]):
                raise AssertionError("native control interiors overlap")
    return masks


def _native_control_contract(cdp, name):
    snapshot = cdp.eval(_NATIVE_SELECT_JS)
    controls = snapshot.get("controls") if isinstance(snapshot, dict) else None
    datetime_controls = (snapshot.get("datetime_controls")
                         if isinstance(snapshot, dict) else None)
    expected = _NATIVE_SELECTS[name]
    expected_datetime = _NATIVE_DATETIMES[name]
    if (snapshot.get("multiple_ids") != list(_NATIVE_SELECT_IDS) or
            snapshot.get("datetime_ids") != list(_NATIVE_DATETIME_IDS) or
            controls != expected or datetime_controls != expected_datetime):
        raise AssertionError("native control contract mismatch: %s" % snapshot)
    masks = _native_control_masks(controls + datetime_controls)
    excluded_pixels = sum((right - left) * (bottom - top)
                          for left, top, right, bottom in masks)
    if excluded_pixels != _NATIVE_CONTROL_EXCLUDED_PIXELS:
        raise AssertionError("native control excluded-pixel count: %s" %
                             excluded_pixels)
    semantic = [{key: control[key] for key in (
        "id", "value", "multiple", "disabled", "hidden", "display",
        "visibility", "options")} for control in controls]
    semantic.extend({key: control[key] for key in (
        "id", "value", "type", "step", "disabled", "hidden", "display",
        "visibility")} for control in datetime_controls)
    return {
        "masks": masks,
        "excluded_pixels": excluded_pixels,
        "semantic_digest": hashlib.sha256(json.dumps(
            semantic, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True).encode()).hexdigest(),
    }


def _assert_native_control_pixels_match(actual, baseline, masks, diff_path):
    actual_image = _decode_png(actual)
    baseline_image = _decode_png(baseline)
    if actual_image[:2] != baseline_image[:2]:
        assert_png_matches(actual, baseline, diff_path)

    width, height, actual_pixels = actual_image
    _, _, baseline_pixels = baseline_image
    excluded = bytearray(width * height)
    for left, top, right, bottom in masks:
        for y in range(top, bottom):
            excluded[y * width + left:y * width + right] = b"\1" * (right - left)

    changed = [index for index in range(width * height)
               if not excluded[index] and actual_pixels[index * 4:index * 4 + 4]
               != baseline_pixels[index * 4:index * 4 + 4]]
    if not changed:
        return 0

    diff = bytearray(b"\xff\xff\xff\xff" * (width * height))
    for index in changed:
        diff[index * 4:index * 4 + 4] = b"\xff\x00\x00\xff"
    diff_path = Path(diff_path)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_bytes(_encode_png(width, height, diff))
    raise AssertionError(
        "%d changed non-native pixel%s; diff artifact: %s" %
        (len(changed), "" if len(changed) == 1 else "s", diff_path))


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

        result = cdp.call("Page.captureScreenshot", {
            "clip": _CAPTURE_CLIP, "captureBeyondViewport": False},
            deadline=deadline)
        cdp.call("LayerTree.disable", deadline=deadline)
        return base64.b64decode(result["data"])
    except TimeoutError as exc:
        raise AssertionError(
            "screenshot paint completion was not observed within %s seconds" %
            _PAINT_BUDGET_SECONDS) from exc
    finally:
        cdp._read_message = original_read


def _capture(cdp, name, geometry=None):
    if (name == "metrics" and
            os.environ.get("AISTAT_VISUAL_CSS_PROBE") == "1"):
        _apply_css_shift_probe(cdp)
    native_controls = (_native_control_contract(cdp, name)
                       if name in _NATIVE_SELECTS else None)
    actual = _wait_for_stable_screenshot(cdp)
    if geometry is not None:
        print("%s visual-geometry=%s" %
              (name, json.dumps(geometry, sort_keys=True)))
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
    if native_controls is None:
        assert_png_matches(actual, baseline.read_bytes(),
                           diff_dir / (name + ".diff.png"))
        return
    changed = _assert_native_control_pixels_match(
        actual, baseline.read_bytes(), native_controls["masks"],
        diff_dir / (name + ".diff.png"))
    print("%s visual-native-controls=%s" % (name, json.dumps({
        "rectangles": native_controls["masks"],
        "excluded_pixels": native_controls["excluded_pixels"],
        "semantic_digest": native_controls["semantic_digest"],
        "non_native_changed_pixels": changed,
    }, sort_keys=True)))


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
            CHROME, extra_args=("--run-all-compositor-stages-before-draw",
                                "--lang=en-GB"))
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
    _set_capture_timezone(cdp)
    _set_viewport(cdp)
    _settle(cdp, _DASHBOARD_READY_JS)
    _capture(cdp, "metrics", _normalize_capture_surface(
        cdp, _DASHBOARD_READY_JS))


def test_login_page_matches_visual_baseline(visual_browser):
    cdp, _, public_base = visual_browser
    cdp.open_page(public_base + "/login", preload_script=_locale_preload("en-US"))
    _set_capture_timezone(cdp)
    _set_viewport(cdp)
    _settle(cdp, _LOGIN_READY_JS)
    _capture(cdp, "login", _normalize_capture_surface(cdp, _LOGIN_READY_JS))


def test_i18n_switch_matches_visual_baseline(visual_browser):
    cdp, dashboard_base, _ = visual_browser
    cdp.open_page(dashboard_base + "/?project=P1",
                  preload_script=_locale_preload("en-US"))
    _set_capture_timezone(cdp)
    _set_viewport(cdp)
    _settle(cdp, _DASHBOARD_READY_JS)
    cdp.eval('document.getElementById("locale-switcher").click()')
    _settle(cdp, _DASHBOARD_RU_READY_JS)
    _capture(cdp, "i18n-switch", _normalize_capture_surface(
        cdp, _DASHBOARD_RU_READY_JS))
