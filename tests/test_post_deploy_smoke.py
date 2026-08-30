"""Executable proof for the mandatory post-deploy HTTP smoke (FAN-3466).

Every case runs against a local stdlib fixture origin. Nothing here contacts
production, reads a real session or creates a credential.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "scripts" / "post_deploy_smoke.py"

COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "c" * 64
SESSION_COOKIE = "aistat_session"
SESSION_VALUE = "fixture-opaque-session-value"

# The exact allowlist the deploy log and any deduplication may rely on.
EVIDENCE_KEYS = {
    "timestamp",
    "run_id",
    "result",
    "reason",
    "expected_commit_sha",
    "expected_tree_sha",
    "expected_manifest_sha256",
    "observed_commit_sha",
    "observed_tree_sha",
    "observed_manifest_sha256",
    "healthz_status",
    "release_identity_status",
    "cache_control_no_store",
    "freshness",
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _send(self, status, payload, headers=()):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        config = self.server.config
        self.server.requests.append((self.path, dict(self.headers)))
        if self.path == "/healthz":
            if config.get("healthz_status", 200) != 200:
                self._send(config["healthz_status"], {"detail": "nope"})
                return
            self._send(200, config.get("healthz_body", {"status": "ok", "version": "t"}))
            return
        if self.path != "/api/release-identity":
            self._send(404, {"detail": "not found"})
            return

        location = config.get("redirect_to")
        if location:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        cookie = self.headers.get("Cookie") or ""
        if "%s=%s" % (SESSION_COOKIE, SESSION_VALUE) not in cookie:
            self._send(401, {"detail": "authentication required"})
            return

        status = config.get("identity_status", 200)
        if status != 200:
            self._send(status, {"detail": "unavailable"})
            return

        headers = []
        if config.get("no_store", True):
            headers.append(("Cache-Control", "no-store"))
        headers.extend(config.get("extra_headers", ()))

        spoofed = self.headers.get("X-Forwarded-Host")
        if spoofed and config.get("trusts_forwarded_headers"):
            # A misconfigured host reflects the attacker's origin instead of
            # answering identically.
            self.send_response(302)
            self.send_header("Location", "https://%s/api/release-identity" % spoofed)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        identity = config["identity"]
        self._send(200, identity() if callable(identity) else identity, headers)


class FixtureOrigin:
    """Local stand-in for the deployed site: `/healthz` + release identity."""

    def __init__(self, **config):
        config.setdefault(
            "identity",
            {
                "source_commit_sha": COMMIT,
                "source_tree_sha": TREE,
                "manifest_sha256": DIGEST,
            },
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.config = config
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return "http://%s:%d" % (host, port)

    @property
    def requests(self):
        return self.server.requests

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def write_cookie_jar(path, url, value=SESSION_VALUE, mode=0o600):
    """A Netscape jar exactly as the caller is expected to supply one."""
    host = url.split("//", 1)[1].split(":", 1)[0]
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        + "\t".join([host, "FALSE", "/", "FALSE", "0", SESSION_COOKIE, value])
        + "\n",
        encoding="utf-8",
    )
    os.chmod(str(path), mode)
    return path


def run_smoke(origin=None, cookie_file=None, base_url=None, **overrides):
    argv = [
        sys.executable,
        str(SMOKE),
        "--base-url", base_url or origin.base_url,
        "--cookie-file", str(cookie_file),
        "--expected-commit", overrides.pop("commit", COMMIT),
        "--expected-tree", overrides.pop("tree", TREE),
        "--expected-manifest-sha256", overrides.pop("digest", DIGEST),
    ]
    for name, value in overrides.items():
        argv += ["--" + name.replace("_", "-"), str(value)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return result, json.loads(result.stdout)


@pytest.fixture
def origin():
    server = FixtureOrigin()
    yield server
    server.close()


@pytest.fixture
def jar(tmp_path, origin):
    return write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)


def test_matching_identity_passes_and_emits_only_the_allowlist(origin, jar):
    result, evidence = run_smoke(origin, jar, run_id="deploy-1")
    assert result.returncode == 0, result.stderr
    assert evidence["result"] == "PASS"
    assert evidence["reason"] == "ok"
    assert evidence["run_id"] == "deploy-1"
    assert set(evidence) == EVIDENCE_KEYS
    assert evidence["observed_commit_sha"] == COMMIT
    assert evidence["cache_control_no_store"] is True
    assert evidence["freshness"] == "fresh"
    assert evidence["healthz_status"] == 200
    assert evidence["release_identity_status"] == 200


def test_cookie_reaches_only_the_authenticated_endpoint(origin, jar):
    result, _evidence = run_smoke(origin, jar)
    assert result.returncode == 0
    for path, headers in origin.requests:
        if path == "/healthz":
            assert "Cookie" not in headers


def test_no_cookie_or_path_bytes_appear_in_any_output(origin, jar):
    result, evidence = run_smoke(origin, jar)
    combined = result.stdout + result.stderr
    assert SESSION_VALUE not in combined
    assert SESSION_COOKIE not in combined
    assert str(jar) not in combined
    assert "version" not in evidence


@pytest.mark.parametrize(
    "commit,tree,digest",
    [
        ("d" * 40, TREE, DIGEST),
        (COMMIT, "e" * 40, DIGEST),
        (COMMIT, TREE, "f" * 64),
    ],
)
def test_wrong_expected_identity_is_terminal(origin, jar, commit, tree, digest):
    result, evidence = run_smoke(origin, jar, commit=commit, tree=tree, digest=digest)
    assert result.returncode == 1
    assert evidence["reason"] == "identity_mismatch"


def test_malformed_observed_identity_is_never_echoed(tmp_path):
    origin = FixtureOrigin(
        identity={
            "source_commit_sha": "<script>alert(1)</script>",
            "source_tree_sha": TREE,
            "manifest_sha256": DIGEST,
        }
    )
    try:
        jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
        result, evidence = run_smoke(origin, jar)
    finally:
        origin.close()
    assert result.returncode == 1
    assert evidence["reason"] == "identity_mismatch"
    assert evidence["observed_commit_sha"] is None
    assert "script" not in result.stdout


def test_missing_no_store_is_terminal(tmp_path):
    origin = FixtureOrigin(no_store=False)
    try:
        jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
        result, evidence = run_smoke(origin, jar)
    finally:
        origin.close()
    assert result.returncode == 1
    assert evidence["reason"] == "identity_cache_control_missing"
    assert evidence["cache_control_no_store"] is False


@pytest.mark.parametrize(
    "extra_headers",
    [
        (("Age", "120"),),
        (("Date", "Tue, 01 Jan 2019 00:00:00 GMT"),),
    ],
)
def test_stale_response_is_terminal(tmp_path, extra_headers):
    origin = FixtureOrigin(extra_headers=extra_headers)
    try:
        jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
        result, evidence = run_smoke(origin, jar)
    finally:
        origin.close()
    assert result.returncode == 1
    assert evidence["reason"] == "identity_stale"
    assert evidence["freshness"] is None


@pytest.mark.parametrize("status", [401, 500, 503])
def test_error_status_on_the_identity_endpoint_is_terminal(tmp_path, status):
    origin = FixtureOrigin(identity_status=status)
    try:
        jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
        result, evidence = run_smoke(origin, jar)
    finally:
        origin.close()
    assert result.returncode == 1
    assert evidence["reason"] == "identity_status_unexpected"


def test_unauthenticated_jar_earns_a_terminal_failure(tmp_path, origin):
    jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url, value="expired")
    result, evidence = run_smoke(origin, jar)
    assert result.returncode == 1
    assert evidence["reason"] == "identity_status_unexpected"


def test_failing_healthz_is_terminal_before_the_cookie_is_read(tmp_path):
    origin = FixtureOrigin(healthz_status=503)
    try:
        jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
        result, evidence = run_smoke(origin, jar)
    finally:
        origin.close()
    assert result.returncode == 1
    assert evidence["reason"] == "healthz_status_unexpected"
    assert all(path == "/healthz" for path, _headers in origin.requests)


def test_redirect_off_origin_is_refused_without_following(tmp_path):
    origin = FixtureOrigin(redirect_to="https://elsewhere.invalid/api/release-identity")
    try:
        jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
        result, evidence = run_smoke(origin, jar)
    finally:
        origin.close()
    assert result.returncode == 1
    assert evidence["reason"] == "identity_redirect_off_origin"


def test_host_that_honours_spoofed_forwarded_headers_is_terminal(tmp_path):
    origin = FixtureOrigin(trusts_forwarded_headers=True)
    try:
        jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
        result, evidence = run_smoke(origin, jar)
    finally:
        origin.close()
    assert result.returncode == 1
    # The spoof probe runs only after the identity already matched.
    assert evidence["observed_commit_sha"] == COMMIT
    assert evidence["reason"] == "proxy_spoof_redirect_off_origin"


def test_unreachable_origin_is_terminal(tmp_path):
    origin = FixtureOrigin()
    base_url = origin.base_url
    jar = write_cookie_jar(tmp_path / "cookies.txt", base_url)
    origin.close()
    result, evidence = run_smoke(base_url=base_url, cookie_file=jar)
    assert result.returncode == 1
    assert evidence["reason"] in ("healthz_transport_failed", "healthz_timeout")


def test_tls_failure_against_a_plain_origin_is_terminal(tmp_path):
    origin = FixtureOrigin()
    try:
        jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
        https_url = origin.base_url.replace("http://", "https://", 1)
        result, evidence = run_smoke(base_url=https_url, cookie_file=jar)
    finally:
        origin.close()
    assert result.returncode == 1
    assert evidence["reason"].startswith("healthz_")
    assert evidence["result"] == "FAIL"


def test_missing_cookie_file_is_terminal(tmp_path, origin):
    result, evidence = run_smoke(origin, tmp_path / "absent.txt")
    assert result.returncode == 1
    assert evidence["reason"] == "cookie_file_missing"


def test_symlinked_cookie_file_is_terminal(tmp_path, origin):
    real = write_cookie_jar(tmp_path / "real.txt", origin.base_url)
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    result, evidence = run_smoke(origin, link)
    assert result.returncode == 1
    assert evidence["reason"] == "cookie_file_symlink"


def test_group_readable_cookie_file_is_terminal(tmp_path, origin):
    jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url, mode=0o640)
    result, evidence = run_smoke(origin, jar)
    assert result.returncode == 1
    assert evidence["reason"] == "cookie_file_permissive"


def test_non_regular_cookie_file_is_terminal(tmp_path, origin):
    fifo = tmp_path / "fifo"
    os.mkfifo(str(fifo), 0o600)
    result, evidence = run_smoke(origin, fifo)
    assert result.returncode == 1
    assert evidence["reason"] == "cookie_file_not_regular"


def test_unparsable_cookie_file_is_terminal_without_echoing_its_bytes(
    tmp_path, origin
):
    """The stdlib loader's own LoadError quotes the offending line verbatim."""
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        "127.0.0.1 truncated line with %s\n" % SESSION_VALUE,
        encoding="utf-8",
    )
    os.chmod(str(jar), 0o600)
    result, evidence = run_smoke(origin, jar)
    assert result.returncode == 1
    assert evidence["reason"] == "cookie_file_unreadable"
    assert SESSION_VALUE not in (result.stdout + result.stderr)


def test_plaintext_http_to_a_non_loopback_host_is_refused(tmp_path, origin):
    jar = write_cookie_jar(tmp_path / "cookies.txt", origin.base_url)
    result, evidence = run_smoke(base_url="http://aistat.invalid", cookie_file=jar)
    assert result.returncode == 1
    assert evidence["reason"] == "base_url_insecure"
    assert origin.requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"commit": "not-a-sha"},
        {"tree": "B" * 40},
        {"digest": "c" * 63},
        {"timeout": "0"},
        {"max_age": "-1"},
    ],
)
def test_invalid_arguments_fail_closed_without_contacting_the_origin(
    tmp_path, origin, overrides
):
    result, evidence = run_smoke(origin, write_cookie_jar(
        tmp_path / "cookies.txt", origin.base_url
    ), **overrides)
    assert result.returncode == 2
    assert evidence["reason"] == "usage_invalid"
    assert origin.requests == []
