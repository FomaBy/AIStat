"""API endpoint tests: FastAPI app over a seeded temporary database."""

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import aistat.server as server_module
from aistat.config import Config
from aistat.db import connect, init_db
from conftest import seed_aggregate_fixture, seed_model_less_fixture


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
    assert mixed["cost_per_sp"] == pytest.approx(0.000125)
    assert mixed["weighted_efficiency"] is None

    null_only = client.get("/api/model-efficiency", params={"agent": "A5"}).json()
    assert [m["model"] for m in null_only["models"]] == [None]
    assert null_only["cost_per_sp"] is None
    assert null_only["weighted_efficiency"] is None
    assert null_only["unpriced_tokens"] == 500
    assert null_only["active_hours"] == pytest.approx(1.0)

    exact = client.get("/api/model-efficiency", params={"project": "P3"}).json()
    assert [m["model"] for m in exact["models"]] == ["m-claude", None]
    assert exact["cost_per_sp"] == pytest.approx(0.000125)
    assert exact["unpriced_tokens"] == 500
    assert exact["weighted_efficiency"] is None

    summary = client.get("/api/summary", params=[
        ("from", "2026-01-04"), ("to", "2026-01-04"), ("project", "P3"),
    ]).json()
    assert summary["cost_per_sp"] == pytest.approx(0.000125)
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
    assert "Нет данных" in table  # an empty selection is spelled out


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
