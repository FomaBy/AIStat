"""Proxy-trust boundary of the public WSGI app (FAN-3458).

``AISTAT_PROXY_TRUST_HOPS`` (``Config.proxy_trust_hops``) must default to 0:
with no trusted proxy, every ``X-Forwarded-*`` header is untrusted client
input. These tests prove both directions against the real request boundary:

* negative (default, hops=0): spoofed ``X-Forwarded-Host``/``X-Forwarded-Proto``
  cannot pass the host allow-list, cannot satisfy ``force_https`` and cannot
  poison the login-throttle client key;
* positive (hops=1): headers appended by the one trusted terminating proxy are
  honoured, and only the right-most forwarded value is consumed, so a client
  that prepends its own ``X-Forwarded-For`` entry cannot choose the address
  the app throttles by.
"""

import re

import pytest
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

from aistat.config import Config
from aistat.wsgi import create_app

PASSWORD = "correct horse battery staple"
SESSION_SECRET = "session-" + "s" * 48


def build_app(tmp_path, *, hops, force_https=True, allowed=("localhost", "testserver")):
    config = Config()
    config.db_path = tmp_path / "public.db"
    config.security_db_path = tmp_path / "security.db"
    config.tenants_dir = tmp_path / "tenants"
    config.auth_username = "sergey"
    config.auth_password_hash = generate_password_hash(
        PASSWORD, method="pbkdf2:sha256:600000"
    )
    config.session_secret = SESSION_SECRET
    config.ingest_secret = "ingest-" + "i" * 48
    config.allowed_hosts = allowed
    config.force_https = force_https
    config.session_cookie_secure = force_https
    config.proxy_trust_hops = hops
    app = create_app(config)
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def hop0_app(tmp_path):
    return build_app(tmp_path, hops=0)


@pytest.fixture
def hop1_app(tmp_path):
    return build_app(tmp_path, hops=1)


# --- default: no proxy is trusted -----------------------------------------


def test_default_trusts_no_proxy(hop0_app):
    """Default 0 leaves the WSGI stack unwrapped, not unconditionally fixed."""
    assert not isinstance(hop0_app.wsgi_app, ProxyFix)


def test_spoofed_forwarded_proto_cannot_satisfy_force_https(hop0_app):
    client = hop0_app.test_client()
    response = client.get(
        "/healthz",
        base_url="http://localhost",
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 308
    assert response.headers["Location"].startswith("https://")


def test_spoofed_forwarded_host_cannot_pass_host_gate(hop1_app):
    """With one trusted hop the forwarded host is authoritative — and it must
    then still face the allow-list, so a proxied spoofed host is rejected."""
    client = hop1_app.test_client()
    response = client.get(
        "/healthz",
        base_url="https://localhost",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "evil.example",
        },
    )
    assert response.status_code == 400


def test_unproxied_spoofed_forwarded_host_is_ignored(hop0_app):
    """No trusted hop: the forwarded host never replaces the real Host.

    force_https is on, so an honoured ``X-Forwarded-Host: evil.example`` would
    have failed the host allow-list with 400; the plain 308 HTTPS redirect
    proves the header was ignored and the real Host (localhost) was kept.
    """
    client = hop0_app.test_client()
    response = client.get(
        "/healthz",
        base_url="http://localhost",
        headers={"X-Forwarded-Host": "evil.example"},
    )
    assert response.status_code == 308


def _csrf_from(page):
    match = re.search(r'name="csrf" value="([^"]+)"', page.get_data(as_text=True))
    assert match
    return match.group(1)


def test_spoofed_forwarded_for_cannot_choose_throttle_key(hop1_app):
    """Only the right-most X-Forwarded-For entry is consumed (x_for=1).

    Exhaust the throttle via ``9.9.9.9, 10.0.0.1`` (client-spoofed prefix plus
    the proxy-appended address). If the app trusted the left-most value the
    key would be 9.9.9.9; a follow-up from ``10.0.0.1`` alone would then be
    fresh. Instead it must hit the same 10.0.0.1 key and be throttled.
    """
    client = hop1_app.test_client()
    for _ in range(5):
        # The fifth failure already answers 429 from inside the loop.
        assert _failed_login_with_for(client, "9.9.9.9, 10.0.0.1") in (401, 429)
    assert _failed_login_with_for(client, "10.0.0.1") == 429


def _failed_login_with_for(client, forwarded_for):
    page = client.get(
        "/login", base_url="https://localhost", headers={"X-Forwarded-For": forwarded_for}
    )
    return client.post(
        "/login",
        data={"csrf": _csrf_from(page), "username": "sergey", "password": "wrong"},
        base_url="https://localhost",
        headers={"X-Forwarded-For": forwarded_for},
    ).status_code


# --- one trusted terminating proxy ----------------------------------------


def test_trusted_forwarded_proto_satisfies_force_https(hop1_app):
    client = hop1_app.test_client()
    response = client.get(
        "/healthz",
        base_url="http://localhost",
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 200


def test_trusted_forwarded_host_is_allowed(hop1_app):
    client = hop1_app.test_client()
    response = client.get(
        "/healthz",
        base_url="https://localhost",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "localhost"},
    )
    assert response.status_code == 200


def test_hops_are_explicit_in_the_wrapper(hop1_app):
    wrapper = hop1_app.wsgi_app
    assert isinstance(wrapper, ProxyFix)
    assert wrapper.x_for == 1
    assert wrapper.x_proto == 1
    assert wrapper.x_host == 1
