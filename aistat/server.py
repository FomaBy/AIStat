"""FastAPI app: aggregate API, SSE live updates and the static dashboard.

Endpoints:
    GET /api/meta        — filter data (projects, agents, models, date span)
    GET /api/summary     — headline cards (?from&to&project)
    GET /api/daily       — per-day series (?group=model|agent|project&from&to&project)
    GET /api/agents      — per-agent totals (?from&to&project)
    GET /api/projects    — per-project cuts (tokens, est. cost, SP, statuses, efficiency)
    GET /api/efficiency  — per-issue tokens/SP, worst first (?project&limit)
    GET /api/health      — health snapshot (alias of /health)
    GET /api/events      — SSE: `update` after every poller data batch
                           (live phase or full cycle), `cycle` on full cycles
    GET /                — the dashboard (static, Chart.js vendored locally)

The poller runs in a separate process; the SSE endpoint watches the
sync_beats counter (bumped by the poller after its live phase and after
every full cycle) plus the poll_cycles table, so the frontend refreshes as
soon as live data lands — not only when a whole cycle finishes.

Run: .venv/bin/uvicorn aistat.server:app --port 8787   (or ./run.sh)
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, aggregates
from .config import Config
from .db import connect, init_db
from .health import snapshot

STATIC_DIR = Path(__file__).resolve().parent / "static"

# SSE tuning: how often the beat watcher polls the DB, and how often an
# idle stream emits a keepalive comment so proxies don't drop it.
SSE_CHECK_SECONDS = 2.0
SSE_KEEPALIVE_SECONDS = 15.0


def _sync_marks(state) -> tuple:
    """The change signature of a sync state: (beat seq, last cycle id)."""
    beat = state.get("beat") or {}
    cycle = state.get("cycle") or {}
    return (beat.get("seq"), cycle.get("id"))


async def update_event_stream(last_sync_state, is_disconnected):
    """SSE frames: `hello` with the current sync state, then `update`
    whenever the poller commits a fresh data batch (its live phase or a full
    cycle) — plus a `cycle` event with the cycle row when a full cycle lands,
    kept for external watchers — with keepalive comments in between.

    Takes callables instead of a request/connection so it can be tested
    directly — Starlette's TestClient buffers whole responses and would hang
    on an endless stream.
    """
    state = last_sync_state()
    last = _sync_marks(state)
    yield "event: hello\ndata: " + json.dumps(state) + "\n\n"
    idle = 0.0
    while not await is_disconnected():
        await asyncio.sleep(SSE_CHECK_SECONDS)
        idle += SSE_CHECK_SECONDS
        state = last_sync_state()
        current = _sync_marks(state)
        if current != last:
            idle = 0.0
            yield "event: update\ndata: " + json.dumps(state) + "\n\n"
            if current[1] != last[1] and state.get("cycle"):
                yield "event: cycle\ndata: " + json.dumps(state["cycle"]) + "\n\n"
            last = current
        elif idle >= SSE_KEEPALIVE_SECONDS:
            idle = 0.0
            yield ": keepalive\n\n"


def create_app(config: Optional[Config] = None) -> FastAPI:
    config = config or Config()
    app = FastAPI(title="AIStat", version=__version__)

    config.ensure_db_dir()
    bootstrap = connect(config.db_path)
    try:
        init_db(bootstrap)
    finally:
        bootstrap.close()

    def db() -> sqlite3.Connection:
        return connect(config.db_path)

    @app.get("/api/meta")
    def api_meta():
        conn = db()
        try:
            return aggregates.meta(conn)
        finally:
            conn.close()

    @app.get("/api/summary")
    def api_summary(
        date_from: Optional[str] = Query(None, alias="from"),
        date_to: Optional[str] = Query(None, alias="to"),
        project: Optional[str] = Query(None),
    ):
        conn = db()
        try:
            return aggregates.summary(
                conn, date_from, date_to, project,
                credits_per_usd=config.credits_per_usd,
            )
        finally:
            conn.close()

    @app.get("/api/daily")
    def api_daily(
        group: str = Query("model"),
        date_from: Optional[str] = Query(None, alias="from"),
        date_to: Optional[str] = Query(None, alias="to"),
        project: Optional[str] = Query(None),
    ):
        conn = db()
        try:
            return aggregates.daily_series(conn, group, date_from, date_to, project)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        finally:
            conn.close()

    @app.get("/api/agents")
    def api_agents(
        date_from: Optional[str] = Query(None, alias="from"),
        date_to: Optional[str] = Query(None, alias="to"),
        project: Optional[str] = Query(None),
    ):
        conn = db()
        try:
            return {"agents": aggregates.agent_totals(conn, date_from, date_to, project)}
        finally:
            conn.close()

    @app.get("/api/projects")
    def api_projects():
        conn = db()
        try:
            return {"projects": aggregates.projects_overview(
                conn, credits_per_usd=config.credits_per_usd
            )}
        finally:
            conn.close()

    @app.get("/api/efficiency")
    def api_efficiency(
        project: Optional[str] = Query(None),
        limit: Optional[int] = Query(None, ge=1, le=1000),
    ):
        conn = db()
        try:
            return {"issues": aggregates.issue_efficiency(conn, project, limit)}
        finally:
            conn.close()

    def health_payload():
        conn = db()
        try:
            return snapshot(conn, db_path=str(config.db_path),
                            credits_per_usd=config.credits_per_usd)
        finally:
            conn.close()

    @app.get("/health")
    def health():
        return health_payload()

    @app.get("/api/health")
    def api_health():
        return health_payload()

    def last_sync_state() -> dict:
        conn = db()
        try:
            beat = conn.execute(
                "SELECT seq, at, phase FROM sync_beats WHERE id = 1"
            ).fetchone()
            cycle = conn.execute(
                "SELECT id, started_at, finished_at, sources_ok, sources_failed "
                "FROM poll_cycles ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "beat": dict(beat) if beat else None,
                "cycle": dict(cycle) if cycle else None,
            }
        finally:
            conn.close()

    @app.get("/api/events")
    async def api_events(request: Request):
        """SSE stream: `update` whenever the poller lands fresh data."""
        return StreamingResponse(
            update_event_stream(last_sync_state, request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/sync")
    def api_sync():
        return last_sync_state()

    # Mounted last so /api and /health win over static paths.
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


app = create_app()
