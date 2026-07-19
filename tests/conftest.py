import base64
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aistat.db import connect, init_db  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


def assert_opaque_session_cookie(value, forbidden):
    """Prove an ``aistat_session`` value is a bare opaque token (FAN-1392).

    The value must be a single URL-safe token — no ``.`` separator, so no
    signed/serialized envelope — and neither the raw value nor any base64
    decoding of it may parse into a claims object or contain any of the
    ``forbidden`` identity/CSRF strings. This is the client-visible half of the
    "no client-authoritative auth claims" property.
    """
    assert value, "no session cookie value"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", value), value
    haystacks = [value]
    padded = value + "=" * (-len(value) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            haystacks.append(decoder(padded.encode("ascii")).decode("latin-1"))
        except Exception:
            pass
    for candidate in haystacks:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            parsed = None
        assert not isinstance(parsed, (dict, list)), candidate
    blob = "\n".join(haystacks)
    for needle in forbidden:
        if needle:
            assert str(needle) not in blob, needle


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    init_db(connection)
    yield connection
    connection.close()


def seed_aggregate_fixture(conn):
    """A small, hand-checkable workspace for aggregation tests.

    Agents: A1 owns (R1, m-claude) alone; A2+A3 share (R2, m-shared).
    2026-01-01 run durations: A1 1h (I1@P1); A2 3h (I1@P1 1h + I2@P1 2h);
    A3 2h (I2@P1 1h + I3@P2 1h) → shared-pair weights A2=0.6 / A3=0.4,
    A3's day is split 50/50 between P1 and P2.
    2026-01-02 daily rows have no matching agent pair (R4 / m-mystery) →
    unattributed; m-mystery has no pricing row → unpriced.
    """
    now = "2026-01-02T00:00:00Z"
    conn.executescript(f"""
    INSERT INTO runtimes (id, name, provider, status, synced_at) VALUES
      ('R1', 'Claude RT', 'claude', 'online', '{now}'),
      ('R2', 'Codex RT', 'codex', 'online', '{now}'),
      ('R4', 'Spare RT', 'codex', 'online', '{now}');

    INSERT INTO agents (id, name, model, runtime_id, synced_at) VALUES
      ('A1', 'Solo Claude', 'm-claude', 'R1', '{now}'),
      ('A2', 'Dev Shared', 'm-shared', 'R2', '{now}'),
      ('A3', 'QA Shared', 'm-shared', 'R2', '{now}');

    INSERT INTO projects (id, title, status, synced_at) VALUES
      ('P1', 'Alpha', 'in_progress', '{now}'),
      ('P2', 'Beta', 'in_progress', '{now}');

    INSERT INTO issues (id, identifier, title, status, project_id, story_points,
                        updated_at, synced_at) VALUES
      ('I1', 'T-1', 'with sp and usage', 'done', 'P1', 5, '{now}', '{now}'),
      ('I2', 'T-2', 'no sp', 'done', 'P1', NULL, '{now}', '{now}'),
      ('I3', 'T-3', 'zero sp', 'done', 'P2', 0, '{now}', '{now}'),
      ('I4', 'T-4', 'sp but no usage', 'in_progress', 'P2', 2, '{now}', '{now}'),
      ('I5', 'T-5', 'usage but no runs', 'done', 'P2', NULL, '{now}', '{now}');

    INSERT INTO issue_usage (issue_id, task_count, total_input_tokens,
                             total_output_tokens, total_cache_read_tokens,
                             total_cache_write_tokens, synced_at) VALUES
      ('I1', 2, 1000, 500, 0, 0, '{now}'),
      ('I2', 2, 2000, 0, 0, 0, '{now}'),
      ('I3', 1, 300, 0, 0, 0, '{now}'),
      ('I5', 1, 100, 0, 0, 0, '{now}');

    INSERT INTO runs (id, issue_id, agent_id, runtime_id, status,
                      started_at, completed_at, synced_at) VALUES
      ('run1', 'I1', 'A1', 'R1', 'completed', '2026-01-01T10:00:00Z', '2026-01-01T11:00:00Z', '{now}'),
      ('run2', 'I1', 'A2', 'R2', 'completed', '2026-01-01T10:00:00Z', '2026-01-01T11:00:00Z', '{now}'),
      ('run3', 'I2', 'A2', 'R2', 'completed', '2026-01-01T12:00:00Z', '2026-01-01T14:00:00Z', '{now}'),
      ('run4', 'I2', 'A3', 'R2', 'completed', '2026-01-01T12:00:00Z', '2026-01-01T13:00:00Z', '{now}'),
      ('run5', 'I3', 'A3', 'R2', 'completed', '2026-01-01T14:00:00Z', '2026-01-01T15:00:00Z', '{now}');

    INSERT INTO daily_usage (runtime_id, model, date, input_tokens, output_tokens,
                             cache_read_tokens, cache_write_tokens,
                             cost_usd, cost_credits, cost_priced, synced_at) VALUES
      ('R1', 'm-claude', '2026-01-01', 1000000, 0, 0, 0, 1.0, 1.0, 1, '{now}'),
      ('R2', 'm-shared', '2026-01-01', 3000000, 0, 0, 0, 6.0, 6.0, 1, '{now}'),
      ('R4', 'm-claude', '2026-01-02', 500000, 0, 0, 0, 0.5, 0.5, 1, '{now}'),
      ('R2', 'm-mystery', '2026-01-02', 200000, 0, 0, 0, NULL, NULL, 0, '{now}');

    INSERT INTO model_pricing (model, input_rate, output_rate, cache_read_rate,
                               cache_write_rate, unpriced, loaded_at) VALUES
      ('m-claude', 1.0, 0.0, 0.0, 0.0, 0, '{now}'),
      ('m-shared', 2.0, 4.0, 0.2, 2.5, 0, '{now}');

    INSERT INTO poll_cycles (started_at, finished_at, sources_ok, sources_failed)
      VALUES ('2026-01-02T00:00:00Z', '2026-01-02T00:00:30Z', 10, 0);
    """)
    conn.commit()


def seed_model_less_fixture(conn):
    """FAN-1247: one issue whose two selected hour-long runs split between a
    known model (A1 / m-claude) and an agent without model metadata (A5).

    Expected split for a window covering both hours: the known model owns
    500 tokens / 2 SP / 1 hour / $0.0005, the model-less half stays unpriced
    (unpriced_tokens=500, has_unpriced), total active hours 2.0.
    """
    now = "2026-01-04T00:00:00Z"
    conn.executescript(f"""
    INSERT INTO projects (id, title, status, synced_at) VALUES
      ('P3', 'Gamma', 'in_progress', '{now}');
    INSERT INTO agents (id, name, model, runtime_id, synced_at) VALUES
      ('A5', 'Modelless', NULL, 'R4', '{now}');
    INSERT INTO issues (id, identifier, title, status, project_id, story_points,
                        updated_at, synced_at) VALUES
      ('I8', 'T-8', 'mixed model metadata', 'done', 'P3', 4, '{now}', '{now}');
    INSERT INTO issue_usage (issue_id, task_count, total_input_tokens,
                             total_output_tokens, total_cache_read_tokens,
                             total_cache_write_tokens, synced_at) VALUES
      ('I8', 1, 1000, 0, 0, 0, '{now}');
    INSERT INTO runs (id, issue_id, agent_id, runtime_id, status,
                      started_at, completed_at, synced_at) VALUES
      ('run9', 'I8', 'A1', 'R1', 'completed',
       '2026-01-04T10:00:00Z', '2026-01-04T11:00:00Z', '{now}'),
      ('run10', 'I8', 'A5', 'R4', 'completed',
       '2026-01-04T11:00:00Z', '2026-01-04T12:00:00Z', '{now}');
    """)
    conn.commit()


@pytest.fixture
def agg_conn(conn):
    seed_aggregate_fixture(conn)
    return conn
