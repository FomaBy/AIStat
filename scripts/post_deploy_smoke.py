#!/usr/bin/env python3
"""Bounded post-deploy HTTP smoke for the released AIStat site (FAN-3466).

Proves, against the *live* origin, that public `/healthz` answers and that an
authenticated `/api/release-identity` reports exactly the commit, tree and
manifest digest that were just published, uncacheable and unaffected by spoofed
forwarded-host/proto headers. Any other outcome is a terminal smoke failure.

The opaque session is read only from a caller-owned Netscape cookie-jar file;
the cookie never appears in argv, in the emitted evidence or in an error. The
single JSON line on stdout is a fixed allowlist of identity/verdict fields, safe
to log and to deduplicate on: no response body, no header echo, no path, no
environment value and no observed value that has not been validated as a hex
digest first.

Exit status is 0 only for PASS.
"""

import argparse
import json
import os
import socket
import ssl
import stat
import sys
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import LoadError, MozillaCookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

# Generous bound against a hostile or broken origin streaming an endless body.
MAX_BODY_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 10.0
# A correct origin sends `Cache-Control: no-store`, so any positive `Age` means
# the answer came from a cache and cannot prove what is live right now.
DEFAULT_MAX_AGE = 0
DEFAULT_MAX_CLOCK_SKEW = 300
# Host header a compliant origin must ignore. Deliberately unroutable.
SPOOF_HOST = "post-deploy-smoke.invalid"

_HEX = set("0123456789abcdef")


class SmokeFailure(Exception):
    """Terminal smoke failure carrying only a stable reason code."""

    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


class OffOriginRedirect(Exception):
    pass


def _origin(url):
    """Scheme/host/port triple, with the default port normalised away."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host, port)


class OriginBoundRedirectHandler(HTTPRedirectHandler):
    """Follows a redirect only while it stays on the expected origin.

    Refusing *before* the follow is what keeps the session cookie from ever
    being offered to another origin, whatever the cookie jar's domain rules
    would have allowed.
    """

    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != self.origin:
            raise OffOriginRedirect()
        return HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl
        )


def _is_hex(value, length):
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX


def _clean_hex(value, length):
    """Observed digests reach the evidence line only after validation."""
    return value if _is_hex(value, length) else None


def load_cookie_jar(path, reason_prefix="cookie_file"):
    """Load the caller-owned jar, refusing anything but a private regular file."""
    try:
        info = os.lstat(path)
    except OSError:
        raise SmokeFailure(reason_prefix + "_missing")
    if stat.S_ISLNK(info.st_mode):
        raise SmokeFailure(reason_prefix + "_symlink")
    if not stat.S_ISREG(info.st_mode):
        raise SmokeFailure(reason_prefix + "_not_regular")
    if info.st_uid != os.geteuid():
        raise SmokeFailure(reason_prefix + "_not_owned")
    if info.st_mode & 0o077:
        raise SmokeFailure(reason_prefix + "_permissive")
    jar = MozillaCookieJar()
    try:
        # An AIStat session cookie is a session cookie, which curl and browsers
        # both write as `expires=0` — the loader's own defaults would silently
        # drop it as discardable or expired and turn a live jar into a bare 401.
        # Liveness is the endpoint's call, not this file's: a genuinely expired
        # session still earns `identity_status_unexpected`.
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except (OSError, LoadError, ValueError):
        raise SmokeFailure(reason_prefix + "_unreadable")
    for cookie in jar:
        # `expires=0` is the Netscape encoding of "session cookie", but both the
        # loader and the send-time policy read it as "expired in 1970". Restore
        # the intended meaning; a cookie with a real past expiry keeps it and is
        # still dropped before the request.
        if not cookie.expires:
            cookie.expires = None
            cookie.discard = True
    return jar


def fetch(url, origin, timeout, jar=None, headers=None, reason_prefix="request"):
    """Return ``(status, headers, parsed_json)`` or raise ``SmokeFailure``.

    Transport, TLS and timeout failures collapse into stable reason codes; the
    origin's error body is read for nothing and never surfaces.
    """
    handlers = [OriginBoundRedirectHandler(origin)]
    if jar is not None:
        handlers.append(HTTPCookieProcessor(jar))
    opener = build_opener(*handlers)
    request = Request(url, method="GET")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        response = opener.open(request, timeout=timeout)
    except OffOriginRedirect:
        raise SmokeFailure(reason_prefix + "_redirect_off_origin")
    except HTTPError:
        # 4xx/5xx, including the 401 an unauthenticated or expired jar earns.
        raise SmokeFailure(reason_prefix + "_status_unexpected")
    except ssl.SSLError:
        raise SmokeFailure(reason_prefix + "_tls_failed")
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise SmokeFailure(reason_prefix + "_tls_failed")
        if isinstance(exc.reason, socket.timeout):
            raise SmokeFailure(reason_prefix + "_timeout")
        raise SmokeFailure(reason_prefix + "_transport_failed")
    except socket.timeout:
        raise SmokeFailure(reason_prefix + "_timeout")
    except OSError:
        raise SmokeFailure(reason_prefix + "_transport_failed")
    with response:
        if _origin(response.geturl()) != origin:
            raise SmokeFailure(reason_prefix + "_redirect_off_origin")
        if response.status != 200:
            raise SmokeFailure(reason_prefix + "_status_unexpected")
        raw = response.read(MAX_BODY_BYTES + 1)
        response_headers = response.headers
    if len(raw) > MAX_BODY_BYTES:
        raise SmokeFailure(reason_prefix + "_body_unexpected")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SmokeFailure(reason_prefix + "_body_unexpected")
    if not isinstance(body, dict):
        raise SmokeFailure(reason_prefix + "_body_unexpected")
    return response.status, response_headers, body


def check_freshness(headers, max_age, max_clock_skew, reason_prefix):
    """Refuse an answer a cache could have produced before this deploy."""
    # Every value, not just the first: an interposed cache appends its own
    # `Age`/`Date`, and reading only the origin's would hide exactly the
    # staleness this check exists to catch.
    for age in headers.get_all("Age") or ():
        try:
            age_seconds = int(age.strip())
        except (AttributeError, ValueError):
            raise SmokeFailure(reason_prefix + "_stale")
        if age_seconds > max_age:
            raise SmokeFailure(reason_prefix + "_stale")
    for date in headers.get_all("Date") or ():
        try:
            served_at = parsedate_to_datetime(date)
        except (TypeError, ValueError):
            raise SmokeFailure(reason_prefix + "_stale")
        if served_at is None:
            raise SmokeFailure(reason_prefix + "_stale")
        if served_at.tzinfo is None:
            served_at = served_at.replace(tzinfo=timezone.utc)
        skew = abs((_now() - served_at).total_seconds())
        if skew > max_clock_skew:
            raise SmokeFailure(reason_prefix + "_stale")


def _now():
    return datetime.now(timezone.utc)


def has_no_store(headers):
    """True only when every `Cache-Control` value present declares `no-store`.

    Checking just the first would let an interposed proxy append its own
    cacheable directive and still look compliant.
    """
    values = headers.get_all("Cache-Control") or ()
    if not values:
        return False
    return all(
        "no-store" in [token.strip().lower() for token in value.split(",")]
        for value in values
    )


def identity_of(body):
    return (
        body.get("source_commit_sha"),
        body.get("source_tree_sha"),
        body.get("manifest_sha256"),
    )


def run_smoke(args, evidence):
    origin = _origin(args.base_url)
    if origin[0] not in ("http", "https") or not origin[1]:
        raise SmokeFailure("base_url_invalid")
    # A typo'd `http://` would carry the session cookie in clear and make the
    # TLS requirement unverifiable, so plaintext is confined to loopback, where
    # only the local fixtures live.
    if origin[0] == "http" and origin[1] not in ("127.0.0.1", "::1", "localhost"):
        raise SmokeFailure("base_url_insecure")
    base = urlunsplit((origin[0], urlsplit(args.base_url).netloc, "", "", ""))
    jar = load_cookie_jar(args.cookie_file)

    # Public probe first, and deliberately without the jar: the cookie is only
    # ever offered to the one endpoint that needs it.
    status, _headers, body = fetch(
        base + "/healthz", origin, args.timeout, reason_prefix="healthz"
    )
    if body.get("status") != "ok":
        raise SmokeFailure("healthz_body_unexpected")
    evidence["healthz_status"] = status

    status, headers, body = fetch(
        base + "/api/release-identity",
        origin,
        args.timeout,
        jar=jar,
        reason_prefix="identity",
    )
    evidence["release_identity_status"] = status
    commit, tree, digest = identity_of(body)
    evidence["observed_commit_sha"] = _clean_hex(commit, 40)
    evidence["observed_tree_sha"] = _clean_hex(tree, 40)
    evidence["observed_manifest_sha256"] = _clean_hex(digest, 64)

    evidence["cache_control_no_store"] = has_no_store(headers)
    if not evidence["cache_control_no_store"]:
        raise SmokeFailure("identity_cache_control_missing")

    check_freshness(headers, args.max_age, args.max_clock_skew, "identity")
    evidence["freshness"] = "fresh"

    if (
        evidence["observed_commit_sha"] != args.expected_commit
        or evidence["observed_tree_sha"] != args.expected_tree
        or evidence["observed_manifest_sha256"] != args.expected_manifest_sha256
    ):
        raise SmokeFailure("identity_mismatch")

    # A host that trusts client-supplied forwarding would now redirect, reject
    # or answer differently. Identical bytes are the only accepted outcome.
    _status, _headers, spoofed = fetch(
        base + "/api/release-identity",
        origin,
        args.timeout,
        jar=jar,
        headers={"X-Forwarded-Host": SPOOF_HOST, "X-Forwarded-Proto": "http"},
        reason_prefix="proxy_spoof",
    )
    if identity_of(spoofed) != (commit, tree, digest):
        raise SmokeFailure("proxy_spoof_honored")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Post-deploy HTTP smoke against the live AIStat origin."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--cookie-file",
        required=True,
        help="caller-owned Netscape cookie jar; never a cookie literal",
    )
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE)
    parser.add_argument("--max-clock-skew", type=int, default=DEFAULT_MAX_CLOCK_SKEW)
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    evidence = {
        "timestamp": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": args.run_id or uuid.uuid4().hex,
        "result": "FAIL",
        "reason": "usage_invalid",
        "expected_commit_sha": args.expected_commit,
        "expected_tree_sha": args.expected_tree,
        "expected_manifest_sha256": args.expected_manifest_sha256,
        "observed_commit_sha": None,
        "observed_tree_sha": None,
        "observed_manifest_sha256": None,
        "healthz_status": None,
        "release_identity_status": None,
        "cache_control_no_store": None,
        "freshness": None,
    }
    if not (
        _is_hex(args.expected_commit, 40)
        and _is_hex(args.expected_tree, 40)
        and _is_hex(args.expected_manifest_sha256, 64)
        and args.timeout > 0
        and args.max_age >= 0
        and args.max_clock_skew >= 0
    ):
        print(json.dumps(evidence, sort_keys=True))
        return 2
    try:
        run_smoke(args, evidence)
    except SmokeFailure as failure:
        evidence["reason"] = failure.reason
        print(json.dumps(evidence, sort_keys=True))
        return 1
    evidence["result"] = "PASS"
    evidence["reason"] = "ok"
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
