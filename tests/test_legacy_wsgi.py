"""Dependency-free cPanel WSGI tests."""

import ast
import importlib
import io
import os
import re
import runpy
from http.cookies import SimpleCookie
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

import pytest
from werkzeug.security import generate_password_hash

from aistat.db import connect
from aistat.snapshot import create_compressed_snapshot
from conftest import seed_aggregate_fixture

PASSWORD = "correct horse battery staple"
SESSION_SECRET = "legacy-session-" + "s" * 48
INGEST_SECRET = "legacy-ingest-" + "i" * 48


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("AISTAT_DB_PATH", str(tmp_path / "public.db"))
    monkeypatch.setenv("AISTAT_SECURITY_DB_PATH", str(tmp_path / "security.db"))
    monkeypatch.setenv("AISTAT_ALLOWED_HOSTS", "localhost,aistat.app")
    monkeypatch.setenv("AISTAT_FORCE_HTTPS", "0")
    monkeypatch.setenv("AISTAT_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("AISTAT_ADMIN_USERNAME", "sergey")
    monkeypatch.setenv(
        "AISTAT_PASSWORD_HASH",
        generate_password_hash(PASSWORD, method="pbkdf2:sha256:600000"),
    )
    monkeypatch.setenv("AISTAT_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("AISTAT_INGEST_SECRET", INGEST_SECRET)

    import aistat.legacy_wsgi as module

    module = importlib.reload(module)
    conn = connect(module.DB_PATH)
    seed_aggregate_fixture(conn)
    conn.close()
    return module


def request(app, path, method="GET", body=b"", headers=None, cookie=None):
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "HTTP_HOST": "localhost",
            "HTTPS": "on",
            "wsgi.url_scheme": "https",
            "REMOTE_ADDR": "127.0.0.1",
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
        }
    )
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    for key, value in (headers or {}).items():
        normalized = key.upper().replace("-", "_")
        if normalized == "CONTENT_TYPE":
            environ["CONTENT_TYPE"] = value
        else:
            environ["HTTP_" + normalized] = value
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = response_headers

    response_body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], response_body


def header_values(headers, name):
    return [value for key, value in headers if key.lower() == name.lower()]


def cookie_jar(headers, existing=""):
    values = {}
    if existing:
        for part in existing.split("; "):
            name, value = part.split("=", 1)
            values[name] = value
    for header in header_values(headers, "Set-Cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        for name, morsel in cookie.items():
            if int(morsel["max-age"] or "1") == 0:
                values.pop(name, None)
            else:
                values[name] = morsel.value
    return "; ".join("{}={}".format(k, v) for k, v in values.items())


def login(module):
    status, headers, page = request(module.application, "/login")
    assert status == "200 OK"
    csrf = re.search(
        rb'name="csrf" value="([^"]+)"', page
    ).group(1).decode("ascii")
    cookies = cookie_jar(headers)
    body = urlencode(
        {
            "csrf": csrf,
            "username": "sergey",
            "password": PASSWORD,
            "next": "/",
        }
    ).encode("utf-8")
    status, headers, _ = request(
        module.application,
        "/login",
        method="POST",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cookie=cookies,
    )
    assert status == "303 See Other"
    return cookie_jar(headers, cookies)


def test_source_parses_as_python_36():
    source = open("aistat/legacy_wsgi.py", encoding="utf-8").read()
    ast.parse(source, filename="legacy_wsgi.py", feature_version=(3, 6))
    source = open("aistat.cgi", encoding="utf-8").read()
    ast.parse(source, filename="aistat.cgi", feature_version=(3, 6))


def test_cgi_loads_only_aistat_private_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "aistat.env"
    env_file.write_text(
        "# production settings\n"
        "AISTAT_ALLOWED_HOSTS=aistat.app\n"
        "UNRELATED_SECRET=must-not-load\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AISTAT_CGI_ENV_FILE", str(env_file))
    monkeypatch.delenv("AISTAT_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)

    namespace = runpy.run_path("aistat.cgi", run_name="aistat_cgi_test")
    namespace["_load_private_environment"]()

    assert os.environ["AISTAT_ALLOWED_HOSTS"] == "aistat.app"
    assert "UNRELATED_SECRET" not in os.environ


def test_cgi_drops_attacker_controlled_proxy_header(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid")
    namespace = runpy.run_path("aistat.cgi", run_name="aistat_cgi_test")

    namespace["_drop_untrusted_cgi_proxy"]()

    assert "HTTP_PROXY" not in os.environ


def test_login_api_and_security_headers(legacy):
    status, _, _ = request(legacy.application, "/api/meta")
    assert status == "401 Unauthorized"
    cookies = login(legacy)
    status, headers, body = request(
        legacy.application, "/api/meta", cookie=cookies
    )
    assert status == "200 OK"
    data = legacy.json.loads(body.decode("utf-8"))
    assert [p["title"] for p in data["projects"]] == ["Alpha", "Beta"]
    assert header_values(headers, "X-Frame-Options") == ["DENY"]
    assert "Secure" in cookies or "aistat_session=" in cookies


def test_logout_requires_csrf(legacy):
    cookies = login(legacy)
    status, _, body = request(
        legacy.application, "/api/session", cookie=cookies
    )
    csrf = legacy.json.loads(body.decode("utf-8"))["csrf"]
    status, _, _ = request(
        legacy.application,
        "/logout",
        method="POST",
        headers={"X-CSRF-Token": "wrong"},
        cookie=cookies,
    )
    assert status == "400 Bad Request"
    status, headers, _ = request(
        legacy.application,
        "/logout",
        method="POST",
        headers={"X-CSRF-Token": csrf},
        cookie=cookies,
    )
    assert status == "303 See Other"
    assert "Max-Age=0" in "\n".join(header_values(headers, "Set-Cookie"))


def test_logout_accepts_form_csrf_for_shared_host_waf(legacy):
    cookies = login(legacy)
    status, _, body = request(
        legacy.application, "/api/session", cookie=cookies
    )
    csrf = legacy.json.loads(body.decode("utf-8"))["csrf"]
    form = urlencode({"csrf": csrf}).encode("utf-8")

    status, headers, _ = request(
        legacy.application,
        "/logout",
        method="POST",
        body=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cookie=cookies,
    )

    assert status == "303 See Other"
    assert "Max-Age=0" in "\n".join(header_values(headers, "Set-Cookie"))


def test_signed_snapshot_ingest(legacy, tmp_path):
    source = tmp_path / "source.db"
    conn = connect(source)
    from aistat.db import init_db

    init_db(conn)
    seed_aggregate_fixture(conn)
    conn.execute(
        "UPDATE daily_usage SET input_tokens = input_tokens + 1000000 "
        "WHERE runtime_id = 'R1'"
    )
    conn.commit()
    conn.close()
    payload = create_compressed_snapshot(source)
    timestamp = int(legacy.time.time())
    signature = legacy._snapshot_signature(timestamp, payload)
    status, _, body = request(
        legacy.application,
        "/api/ingest/snapshot",
        method="POST",
        body=payload,
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Signature": signature,
        },
    )
    assert status == "200 OK"
    assert legacy.json.loads(body.decode("utf-8"))["schema_version"] == 3
    cookies = login(legacy)
    status, _, body = request(
        legacy.application, "/api/summary", cookie=cookies
    )
    assert status == "200 OK"
    assert legacy.json.loads(body.decode("utf-8"))["total_tokens"] == 5_700_000


def test_host_allowlist(legacy):
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/login",
            "HTTP_HOST": "evil.example",
            "HTTPS": "on",
            "wsgi.url_scheme": "https",
            "wsgi.input": io.BytesIO(b""),
            "CONTENT_LENGTH": "0",
        }
    )
    captured = {}
    body = b"".join(
        legacy.application(
            environ,
            lambda status, headers: captured.update(
                {"status": status, "headers": headers}
            ),
        )
    )
    assert captured["status"] == "400 Bad Request"
    assert body == b"Invalid host"
