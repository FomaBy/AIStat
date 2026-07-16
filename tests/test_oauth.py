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
        self._next_id = 0

    def put_oauth_state(self, state, provider, next_url=None, now=None):
        self.states[state] = {"provider": provider, "next_url": next_url}

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
        identity={"sub": "123", "email": "a@example.com", "name": "Alice"},
        captured=captured,
    )
    subject, email, display_name = oauth.fetch_identity(PROVIDER, "tok")
    assert (subject, email, display_name) == ("123", "a@example.com", "Alice")
    assert captured[0].get_header("Authorization") == "Bearer tok"


def test_fetch_identity_accepts_alternate_field_names(monkeypatch):
    # e.g. a Yandex-shaped userinfo body — same core, no code change
    fake_http(
        monkeypatch,
        identity={"id": "9", "default_email": "c@d.example", "real_name": "C"},
    )
    assert oauth.fetch_identity(PROVIDER, "tok") == ("9", "c@d.example", "C")


def test_fetch_identity_requires_subject(monkeypatch):
    fake_http(monkeypatch, identity={"email": "no-subject@example.com"})
    with pytest.raises(oauth.OAuthError):
        oauth.fetch_identity(PROVIDER, "tok")


def test_generate_state_is_unique_and_urlsafe():
    values = {oauth.generate_state() for _ in range(64)}
    assert len(values) == 64
    for value in values:
        assert value and all(c.isalnum() or c in "-_" for c in value)


def test_begin_persists_state_and_returns_authorize_url():
    store = FakeStore()
    url = oauth.begin(store, PROVIDER, "/api/meta")
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert store.states[state] == {"provider": "google", "next_url": "/api/meta"}


def test_finish_happy_path_and_single_use(monkeypatch):
    fake_http(
        monkeypatch,
        token={"access_token": "tok"},
        identity={"sub": "abc", "email": "u@example.com", "name": "U"},
    )
    store = FakeStore()
    url = oauth.begin(store, PROVIDER, "/api/summary")
    state = parse_qs(urlsplit(url).query)["state"][0]

    result = oauth.finish(store, PROVIDER, {"state": state, "code": "c"})
    assert result["email"] == "u@example.com"
    assert result["next_url"] == "/api/summary"
    assert isinstance(result["user_id"], int)

    # the same external identity maps back to the same user id
    url2 = oauth.begin(store, PROVIDER, "/")
    state2 = parse_qs(urlsplit(url2).query)["state"][0]
    again = oauth.finish(store, PROVIDER, {"state": state2, "code": "c"})
    assert again["user_id"] == result["user_id"]

    # replaying the first, already-consumed state is rejected
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": state, "code": "c"})


def test_finish_rejects_missing_state_or_code():
    store = FakeStore()
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"code": "c"})
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": "s"})


def test_finish_rejects_unknown_or_expired_state():
    # an unknown state, and an expired one, both surface from the store as None
    store = FakeStore()
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": "forged", "code": "c"})


def test_finish_rejects_state_minted_for_another_provider(monkeypatch):
    fake_http(monkeypatch)
    store = FakeStore()
    other = oauth.OAuthProvider(
        "yandex", PROVIDER.authorize_url, PROVIDER.token_url,
        PROVIDER.userinfo_url, ("email",), "c", "s", "https://app/cb"
    )
    url = oauth.begin(store, other, "/")
    state = parse_qs(urlsplit(url).query)["state"][0]
    # state was issued for yandex; replaying it against google must fail
    with pytest.raises(oauth.OAuthError):
        oauth.finish(store, PROVIDER, {"state": state, "code": "c"})


def test_finish_rejects_provider_error():
    store = FakeStore()
    with pytest.raises(oauth.OAuthError):
        oauth.finish(
            store, PROVIDER, {"error": "access_denied", "state": "x", "code": "y"}
        )


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
