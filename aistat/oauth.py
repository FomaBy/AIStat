"""Provider-independent OAuth 2.0 authorization-code core.

This module is the single, testable heart of the OAuth login flow. It is shared
by both public entry points so they behave identically:

* the dependency-free ``legacy_wsgi`` app (cPanel system Python 3.6, stdlib
  only, token exchange over ``urllib.request``);
* the Flask ``wsgi`` app.

Because ``legacy_wsgi`` targets Python 3.6 and must stay free of third-party
imports, this module uses only the standard library and avoids constructs
newer than 3.6 (no ``dataclasses``). A provider is pure configuration
(:class:`OAuthProvider`), so a new provider such as Yandex is added with data
and env vars, not by changing this core.

Security notes:

* ``state`` is generated here, persisted one-time by the account store, and
  consumed exactly once on callback; the provider recorded with the state must
  match the callback path, so a state minted for one provider cannot be
  replayed against another.
* every ``state`` is bound to the browser that started the flow: ``begin``
  records the SHA-256 hash of a short-lived browser-context token (the caller
  stores the token itself in an HttpOnly cookie), and ``finish`` rejects a
  callback whose presented token does not hash to the stored value *before*
  any token exchange. The same browser token may bind simultaneous states, so
  overlapping login tabs do not invalidate each other. A valid state alone
  therefore cannot log a different browser in (login CSRF), and no OAuth
  secret ever leaves the server.
* any terminal callback — success, provider error, missing code, provider or
  client mismatch — consumes the state, so it can never be replayed.
* token and userinfo endpoints must be HTTPS; requests are bounded by a
  timeout and a maximum response size.
* secrets (client secret, code, tokens) are never logged.
"""

import hashlib
import hmac
import json
import re
import secrets
from typing import Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

__all__ = [
    "OAuthProvider",
    "OAuthError",
    "generate_state",
    "generate_client_token",
    "is_valid_client_token",
    "client_token_hash",
    "build_authorize_url",
    "exchange_code",
    "fetch_identity",
    "begin",
    "finish",
    "providers_from_env",
    "allowed_emails_from_env",
    "is_email_authorized",
]

# Outgoing HTTPS egress to the provider is bounded so a slow or hostile
# endpoint cannot stall the worker or exhaust memory.
HTTP_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 256 * 1024
USER_AGENT = "AIStat-OAuth/1.0"
_CLIENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class OAuthError(Exception):
    """Raised for any recoverable failure while running the OAuth flow.

    The message is intentionally coarse; callers surface a generic error to the
    user and never echo provider responses or secrets.
    """


class OAuthProvider(object):
    """Immutable configuration for a single OAuth 2.0 provider.

    A provider is entirely data: adding Yandex (or any other authorization-code
    provider) is a matter of supplying these fields, with no change to the flow
    core.
    """

    __slots__ = (
        "name",
        "authorize_url",
        "token_url",
        "userinfo_url",
        "scopes",
        "client_id",
        "client_secret",
        "redirect_uri",
    )

    def __init__(
        self,
        name,
        authorize_url,
        token_url,
        userinfo_url,
        scopes,
        client_id,
        client_secret,
        redirect_uri,
    ):
        self.name = name
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.scopes = tuple(scopes)
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def __repr__(self):
        # Never render secrets.
        return "OAuthProvider(name=%r, redirect_uri=%r)" % (
            self.name,
            self.redirect_uri,
        )


def generate_state() -> str:
    """Return a fresh, unguessable ``state`` value (URL-safe, 256-bit)."""
    return secrets.token_urlsafe(32)


def generate_client_token() -> str:
    """Return a fresh short-lived token identifying the initiating browser.

    The caller hands the token to the browser in an HttpOnly cookie and passes
    it back into :func:`finish`; only its hash is persisted server-side. A
    caller may reuse the same token for simultaneous states in one browser.
    """
    return secrets.token_urlsafe(32)


def is_valid_client_token(client_token) -> bool:
    """Return whether a cookie value has the generated client-token shape."""
    return isinstance(client_token, str) and bool(
        _CLIENT_TOKEN_RE.fullmatch(client_token)
    )


def client_token_hash(client_token: str) -> str:
    """Hash a client token for storage alongside the state row."""
    return hashlib.sha256(client_token.encode("utf-8")).hexdigest()


def _require_https(url: str, what: str) -> None:
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise OAuthError(what + " endpoint must be HTTPS")


def build_authorize_url(provider: "OAuthProvider", state: str) -> str:
    """Build the provider authorization URL for ``state``.

    The user's ``next`` destination is never placed here — it is kept
    server-side with the state row — so this cannot become an open redirect.
    """
    _require_https(provider.authorize_url, "authorize")
    if not state:
        raise OAuthError("state is required")
    params = urlencode(
        {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": provider.redirect_uri,
            "scope": " ".join(provider.scopes),
            "state": state,
        }
    )
    separator = "&" if "?" in provider.authorize_url else "?"
    return provider.authorize_url + separator + params


def _read_json(response):
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OAuthError("provider response too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise OAuthError("provider returned invalid JSON")


def exchange_code(provider: "OAuthProvider", code: str) -> str:
    """Exchange an authorization ``code`` for an access token.

    Performs the server-to-server token request over HTTPS and returns the
    ``access_token`` string. Raises :class:`OAuthError` on any failure.
    """
    _require_https(provider.token_url, "token")
    if not code:
        raise OAuthError("authorization code is required")
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": provider.redirect_uri,
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
        }
    ).encode("utf-8")
    request = Request(
        provider.token_url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        response = urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
    except OSError:  # URLError/HTTPError derive from OSError
        raise OAuthError("token exchange request failed")
    try:
        payload = _read_json(response)
    finally:
        try:
            response.close()
        except Exception:
            pass
    if not isinstance(payload, dict):
        raise OAuthError("token response is not an object")
    token = payload.get("access_token")
    if not token or not isinstance(token, str):
        raise OAuthError("token response missing access_token")
    return token


def fetch_identity(
    provider: "OAuthProvider", access_token: str
) -> Tuple[str, Optional[str], Optional[str]]:
    """Resolve ``(subject, email, display_name)`` from the userinfo endpoint.

    Field names vary by provider, so common aliases are accepted: ``sub`` or
    ``id`` for the subject, ``email`` or ``default_email`` for the email, and
    ``name`` / ``display_name`` / ``real_name`` for the display name. The
    subject is required and is what an account is keyed on.
    """
    _require_https(provider.userinfo_url, "userinfo")
    if not access_token:
        raise OAuthError("access token is required")
    request = Request(
        provider.userinfo_url,
        headers={
            "Authorization": "Bearer " + access_token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        response = urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
    except OSError:
        raise OAuthError("identity request failed")
    try:
        payload = _read_json(response)
    finally:
        try:
            response.close()
        except Exception:
            pass
    if not isinstance(payload, dict):
        raise OAuthError("identity response is not an object")
    subject = payload.get("sub")
    if subject is None or subject == "":
        subject = payload.get("id")
    if subject is None or subject == "":
        raise OAuthError("identity response missing subject")
    email = payload.get("email") or payload.get("default_email")
    display_name = (
        payload.get("name")
        or payload.get("display_name")
        or payload.get("real_name")
    )
    return str(subject), email, display_name


def begin(store, provider: "OAuthProvider", next_url, client_token, now=None) -> str:
    """Start a login: mint a one-time ``state``, persist it, return the URL.

    ``next_url`` must already be a safe, same-site path (each caller sanitises
    it with its own ``safe_next_url`` helper before calling). ``client_token``
    is the short-lived browser-context token from
    :func:`generate_client_token`; the caller must deliver it to the browser
    in an HttpOnly cookie, and only its hash is stored with the state.
    """
    if not client_token:
        raise OAuthError("client token is required")
    state = generate_state()
    store.put_oauth_state(
        state,
        provider.name,
        next_url=next_url,
        client_hash=client_token_hash(client_token),
        now=now,
    )
    return build_authorize_url(provider, state)


def finish(store, provider: "OAuthProvider", params, client_token, now=None) -> dict:
    """Validate an OAuth callback and resolve the account.

    ``params`` is the callback query mapping (supports ``.get``);
    ``client_token`` is the browser-binding cookie value presented with the
    callback (``None`` when absent). Steps:

    1. consume ``state`` exactly once — every terminal callback, including a
       provider ``error``, invalidates it so it can never be replayed;
    2. reject an explicit provider ``error``;
    3. require both ``state`` and ``code``;
    4. verify the state was minted for *this* provider (an unknown,
       already-used, expired or mismatched state is rejected);
    5. verify the presented client token hashes to the value recorded when the
       flow started, so only the initiating browser can complete it — all
       before any token exchange;
    6. exchange the code and resolve the identity;
    7. map the identity to a stable AIStat user id.

    Returns ``{"user_id", "email", "display_name", "next_url"}``. Raises
    :class:`OAuthError` on any failure. Establishing a session and deciding
    whether the account is *authorised* to see data are the caller's job.
    """
    state = params.get("state")
    code = params.get("code")
    error = params.get("error")
    # Consume the state first: a callback is terminal for its state whatever
    # the outcome, so an error or mismatch cannot leave it usable later.
    record = store.take_oauth_state(state, now=now) if state else None
    if error:
        raise OAuthError("provider reported an error")
    if not state or not code:
        raise OAuthError("missing state or code")
    if not record or record.get("provider") != provider.name:
        raise OAuthError("invalid, expired or mismatched state")
    expected_hash = record.get("client_hash")
    if (
        not client_token
        or not expected_hash
        or not hmac.compare_digest(
            expected_hash, client_token_hash(client_token)
        )
    ):
        raise OAuthError("callback is not bound to the initiating browser")
    access_token = exchange_code(provider, code)
    subject, email, display_name = fetch_identity(provider, access_token)
    user_id = store.find_or_create_user_by_identity(
        provider.name,
        subject,
        email=email,
        display_name=display_name,
        now=now,
    )
    return {
        "user_id": user_id,
        "email": email,
        "display_name": display_name,
        "next_url": record.get("next_url") or "/",
    }


_PROVIDER_FIELDS = (
    "authorize_url",
    "token_url",
    "userinfo_url",
    "client_id",
    "client_secret",
    "redirect_uri",
)


def providers_from_env(environ) -> dict:
    """Build the enabled provider registry from a generic env schema.

    ``AISTAT_OAUTH_PROVIDERS`` is a comma list of provider names. For each
    ``<NAME>`` the following keys are read (prefix
    ``AISTAT_OAUTH_<NAME>_``): ``AUTHORIZE_URL``, ``TOKEN_URL``,
    ``USERINFO_URL``, ``SCOPES`` (comma/space separated), ``CLIENT_ID``,
    ``CLIENT_SECRET`` and ``REDIRECT_URI``. A provider missing any field is
    skipped rather than half-configured. The schema is deliberately generic so
    Google or Yandex are configuration only.
    """
    names = [
        item.strip().lower()
        for item in (environ.get("AISTAT_OAUTH_PROVIDERS") or "").split(",")
        if item.strip()
    ]
    providers = {}
    for name in names:
        prefix = "AISTAT_OAUTH_" + name.upper().replace("-", "_") + "_"
        values = {}
        for field in _PROVIDER_FIELDS:
            values[field] = (environ.get(prefix + field.upper()) or "").strip()
        scopes = tuple(
            token
            for token in re.split(r"[,\s]+", environ.get(prefix + "SCOPES") or "")
            if token
        )
        if not scopes or not all(values[field] for field in _PROVIDER_FIELDS):
            continue
        providers[name] = OAuthProvider(name=name, scopes=scopes, **values)
    return providers


def allowed_emails_from_env(environ) -> frozenset:
    """Return the lower-cased set of emails allowed to access private data.

    Fail-closed: an unset or empty ``AISTAT_OAUTH_ALLOWED_EMAILS`` yields an
    empty set, so a successful OAuth login authenticates an identity but grants
    no access to private statistics until an operator lists it.
    """
    raw = environ.get("AISTAT_OAUTH_ALLOWED_EMAILS") or ""
    return frozenset(
        item.strip().lower() for item in raw.split(",") if item.strip()
    )


def is_email_authorized(allowed, email) -> bool:
    """True only if ``email`` is present and appears in ``allowed`` (fail-closed)."""
    if not email or not allowed:
        return False
    return email.strip().lower() in allowed
