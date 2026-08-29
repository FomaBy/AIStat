"""End-to-end lineage traces and pipeline SLOs (FAN-3460).

Two read-only views over the durable rows the poller already collects:

``trace`` reconstructs one delivery chain — issue → runs → candidate → QA →
integration → release — from a single correlation id. The correlation id is the
implementation issue: every other node names it (a QA or DevOps child through
``qa_for_issue_id`` / ``implementation_issue_id``, a run through
``runs.issue_id``), so no stage is joined on a guessed key. Passing a QA or
DevOps child id resolves to the same chain.

``slo`` reports the pipeline's service-level objectives with their window,
numerator, denominator, threshold, owner and error budget, and turns every
breach into a dedupe-ready alert event carrying the exact issue identifiers,
SHAs and run ids behind it. Delivery of those events belongs to the
control-plane alert path, not here.

Truthfulness contract (same as flow metrics): every value comes from observed
rows. A link that was expected and never observed is reported as ``missing``; a
mirrored metadata value that diverges from the immutable first observation is
reported as ``stale`` with both values, never silently reconciled; a stage the
pipeline never expected is ``not_expected``. Nothing is interpolated,
backdated, or inferred from a sibling card.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .flow_metrics import (
    WINDOW_DAYS, _fmt_ts, _has_columns, _parse_ts, _table_exists, validate_days,
)
from .normalize import INTEGRATION_SUCCESS_OUTCOMES

# A trace exposes identifiers, SHAs and run ids for drill-down; issue titles,
# comment bodies and prompt content stay out of the payload exactly as they do
# in /api/flow.
STAGES = ("issue", "run", "candidate", "qa", "integration", "release")

OBSERVED = "observed"
MISSING = "missing"
STALE = "stale"
NOT_EXPECTED = "not_expected"

# Most cards have a handful of runs and QA attempts; a pathological chain is
# capped so one request cannot return an unbounded payload.
MAX_NODES = 200
MAX_SUBJECTS = 20
MAX_TRACE_ID = 128

HOUR = 3600
DAY = 24 * HOUR

# SLO catalogue. `objective` is the share of the denominator that must meet
# `threshold_seconds`; `owner` is the role accountable for the breach.
SLO_DEFINITIONS = (
    {"id": "pm_readiness_latency", "owner": "pm", "objective": 0.90,
     "threshold_seconds": 2 * DAY,
     "sli": "issue created -> first observed dispatch_ready"},
    {"id": "dispatch_latency", "owner": "dispatcher", "objective": 0.90,
     "threshold_seconds": HOUR,
     "sli": "first observed dispatch_ready -> first run created"},
    {"id": "production_data_freshness", "owner": "ops", "objective": 0.99,
     "threshold_seconds": 900,
     "sli": "poll cycles finished with every source healthy"},
    {"id": "ci_green", "owner": "devops", "objective": 0.90,
     "threshold_seconds": None,
     "sli": "observed integration CI conclusions that are success"},
    {"id": "release_gate", "owner": "devops", "objective": 0.90,
     "threshold_seconds": 2 * DAY,
     "sli": "QA PASSED -> post-QA integration observed"},
)


# -- schema probes -------------------------------------------------------------


def _has_lineage_schema(conn: sqlite3.Connection) -> bool:
    return (_table_exists(conn, "lineage_stage_events")
            and _table_exists(conn, "qa_lineage_events")
            and _has_columns(conn, "issues",
                             ("candidate_sha", "qa_issue_id",
                              "integration_required",
                              "integration_outcome", "integration_sha",
                              "release_version")))


def _row_get(row: Optional[sqlite3.Row], name: str) -> Any:
    return None if row is None else row[name]


def _seconds(start: Optional[str], end: Optional[str]) -> Optional[float]:
    """Non-negative elapsed seconds, or None when either bound is unusable."""
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return delta if delta >= 0 else None


# -- trace ---------------------------------------------------------------------


def _resolve_root(conn: sqlite3.Connection,
                  trace_id: str) -> Tuple[Optional[sqlite3.Row],
                                          Optional[sqlite3.Row]]:
    """The requested node and the implementation issue that roots its chain."""
    requested = conn.execute(
        "SELECT * FROM issues WHERE id = ? OR UPPER(identifier) = UPPER(?)",
        (trace_id, trace_id),
    ).fetchone()
    if requested is None:
        return None, None
    parent_id = requested["qa_for_issue_id"]
    if parent_id and parent_id != requested["id"]:
        root = conn.execute(
            "SELECT * FROM issues WHERE id = ?", (parent_id,)
        ).fetchone()
        if root is not None:
            return requested, root
    return requested, requested


def _issue_stage(conn: sqlite3.Connection, root: sqlite3.Row) -> Dict[str, Any]:
    ready = conn.execute(
        "SELECT MIN(observed_at) AS at FROM issue_readiness_events "
        "WHERE issue_id = ? AND initial = 0", (root["id"],),
    ).fetchone() if _table_exists(conn, "issue_readiness_events") else None
    baseline = conn.execute(
        "SELECT MIN(observed_at) AS at FROM issue_readiness_events "
        "WHERE issue_id = ? AND initial = 1", (root["id"],),
    ).fetchone() if _table_exists(conn, "issue_readiness_events") else None
    return {
        "stage": "issue",
        "status": OBSERVED,
        "issue_id": root["id"],
        "identifier": root["identifier"],
        "project_id": root["project_id"],
        "dispatch_lane": root["dispatch_lane"],
        "issue_status": root["status"],
        "created_at": root["created_at"],
        "ready_at": _row_get(ready, "at"),
        # A card already ready when collection started has a baseline only; its
        # preparation time is not derivable and is never back-filled.
        "ready_baseline_only": bool(_row_get(baseline, "at")
                                    and not _row_get(ready, "at")),
    }


def _run_stage(conn: sqlite3.Connection, root: sqlite3.Row) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT id, created_at, dispatched_at, started_at, completed_at, status "
        "FROM runs WHERE issue_id = ? ORDER BY COALESCE(created_at, id) "
        "LIMIT ?", (root["id"], MAX_NODES),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE issue_id = ?", (root["id"],)
    ).fetchone()[0]
    provenance = {}
    if _table_exists(conn, "run_attribution_events"):
        for state, count in conn.execute(
            "SELECT provenance_state, COUNT(*) FROM run_attribution_events "
            "WHERE issue_id = ? GROUP BY provenance_state", (root["id"],),
        ):
            provenance[state] = count
    return {
        "stage": "run",
        "status": OBSERVED if total else MISSING,
        "runs": total,
        "truncated": total > len(rows),
        "run_ids": [row["id"] for row in rows],
        "first_dispatched_at": min(
            [row["dispatched_at"] for row in rows if row["dispatched_at"]] or
            [None]),
        "last_completed_at": max(
            [row["completed_at"] for row in rows if row["completed_at"]] or
            [None]),
        "provenance": provenance,
    }


def _qa_rows(conn: sqlite3.Connection, root_id: str) -> List[sqlite3.Row]:
    if not _table_exists(conn, "qa_lineage_events"):
        return []
    return conn.execute(
        """
        SELECT e.qa_issue_id, e.candidate, e.verdict, e.verdict_at,
               e.observed_at, e.initial, i.identifier AS qa_identifier
        FROM qa_lineage_events e
        LEFT JOIN issues i ON i.id = e.qa_issue_id
        WHERE e.implementation_issue_id = ?
        ORDER BY COALESCE(e.verdict_at, e.observed_at)
        LIMIT ?
        """, (root_id, MAX_NODES),
    ).fetchall()


def _candidate_stage(root: sqlite3.Row,
                     qa_rows: List[sqlite3.Row]) -> Dict[str, Any]:
    mirrored = root["candidate_sha"]
    reviewed = []
    for row in qa_rows:
        if row["candidate"] and row["candidate"] not in reviewed:
            reviewed.append(row["candidate"])
    status = OBSERVED if (mirrored or reviewed) else MISSING
    if mirrored and reviewed and mirrored not in reviewed:
        # The card now advertises a candidate no QA event ever reviewed: the
        # link is reported as stale with both sides, never reconciled.
        status = STALE
    return {
        "stage": "candidate",
        "status": status,
        "candidate_sha": mirrored,
        "candidate_source": "mirrored_metadata" if mirrored else None,
        "reviewed_candidates": reviewed,
    }


def _qa_stage(root: sqlite3.Row, qa_rows: List[sqlite3.Row]) -> Dict[str, Any]:
    attempts = [
        {
            "qa_issue_id": row["qa_issue_id"],
            "identifier": row["qa_identifier"],
            "candidate": row["candidate"],
            "verdict": row["verdict"],
            "verdict_at": row["verdict_at"],
            "observed_at": row["observed_at"],
            "baseline": bool(row["initial"]),
        }
        for row in qa_rows
    ]
    expected = root["qa_issue_id"]
    if attempts:
        status = OBSERVED
    elif expected:
        status = MISSING
    else:
        status = NOT_EXPECTED
    passed = [a for a in attempts if a["verdict"] == "PASSED"]
    return {
        "stage": "qa",
        "status": status,
        "expected_qa_issue_id": expected,
        "attempts": attempts,
        "accepted_candidate": passed[0]["candidate"] if passed else None,
        "accepted_at": passed[0]["verdict_at"] if passed else None,
    }


def _stage_event(conn: sqlite3.Connection, root_id: str,
                 stage: str) -> Optional[sqlite3.Row]:
    if not _table_exists(conn, "lineage_stage_events"):
        return None
    return conn.execute(
        "SELECT * FROM lineage_stage_events "
        "WHERE implementation_issue_id = ? AND stage = ?", (root_id, stage),
    ).fetchone()


def _integration_stage(conn: sqlite3.Connection, root: sqlite3.Row,
                       qa_stage: Dict[str, Any]) -> Dict[str, Any]:
    event = _stage_event(conn, root["id"], "integration")
    required = bool(root["integration_required"])
    mirrored_sha = root["integration_sha"]
    if event is not None:
        status = OBSERVED
        if mirrored_sha and event["reference"] and mirrored_sha != event["reference"]:
            status = STALE
    elif required and qa_stage["accepted_candidate"]:
        # QA accepted a candidate and the pipeline declared integration
        # required, so the absent link is a real gap, not an unused stage.
        status = MISSING
    else:
        # No accepted candidate yet (or no integration required): the gate is
        # legitimately closed, which is not a missing link.
        status = NOT_EXPECTED
    return {
        "stage": "integration",
        "status": status,
        "required": required,
        "integration_issue_id": (_row_get(event, "stage_issue_id")
                                 or root["integration_issue_id"]),
        "outcome": _row_get(event, "outcome") or root["integration_outcome"],
        "integration_sha": _row_get(event, "reference"),
        "mirrored_integration_sha": mirrored_sha,
        "ci_status": _row_get(event, "ci_status") or root["integration_ci_status"],
        "observed_at": _row_get(event, "observed_at"),
        "baseline": bool(_row_get(event, "initial")),
    }


def _release_stage(conn: sqlite3.Connection, root: sqlite3.Row,
                   integration: Dict[str, Any]) -> Dict[str, Any]:
    event = _stage_event(conn, root["id"], "release")
    mirrored = root["release_version"]
    if event is not None:
        status = OBSERVED
        if mirrored and mirrored != event["outcome"]:
            status = STALE
    elif integration["status"] == OBSERVED and integration["outcome"] in \
            INTEGRATION_SUCCESS_OUTCOMES:
        # Integrated work that carries no release version yet is an open gap in
        # the chain; nothing is assumed about a release that was not observed.
        status = MISSING
    else:
        status = NOT_EXPECTED
    return {
        "stage": "release",
        "status": status,
        "release_version": _row_get(event, "outcome"),
        "mirrored_release_version": mirrored,
        "observed_at": _row_get(event, "observed_at"),
        "baseline": bool(_row_get(event, "initial")),
    }


def trace(conn: sqlite3.Connection, trace_id: str) -> Dict[str, Any]:
    """The /api/lineage payload: one delivery chain for one correlation id."""
    trace_id = (trace_id or "").strip()
    if not trace_id:
        raise ValueError("trace is required")
    # A correlation id is a UUID or a short FAN-identifier; anything longer is
    # rejected at the boundary rather than scanned against every issue row.
    if len(trace_id) > MAX_TRACE_ID:
        raise ValueError("trace is too long")
    if not _has_lineage_schema(conn):
        return {"trace": trace_id, "schema_supported": False, "found": False,
                "stages": [], "gaps": [], "complete": False}

    requested, root = _resolve_root(conn, trace_id)
    if root is None:
        return {"trace": trace_id, "schema_supported": True, "found": False,
                "stages": [], "gaps": [], "complete": False}

    qa_rows = _qa_rows(conn, root["id"])
    qa_stage = _qa_stage(root, qa_rows)
    integration = _integration_stage(conn, root, qa_stage)
    stages = [
        _issue_stage(conn, root),
        _run_stage(conn, root),
        _candidate_stage(root, qa_rows),
        qa_stage,
        integration,
        _release_stage(conn, root, integration),
    ]
    gaps = [s["stage"] for s in stages if s["status"] in (MISSING, STALE)]
    return {
        "trace": trace_id,
        "schema_supported": True,
        "found": True,
        "correlation_id": root["id"],
        "identifier": root["identifier"],
        "requested_issue_id": requested["id"],
        "requested_is_root": requested["id"] == root["id"],
        "stages": stages,
        "gaps": gaps,
        "complete": not gaps,
    }


# -- SLOs ----------------------------------------------------------------------


def _budget(numerator: int, denominator: int,
            objective: float) -> Dict[str, Any]:
    """Error budget for one objective; unmeasured windows stay null."""
    if denominator <= 0:
        return {"ratio": None, "measured": False, "breached": False,
                "failures": 0, "allowed_failures": None,
                "budget_remaining": None}
    ratio = float(numerator) / denominator
    failures = denominator - numerator
    allowed = (1.0 - objective) * denominator
    remaining = None
    if allowed > 0:
        remaining = round(max(0.0, 1.0 - failures / allowed), 4)
    elif failures == 0:
        remaining = 1.0
    else:
        remaining = 0.0
    return {"ratio": round(ratio, 4), "measured": True,
            "breached": ratio < objective, "failures": failures,
            "allowed_failures": round(allowed, 2),
            "budget_remaining": remaining}


def _subject(issue_id: Optional[str], identifier: Optional[str],
             **detail: Any) -> Dict[str, Any]:
    subject = {"issue_id": issue_id, "identifier": identifier}
    subject.update(detail)
    return subject


def _latency_slo(rows, threshold: Optional[int]):
    """Split (subject, seconds) pairs into met / breaching / excluded."""
    numerator = 0
    denominator = 0
    excluded = 0
    subjects = []
    for subject, seconds in rows:
        if seconds is None:
            excluded += 1
            continue
        denominator += 1
        if threshold is None or seconds <= threshold:
            numerator += 1
        else:
            subject = dict(subject)
            subject["elapsed_seconds"] = round(seconds)
            subjects.append(subject)
    return numerator, denominator, excluded, subjects


def _pm_readiness(conn, win_from, now):
    rows = conn.execute(
        """
        SELECT r.issue_id AS issue_id, i.identifier AS identifier,
               i.created_at AS created_at, MIN(r.observed_at) AS ready_at
        FROM issue_readiness_events r
        JOIN issues i ON i.id = r.issue_id
        WHERE r.initial = 0 AND i.is_jira = 0
          AND r.observed_at >= ? AND r.observed_at < ?
        GROUP BY r.issue_id
        """, (_fmt_ts(win_from), _fmt_ts(now)),
    ).fetchall()
    return [
        (_subject(row["issue_id"], row["identifier"], ready_at=row["ready_at"]),
         _seconds(row["created_at"], row["ready_at"]))
        for row in rows
    ]


def _dispatch_latency(conn, win_from, now, threshold):
    rows = conn.execute(
        """
        SELECT ready.issue_id AS issue_id, i.identifier AS identifier,
               ready.ready_at AS ready_at,
               MIN(CASE WHEN ru.created_at >= ready.ready_at
                        THEN ru.created_at END) AS dispatched_at,
               MIN(CASE WHEN ru.created_at >= ready.ready_at
                        THEN ru.id END) AS run_id
        FROM (SELECT issue_id, MIN(observed_at) AS ready_at
              FROM issue_readiness_events
              WHERE initial = 0 AND observed_at >= ? AND observed_at < ?
              GROUP BY issue_id) AS ready
        JOIN issues i ON i.id = ready.issue_id
        LEFT JOIN runs ru ON ru.issue_id = ready.issue_id
        WHERE i.is_jira = 0
        GROUP BY ready.issue_id, ready.ready_at
        """, (_fmt_ts(win_from), _fmt_ts(now)),
    ).fetchall()
    pairs = []
    for row in rows:
        subject = _subject(row["issue_id"], row["identifier"],
                           ready_at=row["ready_at"], run_id=row["run_id"])
        if row["dispatched_at"]:
            pairs.append((subject, _seconds(row["ready_at"],
                                            row["dispatched_at"])))
            continue
        waited = _seconds(row["ready_at"], _fmt_ts(now))
        # A card that became ready less than the threshold ago is still inside
        # its objective: censoring it is honest, counting it as a failure is not.
        if waited is not None and waited > threshold:
            pairs.append((subject, waited))
        else:
            pairs.append((subject, None))
    return pairs


def _data_freshness(conn, win_from, now, threshold):
    cycles = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN sources_failed = 0 THEN 1 ELSE 0 END) AS clean "
        "FROM poll_cycles WHERE finished_at IS NOT NULL "
        "AND started_at >= ? AND started_at < ?",
        (_fmt_ts(win_from), _fmt_ts(now)),
    ).fetchone()
    fresh_at = conn.execute(
        "SELECT MAX(last_success_at) AS at FROM sync_state"
    ).fetchone()["at"]
    age = _seconds(fresh_at, _fmt_ts(now))
    subjects = []
    if fresh_at is None or age is None or age > threshold:
        subjects.append(_subject(None, None, last_success_at=fresh_at,
                                 data_age_seconds=None if age is None
                                 else round(age)))
    return int(cycles["clean"] or 0), int(cycles["total"] or 0), subjects, {
        "last_success_at": fresh_at,
        "data_age_seconds": None if age is None else round(age),
        "data_stale": bool(subjects),
    }


def _ci_green(conn, win_from, now):
    rows = conn.execute(
        """
        SELECT e.implementation_issue_id AS issue_id, i.identifier AS identifier,
               e.ci_status AS ci_status, e.reference AS integration_sha
        FROM lineage_stage_events e
        LEFT JOIN issues i ON i.id = e.implementation_issue_id
        WHERE e.stage = 'integration' AND e.ci_status IS NOT NULL
          AND e.observed_at >= ? AND e.observed_at < ?
        """, (_fmt_ts(win_from), _fmt_ts(now)),
    ).fetchall()
    numerator = 0
    subjects = []
    for row in rows:
        if row["ci_status"] == "success":
            numerator += 1
        else:
            subjects.append(_subject(row["issue_id"], row["identifier"],
                                     ci_status=row["ci_status"],
                                     integration_sha=row["integration_sha"]))
    return numerator, len(rows), subjects


def _release_gate(conn, win_from, now, threshold):
    rows = conn.execute(
        """
        SELECT q.implementation_issue_id AS issue_id, i.identifier AS identifier,
               MIN(q.verdict_at) AS passed_at, q.accepted_candidate AS candidate,
               e.observed_at AS integrated_at, e.outcome AS outcome
        FROM qa_lineage_events q
        JOIN issues i ON i.id = q.implementation_issue_id
        LEFT JOIN lineage_stage_events e
               ON e.implementation_issue_id = q.implementation_issue_id
              AND e.stage = 'integration'
        WHERE q.verdict = 'PASSED' AND q.verdict_at IS NOT NULL
          AND q.verdict_at >= ? AND q.verdict_at < ?
          AND i.integration_required = 1
        GROUP BY q.implementation_issue_id
        """, (_fmt_ts(win_from), _fmt_ts(now)),
    ).fetchall()
    pairs = []
    for row in rows:
        subject = _subject(row["issue_id"], row["identifier"],
                           passed_at=row["passed_at"],
                           candidate=row["candidate"],
                           integration_outcome=row["outcome"])
        if row["integrated_at"] and row["outcome"] in INTEGRATION_SUCCESS_OUTCOMES:
            pairs.append((subject, _seconds(row["passed_at"],
                                            row["integrated_at"])))
            continue
        blocked = _seconds(row["passed_at"], _fmt_ts(now))
        if blocked is not None and blocked > threshold:
            pairs.append((subject, blocked))
        else:
            pairs.append((subject, None))
    return pairs


def slo(conn: sqlite3.Connection, days: int = 30,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    """The /api/slo payload: objectives, error budgets and breach events."""
    if days not in WINDOW_DAYS:
        raise ValueError("days must be one of {}".format(
            ", ".join(str(d) for d in WINDOW_DAYS)))
    now = now or datetime.utcnow().replace(microsecond=0)
    win_from = now - timedelta(days=days)
    supported = _has_lineage_schema(conn)

    computed = {}
    if _table_exists(conn, "issue_readiness_events"):
        computed["pm_readiness_latency"] = _latency_slo(
            _pm_readiness(conn, win_from, now),
            SLO_DEFINITIONS[0]["threshold_seconds"]) + ({},)
        computed["dispatch_latency"] = _latency_slo(
            _dispatch_latency(conn, win_from, now,
                              SLO_DEFINITIONS[1]["threshold_seconds"]),
            SLO_DEFINITIONS[1]["threshold_seconds"]) + ({},)
    numerator, denominator, subjects, detail = _data_freshness(
        conn, win_from, now, SLO_DEFINITIONS[2]["threshold_seconds"])
    computed["production_data_freshness"] = (
        numerator, denominator, 0, subjects, detail)
    if supported:
        numerator, denominator, subjects = _ci_green(conn, win_from, now)
        computed["ci_green"] = (numerator, denominator, 0, subjects, {})
        computed["release_gate"] = _latency_slo(
            _release_gate(conn, win_from, now,
                          SLO_DEFINITIONS[4]["threshold_seconds"]),
            SLO_DEFINITIONS[4]["threshold_seconds"]) + ({},)

    slos = []
    alerts = []
    for definition in SLO_DEFINITIONS:
        result = computed.get(definition["id"])
        if result is None:
            numerator = denominator = excluded = 0
            subjects, detail = [], {}
        else:
            numerator, denominator, excluded, subjects, detail = result
        budget = _budget(numerator, denominator, definition["objective"])
        entry = {
            "id": definition["id"],
            "owner": definition["owner"],
            "sli": definition["sli"],
            "window_days": days,
            "objective": definition["objective"],
            "threshold_seconds": definition["threshold_seconds"],
            "numerator": numerator,
            "denominator": denominator,
            "excluded": excluded,
            "subjects": subjects[:MAX_SUBJECTS],
            "subjects_truncated": len(subjects) > MAX_SUBJECTS,
        }
        entry.update(budget)
        entry.update(detail)
        slos.append(entry)

        if detail.get("data_stale"):
            # Currently stale served data is a condition in its own right: it
            # must alert even in a window that recorded no poll cycle at all.
            alerts.append({
                "dedupe_key": "{}|{}d|stale_data".format(definition["id"], days),
                "slo": definition["id"],
                "severity": "breach",
                "owner": definition["owner"],
                "window_days": days,
                "objective": definition["objective"],
                "ratio": budget["ratio"],
                "budget_remaining": budget["budget_remaining"],
                "subjects": subjects[:MAX_SUBJECTS],
                "subjects_truncated": len(subjects) > MAX_SUBJECTS,
            })

        severity = None
        if budget["breached"]:
            severity = "breach"
        elif (budget["measured"] and budget["budget_remaining"] is not None
                and budget["budget_remaining"] <= 0.25):
            severity = "warning"
        if severity:
            alerts.append({
                # Stable identity of the condition: recomputing the same window
                # re-emits the same key, so the delivery path can deduplicate.
                "dedupe_key": "{}|{}d|{}".format(definition["id"], days,
                                                 severity),
                "slo": definition["id"],
                "severity": severity,
                "owner": definition["owner"],
                "window_days": days,
                "objective": definition["objective"],
                "ratio": budget["ratio"],
                "budget_remaining": budget["budget_remaining"],
                "subjects": subjects[:MAX_SUBJECTS],
                "subjects_truncated": len(subjects) > MAX_SUBJECTS,
            })

    return {
        "days": days,
        "now": _fmt_ts(now),
        "window_from": _fmt_ts(win_from),
        "schema_supported": supported,
        "slos": slos,
        "alerts": alerts,
    }


__all__ = ["trace", "slo", "validate_days", "SLO_DEFINITIONS"]
