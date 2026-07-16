"""API endpoint tests: FastAPI app over a seeded temporary database."""

import asyncio

import pytest
from fastapi.testclient import TestClient

import aistat.server as server_module
from aistat.config import Config
from aistat.db import connect, init_db
from conftest import seed_aggregate_fixture


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


def test_projects_endpoint_uses_configured_credit_rate(api):
    client, _ = api
    projects = {p["title"]: p for p in client.get("/api/projects").json()["projects"]}
    assert projects["Alpha"]["cost_usd"] == pytest.approx(0.0065)
    assert projects["Alpha"]["cost_credits"] == pytest.approx(0.013)  # 2 credits/$


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


def test_summary_endpoint_has_cost_efficiency(api):
    client, _ = api
    s = client.get("/api/summary").json()
    assert s["cost_per_sp"] == pytest.approx(0.0005)
    assert s["weighted_efficiency"] == pytest.approx(0.00025)


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
