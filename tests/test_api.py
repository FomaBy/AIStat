"""API endpoint tests: FastAPI app over a seeded temporary database."""

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import aistat.server as server_module
from aistat.config import Config
from aistat.db import connect, init_db
from conftest import (
    seed_aggregate_fixture,
    seed_model_less_fixture,
    seed_opus_transition_fixture,
)


@pytest.fixture
def api(tmp_path):
    config = Config()
    config.db_path = tmp_path / "api.db"
    config.credits_per_usd = 2.0
    conn = connect(config.db_path)
    init_db(conn)
    seed_aggregate_fixture(conn)
    app = server_module.create_app(config)
    with TestClient(app) as client:
        yield client, conn
    conn.close()


def test_meta(api):
    client, _ = api
    meta = client.get("/api/meta").json()
    assert [p["title"] for p in meta["projects"]] == ["Alpha", "Beta"]
    assert len(meta["agents"]) == 3
    assert meta["date_span"] == {"first": "2026-01-01", "last": "2026-01-02"}


def test_configurable_chart_catalog_and_all_allowed_pairs(api):
    client, _ = api
    catalog = client.get("/api/chart-catalog").json()
    assert catalog["version"] == "v1"
    assert [item["id"] for item in catalog["dimensions"]] == [
        "time", "project", "agent", "model", "issue",
    ]
    assert {item["id"] for item in catalog["measures"]} == {
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "total_tokens", "cost_usd", "cost_credits",
        "story_points", "task_count", "run_count", "agent_work_seconds",
        "tokens_per_sp", "cost_per_sp", "weighted_efficiency",
    }
    for dimension in catalog["dimensions"]:
        for measure in catalog["measures"]:
            pair = catalog["compatibility"][dimension["id"]][measure["id"]]
            response = client.get("/api/chart", params={
                "dimension": dimension["id"], "measure": measure["id"],
            })
            if pair["supported"]:
                assert response.status_code == 200, (dimension, measure)
                data = response.json()
                assert data["version"] == "v1"
                assert data["chart_type"] == dimension["chart_type"]
                assert all(set(row) == {
                    "id", "label", "value", "estimated", "has_unpriced",
                } for row in data["rows"])
            else:
                assert pair["reason"] == "unavailable_for_dimension"
                assert response.status_code == 422
                assert response.json()["detail"] == (
                    "unsupported chart combination: %s × %s" % (
                        dimension["id"], measure["id"],
                    )
                )


def test_configurable_chart_preserves_ratio_null_estimate_and_filters(api):
    client, _ = api
    project = client.get("/api/chart", params={
        "dimension": "project", "measure": "tokens_per_sp",
    }).json()
    beta = next(row for row in project["rows"] if row["id"] == "P2")
    assert beta["value"] is None

    ratio = client.get("/api/chart", params={
        "dimension": "agent", "measure": "tokens_per_sp",
        "from": "2026-01-01T10:00Z", "to": "2026-01-01T11:00Z",
        "project": "P1", "agent": "A2", "model": "m-shared",
    }).json()
    assert ratio["estimated"] is True
    assert ratio["rows"] == [{
        "id": "A2", "label": "Dev Shared", "value": 300.0,
        "estimated": True, "has_unpriced": False,
    }]

    assert client.get("/api/chart", params={
        "dimension": "unknown", "measure": "total_tokens",
    }).json() == {"detail": "unsupported chart dimension: unknown"}
    invalid = client.get("/api/chart", params={
        "dimension": "agent", "measure": "task_count",
    })
    assert invalid.status_code == 422
    assert invalid.json() == {
        "detail": "unsupported chart combination: agent × task_count",
    }


def test_opus_5_api_models_filters_and_efficiency_stay_separate(api):
    client, conn = api
    seed_opus_transition_fixture(conn)

    meta = client.get("/api/meta").json()
    assert {"claude-opus-4-8", "claude-opus-5"} <= set(meta["models"])
    daily = client.get("/api/daily", params={
        "group": "model", "from": "2026-07-24", "to": "2026-07-24",
    }).json()["rows"]
    assert {row["key"] for row in daily} == {
        "claude-opus-4-8", "claude-opus-5",
    }
    assert all(row["total_tokens"] == 1_000_000 for row in daily)

    for model in ("claude-opus-4-8", "claude-opus-5"):
        summary = client.get("/api/summary", params={"model": model}).json()
        assert summary["total_tokens"] == 1_000_000
        assert summary["story_points"] == pytest.approx(5.0)
        assert summary["agent_work_seconds"] == 3600
    efficiency = client.get("/api/model-efficiency", params=[
        ("model", "claude-opus-4-8"), ("model", "claude-opus-5"),
    ]).json()
    assert {row["model"] for row in efficiency["models"]} == {
        "claude-opus-4-8", "claude-opus-5",
    }


def test_summary_endpoint(api):
    client, _ = api
    s = client.get("/api/summary").json()
    assert s["total_tokens"] == 4_700_000
    assert s["unpriced_models"] == ["m-mystery"]

    filtered = client.get("/api/summary",
                          params={"from": "2026-01-01", "to": "2026-01-01",
                                  "project": "P1"}).json()
    assert filtered["estimated"] is True
    assert filtered["total_tokens"] == 3_400_000


def test_summary_exposes_accepted_sp_quality_cost(api):
    client, conn = api
    conn.execute(
        "INSERT INTO qa_lineage_events (qa_issue_id, implementation_issue_id, "
        "candidate, verdict, observed_at, accepted_candidate, accepted_story_points) "
        "VALUES ('QA-1', 'I1', 'sha', 'PASSED', '2026-01-02T00:00:00Z', 'sha', 3)"
    )
    conn.commit()
    data = client.get("/api/summary").json()
    assert data["accepted_story_points"] == 3.0
    assert data["quality_adjusted_cost_per_sp"] == pytest.approx(0.0025 / 3)


def test_billing_reconciliation_api_exposes_sanitized_totals_only(api):
    client, conn = api
    conn.execute(
        "INSERT INTO billing_reconciliation VALUES "
        "('anthropic', '2026-02', 90, 100, 0.1, 1, '2026-02-28T00:00:00Z')"
    )
    conn.commit()
    assert client.get("/api/billing-reconciliation").json() == {"rows": [{
        "provider": "anthropic", "period": "2026-02", "calculated_usd": 90.0,
        "actual_usd": 100.0, "variance_ratio": 0.1, "over_threshold": True,
        "diagnostic_emitted": True,
    }]}


def test_pricing_api_exposes_rate_provenance_and_coverage(api):
    client, conn = api
    conn.execute(
        "INSERT INTO model_price_history (model, effective_from, input_rate, "
        "output_rate, cache_read_rate, cache_write_rate, unpriced, source_url, loaded_at) "
        "VALUES ('m', '2026-01-01', 1, 2, .1, 1.25, 0, 'https://vendor/pricing', 'now')"
    )
    conn.commit()
    data = client.get("/api/pricing").json()
    assert data["rates"][0]["source_url"] == "https://vendor/pricing"
    assert data["rates"][0]["effective_from"] == "2026-01-01"
    assert data["coverage"] == {"rows": 4, "priced_rows": 3, "unpriced_rows": 1}


def test_hour_and_dimension_filters_are_validated_and_applied(api):
    client, _ = api
    params = [
        ("from", "2026-01-01T10:00Z"),
        ("to", "2026-01-01T11:00Z"),
        ("project", "P1"),
        ("agent", "A2"),
        ("model", "m-shared"),
    ]
    summary = client.get("/api/summary", params=params).json()
    assert summary["estimated"] is True
    assert summary["total_tokens"] == 600_000
    assert summary["cost_usd"] == pytest.approx(1.2)
    assert client.get(
        "/api/summary", params={"from": "2026-01-01T11:00Z", "to": "2026-01-01T10:00Z"}
    ).status_code == 422


def test_daily_endpoint(api):
    client, _ = api
    daily = client.get("/api/daily", params={"group": "agent"}).json()
    assert daily["estimated"] is True
    names = {r["key"] for r in daily["rows"]}
    assert "Dev Shared" in names and "(не атрибутировано)" in names

    assert client.get("/api/daily", params={"group": "nope"}).status_code == 422


def test_agents_endpoint(api):
    client, _ = api
    agents = client.get("/api/agents").json()["agents"]
    assert agents[0]["total_tokens"] >= agents[-1]["total_tokens"]
    shared = next(a for a in agents if a["name"] == "Dev Shared")
    assert shared["estimated"] is True


def test_agents_endpoint_counts_only_overlapping_hour_runs(api):
    client, _ = api
    agents = client.get("/api/agents", params=[
        ("from", "2026-01-01T10:00Z"), ("to", "2026-01-01T11:00Z"),
        ("project", "P1"), ("agent", "A2"), ("model", "m-shared"),
    ]).json()["agents"]
    assert {agent["agent_id"]: agent["runs"] for agent in agents} == {"A2": 1}


def test_agent_count_and_worktime_expose_and_reconcile(api):
    client, _ = api
    s = client.get("/api/summary").json()
    assert s["agent_count"] == 3
    assert s["agent_work_seconds"] == 21600
    agents = client.get("/api/agents").json()["agents"]
    assert sum(a["work_seconds"] for a in agents) == s["agent_work_seconds"]
    assert sum(1 for a in agents if a["work_seconds"] > 0) == s["agent_count"]


def test_projects_endpoint_uses_configured_credit_rate(api):
    client, _ = api
    projects = {p["title"]: p for p in client.get("/api/projects").json()["projects"]}
    assert projects["Alpha"]["cost_usd"] == pytest.approx(0.0065)
    assert projects["Alpha"]["cost_credits"] == pytest.approx(0.013)  # 2 credits/$


def test_projects_filtered_cost_matches_model_efficiency(api):
    # FAN-1251: the combined project+agent+model+time filter that made
    # /api/projects ($0.00125) and /api/model-efficiency ($0.002) disagree
    # must now report $0.002 in both.
    client, _ = api
    params = [
        ("from", "2026-01-01T10:00Z"), ("to", "2026-01-01T11:00Z"),
        ("project", "P1"), ("agent", "A2"), ("model", "m-shared"),
    ]
    alpha = {p["title"]: p for p in
             client.get("/api/projects", params=params).json()["projects"]}["Alpha"]
    assert alpha["total_tokens"] == pytest.approx(750)
    assert alpha["cost_usd"] == pytest.approx(0.002)
    eff = client.get("/api/model-efficiency", params=params).json()
    assert alpha["cost_usd"] == pytest.approx(eff["cost_usd"]) == pytest.approx(0.002)


def test_efficiency_endpoint(api):
    client, _ = api
    issues = client.get("/api/efficiency").json()["issues"]
    assert [i["identifier"] for i in issues] == ["T-1"]
    assert client.get("/api/efficiency", params={"project": "P2"}).json() == {"issues": []}


def test_model_efficiency_endpoint(api):
    client, _ = api
    data = client.get("/api/model-efficiency").json()
    assert data["cost_per_sp"] == pytest.approx(0.0005)
    assert data["weighted_efficiency"] == pytest.approx(0.00025)
    assert [m["model"] for m in data["models"]] == ["m-claude", "m-shared"]
    empty = client.get("/api/model-efficiency", params={"project": "P2"}).json()
    assert empty["models"] == []
    assert empty["cost_per_sp"] is None


def test_model_efficiency_filters_use_one_run_overlap_set(api):
    # FAN-1244: agent/model/time/combined filters build cost, hours and
    # model membership from the same filtered run overlaps.
    client, _ = api
    agent = client.get("/api/model-efficiency", params={"agent": "A2"}).json()
    assert [m["model"] for m in agent["models"]] == ["m-shared"]
    assert agent["cost_usd"] == pytest.approx(0.002)
    assert agent["active_hours"] == pytest.approx(1.0)
    assert agent["cost_per_sp"] == pytest.approx(0.0008)
    assert agent["weighted_efficiency"] == pytest.approx(0.0008)

    model = client.get("/api/model-efficiency", params={"model": "m-shared"}).json()
    assert [m["model"] for m in model["models"]] == ["m-shared"]
    assert model["active_hours"] == pytest.approx(1.0)
    assert model["weighted_efficiency"] == pytest.approx(0.0008)

    window = client.get("/api/model-efficiency", params=[
        ("from", "2026-01-01T10:00Z"), ("to", "2026-01-01T10:30Z"),
    ]).json()
    assert [m["model"] for m in window["models"]] == ["m-claude", "m-shared"]
    assert window["cost_usd"] == pytest.approx(0.00125)
    assert window["active_hours"] == pytest.approx(1.0)
    assert window["weighted_efficiency"] == pytest.approx(0.0005)

    combined = client.get("/api/model-efficiency", params=[
        ("from", "2026-01-01T10:00Z"), ("to", "2026-01-01T10:30Z"),
        ("project", "P1"), ("agent", "A2"), ("model", "m-shared"),
    ]).json()
    assert [m["model"] for m in combined["models"]] == ["m-shared"]
    assert combined["cost_usd"] == pytest.approx(0.001)
    assert combined["active_hours"] == pytest.approx(0.5)
    assert combined["weighted_efficiency"] == pytest.approx(0.0016)

    summary = client.get("/api/summary", params={"agent": "A2"}).json()
    assert summary["cost_per_sp"] == pytest.approx(0.0008)
    assert summary["weighted_efficiency"] == pytest.approx(0.0008)
    assert summary["efficiency_hours"] == pytest.approx(1.0)


def test_model_efficiency_keeps_model_less_share(api):
    # FAN-1247: mixed known/model-null, all-null and exact project-only cuts.
    client, conn = api
    seed_model_less_fixture(conn)

    mixed = client.get("/api/model-efficiency", params=[
        ("from", "2026-01-04"), ("to", "2026-01-04"), ("project", "P3"),
    ]).json()
    assert [m["model"] for m in mixed["models"]] == ["m-claude", None]
    assert mixed["unpriced_tokens"] == 500
    assert mixed["has_unpriced"] is True
    assert mixed["active_hours"] == pytest.approx(2.0)
    assert mixed["cost_usd"] == pytest.approx(0.0005)
    # Priced cost over the priced 2 SP only, not the model-less 4 (QA FAN-1188).
    assert mixed["cost_per_sp"] == pytest.approx(0.00025)
    assert mixed["weighted_efficiency"] is None

    null_only = client.get("/api/model-efficiency", params={"agent": "A5"}).json()
    assert [m["model"] for m in null_only["models"]] == [None]
    assert null_only["cost_per_sp"] is None
    assert null_only["weighted_efficiency"] is None
    assert null_only["unpriced_tokens"] == 500
    assert null_only["active_hours"] == pytest.approx(1.0)

    exact = client.get("/api/model-efficiency", params={"project": "P3"}).json()
    assert [m["model"] for m in exact["models"]] == ["m-claude", None]
    assert exact["cost_per_sp"] == pytest.approx(0.00025)
    assert exact["unpriced_tokens"] == 500
    assert exact["weighted_efficiency"] is None

    summary = client.get("/api/summary", params=[
        ("from", "2026-01-04"), ("to", "2026-01-04"), ("project", "P3"),
    ]).json()
    assert summary["cost_per_sp"] == pytest.approx(0.00025)
    assert summary["weighted_efficiency"] is None
    assert summary["efficiency_has_unpriced"] is True


def test_efficiency_breakdown_endpoint(api):
    client, _ = api
    data = client.get("/api/efficiency-breakdown").json()
    assert data["metric"] == "tokens_per_sp"
    assert data["estimated"] is True
    assert {row["key"] for row in data["agents"]} == {"A1", "A2"}
    assert data["time"]["granularity"] == "day"

    hourly = client.get(
        "/api/efficiency-breakdown",
        params=[("from", "2026-01-01T10:00Z"), ("to", "2026-01-01T10:30Z"),
                ("agent", "A2"), ("model", "m-shared")],
    ).json()
    assert hourly["time"]["granularity"] == "hour"
    assert hourly["time"]["rows"][0]["total_tokens"] == 375
    assert [row["key"] for row in hourly["agents"]] == ["A2"]
    assert client.get(
        "/api/efficiency-breakdown",
        params={"from": "2026-01-01T11:00Z", "to": "2026-01-01T10:00Z"},
    ).status_code == 422


def test_efficiency_breakdown_empty_selection_returns_empty_cuts(api):
    """The QA scenario behind FAN-1242: a filter matching nothing comes back
    as empty cuts, which the dashboard must render as explicit no-data."""
    client, _ = api
    data = client.get(
        "/api/efficiency-breakdown",
        params=[("from", "2026-01-02T00:00Z"), ("to", "2026-01-02T01:00Z"),
                ("agent", "missing"), ("model", "missing")],
    ).json()
    assert data["agents"] == []
    assert data["models"] == []
    assert data["time"] == {"granularity": "hour", "rows": []}


def test_summary_endpoint_has_cost_efficiency(api):
    client, _ = api
    s = client.get("/api/summary").json()
    assert s["cost_per_sp"] == pytest.approx(0.0005)
    assert s["weighted_efficiency"] == pytest.approx(0.00025)


def test_summary_model_filter_flags_estimated_task_values(api):
    client, _ = api
    s = client.get("/api/summary", params={"model": "m-shared"}).json()
    assert s["estimated"] is False               # whole-day model tokens are exact
    assert s["sp_estimated"] is True             # run-share attribution (FAN-1241)
    assert s["efficiency_estimated"] is True
    assert s["story_points"] == pytest.approx(2.5)
    assert s["tokens_per_sp"] == pytest.approx(300.0)

    base = client.get("/api/summary").json()
    assert base["sp_estimated"] is False
    assert base["efficiency_estimated"] is False


def test_efficiency_rows_carry_estimated_flag(api):
    client, _ = api
    rows = client.get("/api/efficiency", params={"model": "m-shared"}).json()["issues"]
    assert rows[0]["estimated"] is True
    assert rows[0]["story_points"] == pytest.approx(2.5)
    exact = client.get("/api/efficiency").json()["issues"]
    assert all(r["estimated"] is False for r in exact)


def test_health_endpoints(api):
    client, _ = api
    for path in ("/health", "/api/health"):
        health = client.get(path).json()
        assert health["status"] == "ok"
        assert health["row_counts"]["daily_usage"] == 4


def test_dashboard_static_files(api):
    client, _ = api
    index = client.get("/")
    assert index.status_code == 200
    assert "AIStat" in index.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/vendor/chart.umd.min.js").status_code == 200


def _js_function(source, name):
    """The body of one top-level ``function name(...)`` in app.js."""
    start = source.index(f"function {name}(")
    end = source.find("\nfunction ", start)
    return source[start:end if end != -1 else len(source)]


def test_dashboard_renderers_mark_estimated_values():
    """Static contract (FAN-1241): the renderers must consume the estimation
    flags — summary cards via sp_estimated/efficiency_estimated, the task
    table via each row's estimated — and mark those values with ≈."""
    app_js = (Path(server_module.__file__).parent / "static" / "app.js"
              ).read_text(encoding="utf-8")
    render_summary = _js_function(app_js, "renderSummary")
    assert "sp_estimated" in render_summary
    assert "efficiency_estimated" in render_summary
    assert "≈" in render_summary
    render_efficiency = _js_function(app_js, "renderEfficiency")
    assert ".estimated" in render_efficiency
    assert "≈" in render_efficiency


def test_dashboard_note_follows_actual_estimation():
    """FAN-1253 re-QA: the shared ≈-note must be driven by the real API
    estimation flags, not merely by the presence of a filter — an exact
    unique-agent whole-day slice must leave it hidden."""
    app_js = (Path(server_module.__file__).parent / "static" / "app.js"
              ).read_text(encoding="utf-8")
    refresh = _js_function(app_js, "refreshAll")
    assert "estimate-note" in refresh
    # The note reacts to the returned flags, so an exact result hides it.
    assert "summary.estimated" in refresh
    assert "daily.estimated" in refresh
    assert "a.estimated" in refresh


def test_dashboard_efficiency_charts_have_accessible_alternatives():
    """Static contract (FAN-1242): each efficiency chart canvas carries an
    accessible name, a hidden no-data message and a table alternative, and
    the stylesheet keeps the message hidden while its hidden attribute is
    set (display:flex would beat the UA [hidden] rule otherwise)."""
    static = Path(server_module.__file__).parent / "static"
    index_html = (static / "index.html").read_text(encoding="utf-8")
    for chart in ("efficiency-agents", "efficiency-models", "efficiency-time"):
        canvas = re.search(rf'<canvas id="chart-{chart}"[^>]*>', index_html)
        assert canvas is not None, chart
        assert 'role="img"' in canvas.group(0)
        assert "aria-label=" in canvas.group(0)
        assert f'id="empty-{chart}" hidden' in index_html
        assert f'id="table-{chart}-data"' in index_html
    style_css = (static / "style.css").read_text(encoding="utf-8")
    assert ".chart-empty[hidden]" in style_css


def test_dashboard_breakdown_renderer_handles_empty_and_partial_data():
    """Static contract (FAN-1242): renderEfficiencyBreakdown must toggle the
    no-data messages, fill the table alternatives, and keep empty buckets as
    gaps — a null tokens/SP is never coerced to 0 and spanGaps stays off."""
    app_js = (Path(server_module.__file__).parent / "static" / "app.js"
              ).read_text(encoding="utf-8")
    render = _js_function(app_js, "renderEfficiencyBreakdown")
    for chart in ("efficiency-agents", "efficiency-models", "efficiency-time"):
        assert f"empty-{chart}" in render
        assert f"table-{chart}-data" in render
    assert "spanGaps: false" in render
    assert "|| 0" not in render
    table = _js_function(app_js, "renderBreakdownTable")
    assert "—" in table           # a gap is an explicit dash, not a fake 0
    assert 't("noData")' in table  # an empty selection is spelled out
    i18n_js = (Path(server_module.__file__).parent / "static" / "i18n.js"
               ).read_text(encoding="utf-8")
    assert "Нет данных" in i18n_js


def test_dashboard_validates_url_filter_state():
    """Static contract (FAN-1255): readFiltersFromUrl runs after /api/meta
    populated the selects and must validate every URL parameter — unknown
    dimension IDs, non-option days/group values and malformed or unordered
    from/to are dropped, the URL is rewritten to the surviving state and a
    visible note says what was reset. The behavioural counterpart lives in
    tests/test_dashboard_browser.py."""
    app_js = (Path(server_module.__file__).parent / "static" / "app.js"
              ).read_text(encoding="utf-8")
    read = _js_function(app_js, "readFiltersFromUrl")
    for guard in ("PERIOD_VALUES", "GROUP_VALUES", "isValidDateTimeLocal",
                  "rangeIsOrdered", "syncFiltersToUrl", "showFilterError"):
        assert guard in read, guard
    # Chrome's lenient Date.parse normalizes calendar-impossible dates
    # (2026-02-30 parses as March 2) instead of returning NaN, so the
    # validator must judge the parts itself via a UTC round-trip and never
    # delegate to the parser (FAN-1269).
    validator = _js_function(app_js, "isValidDateTimeLocal")
    assert "Date.parse" not in validator
    assert "Date.UTC" in validator
    # The interactive range inputs share the ordering guard, so a reverse
    # range typed by hand never reaches state or the URL either.
    boot = _js_function(app_js, "boot")
    assert "rangeIsOrdered" in boot
    assert "showFilterError" in boot


def test_dashboard_has_filter_reset_and_error_note():
    """Static contract (FAN-1255): the filter bar offers one unambiguous
    reset to canonical / and a dedicated, initially hidden error note that
    stays hidden while its hidden attribute is set."""
    static = Path(server_module.__file__).parent / "static"
    index_html = (static / "index.html").read_text(encoding="utf-8")
    assert 'id="filter-reset"' in index_html
    assert re.search(r'<span id="filter-error"[^>]*hidden', index_html)
    style_css = (static / "style.css").read_text(encoding="utf-8")
    assert ".filter-error[hidden]" in style_css
    app_js = (static / "app.js").read_text(encoding="utf-8")
    reset = _js_function(app_js, "resetFilters")
    for step in ("syncFiltersToUrl", "clearFilterError", "refreshAll"):
        assert step in reset, step


def test_connection_error_is_localized_and_names_the_workspace():
    """Static contract (FAN-1436): the cabinet never shows the raw English
    worker code. Each known code maps to an actionable Russian message, an
    unknown value falls back to the generic Russian one (no verbatim echo), and
    the workspace-resolution failures name the label the owner typed so
    "подключил, но не работает" becomes diagnosable ("рабочее пространство «X»
    не найдено")."""
    app_js = (Path(server_module.__file__).parent / "static" / "app.js"
              ).read_text(encoding="utf-8")
    # The raw allowlist Set is gone; only a code->message map remains.
    assert "SAFE_CONNECTION_ERRORS" not in app_js
    # The workspace failure the incident hit is mapped with the typed label.
    assert '"the connection\'s workspace could not be resolved":' in app_js
    assert '"the connection\'s workspace label is ambiguous":' in app_js
    # Product copy lives in the shared locale catalog, so both RU and EN keep
    # the same actionable workspace-specific diagnostic.
    i18n_js = (Path(server_module.__file__).parent / "static" / "i18n.js"
               ).read_text(encoding="utf-8")
    assert "не найдено у этого PAT" in i18n_js
    assert "was not found for this PAT" in i18n_js
    render = _js_function(app_js, "safeConnectionError")
    assert "CONNECTION_WORKSPACE_ERRORS" in render
    assert "CONNECTION_ERROR_MESSAGES" in render
    assert "CONNECTION_ERROR_FALLBACK" in render
    # The message wraps the label in guillemets rather than echoing a code.
    assert "«" in render
    # Both error render paths pass the host-side workspace label so the
    # message can name it.
    status_message = _js_function(app_js, "connectionStatusMessage")
    assert "workspace_label" in status_message
    render_connection = _js_function(app_js, "renderConnection")
    assert "normalized.workspace_label" in render_connection


# The SSE generator is tested directly: Starlette's TestClient buffers whole
# responses, so an endless /api/events stream cannot be consumed through it.
# The live HTTP path is covered by stage-3 manual verification (curl).


def _collect_sse_frames(get_state, max_disconnect_checks):
    checks = {"n": 0}

    async def is_disconnected():
        checks["n"] += 1
        return checks["n"] > max_disconnect_checks

    async def collect():
        frames = []
        async for frame in server_module.update_event_stream(get_state, is_disconnected):
            frames.append(frame)
        return frames

    return asyncio.get_event_loop().run_until_complete(collect())


def _sync_state(beat_seq, cycle_id, phase="cycle"):
    return {
        "beat": {"seq": beat_seq, "at": "2026-01-01T00:00:00Z", "phase": phase},
        "cycle": {"id": cycle_id} if cycle_id else None,
    }


def _stream_states(states):
    """get_state stub: walk through `states`, then repeat the last one."""
    remaining = iter(states)
    last = {"state": None}

    def get_state():
        last["state"] = next(remaining, last["state"])
        return last["state"]

    return get_state


def test_sse_stream_emits_update_on_live_beat_without_cycle_event(monkeypatch):
    """The mid-cycle live beat must wake clients on its own — that is the
    live-latency fix — and must not fake a completed-cycle event."""
    monkeypatch.setattr(server_module, "SSE_CHECK_SECONDS", 0.005)
    states = [_sync_state(2, 1), _sync_state(2, 1), _sync_state(3, 1, phase="live")]
    frames = _collect_sse_frames(_stream_states(states), max_disconnect_checks=5)

    assert frames[0].startswith("event: hello\n")
    updates = [f for f in frames if f.startswith("event: update")]
    assert len(updates) == 1
    assert '"phase": "live"' in updates[0]
    assert not [f for f in frames if f.startswith("event: cycle")]


def test_sse_stream_emits_update_and_cycle_on_new_poll_cycle(monkeypatch):
    monkeypatch.setattr(server_module, "SSE_CHECK_SECONDS", 0.005)
    states = [_sync_state(2, 1), _sync_state(2, 1), _sync_state(3, 2)]
    frames = _collect_sse_frames(_stream_states(states), max_disconnect_checks=5)

    updates = [f for f in frames if f.startswith("event: update")]
    assert len(updates) == 1
    cycles = [f for f in frames if f.startswith("event: cycle")]
    assert cycles == ['event: cycle\ndata: {"id": 2}\n\n']


def test_sse_stream_sends_keepalive_when_idle(monkeypatch):
    monkeypatch.setattr(server_module, "SSE_CHECK_SECONDS", 0.005)
    monkeypatch.setattr(server_module, "SSE_KEEPALIVE_SECONDS", 0.005)
    frames = _collect_sse_frames(lambda: _sync_state(1, 1), max_disconnect_checks=3)
    assert ": keepalive\n\n" in frames
    assert not [f for f in frames if f.startswith("event: update")]


def test_sse_endpoint_is_registered(api):
    client, _ = api
    routes = {r.path for r in client.app.routes}
    assert "/api/events" in routes


def test_flow_endpoint_shape_and_validation(api):
    """FAN-3306: /api/flow serves truthful nulls + coverage on a database
    without flow history, and rejects a non-contract window."""
    client, _ = api
    out = client.get("/api/flow?days=7").json()
    assert out["days"] == 7
    assert set(out) >= {
        "cycle_time", "rework", "idle", "coverage", "lanes", "frontier",
        "lineage",
    }
    assert out["cycle_time"]["median_seconds"] is None
    assert out["rework"]["rate"] is None
    assert out["idle"]["share"] is None
    assert out["frontier"]["pm_p95_seconds"] is None
    assert out["lineage"]["first_pass_rate"] is None
    assert client.get("/api/flow?days=13").status_code == 422
    assert client.get("/api/flow?days=abc").status_code == 422


def test_dashboard_flow_panel_static_contract():
    """Static contract (FAN-3306): the flow panel exists with its window/lane
    controls and tiles, and renderFlow never coerces a missing metric to 0 —
    N/A stays an explicit dash with the coverage line spelled out."""
    static = Path(server_module.__file__).parent / "static"
    index_html = (static / "index.html").read_text(encoding="utf-8")
    assert 'id="flow-panel"' in index_html
    for control in ("flow-days", "flow-lane"):
        assert f'id="{control}"' in index_html
    for card in ("card-flow-cycle", "card-flow-p90", "card-flow-rework",
                 "card-flow-idle", "card-flow-ready", "card-flow-pm-p95",
                 "card-flow-waiting", "card-flow-first-pass"):
        assert f'id="{card}"' in index_html
    assert 'id="table-flow-groups"' in index_html
    assert 'id="flow-coverage"' in index_html

    app_js = (static / "app.js").read_text(encoding="utf-8")
    render = _js_function(app_js, "renderFlow")
    assert "|| 0" not in render
    assert 't("noData")' in render
    assert "flowCoverageNoEvents" in render  # absent history is spelled out
    assert "flowFirstPassSub" in render
    share = _js_function(app_js, "fmtShare")
    assert "—" in share  # null share/rate renders as a dash, not 0%

    i18n_js = (static / "i18n.js").read_text(encoding="utf-8")
    for key in ("flowMetrics", "flowLane", "allLanes", "flowCoverageDetail",
                "flowFirstPassSub", "flowReady", "flowPmP95", "flowWaiting"):
        assert key + ":" in i18n_js
