# Run Attribution and Frontier Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Store observed versioned run provenance and immutable QA lineage, then expose truthful frontier and first-pass-QA metrics.

**Architecture:** Schema v8 adds append-only run_attribution_events, issue_readiness_events, and qa_lineage_events; normalized issue metadata supplies provenance without fabricating old values. The existing /api/flow aggregate adds frontier/lineage blocks and the dashboard renders four additive cards.

**Tech Stack:** Python 3.9, SQLite, FastAPI, browser JavaScript, pytest.

**Spec:** docs/superpowers/specs/2026-08-26-run-attribution-flow-metrics-design.md

## Global Constraints

- Use only observed Multica run/issue metadata; never copy current catalog values into missing provenance.
- Keep schema changes additive and raw runs / issue_status_events rows unchanged by migration.
- Preserve /api/flow query parameters and existing response fields.
- Return null for unavailable ratios and expose every displayed numerator/denominator.
- Do not expose prompt contents, comments, credentials, or tenant data.

---

### Task 1: Add schema-v8 attribution columns and event tables

**Files:**

- Modify: aistat/db.py:SCHEMA_VERSION, SCHEMA, _ADDED_COLUMNS, init_db
- Modify: aistat/normalize.py:normalize_issue
- Test: tests/test_normalize.py
- Test: tests/test_flow_metrics.py

**Interfaces:**

- Produces SCHEMA_VERSION == 8.
- Produces normalized issue keys attribution_schema_version, model_revision, runtime_revision, prompt_revision, skills_revision, harness_revision, and governance_bundle_revision.
- Creates append-only run_attribution_events, issue_readiness_events, and qa_lineage_events.

- [x] **Step 1: Write the failing normalization test**

~~~python
def test_normalize_issue_keeps_attribution_revisions():
    row = normalize.normalize_issue({
        "id": "i1", "updated_at": "2026-08-26T00:00:00Z",
        "metadata": {
            "attribution_schema_version": 1,
            "model_revision": "model@v1", "runtime_revision": "runtime@v1",
            "prompt_revision": "prompt@v1", "skills_revision": "skills@v1",
            "harness_revision": "harness@v1",
            "governance_bundle_revision": "bundle@v1",
        },
    })
    assert row["attribution_schema_version"] == 1
    assert row["harness_revision"] == "harness@v1"
~~~

- [x] **Step 2: Write the failing migration test**

~~~python
def test_v7_to_v8_migration_is_idempotent_and_keeps_raw_runs(tmp_path):
    conn = connect(tmp_path / "v7.db")
    init_db(conn)
    conn.execute("INSERT INTO runs (id, synced_at) VALUES ('r1', 'old')")
    before = [tuple(row) for row in conn.execute("SELECT * FROM runs")]
    conn.execute("PRAGMA user_version = 7")
    init_db(conn)
    init_db(conn)
    assert [tuple(row) for row in conn.execute("SELECT * FROM runs")] == before
    assert conn.execute("SELECT COUNT(*) FROM run_attribution_events").fetchone()[0] == 0
~~~

- [x] **Step 3: Run the new tests and verify RED**

Run: python3 -m pytest tests/test_normalize.py -q -k attribution tests/test_flow_metrics.py -q -k v7_to_v8

Expected: FAIL because the revision keys and v8 tables do not exist.

- [x] **Step 4: Implement the additive schema and strict extraction**

~~~python
SCHEMA_VERSION = 8

CREATE TABLE IF NOT EXISTS run_attribution_events (
    run_id TEXT PRIMARY KEY, issue_id TEXT,
    attribution_schema_version INTEGER, provenance_state TEXT NOT NULL,
    model_revision TEXT, runtime_revision TEXT, prompt_revision TEXT,
    skills_revision TEXT, harness_revision TEXT, governance_bundle_revision TEXT,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_lineage_events (
    qa_issue_id TEXT PRIMARY KEY, implementation_issue_id TEXT NOT NULL,
    candidate TEXT NOT NULL, verdict TEXT NOT NULL, verdict_at TEXT,
    observed_at TEXT NOT NULL, initial INTEGER NOT NULL DEFAULT 0, accepted_candidate TEXT,
    accepted_story_points REAL
);
CREATE TABLE IF NOT EXISTS issue_readiness_events (
    issue_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    initial INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (issue_id, observed_at)
);
~~~

Add a helper that accepts only integer schema versions. Reuse _meta_str for non-empty revision strings.

- [x] **Step 5: Run the new tests and verify GREEN**

Run: python3 -m pytest tests/test_normalize.py -q -k attribution tests/test_flow_metrics.py -q -k v7_to_v8

Expected: PASS.

- [x] **Step 6: Commit**

~~~bash
git add aistat/db.py aistat/normalize.py tests/test_normalize.py tests/test_flow_metrics.py
git commit -m "feat: add observed run attribution schema"
~~~

### Task 2: Persist frozen run provenance and terminal QA lineage

**Files:**

- Modify: aistat/store.py:upsert_issues, upsert_runs
- Test: tests/test_flow_metrics.py
- Test: tests/test_poller.py

**Interfaces:**

- store.upsert_runs inserts one frozen run_attribution_events row per new run.
- store.upsert_issues inserts one terminal qa_lineage_events row per QA issue.
- A complete set of seven attribution values yields observed; a post-v8 incomplete row yields unknown; migration records pre-v8 runs as legacy_unknown without changing raw rows.

- [x] **Step 1: Write the failing frozen-attribution test**

~~~python
def test_run_attribution_is_frozen_after_first_observation(conn):
    store.upsert_issues(conn, [complete_issue("i1", prompt="prompt@v1")], synced_at=ts())
    store.upsert_runs(conn, [{"id": "r1", "issue_id": "i1"}], synced_at=ts())
    store.upsert_issues(conn, [complete_issue("i1", prompt="prompt@v2")], synced_at=ts())
    store.upsert_runs(conn, [{"id": "r1", "issue_id": "i1"}], synced_at=ts())
    saved = conn.execute(
        "SELECT prompt_revision, provenance_state FROM run_attribution_events"
    ).fetchone()
    assert tuple(saved) == ("prompt@v1", "observed")
~~~

- [x] **Step 2: Write the failing terminal-QA test**

~~~python
def test_terminal_qa_event_snapshots_candidate_and_accepted_sp_once(conn):
    add_issue(conn, "impl", story_points=5)
    row = complete_issue("qa", qa_for_issue_id="impl", qa_candidate="sha-a",
                         qa_verdict="PASSED", qa_verdict_at=ts(days=1))
    store.upsert_issues(conn, [row], synced_at=ts())
    row["qa_candidate"] = "sha-b"
    store.upsert_issues(conn, [row], synced_at=ts())
    saved = conn.execute(
        "SELECT candidate, accepted_candidate, accepted_story_points "
        "FROM qa_lineage_events"
    ).fetchone()
    assert tuple(saved) == ("sha-a", "sha-a", 5.0)
~~~

- [x] **Step 3: Write the failing readiness-transition test**

~~~python
def test_ready_transition_is_append_only_and_initial_rows_are_marked(conn):
    store.upsert_issues(conn, [{"id": "i1", "status": "todo", "dispatch_ready": 0}], synced_at=ts(days=2))
    store.upsert_issues(conn, [{"id": "i1", "status": "todo", "dispatch_ready": 1}], synced_at=ts(days=1))
    store.upsert_issues(conn, [{"id": "i1", "status": "todo", "dispatch_ready": 1}], synced_at=ts())
    rows = conn.execute("SELECT observed_at, initial FROM issue_readiness_events").fetchall()
    assert [tuple(row) for row in rows] == [(ts(days=1), 0)]
~~~

- [x] **Step 4: Run the new tests and verify RED**

Run: python3 -m pytest tests/test_flow_metrics.py -q -k 'frozen or terminal_qa or ready_transition' tests/test_poller.py -q -k attribution

Expected: FAIL because event rows are not written.

- [x] **Step 5: Implement first-observation inserts**

~~~python
conn.execute(
    "INSERT OR IGNORE INTO run_attribution_events "
    "(run_id, issue_id, attribution_schema_version, provenance_state, observed_at) "
    "VALUES (?, ?, ?, ?, ?)",
    (row["id"], row.get("issue_id"), version, state, synced_at),
)
~~~

Populate all revision columns in the same statement. In upsert_issues, insert a readiness event only for a false→true observed transition, and insert QA lineage only with qa_for_issue_id, qa_candidate, and a validated terminal verdict. Initial rows found at migration are marked initial=1. Read implementation story_points in the transaction and set accepted fields only for PASSED.

- [x] **Step 6: Run the new tests and verify GREEN**

Run: python3 -m pytest tests/test_flow_metrics.py -q -k 'frozen or terminal_qa or ready_transition' tests/test_poller.py -q -k attribution

Expected: PASS.

- [x] **Step 7: Commit**

~~~bash
git add aistat/store.py tests/test_flow_metrics.py tests/test_poller.py
git commit -m "feat: persist immutable run and QA lineage"
~~~

### Task 3: Aggregate frontier and lineage metrics through /api/flow

**Files:**

- Modify: aistat/flow_metrics.py:flow
- Test: tests/test_flow_metrics.py
- Test: tests/test_api.py:test_flow_endpoint_shape_and_validation

**Interfaces:**

- Produces flow(conn, days=7, now=NOW)[frontier] with ready, ready_story_points, pm_p95_seconds, pm_measured, waiting_median_seconds, waiting_p95_seconds, and waiting.
- Produces flow(conn, days=7, now=NOW)[lineage] with attempts, rework, accepted_candidates, accepted_story_points, first_pass_rate, first_passed, first_pass_denominator, unwindowed, unknown, and legacy_unknown.

- [x] **Step 1: Write the failing frontier test**

~~~python
def test_frontier_reports_ready_pm_p95_and_waiting_age(conn):
    add_issue(conn, "r1", status="todo", dispatch_ready=1, created_at=ts(days=3))
    add_issue(conn, "r2", status="backlog", dispatch_ready=1, created_at=ts(days=2))
    add_ready_event(conn, "r1", ts(days=2))
    add_ready_event(conn, "r2", ts(days=1))
    out = flow_metrics.flow(conn, days=7, now=NOW)["frontier"]
    assert out["ready"] == 2
    assert out["pm_measured"] == 2
    assert out["waiting"] == 2
~~~

- [x] **Step 2: Write the failing first-pass test**

~~~python
def test_first_pass_qa_uses_earliest_terminal_lineage_event(conn):
    add_lineage(conn, "qa-1", "impl", "sha-a", "FAILED", ts(days=3))
    add_lineage(conn, "qa-2", "impl", "sha-b", "PASSED", ts(days=2))
    out = flow_metrics.flow(conn, days=7, now=NOW)["lineage"]
    assert out["first_passed"] == 0
    assert out["first_pass_denominator"] == 1
    assert out["first_pass_rate"] == 0.0
~~~

- [x] **Step 3: Run the new tests and verify RED**

Run: python3 -m pytest tests/test_flow_metrics.py -q -k 'frontier or first_pass' tests/test_api.py -q -k flow

Expected: FAIL because /api/flow has no new metric blocks.

- [x] **Step 4: Implement read-only observed-event aggregates**

~~~python
first_event = min(events, key=lambda event: (event["event_at"], event["qa_issue_id"]))
first_passed += int(first_event["verdict"] == "PASSED")
~~~

Compute PM p95 from issues.created_at to a non-initial readiness event; compute waiting ages only from current ready cards with a non-initial recorded readiness event. Filter all issue-scoped values by existing project/lane rules. Return None rather than zero for absent percentiles or ratios.

- [x] **Step 5: Run the new tests and verify GREEN**

Run: python3 -m pytest tests/test_flow_metrics.py -q -k 'frontier or first_pass' tests/test_api.py -q -k flow

Expected: PASS.

- [x] **Step 6: Commit**

~~~bash
git add aistat/flow_metrics.py tests/test_flow_metrics.py tests/test_api.py
git commit -m "feat: expose frontier and first-pass QA metrics"
~~~

### Task 4: Render the metrics, document the contract, and certify

**Files:**

- Modify: aistat/static/index.html:flow-panel
- Modify: aistat/static/app.js:renderFlow
- Modify: aistat/static/i18n.js:TRANSLATIONS
- Modify: docs/flow-metrics.md
- Test: tests/test_api.py:test_dashboard_flow_panel_static_contract
- Test: tests/test_dashboard_browser.py:test_flow_metrics_panel_renders_truthful_empty_state

**Interfaces:**

- Consumes additive frontier and lineage blocks.
- Produces cards card-flow-ready, card-flow-pm-p95, card-flow-waiting, and card-flow-first-pass; missing values render as —.

- [x] **Step 1: Write failing static/browser assertions**

~~~python
for card in ("card-flow-ready", "card-flow-pm-p95",
             "card-flow-waiting", "card-flow-first-pass"):
    assert f'id="{card}"' in index_html

assert cdp.eval(
    'document.getElementById("card-flow-first-pass").textContent'
) == "—"
~~~

- [x] **Step 2: Run the new tests and verify RED**

Run: python3 -m pytest tests/test_api.py -q -k dashboard_flow tests/test_dashboard_browser.py -q -k flow_metrics

Expected: FAIL because the new cards are absent.

- [x] **Step 3: Add cards, translations, and coverage copy**

~~~javascript
$("card-flow-first-pass").textContent = fmtShare(lineage.first_pass_rate);
$("card-flow-first-pass-sub").textContent = t("flowFirstPassSub", {
  passed: lineage.first_passed,
  denominator: lineage.first_pass_denominator,
});
~~~

Use existing fmtDuration and fmtShare. Append unknown and legacy attribution counts to coverage. Document each v8 event table, metric numerator/denominator, legacy behaviour, and additive migration in docs/flow-metrics.md.

- [x] **Step 4: Run focused checks and verify GREEN**

Run: python3 -m pytest tests/test_normalize.py tests/test_poller.py tests/test_flow_metrics.py tests/test_api.py tests/test_dashboard_browser.py -q

Expected: PASS.

- [x] **Step 5: Run the certifying regression**

Run: python3 -m pytest -q

Expected: PASS.

- [x] **Step 6: Inspect, commit, and push**

~~~bash
git diff --check
git status --short
git add aistat/static/index.html aistat/static/app.js aistat/static/i18n.js docs/flow-metrics.md docs/superpowers/specs/2026-08-26-run-attribution-flow-metrics-design.md docs/superpowers/plans/2026-08-26-run-attribution-flow-metrics.md
git commit -m "feat: add run attribution frontier metrics"
git push -u origin codex/fan-3454-run-attribution
~~~
