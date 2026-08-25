"""Cross-tenant "Efficiency by models" aggregates (FAN-2392, FAN-2397).

The host materializes, per tenant and model, only the fields needed to price
work — token counts by kind, the model's story-point share and its estimated
cost — into ``global_model_stats`` inside security.db. No issue, project,
agent, account or timing data is stored, and the API surface exposes only
sums across every tenant, so one user can never read another user's rows.

Two different privacy claims apply, and they must not be conflated:

* the **stored rows are pseudonymous**, not anonymous — they are keyed by the
  internal numeric tenant id, so a host-side reader could still attribute a
  row to an account;
* only the **published aggregate is anonymized**, because
  :func:`model_efficiency` drops every model cohort carried by fewer than
  :data:`MIN_TENANTS` distinct tenants, so no returned figure can describe a
  single identifiable tenant's usage.

Suppression fails closed: a cohort below the threshold is omitted entirely and
never degrades to a smaller-cohort or single-tenant value. A fully suppressed
result is a normal empty result.

Rows are keyed by the internal numeric tenant id purely so a tenant's next
snapshot replaces its own contribution idempotently; deleting the user
deletes the rows in the same security.db transaction
(:meth:`aistat.security.SecurityStore.delete_user`).

``global_stats_tenants`` records which installed snapshot (sha256) each
tenant's rows were derived from, so a crash between snapshot install and
aggregation self-heals on the next worker start (:func:`sync_tenants`).

This module is dependency-free and legacy-interpreter compatible: the cPanel WSGI
entry point imports it on the shared host.
"""

import os
import sqlite3
import time

from . import aggregates
from .db import connect_readonly, schema_admission_error
from .tenant import canonical_tenant_id, tenant_db_path

# Minimum distinct tenants a model cohort needs before its sums may be
# published. Below it the whole cohort is dropped, so no published figure can
# be read back as one identifiable tenant's usage (FAN-2397).
MIN_TENANTS = 5

GLOBAL_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS global_model_stats (
    tenant_id           INTEGER NOT NULL,
    model               TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    story_points        REAL NOT NULL DEFAULT 0,
    cost_usd            REAL,
    priced              INTEGER NOT NULL DEFAULT 0,
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, model)
);
CREATE TABLE IF NOT EXISTS global_stats_tenants (
    tenant_id        INTEGER PRIMARY KEY,
    snapshot_sha256  TEXT,
    refreshed_at     INTEGER NOT NULL
);
"""


def refresh_tenant(conn, tenant_id, tenant_db_file, snapshot_sha256=None,
                   now=None):
    """Replace one tenant's per-model rows from its installed database.

    Reads the tenant database read-only, keeps only the model rows of the
    unfiltered lifetime :func:`aggregates.efficiency_breakdown` (model, token
    counts, story-point share, priced cost) and swaps them in atomically.
    Returns ``False`` without touching stored rows when the tenant database
    is not servable (pre-current schema), ``True`` otherwise.
    """
    tenant_id = canonical_tenant_id(tenant_id)
    now = int(time.time()) if now is None else int(now)
    tenant_conn = connect_readonly(tenant_db_file)
    try:
        if schema_admission_error(tenant_conn) is not None:
            return False
        models = aggregates.efficiency_breakdown(tenant_conn)["models"]
    finally:
        tenant_conn.close()
    # No explicit BEGIN: the DML below joins the connection's (possibly already
    # open) transaction, so the swap stays atomic and a caller mid-transaction
    # (e.g. boot-time recovery sharing the connection) is not broken.
    try:
        conn.execute(
            "DELETE FROM global_model_stats WHERE tenant_id = ?", (tenant_id,)
        )
        for row in models:
            conn.execute(
                "INSERT INTO global_model_stats "
                "(tenant_id, model, input_tokens, output_tokens, "
                " cache_read_tokens, cache_write_tokens, story_points, "
                " cost_usd, priced, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    row["model"],
                    row["input_tokens"],
                    row["output_tokens"],
                    row["cache_read_tokens"],
                    row["cache_write_tokens"],
                    row["story_points"],
                    row["cost_usd"],
                    0 if row["has_unpriced"] else 1,
                    now,
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO global_stats_tenants "
            "(tenant_id, snapshot_sha256, refreshed_at) VALUES (?, ?, ?)",
            (tenant_id, snapshot_sha256, now),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True


def sync_tenants(conn, tenants_dir, now=None):
    """Aggregate any installed snapshot the stats do not yet reflect.

    Compares each ``tenants`` row's installed snapshot sha256 with the sha the
    tenant's stats were last derived from, so this both backfills tenants that
    published before the feature existed and repairs a crash between snapshot
    install and aggregation. One tenant's failure never blocks the others.
    Callers must hold the ingest lock. Returns the number of refreshed tenants.
    """
    refreshed = 0
    rows = conn.execute(
        "SELECT t.user_id, t.last_snapshot_sha256, s.snapshot_sha256 AS agg_sha"
        " FROM tenants t"
        " LEFT JOIN global_stats_tenants s ON s.tenant_id = t.user_id"
    ).fetchall()
    for row in rows:
        if row["last_snapshot_sha256"] is None:
            continue
        if row["agg_sha"] == row["last_snapshot_sha256"]:
            continue
        try:
            path = tenant_db_path(tenants_dir, row["user_id"])
            if not os.path.isfile(path):
                continue
            if refresh_tenant(
                conn, row["user_id"], path, row["last_snapshot_sha256"],
                now=now,
            ):
                refreshed += 1
        except (sqlite3.Error, OSError, ValueError):
            continue
    return refreshed


def delete_tenant(conn, tenant_id):
    """Remove one tenant's rows; safe inside a caller-owned transaction."""
    tenant_id = canonical_tenant_id(tenant_id)
    conn.execute(
        "DELETE FROM global_model_stats WHERE tenant_id = ?", (tenant_id,)
    )
    conn.execute(
        "DELETE FROM global_stats_tenants WHERE tenant_id = ?", (tenant_id,)
    )


def model_efficiency(conn):
    """Anonymized per-model efficiency summed over every tenant's stored rows.

    Only cohorts carried by at least :data:`MIN_TENANTS` distinct tenants are
    returned; the ``HAVING`` drops the rest before any figure is computed, so a
    rare cohort is never published in reduced form and no contributor count is
    derivable from the result. An all-suppressed store yields ``models == []``.

    Follows the FAN-1188 pairing rule: ``cost_per_sp`` divides priced cost by
    the story points of those same priced shares only, so an unpriced tenant
    share never lands in the denominator against a $0 cost. A model with no
    priced share reports ``cost_per_sp = cost_usd = None``, never $0.
    """
    rows = conn.execute(
        """
        SELECT model,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               SUM(story_points) AS story_points,
               SUM(CASE WHEN priced = 1 THEN cost_usd ELSE 0 END) AS priced_cost,
               SUM(CASE WHEN priced = 1 THEN story_points ELSE 0 END) AS priced_sp,
               MAX(CASE WHEN priced = 0 THEN 1 ELSE 0 END) AS has_unpriced
        FROM global_model_stats
        GROUP BY model
        HAVING COUNT(DISTINCT tenant_id) >= ?
        """,
        (MIN_TENANTS,),
    ).fetchall()
    models = []
    for row in rows:
        total_tokens = (
            row["input_tokens"] + row["output_tokens"]
            + row["cache_read_tokens"] + row["cache_write_tokens"]
        )
        priced = row["priced_sp"] > 0
        models.append({
            "model": row["model"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "cache_write_tokens": row["cache_write_tokens"],
            "total_tokens": total_tokens,
            "story_points": row["story_points"],
            "cost_usd": row["priced_cost"] if priced else None,
            "tokens_per_sp": (
                total_tokens / row["story_points"]
                if row["story_points"] > 0 else None
            ),
            "cost_per_sp": (
                row["priced_cost"] / row["priced_sp"] if priced else None
            ),
            "has_unpriced": bool(row["has_unpriced"]),
        })
    # Cheapest cost per story point first; unpriced models sink to the bottom.
    models.sort(key=lambda m: (m["cost_per_sp"] is None, m["cost_per_sp"] or 0.0))
    return {"estimated": True, "models": models}
