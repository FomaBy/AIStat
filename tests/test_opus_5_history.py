"""FAN-1715 regression tests for immutable Opus model identities."""

from aistat import aggregates as ag
from aistat import store
from aistat.db import SCHEMA_VERSION, connect, init_db
from conftest import seed_opus_transition_fixture


def test_init_db_backfills_legacy_runs_at_exact_opus_cutoff(tmp_path):
    path = tmp_path / "legacy-v4.db"
    conn = connect(path)
    init_db(conn)
    conn.executescript("""
    DROP TABLE runs;
    CREATE TABLE runs (
        id TEXT PRIMARY KEY, issue_id TEXT, agent_id TEXT, runtime_id TEXT,
        kind TEXT, status TEXT, attempt INTEGER, error TEXT, created_at TEXT,
        dispatched_at TEXT, started_at TEXT, completed_at TEXT,
        synced_at TEXT NOT NULL
    );
    """)
    agent_id = "e2e1c89f-587d-4a2d-bbaa-ce9b5dea908d"
    conn.execute(
        "INSERT INTO agents (id, model, synced_at) VALUES (?, ?, ?)",
        (agent_id, "claude-opus-5", "2026-07-24T23:00:00Z"),
    )
    conn.executemany(
        "INSERT INTO runs (id, agent_id, started_at, synced_at) VALUES (?, ?, ?, ?)",
        [
            ("old", agent_id, "2026-07-24T21:31:45Z", "2026-07-24T23:00:00Z"),
            ("new", agent_id, "2026-07-24T21:31:46Z", "2026-07-24T23:00:00Z"),
        ],
    )
    conn.execute("PRAGMA user_version = 4")
    init_db(conn)
    first = dict(conn.execute("SELECT id, model FROM runs ORDER BY id").fetchall())
    assert first == {"new": "claude-opus-5", "old": "claude-opus-4-8"}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5

    # Reopening/migrating is idempotent and cannot rewrite stored snapshots.
    conn.execute("UPDATE agents SET model = 'claude-fable-5' WHERE id = ?", (agent_id,))
    init_db(conn)
    assert dict(conn.execute("SELECT id, model FROM runs ORDER BY id").fetchall()) == first
    conn.close()


def test_opus_history_survives_catalog_change_in_aggregates(conn):
    seed_opus_transition_fixture(conn)
    models = dict(conn.execute("SELECT id, model FROM runs ORDER BY id").fetchall())
    assert models == {
        "run-opus-4": "claude-opus-4-8",
        "run-opus-5": "claude-opus-5",
    }

    # The literal daily models stay separate despite sharing runtime and date.
    assert ag.meta(conn)["models"] == ["claude-opus-4-8", "claude-opus-5"]
    daily = ag.daily_series(conn, group="model")["rows"]
    assert {(r["date"], r["key"]): r["total_tokens"] for r in daily} == {
        ("2026-07-24", "claude-opus-4-8"): 1_000_000,
        ("2026-07-24", "claude-opus-5"): 1_000_000,
    }

    # Changing today's catalog model must not alter past task/SP/time cuts or
    # make the 4.8 daily row unattributed.
    conn.execute(
        "UPDATE agents SET model = 'claude-fable-5' "
        "WHERE id = 'e2e1c89f-587d-4a2d-bbaa-ce9b5dea908d'"
    )
    conn.commit()
    for model in ("claude-opus-4-8", "claude-opus-5"):
        filters = ag.make_filters(models=[model])
        summary = ag.summary(conn, filters=filters)
        assert summary["total_tokens"] == 1_000_000
        assert summary["story_points"] == 5
        assert summary["agent_work_seconds"] == 3600
        assert ag.agent_totals(conn, filters=filters)[0]["runs"] == 1

    model_efficiency = ag.efficiency_breakdown(conn)["models"]
    assert {row["model"] for row in model_efficiency} == {
        "claude-opus-4-8", "claude-opus-5",
    }

    # A duplicate arrival through issue-runs/agent-tasks keeps its first model.
    store.upsert_runs(conn, [{
        "id": "run-opus-4", "agent_id": "e2e1c89f-587d-4a2d-bbaa-ce9b5dea908d",
        "runtime_id": "fb4bfde9-ea2a-4dba-8a4f-bafd8d7c9188",
        "started_at": "2026-07-24T21:31:45Z",
    }])
    assert conn.execute(
        "SELECT model FROM runs WHERE id = 'run-opus-4'"
    ).fetchone()[0] == "claude-opus-4-8"
