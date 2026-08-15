"""Cross-tenant "Efficiency by models" aggregates (FAN-2392, FAN-2397).

Store-level tests prove the materialized rows carry only cost-relevant fields
(model, tokens, story points, priced cost), replace idempotently per tenant,
follow the FAN-1188 priced-pairing rule across tenants and disappear with the
user. The FAN-2397 privacy tests prove the published result is an anonymized
aggregate: every model cohort under ``MIN_TENANTS`` distinct tenants is
suppressed whole — never reduced, never fallen back to single-tenant data —
and no contributor count reaches the response. Surface tests prove both hosted
contours populate the store at snapshot ingest, self-heal missed aggregation
at boot, and serve the same suppressed-or-anonymized contract to an
authenticated session only.

Only synthetic users, secrets and databases are used.
"""

import ast
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import aistat
from aistat import global_stats
from aistat.config import Config
from aistat.db import connect, init_db
from aistat.migrate import migrate_owner_database
from aistat.security import SecurityStore, purge_user, snapshot_signature
from aistat.snapshot import create_compressed_snapshot
from aistat.tenant import tenant_db_path
from aistat.wsgi import create_app
from conftest import seed_aggregate_fixture, seed_model_less_fixture

from cdp_harness import BOOTED_JS, CHROME, NO_CHROME_REASON
from test_dashboard_browser import dashboard
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


MIN_TENANTS = global_stats.MIN_TENANTS

# Every field the anonymized per-model result may carry. A contributor count
# is deliberately absent: it is not needed to compute cost and would expose a
# rare cohort (FAN-2397 acceptance criterion 3).
MODEL_FIELDS = {
    "model", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "total_tokens", "story_points", "cost_usd",
    "tokens_per_sp", "cost_per_sp", "has_unpriced",
}


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


def contribute(stats_conn, tmp_path, tenant_ids, seed=seed_aggregate_fixture):
    """Publish one identical contribution per tenant id.

    Tenant databases are named ``<id>.db`` so ``tmp_path`` doubles as the
    tenants directory :func:`global_stats.sync_tenants` scans.
    """
    for tenant_id in tenant_ids:
        path = build_tenant_db(tmp_path / "{}.db".format(tenant_id), seed=seed)
        assert global_stats.refresh_tenant(
            stats_conn, tenant_id, path, "sha-{}".format(tenant_id)
        ) is True


def insert_row(conn, tenant_id, model, story_points=1.0, cost_usd=0.5,
               priced=1):
    """One already-materialized contribution, for cohort-shape tests."""
    conn.execute(
        "INSERT INTO global_model_stats (tenant_id, model, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, story_points, "
        "cost_usd, priced, updated_at) VALUES (?, ?, 10, 20, 30, 40, ?, ?, ?, 0)",
        (tenant_id, model, story_points, cost_usd, priced),
    )


@pytest.mark.parametrize("tenants", [1, 2, 3, 4])
def test_cohort_below_minimum_is_suppressed_whole(stats_conn, tmp_path, tenants):
    """FAN-2397 criterion 2: fewer than five contributing tenants publishes
    nothing at all. The suppressed result is a normal empty result — never a
    reduced figure and never a fallback to the surviving tenants' data."""
    contribute(stats_conn, tmp_path, range(1, tenants + 1))
    # The contributions really are stored; only publication is withheld.
    stored = stats_conn.execute(
        "SELECT COUNT(*) FROM global_model_stats"
    ).fetchone()[0]
    assert stored == tenants * 2

    assert global_stats.model_efficiency(stats_conn) == {
        "estimated": True, "models": []
    }


def test_fifth_tenant_publishes_the_cohort(stats_conn, tmp_path):
    """FAN-2397 criterion 2, positive boundary: the cohort becomes publishable
    at exactly ``MIN_TENANTS`` and then carries every tenant's share."""
    assert MIN_TENANTS == 5
    contribute(stats_conn, tmp_path, range(1, MIN_TENANTS))
    assert global_stats.model_efficiency(stats_conn)["models"] == []

    contribute(stats_conn, tmp_path, [MIN_TENANTS])
    data = global_stats.model_efficiency(stats_conn)
    assert data["estimated"] is True
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]
    # Per tenant m-claude carries 2.5 SP / 750 tokens / $0.0005 and m-shared
    # 2.5 SP / 750 tokens / $0.002 (see test_aggregates), times five tenants.
    claude = data["models"][0]
    assert claude["story_points"] == pytest.approx(12.5)
    assert claude["total_tokens"] == 3750
    assert claude["cost_usd"] == pytest.approx(0.0025)
    assert claude["cost_per_sp"] == pytest.approx(0.0002)
    assert claude["tokens_per_sp"] == pytest.approx(300.0)
    assert claude["has_unpriced"] is False
    assert data["models"][1]["cost_per_sp"] == pytest.approx(0.0008)


def test_suppression_is_per_model_cohort(stats_conn):
    """A rare model is dropped while a popular one publishes, and the dropped
    rows contribute nothing to the surviving cohort's sums."""
    for tenant_id in range(1, MIN_TENANTS + 1):
        insert_row(stats_conn, tenant_id, "popular", story_points=2.0)
    for tenant_id in range(1, MIN_TENANTS):        # one short of the minimum
        insert_row(stats_conn, tenant_id, "rare", story_points=99.0)
    insert_row(stats_conn, 42, "solo", story_points=7.0)

    models = global_stats.model_efficiency(stats_conn)["models"]
    assert [m["model"] for m in models] == ["popular"]
    assert models[0]["story_points"] == pytest.approx(2.0 * MIN_TENANTS)


def test_repeat_contributions_by_one_tenant_never_reach_the_minimum(stats_conn):
    """The threshold counts distinct tenants, so one tenant's many models
    (or a replayed row) can never unlock publication on its own."""
    for model in ("a", "b", "c", "d", "e", "f"):
        insert_row(stats_conn, 1, model)
    assert global_stats.model_efficiency(stats_conn)["models"] == []


def test_result_exposes_only_allowlisted_cost_fields(stats_conn, tmp_path):
    """FAN-2397 criterion 3: neither a global nor a per-model contributor
    count leaves the store, at any depth of the response."""
    contribute(stats_conn, tmp_path, range(1, MIN_TENANTS + 1))
    data = global_stats.model_efficiency(stats_conn)

    assert set(data) == {"estimated", "models"}
    assert data["models"]
    for model in data["models"]:
        assert set(model) == MODEL_FIELDS

    blob = json.dumps(data).lower()
    for banned in ("tenant", "count", "user", "contributor"):
        assert banned not in blob, banned


def test_refresh_is_idempotent_and_replacement_can_suppress(stats_conn, tmp_path):
    contribute(stats_conn, tmp_path, range(1, MIN_TENANTS + 1))
    # Replaying the very same snapshot replaces each contribution instead of
    # accumulating a second copy of it.
    for tenant_id in range(1, MIN_TENANTS + 1):
        assert global_stats.refresh_tenant(
            stats_conn, tenant_id, tmp_path / "{}.db".format(tenant_id),
            "sha-{}".format(tenant_id),
        ) is True
    data = global_stats.model_efficiency(stats_conn)
    assert data["models"][0]["story_points"] == pytest.approx(12.5)

    # A tenant's next snapshot has nothing measurable: its contribution is
    # replaced (not added to), the cohort drops to four and publication stops.
    empty = build_tenant_db(tmp_path / "1-empty.db", seed=None)
    assert global_stats.refresh_tenant(stats_conn, 1, empty, "sha-1b") is True
    assert global_stats.model_efficiency(stats_conn)["models"] == []
    assert stats_conn.execute(
        "SELECT COUNT(*) FROM global_model_stats WHERE tenant_id = 1"
    ).fetchone()[0] == 0


def test_unpriced_share_stays_out_of_cost_denominator(stats_conn, tmp_path):
    """One tenant's m-shared has no pricing row → its share is unpriced. The
    global m-shared cost pairs the priced tenants' cost with their SP only
    (FAN-1188), never dividing by the unpriced tenant's SP."""
    contribute(stats_conn, tmp_path, range(1, MIN_TENANTS + 1))
    unpriced = build_tenant_db(tmp_path / "unpriced.db")
    conn = connect(unpriced)
    conn.execute("DELETE FROM model_pricing WHERE model = 'm-shared'")
    conn.commit()
    conn.close()
    assert global_stats.refresh_tenant(
        stats_conn, 1, unpriced, "sha-1b"
    ) is True

    by_model = {
        m["model"]: m
        for m in global_stats.model_efficiency(stats_conn)["models"]
    }
    shared = by_model["m-shared"]
    assert shared["story_points"] == pytest.approx(12.5)     # all five tenants
    assert shared["cost_usd"] == pytest.approx(0.008)        # four priced ones
    assert shared["cost_per_sp"] == pytest.approx(0.0008)    # 0.008 / 10, not / 12.5
    assert shared["has_unpriced"] is True
    assert by_model["m-claude"]["has_unpriced"] is False


def test_model_less_share_reports_null_model_without_cost(stats_conn, tmp_path):
    """FAN-1247: the model-less half of I8 lands on model NULL, unpriced. It
    never prices as $0 and sorts below every priced model. The NULL cohort
    obeys the same minimum as a named one."""
    contribute(
        stats_conn, tmp_path, range(1, MIN_TENANTS + 1),
        seed=lambda conn: (seed_aggregate_fixture(conn),
                           seed_model_less_fixture(conn)),
    )
    models = global_stats.model_efficiency(stats_conn)["models"]
    assert models[-1]["model"] is None
    assert models[-1]["story_points"] == pytest.approx(2.0 * MIN_TENANTS)
    assert models[-1]["cost_usd"] is None
    assert models[-1]["cost_per_sp"] is None
    assert models[-1]["has_unpriced"] is True


def test_null_model_cohort_below_minimum_is_suppressed(stats_conn):
    for tenant_id in range(1, MIN_TENANTS):
        insert_row(stats_conn, tenant_id, None, priced=0)
    assert global_stats.model_efficiency(stats_conn)["models"] == []
    insert_row(stats_conn, MIN_TENANTS, None, priced=0)
    models = global_stats.model_efficiency(stats_conn)["models"]
    assert [m["model"] for m in models] == [None]


def test_refresh_refuses_unservable_schema(stats_conn, tmp_path):
    from test_security_snapshot import build_v4_database

    v4 = tmp_path / "old.db"
    build_v4_database(v4)
    assert global_stats.refresh_tenant(stats_conn, 1, v4, "sha-old") is False
    assert global_stats.model_efficiency(stats_conn)["models"] == []


def test_sync_tenants_backfills_by_snapshot_sha(stats_conn, tmp_path):
    """Boot-time backfill stays idempotent and keyed on the installed sha."""
    for tenant_id in range(1, MIN_TENANTS + 1):
        build_tenant_db(tmp_path / "{}.db".format(tenant_id))
        stats_conn.execute(
            "INSERT INTO tenants (user_id, last_snapshot_sha256) VALUES (?, ?)",
            (tenant_id, "sha-{}".format(tenant_id)),
        )
    # Never published (no sha) and published-but-file-missing tenants are
    # skipped without blocking the others.
    stats_conn.execute(
        "INSERT INTO tenants (user_id, last_snapshot_sha256) VALUES (91, NULL)"
    )
    stats_conn.execute(
        "INSERT INTO tenants (user_id, last_snapshot_sha256) VALUES (92, 'sha-92')"
    )
    assert global_stats.sync_tenants(stats_conn, tmp_path) == MIN_TENANTS
    assert [
        m["model"] for m in global_stats.model_efficiency(stats_conn)["models"]
    ] == ["m-claude", "m-shared"]
    # Already aggregated for this sha → nothing to do.
    assert global_stats.sync_tenants(stats_conn, tmp_path) == 0
    # A new installed snapshot (different sha) is re-aggregated.
    stats_conn.execute(
        "UPDATE tenants SET last_snapshot_sha256 = 'sha-1b' WHERE user_id = 1"
    )
    assert global_stats.sync_tenants(stats_conn, tmp_path) == 1


def test_stored_columns_carry_no_identity_or_timing_data(stats_conn, tmp_path):
    """FAN-2397 criterion 4: the stored contribution stays limited to the
    internal tenant key, canonical model, four token buckets, fractional story
    points, priced cost/flag and the operational refresh stamp. No account,
    workspace, issue, agent, run or event-time column may exist to hold
    anything else."""
    contribute(stats_conn, tmp_path, [1])
    columns = {
        row[1]
        for row in stats_conn.execute("PRAGMA table_info(global_model_stats)")
    }
    assert columns == {
        "tenant_id", "model", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "story_points",
        "cost_usd", "priced", "updated_at",
    }
    state = {
        row[1]
        for row in stats_conn.execute("PRAGMA table_info(global_stats_tenants)")
    }
    assert state == {"tenant_id", "snapshot_sha256", "refreshed_at"}


def test_module_parses_as_python_36():
    """The legacy cPanel contour imports this module on Python 3.6.8, so the
    suppression change must not introduce newer syntax (FAN-2397)."""
    source = open("aistat/global_stats.py", encoding="utf-8").read()
    ast.parse(source, filename="aistat/global_stats.py",
              feature_version=(3, 6))


def test_delete_user_removes_rows_and_can_suppress_the_cohort(tmp_path):
    config = Config()
    config.security_db_path = tmp_path / "security.db"
    config.tenants_dir = tmp_path / "tenants"
    config.ensure_tenants_dir()
    store = SecurityStore(config.security_db_path)

    user_ids = []
    for index in range(MIN_TENANTS):
        user_id = store.find_or_create_user_by_identity(
            "google", "sub-{}".format(index),
            email="user{}@example.com".format(index), now=100,
        )
        store.ensure_tenant(user_id, now=100)
        tenant_db = build_tenant_db(config.tenant_db_path(user_id))
        assert store.refresh_global_stats(
            user_id, tenant_db, "sha-{}".format(index)
        ) is True
        user_ids.append(user_id)
    assert store.global_model_efficiency()["models"]

    # Account deletion removes the contribution in the same transaction, and
    # the remaining four-tenant cohort stops being publishable.
    purge_user(config, user_ids[0])
    assert store.global_model_efficiency() == {"estimated": True, "models": []}
    conn = sqlite3.connect(str(config.security_db_path))
    try:
        for table in ("global_model_stats", "global_stats_tenants"):
            assert conn.execute(
                "SELECT COUNT(*) FROM {} WHERE tenant_id = ?".format(table),
                (user_ids[0],),
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


def _add_store_tenants(store, security_db_path, tenants_dir, count, start=0):
    """Add ``count`` hosted tenants in the state a real snapshot install leaves.

    Each gets a tenant database at its canonical path, the installed sha
    recorded on its ``tenants`` row and its contribution aggregated — so the
    boot-time :func:`global_stats.sync_tenants` backfill can also find them.
    """
    os.makedirs(str(tenants_dir), exist_ok=True)
    user_ids = []
    for index in range(start, start + count):
        user_id = store.find_or_create_user_by_identity(
            "google", "extra-{}".format(index),
            email="extra{}@example.com".format(index), now=100,
        )
        store.ensure_tenant(user_id, now=100)
        sha = "sha-extra-{}".format(index)
        path = build_tenant_db(tenant_db_path(tenants_dir, user_id))
        conn = sqlite3.connect(str(security_db_path))
        try:
            conn.execute(
                "UPDATE tenants SET last_snapshot_sha256 = ? WHERE user_id = ?",
                (sha, user_id),
            )
            conn.commit()
        finally:
            conn.close()
        assert store.refresh_global_stats(user_id, path, sha) is True
        user_ids.append(user_id)
    return user_ids


def test_flask_requires_login(public_app):
    app, _ = public_app
    assert _flask_global(app.test_client()).status_code == 401


def test_flask_ingest_populates_and_suppresses_until_the_minimum(
    public_app, tmp_path
):
    app, config = public_app
    client = app.test_client()
    assert flask_login(client).status_code == 303

    # The owner's migrated database was aggregated by the boot-time sync, but
    # one tenant is far below the minimum, so nothing is published.
    assert _flask_global(client).get_json() == {"estimated": True, "models": []}

    # Three more hosted tenants: still one short of the minimum.
    store = SecurityStore(config.security_db_path)
    _add_store_tenants(
        store, config.security_db_path, config.tenants_dir, MIN_TENANTS - 2
    )
    assert _flask_global(client).get_json()["models"] == []

    # The fifth tenant arrives through the real signed-snapshot ingest path.
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
    assert set(data) == {"estimated", "models"}
    claude = data["models"][0]
    assert claude["model"] == "m-claude"
    assert claude["story_points"] == pytest.approx(12.5)
    assert claude["cost_per_sp"] == pytest.approx(0.0002)
    # Sums only: no identity fields, no per-tenant breakdown and no
    # contributor count leave the host.
    body = _flask_global(client).get_data(as_text=True)
    assert "bob" not in body
    assert "tenant" not in body.lower()
    assert "count" not in body.lower()
    assert set(claude) == MODEL_FIELDS

    # Deleting one user drops the cohort back under the minimum, and the
    # result fails closed to empty rather than to the survivors' data.
    purge_user(config, b_id)
    assert _flask_global(client).get_json() == {"estimated": True, "models": []}


def test_dashboard_filters_never_narrow_the_all_time_aggregate(
    public_app, tmp_path
):
    """FAN-2397 criterion 6: this is an all-time cross-tenant aggregate. A
    filter sent to the endpoint changes nothing — narrowing it could shrink a
    cohort below the minimum after the fact — and the dashboard never appends
    the filter query to this call in the first place."""
    app, config = public_app
    client = app.test_client()
    assert flask_login(client).status_code == 303
    store = SecurityStore(config.security_db_path)
    _add_store_tenants(
        store, config.security_db_path, config.tenants_dir, MIN_TENANTS - 1
    )
    unfiltered = _flask_global(client).get_data(as_text=True)
    assert json.loads(unfiltered)["models"]

    filtered = client.get(
        "/api/global-model-efficiency"
        "?from=2026-01-01T00:00&to=2026-01-01T01:00&agent=A1&project=P1",
        base_url="https://localhost",
    )
    assert filtered.status_code == 200
    assert filtered.get_data(as_text=True) == unfiltered

    app_js = (
        Path(aistat.__file__).parent / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert 'fetchJSON("/api/global-model-efficiency")' in app_js


def test_flask_boot_sync_self_heals_missing_aggregation(public_app, tmp_path):
    app, config = public_app
    store = SecurityStore(config.security_db_path)
    _add_store_tenants(
        store, config.security_db_path, config.tenants_dir, MIN_TENANTS - 1
    )

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
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]
    assert set(data["models"][0]) == MODEL_FIELDS


# --------------------------------------------------------------------------- #
# Legacy cPanel contour (aistat.legacy_wsgi)
# --------------------------------------------------------------------------- #


def test_legacy_serves_the_same_suppressed_or_anonymized_contract(
    legacy, tmp_path
):
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

    def fetch(query=""):
        status, _, body = legacy_request(
            module.application,
            "/api/global-model-efficiency" + query,
            cookie=cookies,
        )
        assert status == "200 OK"
        return json.loads(body.decode("utf-8"))

    # The ingest populated the store, but one tenant stays suppressed.
    assert fetch() == {"estimated": True, "models": []}

    store = SecurityStore(module.SECURITY_DB_PATH)
    _add_store_tenants(
        store, module.SECURITY_DB_PATH, module.TENANTS_DIR, MIN_TENANTS - 2
    )
    assert fetch()["models"] == []                       # still one short

    _add_store_tenants(
        store, module.SECURITY_DB_PATH, module.TENANTS_DIR, 1,
        start=MIN_TENANTS,
    )
    data = fetch()
    assert set(data) == {"estimated", "models"}
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]
    assert data["models"][0]["cost_per_sp"] == pytest.approx(0.0002)
    assert set(data["models"][0]) == MODEL_FIELDS
    # Same all-time contract as the Flask contour: filters change nothing.
    assert fetch("?from=2026-01-01T00:00&to=2026-01-01T01:00&agent=A1") == data


# --------------------------------------------------------------------------- #
# Dashboard DOM contract (real browser, real markup)
# --------------------------------------------------------------------------- #


GLOBAL_PANEL_DOM_JS = '''(() => {
  const panel = document.getElementById("global-models-panel");
  const table = document.getElementById("table-global-models-data");
  return {
    panelHidden: panel.hidden,
    emptyHidden: document.getElementById("empty-global-models").hidden,
    headers: [...table.querySelectorAll("thead th")]
      .map((th) => th.textContent.trim()),
    rows: [...table.querySelectorAll("tbody tr")]
      .map((tr) => [...tr.querySelectorAll("td")]
        .map((td) => td.textContent.trim())),
    // textContent, not innerText: the table lives inside a collapsed
    // <details>, so a leak must be caught even while it is not painted.
    text: panel.textContent,
    note: panel.querySelector("p.note").textContent,
  };
})()'''


@pytest.mark.skipif(CHROME is None, reason=NO_CHROME_REASON)
def test_dashboard_renders_neither_suppressed_data_nor_a_user_count(dashboard):
    """FAN-2397 criteria 3 and 7, in a real browser against the real markup.

    The panel renders exactly the anonymized aggregate it is handed: a fully
    suppressed result shows the empty state and no data row, a published
    cohort shows five cost columns, and a contributor count — even one planted
    in the payload — reaches no header, cell or text node.
    """
    cdp, base = dashboard
    cdp.open_page(base + "/")
    cdp.wait_for(BOOTED_JS)
    cdp.eval('I18N.setLocale("en")')

    # The local single-user surface has no cross-tenant endpoint, so a failed
    # fetch hides the panel instead of erroring.
    assert cdp.eval(
        'document.getElementById("global-models-panel").hidden'
    ) is True

    # A fully suppressed aggregate is a normal empty result.
    cdp.eval('renderGlobalModelEfficiency({estimated: true, models: []})')
    dom = cdp.eval(GLOBAL_PANEL_DOM_JS)
    assert dom["panelHidden"] is False
    assert dom["emptyHidden"] is False
    assert dom["rows"] == [["No data that can be shared anonymously yet."]]

    # A published cohort renders exactly the five cost columns. The planted
    # tenant_count is unmistakable, so any rendering of it would show up.
    cdp.eval('''renderGlobalModelEfficiency({estimated: true, models: [{
      model: "m-claude", story_points: 12.5, total_tokens: 3750,
      cost_usd: 0.0025, cost_per_sp: 0.0002, tokens_per_sp: 300,
      has_unpriced: false, tenant_count: 424242}]})''')
    dom = cdp.eval(GLOBAL_PANEL_DOM_JS)
    assert dom["headers"] == ["Model", "SP", "Tokens", "Cost ≈", "Cost / SP ≈"]
    assert [len(row) for row in dom["rows"]] == [5]
    assert dom["rows"][0][0] == "m-claude"
    assert dom["emptyHidden"] is True
    assert "424242" not in dom["text"]
    for banned in ("Users", "Пользователи"):
        assert banned not in dom["text"], banned
    # The note tells the reader the real rule behind an empty or partial table.
    assert "at least 5 different users" in dom["note"]
