"""Minimal FastAPI app for stage 1: health endpoint only.

Stage 3 adds aggregate APIs, SSE live updates and the dashboard frontend.

Run: .venv/bin/uvicorn aistat.server:app --port 8787
"""

from fastapi import FastAPI

from . import __version__
from .config import Config
from .db import connect, init_db
from .health import snapshot

app = FastAPI(title="AIStat", version=__version__)
config = Config()


@app.get("/")
def root():
    return {"app": "AIStat", "version": __version__, "endpoints": ["/health"]}


@app.get("/health")
def health():
    config.ensure_db_dir()
    conn = connect(config.db_path)
    try:
        init_db(conn)
        return snapshot(conn, db_path=str(config.db_path),
                        credits_per_usd=config.credits_per_usd)
    finally:
        conn.close()
