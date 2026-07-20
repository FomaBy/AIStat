"""Real-browser proof that the auth cookie is opaque to scripts (FAN-1392).

This is the committed, regression-proof form of the disposable FAN-1400 probe:
the whole opaque-session security contract is verified through a real headless
Chrome for **all four** login surfaces, not just the two Flask ones —

    (Flask WSGI, legacy cPanel WSGI) x (password login, mock-Google login).

Each combination runs its authenticated app plus a local mock Google provider on
loopback HTTPS servers and drives them through Chrome over the shared DevTools
harness (``cdp_harness``). After a real login it asserts, per combination, that:

* the ``aistat_session`` cookie the browser holds is one opaque CSPRNG token —
  ``HttpOnly``, production-configured ``Secure``, ``SameSite=Lax``, ``Path=/``,
  32 decoded random bytes, no ``.`` envelope and no decodable identity/CSRF, and
  its lifetime never outlives the server-side ``sessions.expires_at`` record;
* ``document.cookie``, the DOM, ``localStorage`` and ``sessionStorage`` expose
  neither the token nor any identity/CSRF material, while the protected route is
  reachable until the session is revoked;
* an invalid CSRF against ``/logout``, ``/api/connection`` and
  ``/api/connection/revoke`` mutates neither the connection nor the session, and
  a valid logout revokes only the current token — a second independent session
  survives while the captured cookie replays dead (``401``) at least three times.

Only synthetic identities, disposable databases/tenants and a task-owned browser
profile are used; no production credentials or real user data are involved. The
four cases skip cleanly where no Chrome binary exists, but an all-skipped run is
not treated as green evidence: ``test_browser_gate_is_certified`` turns a missing
Chrome into a hard failure whenever ``AISTAT_REQUIRE_BROWSER`` is set (the QA/CI
certification gate).
"""

import base64
import http.cookiejar
import importlib
import json
import os
import re
import socket
import sqlite3
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

from aistat import handoff, oauth
from aistat.config import Config
from aistat.db import connect, init_db
from aistat.migrate import migrate_owner_database
from aistat.wsgi import create_app
from conftest import assert_opaque_session_cookie, seed_aggregate_fixture
from cdp_harness import BOOTED_JS, CHROME, NO_CHROME_REASON, launch_chrome

requires_chrome = pytest.mark.skipif(CHROME is None, reason=NO_CHROME_REASON)

# A certifying run demands the browser matrix actually executed; when this is
# set an absent Chrome is a hard failure instead of a silent skip (see the gate
# test at the bottom), so an all-skipped run can never masquerade as green.
_REQUIRE_BROWSER = os.environ.get("AISTAT_REQUIRE_BROWSER", "").strip().lower() in {
    "1", "true", "yes", "on",
}

USERNAME = "sergey"
PASSWORD = "correct horse battery staple"
SESSION_SECRET = "browser-session-" + "s" * 48
INGEST_SECRET = "browser-ingest-" + "i" * 48
# Distinct from the session/ingest secrets and >= 32 bytes so the connection
# feature can be switched on and its CSRF defence actually exercised.
WORKER_SECRET = "browser-worker-" + "w" * 48
GOOGLE_EMAIL = "allowed@example.com"
GOOGLE_SUBJECT = "g-browser-subject"
GOOGLE_IDENTITY = {
    "sub": GOOGLE_SUBJECT,
    "email": GOOGLE_EMAIL,
    "email_verified": True,
    "name": "Owner Person",
}

# The browser cookie carries a 32-byte ``secrets.token_urlsafe(32)`` value, and
# the server stores ``expires_at`` and the browser stores the cookie's expiry at
# the same login instant with the same TTL. On loopback they differ only by sub-
# second latency and the server's ``int()`` truncation; this leeway absorbs that
# while staying trivially tight against the 12h session lifetime.
_TOKEN_BYTES = 32
_EXPIRY_LEEWAY_SECONDS = 5

# (contour, login method) — the full committed matrix.
MATRIX = [
    ("flask", "password"),
    ("flask", "google"),
    ("legacy", "password"),
    ("legacy", "google"),
]
MATRIX_IDS = ["{0}-{1}".format(contour, method) for contour, method in MATRIX]

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


def _password_session_token(base_url):
    """Log in with the owner password out of band and return the resulting
    ``aistat_session`` value — a second, independent server-side session used to
    prove that logout revokes only the current token, never all of a user's."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_UNVERIFIED_TLS),
        urllib.request.HTTPCookieProcessor(jar),
    )
    with opener.open(base_url + "/login", timeout=10) as response:
        page = response.read().decode("utf-8")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    body = urllib.parse.urlencode(
        {"csrf": csrf, "username": USERNAME, "password": PASSWORD, "next": "/"}
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/login",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(request, timeout=10):
        pass
    for cookie in jar:
        if cookie.name == "aistat_session":
            return cookie.value
    raise AssertionError("out-of-band password login set no session cookie")


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


def _session_csrf_js():
    return ("(async () => (await (await fetch('/api/session', "
            "{credentials: 'same-origin'})).json()).csrf)()")


def _fetch_status(cdp, method, path, csrf=None):
    """Status of a same-origin ``fetch`` from the page; the HttpOnly session
    cookie rides along automatically. ``redirect: 'manual'`` keeps the raw
    status so a would-be 303 is never followed into a 200."""
    headers = {} if csrf is None else {"X-CSRF-Token": csrf}
    js = (
        "(async () => { const r = await fetch(%s, {method: %s, "
        "credentials: 'same-origin', redirect: 'manual', headers: %s}); "
        "return r.status; })()"
    ) % (json.dumps(path), json.dumps(method), json.dumps(headers))
    return cdp.eval(js)


def _connection_status(cdp):
    js = ("(async () => { const r = await fetch('/api/connection', "
          "{credentials: 'same-origin'}); return (await r.json()).status; })()")
    return cdp.eval(js)


def _build_flask_app(tmp_path, base_url, provider_url):
    config = Config()
    config.db_path = tmp_path / "public.db"
    config.security_db_path = tmp_path / "security.db"
    config.tenants_dir = tmp_path / "tenants"
    config.credits_per_usd = 2.0
    config.auth_username = USERNAME
    config.auth_password_hash = generate_password_hash(
        PASSWORD, method="pbkdf2:sha256:600000"
    )
    config.session_secret = SESSION_SECRET
    config.ingest_secret = INGEST_SECRET
    config.worker_secret = WORKER_SECRET
    config.multica_connect_enabled = True
    config.allowed_hosts = ("127.0.0.1", "localhost", "testserver")
    config.force_https = False
    config.session_cookie_secure = True  # production posture: Secure cookie
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
    return create_app(config), config.security_db_path


def _build_legacy_app(tmp_path, monkeypatch, base_url, provider_url):
    env = {
        "AISTAT_DB_PATH": str(tmp_path / "public.db"),
        "AISTAT_SECURITY_DB_PATH": str(tmp_path / "security.db"),
        "AISTAT_TENANTS_DIR": str(tmp_path / "tenants"),
        "AISTAT_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "AISTAT_FORCE_HTTPS": "0",
        "AISTAT_SESSION_COOKIE_SECURE": "1",  # production posture: Secure cookie
        "AISTAT_ADMIN_USERNAME": USERNAME,
        "AISTAT_PASSWORD_HASH": generate_password_hash(
            PASSWORD, method="pbkdf2:sha256:600000"
        ),
        "AISTAT_SESSION_SECRET": SESSION_SECRET,
        "AISTAT_INGEST_SECRET": INGEST_SECRET,
        "AISTAT_WORKER_SECRET": WORKER_SECRET,
        "AISTAT_MULTICA_CONNECT_ENABLED": "1",
        "AISTAT_MULTICA_OFFICIAL_URL": handoff.OFFICIAL_MULTICA_URL,
        "AISTAT_OAUTH_PROVIDERS": "google",
        "AISTAT_OAUTH_GOOGLE_AUTHORIZE_URL": provider_url + "/authorize",
        "AISTAT_OAUTH_GOOGLE_TOKEN_URL": provider_url + "/token",
        "AISTAT_OAUTH_GOOGLE_USERINFO_URL": provider_url + "/userinfo",
        "AISTAT_OAUTH_GOOGLE_SCOPES": "openid email profile",
        "AISTAT_OAUTH_GOOGLE_CLIENT_ID": "client-id",
        "AISTAT_OAUTH_GOOGLE_CLIENT_SECRET": "client-secret",
        "AISTAT_OAUTH_GOOGLE_REDIRECT_URI": base_url + "/auth/google/callback",
        "AISTAT_OAUTH_ALLOWED_EMAILS": GOOGLE_EMAIL,
        "AISTAT_ADMIN_EMAIL": GOOGLE_EMAIL,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import aistat.legacy_wsgi as legacy_module

    legacy_module = importlib.reload(legacy_module)
    conn = connect(legacy_module.DB_PATH)
    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.close()
    migrate_owner_database(Config(), now=1000)
    return legacy_module.application, Path(legacy_module.SECURITY_DB_PATH)


class _Case:
    """One resolved (contour, method) fixture: a booted Chrome bound to a live
    loopback app, plus the paths a test needs to read authoritative state."""

    def __init__(self, cdp, base_url, security_db, contour, method):
        self.cdp = cdp
        self.base_url = base_url
        self.security_db = security_db
        self.contour = contour
        self.method = method

    def forbidden(self, csrf):
        return [USERNAME, GOOGLE_EMAIL, GOOGLE_SUBJECT, "google", csrf]


@pytest.fixture(params=MATRIX, ids=MATRIX_IDS)
def auth_case(request, tmp_path, monkeypatch):
    """Build exactly one (contour, method) combination — its own disposable
    databases, loopback app, mock Google provider and a fresh task-owned Chrome.
    Every resource (Chrome + HOME/TMP/profile, both servers, the DBs and any env
    / patched egress) is owned here and released on the way out, pass or fail."""
    contour, method = request.param

    # The app's server-side token/userinfo calls hit the self-signed loopback
    # provider, so give this in-process app an unverified context for them.
    original_urlopen = oauth.urlopen

    def _loopback_urlopen(request_obj, timeout=None):
        return original_urlopen(
            request_obj, timeout=timeout, context=_UNVERIFIED_TLS
        )

    monkeypatch.setattr(oauth, "urlopen", _loopback_urlopen)

    app_port = _free_port()
    provider_port = _free_port()
    base_url = "https://127.0.0.1:{0}".format(app_port)
    provider_url = "https://127.0.0.1:{0}".format(provider_port)

    if contour == "flask":
        app, security_db = _build_flask_app(tmp_path, base_url, provider_url)
    else:
        app, security_db = _build_legacy_app(
            tmp_path, monkeypatch, base_url, provider_url
        )

    provider_server = _Loopback(
        provider_port, _mock_google_app(GOOGLE_IDENTITY), ssl_context="adhoc"
    )
    app_server = _Loopback(app_port, app, ssl_context="adhoc")
    cdp = None
    try:
        provider_server.start()
        app_server.start()
        cdp = launch_chrome(CHROME, extra_args=("--ignore-certificate-errors",))
        yield _Case(cdp, base_url, security_db, contour, method)
    finally:
        if cdp is not None:
            cdp.close()
        for server in (app_server, provider_server):
            server.close()


def _authenticate(case):
    """Drive the real login for this case; both paths end with the session
    cookie set (Google waits out the whole start->callback redirect loop)."""
    cdp, base_url = case.cdp, case.base_url
    if case.method == "password":
        cdp.open_page(base_url + "/login")
        assert cdp.eval(LOGIN_JS) == 200
    else:
        cdp.open_page(base_url + "/auth/google/start")
        cdp.wait_for(BOOTED_JS)


def _boot_dashboard(cdp, base_url):
    cdp.open_page(base_url + "/")
    cdp.wait_for(BOOTED_JS)


def _session_cookie(cdp, base_url):
    cdp.call("Network.enable")
    cookies = cdp.call("Network.getCookies", {"urls": [base_url + "/"]})[
        "cookies"
    ]
    return next(c for c in cookies if c["name"] == "aistat_session")


def _server_session_expiry(security_db):
    conn = sqlite3.connect(str(security_db))
    try:
        row = conn.execute("SELECT MAX(expires_at) FROM sessions").fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] is not None, "no server-side session row"
    return int(row[0])


def _decode_token(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _assert_cookie_is_opaque_and_hardened(cookie, server_expires):
    """AC2: the cookie is one opaque, hardened, server-bounded token."""
    token = cookie["value"]
    assert cookie["httpOnly"] is True
    assert cookie["secure"] is True
    assert cookie["sameSite"] == "Lax"
    assert cookie["path"] == "/"
    assert cookie["session"] is False, "auth cookie must be persistent, not a session cookie"
    assert "." not in token, token  # no signed/serialized envelope
    assert len(_decode_token(token)) == _TOKEN_BYTES
    # The browser cookie never outlives the authoritative server session.
    assert cookie["expires"] <= server_expires + _EXPIRY_LEEWAY_SECONDS
    assert abs(cookie["expires"] - server_expires) <= _EXPIRY_LEEWAY_SECONDS


def _assert_no_script_visible_auth(cdp, base_url, token, forbidden):
    """AC3: no token/identity/CSRF leaks into any script-reachable surface."""
    state = json.loads(cdp.eval(INSPECT_JS))
    assert "aistat_session" not in state["cookie"], state["cookie"]
    assert token not in state["cookie"], state["cookie"]
    assert state["local"] == []
    assert state["session"] == []
    for surface in (state["cookie"], "\n".join(state["local"] + state["session"])):
        for needle in forbidden:
            assert needle not in surface, needle
    cookie = _session_cookie(cdp, base_url)
    assert cookie["httpOnly"] is True
    assert cookie["value"] == token
    assert "." not in cookie["value"]


@requires_chrome
def test_opaque_session_matrix(auth_case):
    cdp, base_url = auth_case.cdp, auth_case.base_url

    # A real login for this contour+method, then a booted dashboard.
    _authenticate(auth_case)
    _boot_dashboard(cdp, base_url)

    cookie = _session_cookie(cdp, base_url)
    token = cookie["value"]
    server_expires = _server_session_expiry(auth_case.security_db)
    _assert_cookie_is_opaque_and_hardened(cookie, server_expires)

    csrf = cdp.eval(_session_csrf_js())
    forbidden = auth_case.forbidden(csrf)
    assert_opaque_session_cookie(token, forbidden)
    _assert_no_script_visible_auth(cdp, base_url, token, forbidden)

    # The protected route is reachable while the session is live.
    session_cookie = "aistat_session=" + token
    assert _api_status(base_url, "/api/meta", session_cookie) == 200

    # AC4: an invalid CSRF mutates neither the connection nor the session.
    baseline = _connection_status(cdp)
    assert _fetch_status(cdp, "POST", "/logout", csrf="wrong") == 400
    assert _fetch_status(cdp, "POST", "/api/connection", csrf="wrong") == 400
    assert _fetch_status(cdp, "POST", "/api/connection/revoke", csrf="wrong") == 400
    assert _connection_status(cdp) == baseline
    assert _api_status(base_url, "/api/meta", session_cookie) == 200

    # A second, independent session must outlive this browser's logout.
    other_cookie = "aistat_session=" + _password_session_token(base_url)
    assert _api_status(base_url, "/api/meta", other_cookie) == 200

    # A valid logout revokes only the current token; the captured cookie replays
    # dead while the independent session keeps working.
    assert cdp.eval(LOGOUT_JS) == 401
    for _ in range(3):
        assert _api_status(base_url, "/api/meta", session_cookie) == 401
    assert _api_status(base_url, "/api/meta", other_cookie) == 200


def test_browser_gate_is_certified():
    """AC6: an all-skipped browser matrix is not green evidence.

    In a certifying run (``AISTAT_REQUIRE_BROWSER`` set) a missing Chrome is a
    hard failure, so a suite that never actually drove a browser cannot pass as
    if it had. Where Chrome is present, this also pins the committed matrix to
    the full four contour/login combinations.
    """
    if CHROME is None:
        if _REQUIRE_BROWSER:
            pytest.fail(
                NO_CHROME_REASON
                + " — the real-browser opaque-session matrix was NOT executed; "
                "a certifying run (AISTAT_REQUIRE_BROWSER=1) requires a real "
                "Chrome so an all-skipped suite is never mistaken for green."
            )
        pytest.skip(
            NO_CHROME_REASON
            + " (set AISTAT_REQUIRE_BROWSER=1 to make this a hard gate)"
        )
    assert MATRIX == [
        ("flask", "password"),
        ("flask", "google"),
        ("legacy", "password"),
        ("legacy", "google"),
    ]
