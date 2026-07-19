"""Unit tests for the provider-independent OAuth authorization-code core.

The token exchange and identity fetch talk to the network, so every test that
reaches them replaces ``aistat.oauth.urlopen`` with an in-memory fake. The flow
orchestration (``begin`` / ``finish``) is driven against a fake account store so
the state/CSRF/replay guarantees are exercised in isolation from the two WSGI
contours that share this code.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from aistat import oauth

PROVIDER = oauth.OAuthProvider(
    name="google",
    authorize_url="https://accounts.example/authorize",
    token_url="https://oauth.example/token",
    userinfo_url="https://api.example/userinfo",
    scopes=("openid", "email", "profile"),
    client_id="client-id",
    client_secret="client-secret",
    redirect_uri="https://app.example/auth/google/callback",
)


class FakeResponse:
    def __init__(self, payload=None, raw=None):
        self._data = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        if size is None or size < 0:
            data, self._data = self._data, b""
            return data
        data, self._data = self._data[:size], self._data[size:]
        return data

    def close(self):
        pass


class FakeStore:
    """Minimal stand-in for SecurityStore's account/state methods."""

    def __init__(self):
        self.states = {}
        self.identities = {}
        self.owner_links = {}
        self.tenants = set()
        self.raise_on_register = False
        self._next_id = 0

    def put_oauth_state(
        self, state, provider, next_url=None, client_hash=None, now=None
    ):
        self.states[state] = {
            "provider": provider,
            "next_url": next_url,
            "client_hash": client_hash,
        }

    def take_oauth_state(self, state, now=None):
        return self.states.pop(state, None)

    def find_or_create_user_by_identity(
        self, provider, subject, email=None, display_name=None, now=None
    ):
        key = (provider, subject)
        if key not in self.identities:
            self._next_id += 1
            self.identities[key] = self._next_id
        return self.identities[key]

    def link_identity_to_owner(
        self, provider, subject, owner_user_id, email=None, now=None
    ):
        key = (provider, subject)
        linked = self.owner_links.get(key)
        if linked is not None:
            if linked != owner_user_id:
                raise ValueError("identity linked to a non-owner user")
            return linked
        self.owner_links[key] = int(owner_user_id)
        return int(owner_user_id)

    def register_or_link_identity(
        self, provider, subject, email=None, display_name=None,
        admin_email=None, allowed_emails=None, owner_user_id=None, now=None,
    ):
        """In-memory mirror of SecurityStore.register_or_link_identity."""
        if self.raise_on_register:
            raise RuntimeError("injected store anomaly")
        key = (provider, subject)
        if key in self.identities:
            return {"user_id": self.identities[key], "outcome": "existing"}
        normalized = email.strip().lower() if email else None
        owner_email = (admin_email or "").strip().lower()
        if owner_email and owner_user_id and normalized == owner_email:
            self.identities[key] = int(owner_user_id)
            self.owner_links[key] = int(owner_user_id)
            return {"user_id": int(owner_user_id), "outcome": "linked_owner"}
        if allowed_emails and (
            normalized is None or normalized not in allowed_emails
        ):
            return {"user_id": None, "outcome": "denied"}
        self._next_id += 1
        self.identities[key] = self._next_id
        self.tenants.add(self._next_id)
        return {"user_id": self._next_id, "outcome": "created"}


def fake_http(monkeypatch, token=None, identity=None, captured=None):
    def fake_urlopen(request, timeout=None):
        if captured is not None:
            captured.append(request)
        url = request.full_url
        if url == PROVIDER.token_url:
            return FakeResponse(token if token is not None else {"access_token": "at"})
        if url == PROVIDER.userinfo_url:
            return FakeResponse(identity if identity is not None else {"sub": "s"})
        raise AssertionError("unexpected URL: " + url)

    monkeypatch.setattr(oauth, "urlopen", fake_urlopen)


def test_build_authorize_url_has_params_and_no_secret():
    url = oauth.build_authorize_url(PROVIDER, "STATE-1")
    split = urlsplit(url)
    assert url.startswith(PROVIDER.authorize_url)
    query = parse_qs(split.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [PROVIDER.redirect_uri]
    assert query["scope"] == ["openid email profile"]
    assert query["state"] == ["STATE-1"]
    # the client secret is never exposed in a front-channel URL
    assert "client-secret" not in url


def test_build_authorize_url_requires_https():
    insecure = oauth.OAuthProvider(
        "p", "http://insecure/authorize", PROVIDER.token_url,
        PROVIDER.userinfo_url, ("email",), "c", "s", "https://app/cb"
    )
    with pytest.raises(oauth.OAuthError):
        oauth.build_authorize_url(insecure, "s")


def test_exchange_code_posts_form_and_returns_token(monkeypatch):
    captured = []
    fake_http(monkeypatch, token={"access_token": "tok-123"}, captured=captured)
    token = oauth.exchange_code(PROVIDER, "auth-code")
    assert token == "tok-123"
    request = captured[0]
    assert request.get_method() == "POST"
    assert request.full_url == PROVIDER.token_url
    body = parse_qs(request.data.decode("utf-8"))
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["auth-code"]
    assert body["client_secret"] == ["client-secret"]
    assert body["redirect_uri"] == [PROVIDER.redirect_uri]


def test_exchange_code_missing_token_raises(monkeypatch):
    fake_http(monkeypatch, token={"error": "invalid_grant"})
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code(PROVIDER, "auth-code")


def test_exchange_code_requires_https():
    insecure = oauth.OAuthProvider(
        "p", PROVIDER.authorize_url, "http://insecure/token",
        PROVIDER.userinfo_url, ("email",), "c", "s", "https://app/cb"
    )
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code(insecure, "code")


def test_fetch_identity_google_shape(monkeypatch):
    captured = []
    fake_http(
        monkeypatch,
        identity={
            "sub": "123",
            "email": "a@example.com",
            "email_verified": True,
            "name": "Alice",
        },
        captured=captured,
    )
    identity = oauth.fetch_identity(PROVIDER, "tok")
    assert identity == ("123", "a@example.com", True, "Alice")
    assert captured[0].get_header("Authorization") == "Bearer tok"


def test_fetch_identity_accepts_alternate_field_names(monkeypatch):
    # e.g. a Yandex-shaped userinfo body — same core, no code change. The
    # verified flag also accepts the string form some providers send.
    fake_http(
        monkeypatch,
        identity={
            "id": "9",
            "default_email": "c@d.example",
            "verified_email": "true",
            "real_name": "C",
        },
    )
    assert oauth.fetch_identity(PROVIDER, "tok") == ("9", "c@d.example", True, "C")


def test_fetch_identity_unverified_email_is_fail_closed(monkeypatch):
    # absent verified flag -> False; an explicit false stays false
    fake_http(monkeypatch, identity={"sub": "1", "email": "u@e.com"})
    assert oauth.fetch_identity(PROVIDER, "tok") == ("1", "u@e.com", False, None)
    fake_http(
        monkeypatch,
        identity={"sub": "1", "email": "u@e.com", "email_verified": False},
    )
    assert oauth.fetch_identity(PROVIDER, "tok")[2] is False


def test_fetch_identity_requires_subject(monkeypatch):
    fake_http(monkeypatch, identity={"email": "no-subject@example.com"})
    with pytest.raises(oauth.OAuthError):
        oauth.fetch_identity(PROVIDER, "tok")


def test_generate_state_is_unique_and_urlsafe():
    values = {oauth.generate_state() for _ in range(64)}
    assert len(values) == 64
    for value in values:
        assert value and all(c.isalnum() or c in "-_" for c in value)


def test_generate_client_token_has_validated_cookie_shape():
    values = {oauth.generate_client_token() for _ in range(64)}
    assert len(values) == 64
    assert all(oauth.is_valid_client_token(value) for value in values)
    assert not oauth.is_valid_client_token(None)
    assert not oauth.is_valid_client_token("")
    assert not oauth.is_valid_client_token("invalid;cookie")


def test_begin_persists_state_and_returns_authorize_url():
    store = FakeStore()
    url = oauth.begin(store, PROVIDER, "/api/meta", "browser-token")
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert store.states[state] == {
        "provider": "google",
        "next_url": "/api/meta",
        "client_hash": oauth.client_token_hash("browser-token"),
    }
    # only the hash of the browser token is persisted, never the token itself
    assert "browser-token" not in str(store.states[state])


def test_begin_requires_client_token():
    store = FakeStore()
    with pytest.raises(oauth.OAuthError):
        oauth.begin(store, PROVIDER, "/", "")
    assert store.states == {}


def test_finish_happy_path_and_single_use(monkeypatch):
    fake_http(
        monkeypatch,
        token={"access_token": "tok"},
        identity={"sub": "abc", "email": "u@example.com", "name": "U"},
    )
    store = FakeStore()
    url = oauth.begin(store, PROVIDER, "/api/summary", "tok-a")
    state = parse_qs(urlsplit(url).query)["state"][0]

    result = oauth.finish(store, PROVIDER, {"state": state, "code": "c"}, "tok-a")
    assert result["email"] == "u@example.com"
    assert result["next_url"] == "/api/summary"
    assert isinstance(result["user_id"], int)

    # the same external identity maps back to the same user id
    url2 = oauth.begin(store, PROVIDER, "/", "tok-b")
    state2 = parse_qs(urlsplit(url2).query)["state"][0]
    again = oauth.finish(store, PROVIDER, {"state": state2, "code": "c"}, "tok-b")
    assert again["user_id"] == result["user_id"]

    # replaying the first, already-consumed state is rejected
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": state, "code": "c"}, "tok-a")


def test_finish_rejects_missing_state_or_code():
    store = FakeStore()
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"code": "c"}, "tok")
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": "s"}, "tok")


def test_finish_missing_code_still_consumes_state(monkeypatch):
    # a callback that arrives without a code is terminal for its state
    captured = []
    fake_http(monkeypatch, captured=captured)
    store = FakeStore()
    url = oauth.begin(store, PROVIDER, "/", "tok")
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": state}, "tok")
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": state, "code": "c"}, "tok")
    assert captured == []


def test_finish_rejects_unknown_or_expired_state():
    # an unknown state, and an expired one, both surface from the store as None
    store = FakeStore()
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": "forged", "code": "c"}, "tok")


def test_finish_rejects_state_minted_for_another_provider(monkeypatch):
    fake_http(monkeypatch)
    store = FakeStore()
    other = oauth.OAuthProvider(
        "yandex", PROVIDER.authorize_url, PROVIDER.token_url,
        PROVIDER.userinfo_url, ("email",), "c", "s", "https://app/cb"
    )
    url = oauth.begin(store, other, "/", "tok")
    state = parse_qs(urlsplit(url).query)["state"][0]
    # state was issued for yandex; replaying it against google must fail
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": state, "code": "c"}, "tok")
    # the mismatch consumed the state, so it is dead for yandex too
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, other, {"state": state, "code": "c"}, "tok")


def test_finish_rejects_provider_error():
    store = FakeStore()
    with pytest.raises(oauth.OAuthError):
        oauth.finish(
            store,
            PROVIDER,
            {"error": "access_denied", "state": "x", "code": "y"},
            "tok",
        )


def test_finish_provider_error_consumes_state(monkeypatch):
    # error-then-code reuse: the errored callback must burn the state
    captured = []
    fake_http(monkeypatch, captured=captured)
    store = FakeStore()
    url = oauth.begin(store, PROVIDER, "/", "tok")
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(oauth.OAuthError):
        oauth.finish(
            store, PROVIDER, {"error": "access_denied", "state": state}, "tok"
        )
    assert state not in store.states
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": state, "code": "c"}, "tok")
    assert captured == []


@pytest.mark.parametrize("wrong_token", ["attacker-token", None, ""])
def test_finish_rejects_callback_from_another_client(
    monkeypatch, wrong_token
):
    # a valid state presented by a browser that did not start the flow is
    # rejected before any token exchange, and the state is consumed
    captured = []
    fake_http(monkeypatch, captured=captured)
    store = FakeStore()
    url = oauth.begin(store, PROVIDER, "/", "victim-token")
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(oauth.OAuthError):
        oauth.finish(
            store,
            PROVIDER,
            {"state": state, "code": "c"},
            wrong_token,
        )
    assert state not in store.states
    assert captured == []


def test_finish_rejects_state_without_client_binding(monkeypatch):
    # a pre-migration row with no stored client hash is rejected fail-closed
    captured = []
    fake_http(monkeypatch, captured=captured)
    store = FakeStore()
    store.states["legacy-state"] = {
        "provider": "google",
        "next_url": "/",
        "client_hash": None,
    }
    with pytest.raises(oauth.OAuthError):
        oauth.finish(
            store, PROVIDER, {"state": "legacy-state", "code": "c"}, "tok"
        )
    assert captured == []


def test_providers_from_env_builds_generic_provider():
    env = {
        "AISTAT_OAUTH_PROVIDERS": "google, yandex",
        "AISTAT_OAUTH_GOOGLE_AUTHORIZE_URL": "https://a/authorize",
        "AISTAT_OAUTH_GOOGLE_TOKEN_URL": "https://a/token",
        "AISTAT_OAUTH_GOOGLE_USERINFO_URL": "https://a/userinfo",
        "AISTAT_OAUTH_GOOGLE_SCOPES": "openid email profile",
        "AISTAT_OAUTH_GOOGLE_CLIENT_ID": "cid",
        "AISTAT_OAUTH_GOOGLE_CLIENT_SECRET": "secret",
        "AISTAT_OAUTH_GOOGLE_REDIRECT_URI": "https://app/auth/google/callback",
        # yandex is listed but only partially configured -> skipped
        "AISTAT_OAUTH_YANDEX_AUTHORIZE_URL": "https://y/authorize",
    }
    providers = oauth.providers_from_env(env)
    assert set(providers) == {"google"}
    google = providers["google"]
    assert google.scopes == ("openid", "email", "profile")
    assert google.client_secret == "secret"


def test_is_email_authorized_is_fail_closed():
    assert oauth.is_email_authorized(frozenset(), "a@b.c") is False
    assert oauth.is_email_authorized(frozenset({"a@b.c"}), None) is False
    assert oauth.is_email_authorized(frozenset({"a@b.c"}), "A@B.C") is True
    assert oauth.allowed_emails_from_env({}) == frozenset()


def test_build_authorize_url_requires_https_redirect():
    # the callback must be HTTPS: a plaintext redirect_uri would leak the code
    insecure = oauth.OAuthProvider(
        "p", PROVIDER.authorize_url, PROVIDER.token_url, PROVIDER.userinfo_url,
        ("email",), "c", "s", "http://app/cb"
    )
    with pytest.raises(oauth.OAuthError):
        oauth.build_authorize_url(insecure, "state")


OWNER_ALLOWED = frozenset({"owner@example.com"})


def _register(store, subject, email, email_verified=True, display_name="N",
              allowed_emails=frozenset(), admin_email="owner@example.com",
              owner_user_id=7):
    return oauth.open_registration_identity(
        store, "google", subject, email, email_verified, display_name,
        allowed_emails=allowed_emails, admin_email=admin_email,
        owner_user_id=owner_user_id,
    )


def test_open_registration_creates_ordinary_user_on_empty_allowlist():
    # an empty allow list admits any verified user as a fresh ordinary account
    store = FakeStore()
    uid = _register(store, "sub-1", "new@example.com")
    assert isinstance(uid, int)
    assert uid != 7  # not the owner
    # the new user got its own tenant registry row
    assert uid in store.tenants


def test_open_registration_is_subject_first_across_email_and_allowlist():
    # the same subject always maps back to the same user, whatever the email
    # or allow list later become
    store = FakeStore()
    first = _register(store, "sub-1", "a@example.com")
    # email changed at the provider -> still the same account, no second user
    again = _register(store, "sub-1", "b@example.com")
    assert again == first
    # and even once an allow list is configured that would exclude them, an
    # already-registered subject still signs in
    still = _register(
        store, "sub-1", "a@example.com",
        allowed_emails=frozenset({"someone@else.com"}),
    )
    assert still == first


def test_open_registration_distinct_subjects_same_email_are_distinct_users():
    store = FakeStore()
    one = _register(store, "sub-1", "shared@example.com")
    two = _register(store, "sub-2", "shared@example.com")
    assert one != two


def test_open_registration_links_owner_by_admin_email():
    # a new subject whose verified email is the admin email links to the owner
    store = FakeStore()
    uid = _register(
        store, "owner-sub", "Owner@Example.com",
        admin_email="owner@example.com", owner_user_id=7,
    )
    assert uid == 7
    assert store.owner_links == {("google", "owner-sub"): 7}
    # no fresh tenant is minted for the owner-linked identity
    assert store.tenants == set()


def test_open_registration_allowlist_admits_listed_and_denies_outsider():
    listed = FakeStore()
    uid = _register(
        listed, "sub-in", "Owner@Example.com", allowed_emails=OWNER_ALLOWED,
    )
    assert isinstance(uid, int)

    outsider = FakeStore()
    with pytest.raises(oauth.RegistrationClosedError):
        _register(
            outsider, "sub-out", "stranger@example.com",
            allowed_emails=OWNER_ALLOWED,
        )
    # a denied new subject writes no identity or tenant row
    assert outsider.identities == {}
    assert outsider.tenants == set()


@pytest.mark.parametrize(
    ("email", "email_verified"),
    [
        (None, True),                  # missing email
        ("u@example.com", False),      # unverified email
        ("", True),                    # empty email
    ],
)
def test_open_registration_rejects_missing_or_unverified_email(
    email, email_verified
):
    store = FakeStore()
    with pytest.raises(oauth.OAuthError) as excinfo:
        _register(store, "sub-x", email, email_verified=email_verified)
    # a plain OAuthError (generic), not the closed-registration variant
    assert not isinstance(excinfo.value, oauth.RegistrationClosedError)
    assert store.identities == {}


def test_open_registration_wraps_store_anomaly_as_oauth_error():
    store = FakeStore()
    store.raise_on_register = True
    with pytest.raises(oauth.OAuthError) as excinfo:
        _register(store, "sub-x", "owner@example.com")
    # a store anomaly fails closed as a generic error, never a 500
    assert not isinstance(excinfo.value, oauth.RegistrationClosedError)
