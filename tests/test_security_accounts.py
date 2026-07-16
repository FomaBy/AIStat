"""Unit tests for the multi-user account model in SecurityStore."""

from aistat.security import OAUTH_STATE_TTL_SECONDS, SecurityStore


def test_find_or_create_user_is_idempotent_per_identity(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    first = store.find_or_create_user_by_identity(
        "google", "sub-1", email="a@example.com", display_name="Alice", now=100
    )
    again = store.find_or_create_user_by_identity(
        "google", "sub-1", email="a@example.com", display_name="Alice", now=200
    )
    assert first == again


def test_distinct_identities_map_to_distinct_users(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    alice = store.find_or_create_user_by_identity("google", "sub-1", now=100)
    bob = store.find_or_create_user_by_identity("google", "sub-2", now=100)
    assert alice != bob
    # the same subject on another provider is a separate identity/user
    cross = store.find_or_create_user_by_identity("yandex", "sub-1", now=100)
    assert cross not in (alice, bob)


def test_oauth_state_is_single_use(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    store.put_oauth_state(
        "state-1", "google", next_url="/api/meta", client_hash="hash-1", now=100
    )
    taken = store.take_oauth_state("state-1", now=110)
    assert taken == {
        "provider": "google",
        "next_url": "/api/meta",
        "client_hash": "hash-1",
    }
    # a second consumption of the same state is rejected
    assert store.take_oauth_state("state-1", now=111) is None


def test_oauth_state_unknown_is_rejected(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    assert store.take_oauth_state("never-issued", now=100) is None


def test_oauth_state_expires(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    store.put_oauth_state("state-x", "google", next_url="/", now=100)
    expired_at = 100 + OAUTH_STATE_TTL_SECONDS + 1
    assert store.take_oauth_state("state-x", now=expired_at) is None


def test_account_model_does_not_disturb_throttle_or_ingest(tmp_path):
    store = SecurityStore(tmp_path / "security.db")
    store.find_or_create_user_by_identity("google", "sub", now=100)
    store.put_oauth_state("s", "google", now=100)
    # pre-existing throttle and ingest replay state keep working
    assert store.record_login_failure("client", now=100) == 0
    assert store.login_retry_after("client", now=100) == 0
    assert store.record_ingest_timestamp(500) is True
    assert store.record_ingest_timestamp(500) is False
