# Flow metrics: cycle time, rework rate, idle-fleet share (FAN-3306)

The Flow Metrics panel (`/api/flow`, dashboard section «Метрики потока
конвейера») reports how the pipeline spends time, computed **only from
observed durable rows**. History that predates collection is reported through
coverage counters and start timestamps — never estimated, interpolated or
backdated.

## Collection (schema v6)

The poller records three kinds of durable flow rows; all writes are idempotent
and re-running any number of cycles produces no duplicates.

- **`issue_status_events`** — written by `store.upsert_issues` whenever a
  freshly synced status differs from the stored one. The first observation of
  an issue is a baseline (`initial=1`), not a transition; subsequent changes
  are real observed transitions (`initial=0`). Observation time is the sync
  time, so its error is bounded by the poll interval (45 s by default).
- **`issues.dispatch_lane / dispatch_ready / qa_verdict / qa_verdict_at /
  qa_candidate / qa_for_issue_id`** — mirrored from Multica issue metadata at
  ingest (`normalize.normalize_issue`). `qa_candidate` prefers the exact
  commit SHA (`qa_candidate_sha`, `candidate_sha`) and falls back to the
  artifact-revision string used by non-repository QA. Only the three terminal
  verdicts PASSED/FAILED/INCONCLUSIVE are accepted; anything else stays NULL.
- **`fleet_snapshots` / `fleet_snapshot_lanes`** — one capacity snapshot per
  poll cycle (`store.record_fleet_snapshot`), recorded **only** when the
  agents, issues and agent-tasks sources all synced cleanly that cycle: a
  degraded cycle leaves a truthful coverage gap instead of a fabricated
  capacity state. Eligibility and lane compatibility are resolved at snapshot
  time from the machine-readable agent profile
  (`dispatch_profile=...; role=...; native_lane=...; borrow_lanes=...`);
  agents without a parseable delivery profile (PM, dispatcher, RETIRED) are
  not part of the fleet. The old `done → next in_progress` approximation is
  deliberately not derivable and remains forbidden.

## Metric definitions and deterministic edge behavior

Windows are rolling 7/30/90-day UTC intervals anchored at request time.
Filters: `project` (repeatable), `lane` (repeatable; `unknown` matches issues
without a recorded lane — unknown/legacy lanes are reported explicitly, never
discarded).

### Cycle time

First trusted `in_progress` observation → **first** `done` observation.

- An `initial` in_progress baseline is trusted only when the issue was created
  after collection began (`issues.created_at >=` first recorded event);
  otherwise the card's completion is counted as `excluded_no_start`.
- Cancelled cards are excluded from percentile values and reported in the
  `cancelled` coverage count; a card that was done and later cancelled counts
  as cancelled.
- Open cards with a trusted start and no done are `open_censored` (excluded
  from percentiles, reported in coverage).
- A card reopened after done keeps its first done; the reopening does not
  change its cycle time.
- Median is the standard median (mean of the middle pair for even counts);
  p90 is nearest-rank (`ceil(0.9·n)`, 1-indexed). Grouped by project and
  dispatch lane.

### Rework rate

A **candidate** is one distinct (implementation issue, candidate SHA or
artifact revision) pair, resolved through `qa_for_issue_id`. QA retries of the
same candidate count once: a candidate is *reworked* when it received terminal
FAILED/INCONCLUSIVE verdicts and never a PASSED; a candidate that eventually
PASSED (e.g. after an INCONCLUSIVE re-run) is not reworked. The rate is
reworked ÷ candidates-with-a-terminal-verdict inside the window; weekly
buckets use the earliest `qa_verdict_at` (UTC Monday). Verdicts without
`qa_verdict_at` cannot be placed in any window and are reported as
`unwindowed` coverage — no synthetic timestamps.

### Idle-fleet share

Time-weighted share of observed time in which at least one eligible delivery
agent was idle while no lane-compatible `dispatch_ready` card existed
(`starved_idle > 0`) and the workspace was not paused.

- Consecutive snapshots ≤ 900 s apart form an observed interval carrying the
  earlier snapshot's state; wider gaps are coverage gaps (excluded from both
  numerator and denominator, reported as `gap_seconds` and via
  `coverage_pct`).
- Paused intervals are excluded from both numerator and denominator
  (`paused_seconds`). No workspace pause signal is observable today, so
  collected snapshots record `paused=0`; the column and the exclusion logic
  exist so a future pause signal keeps history honest.
- Agents are workspace-scoped: the project filter does not narrow this metric
  (`workspace_wide` / `project_filter_ignored` flags in the payload). The lane
  filter aggregates the per-lane snapshot rows (agents attributed to their
  native lane).
- A `dispatch_ready` card waits only while its status is todo/backlog; cards
  without a `dispatch_lane` are counted but match no agent.

## API

`GET /api/flow?days=7|30|90&project=<id>&lane=<lane>` on all three serving
surfaces (local FastAPI, hosted Flask, dependency-free cPanel WSGI). Any other
`days` value is a 422. The response carries every numerator/denominator and
the coverage block (`coverage.events_start`, `coverage.snapshots_start`);
missing history yields `null` values, never zeros. Read-only aggregates: no
issue titles, comment bodies, tenant secrets or prompt content are exposed —
only counts, durations, lane labels and project ids.

## Compatibility, migration, rollback

Schema v6 only **adds** tables and issue columns. `MIN_SERVABLE_SCHEMA_VERSION`
stays 5: an already-published v5 tenant snapshot keeps serving every existing
aggregate, and `/api/flow` truthfully reports `schema_supported: false` with
empty coverage instead of failing. The migration is `init_db` itself —
idempotent, in place, preserving data (`tests/test_flow_metrics.py::
test_migration_v5_to_v6_is_idempotent_and_preserves_data`). Rollback: the
additive tables are invisible to pre-v6 code; setting `PRAGMA user_version = 5`
(or restoring a pre-upgrade backup) returns a servable v5 database.

## Performance

Aggregation is single-pass SQL plus O(n) Python over the fetched rows — no
per-issue queries. The certifying bound (AC6) is
`tests/test_flow_metrics.py::test_100k_event_aggregation_under_one_second`:
a deterministic 100 000-event fixture must aggregate in < 1 s.
