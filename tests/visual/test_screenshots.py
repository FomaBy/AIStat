import base64
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
from visual_regression import assert_png_matches


pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome/Chromium binary for browser regression")

_BASELINES = Path(__file__).with_name("baselines")
_VIEWPORT = {"width": 1440, "height": 1000, "deviceScaleFactor": 1,
             "mobile": False}


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


def _settle(cdp):
    cdp.eval("document.fonts.ready")
    cdp.eval("new Promise(resolve => setTimeout(resolve, 1200))")
    cdp.eval("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def _set_viewport(cdp):
    cdp.call("Emulation.setDeviceMetricsOverride", _VIEWPORT)


def _capture(cdp, name):
    actual = base64.b64decode(cdp.call("Page.captureScreenshot")["data"])
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
    assert_png_matches(actual, baseline.read_bytes(), diff_dir / (name + ".diff.png"))


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

        session.cdp = launch_chrome(CHROME)
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
    cdp.wait_for(BOOTED_JS)
    _settle(cdp)
    _capture(cdp, "metrics")


def test_login_page_matches_visual_baseline(visual_browser):
    cdp, _, public_base = visual_browser
    cdp.open_page(public_base + "/login", preload_script=_locale_preload("en-US"))
    _set_viewport(cdp)
    cdp.wait_for('document.getElementById("locale-switcher") !== null')
    cdp.eval("document.activeElement.blur()")
    _settle(cdp)
    _capture(cdp, "login")


def test_i18n_switch_matches_visual_baseline(visual_browser):
    cdp, dashboard_base, _ = visual_browser
    cdp.open_page(dashboard_base + "/?project=P1",
                 preload_script=_locale_preload("en-US"))
    _set_viewport(cdp)
    cdp.wait_for(BOOTED_JS)
    cdp.eval('document.getElementById("locale-switcher").click()')
    cdp.wait_for(
        'document.documentElement.lang === "ru" && '
        'document.getElementById("card-tokens").textContent.includes("млн")'
    )
    _settle(cdp)
    _capture(cdp, "i18n-switch")
