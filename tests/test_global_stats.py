"""Anonymized cross-tenant "Efficiency by models" aggregates (FAN-2392).

Store-level tests prove the materialized rows carry only cost-relevant fields
(model, tokens, story points, priced cost), replace idempotently per tenant,
follow the FAN-1188 priced-pairing rule across tenants and disappear with the
user. Surface tests prove both hosted contours populate the store at snapshot
ingest, self-heal missed aggregation at boot, and serve only authenticated
cross-tenant sums.

Only synthetic users, secrets and databases are used.
"""

import json
import sqlite3
import time

import pytest

from aistat import global_stats
from aistat.config import Config
from aistat.db import connect, init_db
from aistat.migrate import migrate_owner_database
from aistat.security import SecurityStore, purge_user, snapshot_signature
from aistat.snapshot import create_compressed_snapshot
from aistat.wsgi import create_app
from conftest import seed_aggregate_fixture, seed_model_less_fixture

from test_wsgi import (
    INGEST_SECRET,
    login as flask_login,
    public_app,
)
from test_legacy_wsgi import (
    INGEST_SECRET as LEGACY_INGEST_SECRET,
    legacy,
    login as legacy_login,
    request as legacy_request,
)


# --------------------------------------------------------------------------- #
# Store-level unit tests
# --------------------------------------------------------------------------- #


@pytest.fixture
def stats_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "stats.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(global_stats.GLOBAL_STATS_SCHEMA)
    # The minimal slice of security.db's tenants table sync_tenants reads.
    conn.execute(
        "CREATE TABLE tenants (user_id INTEGER PRIMARY KEY, "
        "last_snapshot_sha256 TEXT)"
    )
    yield conn
    conn.close()


def build_tenant_db(path, seed=seed_aggregate_fixture):
    conn = connect(path)
    init_db(conn)
    if seed is not None:
        seed(conn)
    conn.close()
    return path


def test_refresh_and_cross_tenant_sums(stats_conn, tmp_path):
    # Two identical tenants: per tenant m-claude carries 2.5 SP / 750 tokens /
    # $0.0005 and m-shared 2.5 SP / 750 tokens / $0.002 (see test_aggregates).
    t1 = build_tenant_db(tmp_path / "1.db")
    t2 = build_tenant_db(tmp_path / "2.db")
    assert global_stats.refresh_tenant(stats_conn, 1, t1, "sha-1") is True
    assert global_stats.refresh_tenant(stats_conn, 2, t2, "sha-2") is True

    data = global_stats.model_efficiency(stats_conn)
    assert data["estimated"] is True
    assert data["tenant_count"] == 2
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]
    claude = data["models"][0]
    assert claude["story_points"] == pytest.approx(5.0)
    assert claude["total_tokens"] == 1500
    assert claude["cost_usd"] == pytest.approx(0.001)
    assert claude["cost_per_sp"] == pytest.approx(0.0002)
    assert claude["tokens_per_sp"] == pytest.approx(300.0)
    assert claude["has_unpriced"] is False
    assert claude["tenant_count"] == 2
    assert data["models"][1]["cost_per_sp"] == pytest.approx(0.0008)

    # Only cost-relevant fields are materialized: no issue/project/agent/
    # account values can leak because no column exists to hold them.
    columns = {
        row[1]
        for row in stats_conn.execute("PRAGMA table_info(global_model_stats)")
    }
    assert columns == {
        "tenant_id", "model", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "story_points",
        "cost_usd", "priced", "updated_at",
    }


def test_refresh_is_idempotent_and_replaces(stats_conn, tmp_path):
    t1 = build_tenant_db(tmp_path / "1.db")
    global_stats.refresh_tenant(stats_conn, 1, t1, "sha-1")
    global_stats.refresh_tenant(stats_conn, 1, t1, "sha-1")
    data = global_stats.model_efficiency(stats_conn)
    assert data["tenant_count"] == 1
    assert data["models"][0]["story_points"] == pytest.approx(2.5)

    # A new snapshot without measurable issues replaces the old contribution.
    empty = build_tenant_db(tmp_path / "1-empty.db", seed=None)
    assert global_stats.refresh_tenant(stats_conn, 1, empty, "sha-2") is True
    data = global_stats.model_efficiency(stats_conn)
    assert data["models"] == []
    assert data["tenant_count"] == 0


def test_unpriced_share_stays_out_of_cost_denominator(stats_conn, tmp_path):
    # Tenant 2's m-shared has no pricing row → its share is unpriced. The
    # global m-shared cost pairs tenant 1's cost with tenant 1's SP only
    # (FAN-1188), never dividing by the unpriced tenant's SP.
    t1 = build_tenant_db(tmp_path / "1.db")
    t2 = build_tenant_db(tmp_path / "2.db")
    conn = connect(t2)
    conn.execute("DELETE FROM model_pricing WHERE model = 'm-shared'")
    conn.commit()
    conn.close()
    global_stats.refresh_tenant(stats_conn, 1, t1, "sha-1")
    global_stats.refresh_tenant(stats_conn, 2, t2, "sha-2")

    by_model = {
        m["model"]: m
        for m in global_stats.model_efficiency(stats_conn)["models"]
    }
    shared = by_model["m-shared"]
    assert shared["story_points"] == pytest.approx(5.0)
    assert shared["cost_usd"] == pytest.approx(0.002)
    assert shared["cost_per_sp"] == pytest.approx(0.0008)  # 0.002 / 2.5, not / 5
    assert shared["has_unpriced"] is True
    assert by_model["m-claude"]["has_unpriced"] is False


def test_model_less_share_reports_null_model_without_cost(stats_conn, tmp_path):
    # FAN-1247: the model-less half of I8 lands on model NULL, unpriced. It
    # never prices as $0 and sorts below every priced model.
    t1 = build_tenant_db(tmp_path / "1.db")
    conn = connect(t1)
    seed_model_less_fixture(conn)
    conn.close()
    global_stats.refresh_tenant(stats_conn, 1, t1, "sha-1")

    models = global_stats.model_efficiency(stats_conn)["models"]
    assert models[-1]["model"] is None
    assert models[-1]["story_points"] == pytest.approx(2.0)
    assert models[-1]["cost_usd"] is None
    assert models[-1]["cost_per_sp"] is None
    assert models[-1]["has_unpriced"] is True


def test_refresh_refuses_unservable_schema(stats_conn, tmp_path):
    from test_security_snapshot import build_v4_database

    v4 = tmp_path / "old.db"
    build_v4_database(v4)
    assert global_stats.refresh_tenant(stats_conn, 1, v4, "sha-old") is False
    assert global_stats.model_efficiency(stats_conn)["models"] == []


def test_sync_tenants_backfills_by_snapshot_sha(stats_conn, tmp_path):
    build_tenant_db(tmp_path / "1.db")
    stats_conn.execute(
        "INSERT INTO tenants (user_id, last_snapshot_sha256) VALUES (1, 'sha-1')"
    )
    # Never published (no sha) and published-but-file-missing tenants are
    # skipped without blocking the others.
    stats_conn.execute(
        "INSERT INTO tenants (user_id, last_snapshot_sha256) VALUES (2, NULL)"
    )
    stats_conn.execute(
        "INSERT INTO tenants (user_id, last_snapshot_sha256) VALUES (3, 'sha-3')"
    )
    assert global_stats.sync_tenants(stats_conn, tmp_path) == 1
    assert global_stats.model_efficiency(stats_conn)["tenant_count"] == 1
    # Already aggregated for this sha → nothing to do.
    assert global_stats.sync_tenants(stats_conn, tmp_path) == 0
    # A new installed snapshot (different sha) is re-aggregated.
    stats_conn.execute(
        "UPDATE tenants SET last_snapshot_sha256 = 'sha-1b' WHERE user_id = 1"
    )
    assert global_stats.sync_tenants(stats_conn, tmp_path) == 1


def test_delete_user_removes_global_stats_rows(tmp_path):
    config = Config()
    config.security_db_path = tmp_path / "security.db"
    config.tenants_dir = tmp_path / "tenants"
    config.ensure_tenants_dir()
    store = SecurityStore(config.security_db_path)
    user_id = store.find_or_create_user_by_identity(
        "google", "bob-sub", email="bob@example.com", now=100
    )
    store.ensure_tenant(user_id, now=100)
    tenant_db = build_tenant_db(config.tenant_db_path(user_id))
    assert store.refresh_global_stats(user_id, tenant_db, "sha-1") is True
    assert store.global_model_efficiency()["tenant_count"] == 1

    purge_user(config, user_id)
    data = store.global_model_efficiency()
    assert data["models"] == []
    assert data["tenant_count"] == 0
    conn = sqlite3.connect(str(config.security_db_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM global_stats_tenants"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Flask contour (aistat.wsgi)
# --------------------------------------------------------------------------- #


def _flask_global(client):
    return client.get(
        "/api/global-model-efficiency", base_url="https://localhost"
    )


def test_flask_requires_login(public_app):
    app, _ = public_app
    assert _flask_global(app.test_client()).status_code == 401


def test_flask_ingest_populates_and_serves_cross_tenant_sums(
    public_app, tmp_path
):
    app, config = public_app
    client = app.test_client()
    assert flask_login(client).status_code == 303

    # The owner's migrated database was aggregated by the boot-time sync.
    data = _flask_global(client).get_json()
    assert data["tenant_count"] == 1
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]

    # A second tenant's signed snapshot ingest adds its anonymized share.
    store = SecurityStore(config.security_db_path)
    b_id = store.find_or_create_user_by_identity(
        "google", "bob-sub", email="bob@example.com", now=100
    )
    store.ensure_tenant(b_id, now=100)
    src = build_tenant_db(tmp_path / "b-src.db")
    payload = create_compressed_snapshot(src)
    timestamp = int(time.time())
    response = client.post(
        "/api/ingest/snapshot",
        data=payload,
        content_type="application/vnd.aistat.snapshot+gzip",
        headers={
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Tenant": str(b_id),
            "X-AIStat-Signature": snapshot_signature(
                INGEST_SECRET, b_id, timestamp, payload
            ),
        },
    )
    assert response.status_code == 200

    data = _flask_global(client).get_json()
    assert data["tenant_count"] == 2
    claude = data["models"][0]
    assert claude["model"] == "m-claude"
    assert claude["story_points"] == pytest.approx(5.0)
    assert claude["cost_per_sp"] == pytest.approx(0.0002)
    assert claude["tenant_count"] == 2
    # Sums only: no identity fields and no per-tenant breakdown leave the host.
    body = _flask_global(client).get_data(as_text=True)
    assert "bob" not in body
    assert set(claude) == {
        "model", "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "total_tokens", "story_points", "cost_usd",
        "tokens_per_sp", "cost_per_sp", "has_unpriced", "tenant_count",
    }

    # Deleting the user removes its contribution.
    purge_user(config, b_id)
    data = _flask_global(client).get_json()
    assert data["tenant_count"] == 1
    assert data["models"][0]["story_points"] == pytest.approx(2.5)


def test_flask_boot_sync_self_heals_missing_aggregation(public_app):
    app, config = public_app
    # Simulate a crash between snapshot install and aggregation: wipe the
    # stored rows, then boot a new worker on the same host state.
    conn = sqlite3.connect(str(config.security_db_path))
    conn.execute("DELETE FROM global_model_stats")
    conn.execute("DELETE FROM global_stats_tenants")
    conn.commit()
    conn.close()

    rebooted = create_app(config)
    rebooted.config.update(TESTING=True)
    client = rebooted.test_client()
    assert flask_login(client).status_code == 303
    data = _flask_global(client).get_json()
    assert data["tenant_count"] == 1
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]


# --------------------------------------------------------------------------- #
# Legacy cPanel contour (aistat.legacy_wsgi)
# --------------------------------------------------------------------------- #


def test_legacy_ingest_populates_and_serves_global_stats(legacy, tmp_path):
    module = legacy
    owner_id = migrate_owner_database(Config())["owner_user_id"]

    status, _, _ = legacy_request(
        module.application, "/api/global-model-efficiency"
    )
    assert status == "401 Unauthorized"

    src = build_tenant_db(tmp_path / "legacy-src.db")
    payload = create_compressed_snapshot(src)
    timestamp = int(time.time())
    status, _, _ = legacy_request(
        module.application,
        "/api/ingest/snapshot",
        method="POST",
        body=payload,
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Tenant": str(owner_id),
            "X-AIStat-Signature": snapshot_signature(
                LEGACY_INGEST_SECRET, owner_id, timestamp, payload
            ),
        },
    )
    assert status == "200 OK"

    cookies = legacy_login(module)
    status, _, body = legacy_request(
        module.application, "/api/global-model-efficiency", cookie=cookies
    )
    assert status == "200 OK"
    data = json.loads(body.decode("utf-8"))
    assert data["tenant_count"] == 1
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]
    assert data["models"][0]["cost_per_sp"] == pytest.approx(0.0002)
