"""Real-browser proof that the auth cookie is opaque to scripts (FAN-1392).

Runs the authenticated Flask WSGI contour and a mock Google provider on
loopback HTTP servers and drives them through headless Chrome over the shared
DevTools harness (``cdp_harness``). After a real password login and a real
mock-Google login it asserts that:

* the ``aistat_session`` cookie is present but ``HttpOnly`` and opaque, so
  ``document.cookie`` never exposes it and it carries no decodable identity;
* ``localStorage`` and ``sessionStorage`` hold no auth material;
* a session token captured out of band replays dead once its browser logs out.

Only synthetic identities, disposable databases/tenants and a task-owned
browser profile are used. The suite skips cleanly where no Chrome binary exists.
"""

import json
import socket
import ssl
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

from aistat import oauth
from aistat.config import Config
from aistat.db import connect, init_db
from aistat.migrate import migrate_owner_database
from aistat.wsgi import create_app
from conftest import seed_aggregate_fixture
from cdp_harness import BOOTED_JS, CHROME, NO_CHROME_REASON, launch_chrome

pytestmark = pytest.mark.skipif(CHROME is None, reason=NO_CHROME_REASON)

USERNAME = "sergey"
PASSWORD = "correct horse battery staple"
SESSION_SECRET = "browser-session-" + "s" * 48
INGEST_SECRET = "browser-ingest-" + "i" * 48
GOOGLE_EMAIL = "allowed@example.com"
GOOGLE_SUBJECT = "g-browser-subject"

# The loopback app + provider run under werkzeug's self-signed "adhoc" TLS
# (OAuth endpoints must be HTTPS), so both Chrome and the app's server-side
# provider calls skip verification for these throwaway loopback certs only.
_UNVERIFIED_TLS = ssl.create_default_context()
_UNVERIFIED_TLS.check_hostname = False
_UNVERIFIED_TLS.verify_mode = ssl.CERT_NONE


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _mock_google_app(identity):
    """A minimal loopback Google: authorize redirects back with a code, and
    token/userinfo return the synthetic identity to the app's server-side call.
    """

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/authorize":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            location = "{0}?code=abc&state={1}".format(
                query["redirect_uri"][0], query["state"][0]
            )
            start_response(
                "303 See Other",
                [("Location", location), ("Content-Length", "0")],
            )
            return [b""]
        if path in ("/token", "/userinfo"):
            body = json.dumps(
                {"access_token": "at"} if path == "/token" else identity
            ).encode("utf-8")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]
        start_response("404 Not Found", [("Content-Length", "0")])
        return [b""]

    return app


class _Loopback:
    """A werkzeug WSGI app on its own thread, shut down defensively."""

    def __init__(self, port, app, ssl_context=None):
        self.server = make_server(
            "127.0.0.1", port, app, threaded=True, ssl_context=ssl_context
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    def start(self):
        self.thread.start()

    def close(self):
        try:
            self.server.shutdown()
        finally:
            self.thread.join(timeout=10)


def _api_status(base_url, path, cookie=None):
    request = urllib.request.Request(base_url + path)
    if cookie is not None:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(
            request, timeout=10, context=_UNVERIFIED_TLS
        ) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


LOGIN_JS = """(async () => {
  const page = await fetch("/login", {credentials: "same-origin"});
  const html = await page.text();
  const match = html.match(/name="csrf" value="([^"]+)"/);
  if (!match) return "no-csrf";
  const body = new URLSearchParams({
    csrf: match[1], username: %(username)s, password: %(password)s, next: "/",
  });
  const posted = await fetch("/login", {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/x-www-form-urlencoded"}, body,
  });
  return posted.status;
})()""" % {"username": json.dumps(USERNAME), "password": json.dumps(PASSWORD)}

INSPECT_JS = """JSON.stringify({
  cookie: document.cookie,
  local: Object.keys(localStorage).map(k => k + "=" + localStorage.getItem(k)),
  session: Object.keys(sessionStorage).map(
    k => k + "=" + sessionStorage.getItem(k)),
})"""

LOGOUT_JS = """(async () => {
  const auth = await (await fetch("/api/session",
    {credentials: "same-origin"})).json();
  await fetch("/logout", {method: "POST", credentials: "same-origin",
    headers: {"X-CSRF-Token": auth.csrf}, redirect: "manual"});
  const after = await fetch("/api/meta", {credentials: "same-origin"});
  return after.status;
})()"""


@pytest.fixture(scope="module")
def auth_site():
    """The Flask auth app + a mock Google provider on loopback, plus one
    headless Chrome. Ports are pre-allocated so the provider redirect_uri and
    the app's provider URLs point at each other before the app is built."""
    app_port = _free_port()
    provider_port = _free_port()
    base_url = "https://127.0.0.1:{0}".format(app_port)
    provider_url = "https://127.0.0.1:{0}".format(provider_port)

    # The app's server-side token/userinfo calls hit the self-signed loopback
    # provider, so give this in-process app an unverified context for them.
    original_urlopen = oauth.urlopen

    def _loopback_urlopen(request, timeout=None):
        return original_urlopen(request, timeout=timeout, context=_UNVERIFIED_TLS)

    oauth.urlopen = _loopback_urlopen
    tmp = tempfile.TemporaryDirectory(prefix="aistat-auth-browser-")
    app_server = provider_server = cdp = None
    try:
        root = Path(tmp.name)
        config = Config()
        config.db_path = root / "public.db"
        config.security_db_path = root / "security.db"
        config.tenants_dir = root / "tenants"
        config.credits_per_usd = 2.0
        config.auth_username = USERNAME
        config.auth_password_hash = generate_password_hash(
            PASSWORD, method="pbkdf2:sha256:600000"
        )
        config.session_secret = SESSION_SECRET
        config.ingest_secret = INGEST_SECRET
        config.allowed_hosts = ("127.0.0.1", "localhost", "testserver")
        config.force_https = False
        config.session_cookie_secure = False  # plain-http loopback
        config.admin_email = GOOGLE_EMAIL
        config.oauth_allowed_emails = frozenset({GOOGLE_EMAIL})
        config.oauth_providers = {
            "google": oauth.OAuthProvider(
                name="google",
                authorize_url=provider_url + "/authorize",
                token_url=provider_url + "/token",
                userinfo_url=provider_url + "/userinfo",
                scopes=("openid", "email", "profile"),
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=base_url + "/auth/google/callback",
            )
        }

        conn = connect(config.db_path)
        init_db(conn)
        seed_aggregate_fixture(conn)
        conn.close()
        migrate_owner_database(config, now=1000)

        app_server = _Loopback(
            app_port, create_app(config), ssl_context="adhoc"
        )
        provider_server = _Loopback(
            provider_port,
            _mock_google_app(
                {
                    "sub": GOOGLE_SUBJECT,
                    "email": GOOGLE_EMAIL,
                    "email_verified": True,
                    "name": "Owner Person",
                }
            ),
            ssl_context="adhoc",
        )
        app_server.start()
        provider_server.start()
        cdp = launch_chrome(
            CHROME, extra_args=("--ignore-certificate-errors",)
        )
        yield cdp, base_url
    finally:
        if cdp is not None:
            cdp.close()
        for server in (app_server, provider_server):
            if server is not None:
                server.close()
        tmp.cleanup()
        oauth.urlopen = original_urlopen


def _boot_dashboard(cdp, base_url):
    cdp.open_page(base_url + "/")
    cdp.wait_for(BOOTED_JS)


def _session_cookie(cdp, base_url):
    cdp.call("Network.enable")
    cookies = cdp.call("Network.getCookies", {"urls": [base_url + "/"]})[
        "cookies"
    ]
    return next(c for c in cookies if c["name"] == "aistat_session")


def _assert_no_script_visible_auth(cdp, base_url, token, forbidden):
    state = json.loads(cdp.eval(INSPECT_JS))
    assert "aistat_session" not in state["cookie"], state["cookie"]
    assert token not in state["cookie"], state["cookie"]
    assert state["local"] == []
    assert state["session"] == []
    for surface in (state["cookie"], "\n".join(state["local"] + state["session"])):
        for needle in forbidden:
            assert needle not in surface, needle
    # The cookie the browser actually holds is HttpOnly and opaque.
    cookie = _session_cookie(cdp, base_url)
    assert cookie["httpOnly"] is True
    assert cookie["value"] == token
    assert "." not in cookie["value"]


def test_password_login_hides_session_token_and_revoked_replay_fails(auth_site):
    cdp, base_url = auth_site
    cdp.open_page(base_url + "/login")
    assert cdp.eval(LOGIN_JS) == 200
    _boot_dashboard(cdp, base_url)

    token = _session_cookie(cdp, base_url)["value"]
    _assert_no_script_visible_auth(
        cdp, base_url, token, [USERNAME, GOOGLE_EMAIL, "google"]
    )

    # A token captured out of band works — until this browser logs out.
    cookie = "aistat_session=" + token
    assert _api_status(base_url, "/api/meta", cookie) == 200
    assert cdp.eval(LOGOUT_JS) == 401
    for _ in range(3):
        assert _api_status(base_url, "/api/meta", cookie) == 401


def test_google_login_hides_session_token_from_scripts(auth_site):
    cdp, base_url = auth_site
    # The whole /auth/google/start -> mock provider -> callback loop runs for
    # real; only the identity behind token/userinfo is synthetic.
    cdp.open_page(base_url + "/auth/google/start")
    cdp.wait_for(BOOTED_JS)

    token = _session_cookie(cdp, base_url)["value"]
    _assert_no_script_visible_auth(
        cdp, base_url, token, [GOOGLE_EMAIL, GOOGLE_SUBJECT, "google"]
    )
    # Clean up so the module's shared browser leaves no live session behind.
    assert cdp.eval(LOGOUT_JS) == 401
