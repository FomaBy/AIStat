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
    "RegistrationClosedError",
    "generate_state",
    "generate_client_token",
    "is_valid_client_token",
    "client_token_hash",
    "build_authorize_url",
    "exchange_code",
    "fetch_identity",
    "normalize_email",
    "begin",
    "finish",
    "open_registration_identity",
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
# A deliberately strict, provider-agnostic ASCII address grammar used as the
# single fail-closed boundary before any account row is written. It gates open
# registration, so an exotic, non-ASCII or forged address fails closed rather
# than creating a half-identified account. IDN/EAI is explicitly out of scope
# and fails closed with everything else non-ASCII.
#
# ``RFC 5321/5322`` unquoted ``atext``: ASCII letters, digits and a fixed set of
# punctuation. The quoted-string and comment local-part forms are deliberately
# excluded, so only a plain dot-atom local part is accepted.
_LOCAL_ATEXT = r"A-Za-z0-9!#$%&'*+/=?^_`{|}~-"
# A dot-atom local part: one or more ``atext`` atoms joined by single dots, with
# no empty, leading, trailing or consecutive dots.
_LOCAL_RE = re.compile(
    r"[" + _LOCAL_ATEXT + r"]+(?:\.[" + _LOCAL_ATEXT + r"]+)*"
)
# A single LDH domain label: 1–63 ASCII letters/digits/hyphens with no leading
# or trailing hyphen.
_DOMAIN_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
# RFC 5321 length ceilings: the whole stored address and the local part.
MAX_EMAIL_LENGTH = 254
MAX_LOCAL_LENGTH = 64


class OAuthError(Exception):
    """Raised for any recoverable failure while running the OAuth flow.

    The message is intentionally coarse; callers surface a generic error to the
    user and never echo provider responses or secrets.
    """


class RegistrationClosedError(OAuthError):
    """Raised when a *new* verified identity is not permitted to register.

    Distinct from the generic :class:`OAuthError` so a contour can show the
    dedicated, non-secret "registration is closed" page for a rejected new user
    instead of the generic provider-error page. It is only ever raised for an
    unseen ``(provider, subject)``: an already-registered subject always signs
    in, whatever the current allow list. The message never reveals whether an
    email is on any list.
    """


class OAuthProvider(object):
    """Immutable configuration for a single OAuth 2.0 provider.

    A provider is entirely data: adding Yandex (or any other authorization-code
    provider) is a matter of supplying these fields, with no change to the flow
    core.

    ``assume_email_verified`` covers providers whose userinfo endpoint exposes
    only already-confirmed addresses and therefore sends no verified claim at
    all (Yandex ID's ``login.yandex.ru/info``). It is an explicit per-provider
    opt-in: when set, a *present* email counts as verified; the default keeps
    the fail-closed behaviour where an absent claim means unverified.
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
        "assume_email_verified",
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
        assume_email_verified=False,
    ):
        self.name = name
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.scopes = tuple(scopes)
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.assume_email_verified = bool(assume_email_verified)

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
    The callback (``redirect_uri``) must itself be HTTPS: the browser returns
    the authorization ``code`` there, so a plaintext callback would leak it.
    """
    _require_https(provider.authorize_url, "authorize")
    _require_https(provider.redirect_uri, "redirect")
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


def _coerce_verified(*values) -> bool:
    """Fail-closed boolean for a provider ``email_verified`` claim.

    Providers spell it ``email_verified`` (OIDC) or ``verified_email`` (Google
    v2 userinfo) and may send a JSON boolean or a string. Anything that is not
    an explicit truthy value is treated as unverified.
    """
    for value in values:
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
            return True
    return False


def fetch_identity(
    provider: "OAuthProvider", access_token: str
) -> Tuple[str, Optional[str], bool, Optional[str]]:
    """Resolve ``(subject, email, email_verified, display_name)`` from userinfo.

    Field names vary by provider, so common aliases are accepted: ``sub`` or
    ``id`` for the subject, ``email`` or ``default_email`` for the email,
    ``email_verified`` or ``verified_email`` for the verified flag (fail-closed:
    absent or non-truthy means ``False``), and ``name`` / ``display_name`` /
    ``real_name`` for the display name. The subject is required and is what an
    account is keyed on.

    A provider configured with ``assume_email_verified`` (Yandex ID exposes
    only confirmed addresses and sends no verified claim) counts a present
    email as verified; an explicit false claim from such a provider still
    wins, and a missing email stays unverified.
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
    verified_claims = (
        payload.get("email_verified"),
        payload.get("verified_email"),
    )
    email_verified = _coerce_verified(*verified_claims)
    if (
        not email_verified
        and provider.assume_email_verified
        and email
        and all(claim is None for claim in verified_claims)
    ):
        # The provider vouches for addresses by construction (opt-in via
        # configuration) and sent no claim either way; an explicit false
        # claim above still fails closed.
        email_verified = True
    display_name = (
        payload.get("name")
        or payload.get("display_name")
        or payload.get("real_name")
    )
    return str(subject), email, email_verified, display_name


def normalize_email(email) -> Optional[str]:
    """Return the canonical provider email, or ``None`` if unusable.

    This is the single, shared fail-closed boundary for a provider-supplied
    email, applied identically by both WSGI contours before any account-store
    call. It enforces one deterministic, conservative ASCII policy:

    * the value must be a ``str``; only outer ``U+0020`` SPACE is trimmed (tabs,
      newlines and every other control are structural rejects, never silently
      stripped);
    * the whole stored address is capped at 254 characters and must be pure
      ASCII — this rejects IDN/EAI, C1 controls (``U+0080``–``U+009F``) and
      Unicode format/bidi controls such as ``U+200B``/``U+202E`` in one step;
    * any remaining C0 control or ``DEL`` is rejected;
    * there must be exactly one ``@``; the local part is a 1–64 character RFC
      unquoted dot-atom (``atext`` atoms joined by single dots, with no empty,
      leading, trailing or consecutive dots); the domain is two or more LDH
      labels, each 1–63 characters with no leading or trailing hyphen.

    ``None``, a non-``str``, an empty, whitespace-only, control-bearing,
    non-ASCII or otherwise structurally malformed value all yield ``None`` so
    the caller can reject the login before any user/identity/tenant/session row
    is written — even when the provider marked the email verified.

    A usable value is returned with only outer ``U+0020`` trimmed and its case
    preserved for storage, while allow-list and owner comparison stay
    case-insensitive, so the canonical form is stable across logins.
    """
    if not isinstance(email, str):
        return None
    # Trim only outer U+0020 SPACE. Tab/newline/other whitespace is not a
    # harmless pad here — it fails closed below as a control character.
    trimmed = email.strip(" ")
    if not trimmed or len(trimmed) > MAX_EMAIL_LENGTH:
        return None
    # ASCII-only in a single check: this rejects all non-ASCII at once —
    # IDN/EAI, C1 controls (U+0080–U+009F, e.g. U+0081) and Unicode
    # format/bidi controls (e.g. U+200B, U+202E) — none of which are usable.
    try:
        trimmed.encode("ascii")
    except UnicodeEncodeError:
        return None
    # Reject any remaining C0 control or DEL (an interior tab/newline, or a
    # leading/trailing control that survived the space-only trim).
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in trimmed):
        return None
    if trimmed.count("@") != 1:
        return None
    local, domain = trimmed.split("@", 1)
    if not local or len(local) > MAX_LOCAL_LENGTH or not _LOCAL_RE.fullmatch(local):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or not all(
        _DOMAIN_LABEL_RE.fullmatch(label) for label in labels
    ):
        return None
    return trimmed


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


def finish(
    store,
    provider: "OAuthProvider",
    params,
    client_token,
    resolve_identity=None,
    now=None,
) -> dict:
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
    7. map the identity to a stable AIStat user id via ``resolve_identity``.

    ``resolve_identity(subject, email, email_verified, display_name) -> int`` is
    the account policy. It runs *after* the identity is known and may raise
    :class:`OAuthError` to reject the login before any account row is written
    (for example, the open-registration policy in
    :func:`open_registration_identity`). When it is ``None`` the identity is
    linked or created unconditionally via the store's
    ``find_or_create_user_by_identity`` (legacy open behaviour).

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
    subject, email, email_verified, display_name = fetch_identity(
        provider, access_token
    )
    if resolve_identity is None:
        user_id = store.find_or_create_user_by_identity(
            provider.name,
            subject,
            email=email,
            display_name=display_name,
            now=now,
        )
    else:
        user_id = resolve_identity(subject, email, email_verified, display_name)
    return {
        "user_id": user_id,
        # Surface the canonical trimmed email (when the address is usable) so the
        # session a contour establishes carries the same form that was stored,
        # never provider whitespace. A ``None``/absent email (legacy open path)
        # is preserved as-is.
        "email": normalize_email(email) or email,
        "display_name": display_name,
        "next_url": record.get("next_url") or "/",
    }


def open_registration_identity(
    store,
    provider_name,
    subject,
    email,
    email_verified,
    display_name,
    allowed_emails,
    admin_email,
    owner_user_id,
):
    """Open-registration sign-in policy, applied identically by both contours.

    Identity resolution is *subject-first*: an already-registered
    ``(provider, subject)`` always maps back to the same AIStat user, regardless
    of a later email change or any allow-list change, and is never merged or
    elevated. Only a brand-new subject is subjected to policy:

    * a verified email equal to the configured admin email
      (``AISTAT_ADMIN_EMAIL``) links to the pre-existing owner account — the
      owner stays the single admin and keeps its password login and tenant;
    * any other new subject registers a fresh ordinary user (``is_admin=0``)
      together with its own identity and empty tenant, in one atomic
      transaction. When an allow list is configured the email must match it
      (case-insensitive); an empty allow list admits any verified user.

    Every callback must carry a structurally valid, provider-*verified* email.
    The address is normalized once here (:func:`normalize_email`) before any
    account-store call, so ``None``, empty, whitespace-only, control-character
    and malformed values fail closed — even when ``email_verified`` is true —
    and the store only ever sees the canonical trimmed form. A new subject that
    is neither the owner nor allow-listed is rejected with
    :class:`RegistrationClosedError` and writes no user, identity, tenant or
    session row. Any store-level anomaly fails the login closed rather than
    surfacing a 500 on the public callback.
    """
    normalized_email = normalize_email(email)
    if normalized_email is None:
        # None, empty, whitespace-only, control-character-only, non-string or
        # structurally malformed: fail closed before any row is written.
        raise OAuthError("provider returned no usable email")
    if email_verified is not True:
        raise OAuthError("provider email is not verified")
    try:
        result = store.register_or_link_identity(
            provider_name,
            subject,
            email=normalized_email,
            display_name=display_name,
            admin_email=admin_email,
            allowed_emails=allowed_emails,
            owner_user_id=owner_user_id,
        )
    except OAuthError:
        raise
    except Exception:
        # A store-level anomaly (identity already linked elsewhere, a missing
        # owner row, a write failure) must fail the login closed.
        raise OAuthError("account registration failed")
    user_id = result.get("user_id") if result else None
    if user_id is None:
        # A new subject that is neither the owner nor permitted to register.
        raise RegistrationClosedError("registration is closed")
    return int(user_id)


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
    skipped rather than half-configured. The optional
    ``ASSUME_EMAIL_VERIFIED`` key (``1``/``true``/``yes``) opts the provider
    into :attr:`OAuthProvider.assume_email_verified` — required for Yandex ID,
    whose userinfo carries no verified-email claim; anything else keeps the
    fail-closed default. The schema is deliberately generic so Google or
    Yandex are configuration only.
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
        assume_email_verified = _coerce_verified(
            environ.get(prefix + "ASSUME_EMAIL_VERIFIED")
        )
        providers[name] = OAuthProvider(
            name=name,
            scopes=scopes,
            assume_email_verified=assume_email_verified,
            **values
        )
    return providers


def allowed_emails_from_env(environ) -> frozenset:
    """Return the lower-cased registration allow list.

    This gates only the *first* registration of a new provider subject (see
    :func:`open_registration_identity`): an unset or empty
    ``AISTAT_OAUTH_ALLOWED_EMAILS`` yields an empty set, which means **open**
    registration — any verified user may register — while a non-empty set
    restricts new registrations to the listed emails. It never blocks an
    already-registered account.
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
