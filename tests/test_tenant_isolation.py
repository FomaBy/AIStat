"""End-to-end cross-tenant isolation proof for both public WSGI contours.

Every browser/API data route of the Flask (``aistat.wsgi``) and the
dependency-free legacy (``aistat.legacy_wsgi``) contour must open a tenant
database derived *only* from the active server-side session's ``user_id``. No
request-controlled input — ``tenant`` / ``tenant_id`` / ``user_id`` query
params, a direct URL, or an ``X-AIStat-Tenant`` header — may change the tenant
data path, and no endpoint may leak another tenant's data or its existence.

These tests complement the per-feature suites (test_wsgi, test_legacy_wsgi,
test_health, test_tenants, test_security_*) with the A/B sentinel + fuzz +
unauthenticated-oracle + no-store + route-inventory matrix the FAN-1222
acceptance criteria require. Only synthetic tenants and secrets are used.
"""

import json
from pathlib import Path

from aistat.db import connect, init_db
from aistat.security import SecurityStore

# Reuse the fully wired fixtures/helpers from the per-contour suites so this
# module proves isolation against the exact same app construction they use.
from test_wsgi import (
    public_app,
    login as flask_login,
)
from test_legacy_wsgi import (
    legacy,
    login as legacy_login,
    request as legacy_request,
)

# Deterministic, session-scoped data routes (no wall-clock fields), so a
# request with fuzz params must return byte-for-byte the same body as one
# without them.
DETERMINISTIC_DATA_ROUTES = (
    "/api/meta",
    "/api/summary",
    "/api/daily",
    "/api/agents",
    "/api/projects",
    "/api/efficiency",
    "/api/model-efficiency",
    "/api/efficiency-breakdown",
    "/api/sync",
)
# Health carries a generated_at timestamp, so it is checked for sentinel/path
# absence rather than byte-equality.
HEALTH_ROUTES = ("/health", "/api/health")

SENTINEL_B = "SecretBravo"


def _seed_tenant_db(path, title=SENTINEL_B, project_id="PB"):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    init_db(conn)
    conn.execute(
        "INSERT INTO projects (id, title, status, synced_at) "
        "VALUES (?, ?, 'in_progress', '2026-01-02T00:00:00Z')",
        (project_id, title),
    )
    conn.commit()
    conn.close()


def _fuzz_query(tenant_id):
    return "tenant={0}&tenant_id={0}&user_id={0}".format(tenant_id)


# --------------------------------------------------------------------------- #
# Flask contour (aistat.wsgi)
# --------------------------------------------------------------------------- #


def _flask_make_tenant_b(config):
    """Create a second tenant B (user + tenant row + seeded DB) on the host."""
    store = SecurityStore(config.security_db_path)
    b_id = store.find_or_create_user_by_identity(
        "google", "bob-sub", email="bob@example.com", now=100
    )
    store.ensure_tenant(b_id, now=100)
    _seed_tenant_db(config.tenant_db_path(b_id))
    return b_id


def _flask_authorize_session(client, config, user_id, email):
    """Attach an authorised server-side session for ``user_id`` to ``client``.

    Mirrors what a real OAuth callback establishes (user_id + allow-listed
    email + a live server-side session id) so a *second* tenant identity can be
    exercised even though the production OAuth policy is owner-only.
    """
    store = SecurityStore(config.security_db_path)
    sid = store.create_session(user_id, 3600)
    config.oauth_allowed_emails = frozenset(
        set(config.oauth_allowed_emails) | {email}
    )
    with client.session_transaction(base_url="https://localhost") as sess:
        sess["user_id"] = user_id
        sess["email"] = email
        sess["provider"] = "google"
        sess["sid"] = sid
        sess.permanent = True


def test_flask_fuzz_params_never_change_owner_tenant(public_app):
    """AC1/AC4/AC6: no param/header/URL lets the owner session read tenant B."""
    app, config = public_app
    b_id = _flask_make_tenant_b(config)
    client = app.test_client()
    assert flask_login(client).status_code == 303

    fuzz = _fuzz_query(b_id)
    header = {"X-AIStat-Tenant": str(b_id)}
    for route in DETERMINISTIC_DATA_ROUTES + HEALTH_ROUTES:
        base = client.get(route, base_url="https://localhost")
        sep = "&" if "?" in route else "?"
        fuzzed = client.get(
            route + sep + fuzz, base_url="https://localhost", headers=header
        )
        assert base.status_code == 200, route
        assert fuzzed.status_code == 200, route
        assert SENTINEL_B.encode() not in fuzzed.get_data(), route
        if route in DETERMINISTIC_DATA_ROUTES:
            # The tenant path is unaffected by the injected identifiers.
            assert base.get_data() == fuzzed.get_data(), route

    meta = client.get("/api/meta", base_url="https://localhost").get_json()
    assert {p["title"] for p in meta["projects"]} == {"Alpha", "Beta"}


def test_flask_second_tenant_session_sees_only_its_own_data(public_app):
    """AC1/AC3/AC4: a distinct tenant session sees only its own DB, and two
    concurrent sessions never reuse each other's DB/cache/connection state."""
    app, config = public_app
    b_id = _flask_make_tenant_b(config)
    owner_id = config.publish_tenant_id

    client_b = app.test_client()
    _flask_authorize_session(client_b, config, b_id, "bob@example.com")
    meta_b = client_b.get("/api/meta", base_url="https://localhost")
    assert meta_b.status_code == 200
    assert {p["title"] for p in meta_b.get_json()["projects"]} == {SENTINEL_B}

    # B cannot reach the owner's tenant via injected identifiers either.
    fuzzed = client_b.get(
        "/api/meta?" + _fuzz_query(owner_id),
        base_url="https://localhost",
        headers={"X-AIStat-Tenant": str(owner_id)},
    )
    assert {p["title"] for p in fuzzed.get_json()["projects"]} == {SENTINEL_B}

    # A separate, concurrent owner session still sees only the owner tenant.
    client_a = app.test_client()
    flask_login(client_a)
    meta_a = client_a.get("/api/meta", base_url="https://localhost")
    assert {p["title"] for p in meta_a.get_json()["projects"]} == {"Alpha", "Beta"}
    # Interleave once more to confirm no shared per-process state bled across.
    assert {
        p["title"] for p in client_b.get(
            "/api/meta", base_url="https://localhost"
        ).get_json()["projects"]
    } == {SENTINEL_B}


def test_flask_unauthenticated_is_uniform_and_no_store(public_app):
    """AC2/AC3: an unauthenticated data request is a uniform 401/redirect with
    no tenant-existence oracle and a no-store cache directive."""
    app, config = public_app
    b_id = _flask_make_tenant_b(config)
    client = app.test_client()

    existing = client.get(
        "/api/meta", base_url="https://localhost",
        headers={"X-AIStat-Tenant": str(b_id)},
    )
    missing = client.get(
        "/api/meta", base_url="https://localhost",
        headers={"X-AIStat-Tenant": str(b_id + 9999)},
    )
    assert existing.status_code == missing.status_code == 401
    assert existing.get_json() == missing.get_json()  # no existence oracle
    assert existing.headers["Cache-Control"] == "no-store"
    assert SENTINEL_B.encode() not in existing.get_data()

    dashboard = client.get("/", base_url="https://localhost")
    assert dashboard.status_code == 303
    assert dashboard.headers["Location"].startswith("/login")
    assert dashboard.headers["Cache-Control"] == "no-store"


def test_flask_private_success_and_error_are_no_store(public_app):
    """AC3: private success and error responses both carry no-store."""
    app, _ = public_app
    client = app.test_client()
    flask_login(client)

    ok = client.get("/api/meta", base_url="https://localhost")
    assert ok.status_code == 200
    assert ok.headers["Cache-Control"] == "no-store"

    err = client.get("/api/summary?from=not-a-date", base_url="https://localhost")
    assert err.status_code == 422
    assert err.headers["Cache-Control"] == "no-store"
    assert "Traceback" not in err.get_data(as_text=True)


def test_flask_health_hides_filesystem_path(public_app):
    """AC5: the health endpoints reveal no filesystem path or traceback."""
    app, config = public_app
    client = app.test_client()
    flask_login(client)
    tenant_path = str(config.tenant_db_path(config.publish_tenant_id))
    for route in HEALTH_ROUTES:
        resp = client.get(route, base_url="https://localhost")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["db_path"] is None
        text = resp.get_data(as_text=True)
        assert tenant_path not in text
        assert str(config.tenants_dir) not in text
        assert "Traceback" not in text
        assert resp.headers["Cache-Control"] == "no-store"


def test_flask_route_inventory_is_fully_classified(public_app):
    """AC6: every route is a known public / service-auth / session endpoint;
    a new, unclassified route makes this fail so it cannot ship unprotected."""
    app, _ = public_app
    # Public (service-auth or unauthenticated liveness): each enforces its own
    # gate (login, static login asset, liveness probe, HMAC ingest, OAuth,
    # worker HMAC pull/ack) — never a browser session.
    public = {
        "login",
        "login_css",
        "healthz",
        "ingest_snapshot",
        "oauth_start",
        "oauth_callback",
        "worker_connection_pull",
        "worker_connection_ack",
    }
    # Session-authenticated (data or session-bound action).
    session_scoped = {
        "logout",
        "api_session",
        "api_meta",
        "api_summary",
        "api_daily",
        "api_agents",
        "api_projects",
        "api_efficiency",
        "api_model_efficiency",
        "api_efficiency_breakdown",
        "api_health",
        "api_sync",
        "api_events",
        "api_connection",
        "api_connection_submit",
        "api_connection_revoke",
        "dashboard",
        "dashboard_asset",
    }
    known = public | session_scoped
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    unknown = endpoints - known
    assert not unknown, (
        "unclassified route(s) must be classified and protected: %s" % unknown
    )

    # Every session-scoped GET data route rejects an unauthenticated request.
    client = app.test_client()
    for route in DETERMINISTIC_DATA_ROUTES + HEALTH_ROUTES:
        resp = client.get(route, base_url="https://localhost")
        assert resp.status_code in (401, 303), route


# --------------------------------------------------------------------------- #
# Legacy dependency-free contour (aistat.legacy_wsgi)
# --------------------------------------------------------------------------- #


def _legacy_make_tenant_b(module):
    store = SecurityStore(module.SECURITY_DB_PATH)
    b_id = store.find_or_create_user_by_identity(
        "google", "bob-sub", email="bob@example.com", now=100
    )
    store.ensure_tenant(b_id, now=100)
    _seed_tenant_db(Path(module.tenant_db_path(module.TENANTS_DIR, b_id)))
    return b_id


def test_legacy_fuzz_params_never_change_owner_tenant(legacy):
    """AC1/AC4/AC6 for the stdlib contour."""
    b_id = _legacy_make_tenant_b(legacy)
    cookie = legacy_login(legacy)
    fuzz = _fuzz_query(b_id)
    header = {"X-AIStat-Tenant": str(b_id)}
    for route in DETERMINISTIC_DATA_ROUTES + HEALTH_ROUTES:
        status, _, base = legacy_request(legacy.application, route, cookie=cookie)
        sep = "&" if "?" in route else "?"
        fstatus, _, fbody = legacy_request(
            legacy.application, route + sep + fuzz, headers=header, cookie=cookie
        )
        assert status == "200 OK", route
        assert fstatus == "200 OK", route
        assert SENTINEL_B.encode() not in fbody, route
        if route in DETERMINISTIC_DATA_ROUTES:
            assert base == fbody, route

    _, _, meta = legacy_request(legacy.application, "/api/meta", cookie=cookie)
    assert {p["title"] for p in json.loads(meta)["projects"]} == {"Alpha", "Beta"}


def test_legacy_unauthenticated_is_uniform_and_no_store(legacy):
    """AC2/AC3 for the stdlib contour."""
    b_id = _legacy_make_tenant_b(legacy)
    status_e, headers_e, body_e = legacy_request(
        legacy.application, "/api/meta", headers={"X-AIStat-Tenant": str(b_id)}
    )
    status_m, _, body_m = legacy_request(
        legacy.application, "/api/meta",
        headers={"X-AIStat-Tenant": str(b_id + 9999)},
    )
    assert status_e == status_m == "401 Unauthorized"
    assert body_e == body_m  # no tenant-existence oracle
    assert SENTINEL_B.encode() not in body_e
    assert ("Cache-Control", "no-store") in headers_e


def test_legacy_private_success_and_error_are_no_store(legacy):
    """AC3 for the stdlib contour."""
    cookie = legacy_login(legacy)
    status_ok, headers_ok, _ = legacy_request(
        legacy.application, "/api/meta", cookie=cookie
    )
    assert status_ok == "200 OK"
    assert ("Cache-Control", "no-store") in headers_ok

    status_err, headers_err, body_err = legacy_request(
        legacy.application, "/api/summary?from=not-a-date", cookie=cookie
    )
    assert status_err == "422 Unprocessable Entity"
    assert ("Cache-Control", "no-store") in headers_err
    assert b"Traceback" not in body_err


def test_legacy_health_hides_filesystem_path(legacy):
    """AC5 for the stdlib contour."""
    cookie = legacy_login(legacy)
    for route in HEALTH_ROUTES:
        status, headers, body = legacy_request(
            legacy.application, route, cookie=cookie
        )
        assert status == "200 OK"
        payload = json.loads(body)
        assert "db_path" not in payload
        text = body.decode("utf-8")
        assert legacy.TENANTS_DIR not in text
        assert "/tenants/" not in text
        assert "Traceback" not in text
        assert ("Cache-Control", "no-store") in headers
