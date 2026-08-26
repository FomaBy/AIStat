# Run Attribution and Frontier Metrics Design

## Goal

Persist observed run provenance and QA lineage without relabelling history, then
expose reproducible delivery metrics through the existing flow endpoint and
dashboard.

## Constraints

- SQLite schema changes are additive and `init_db` remains idempotent.
- Existing `runs`, `issues`, and raw status events are not updated by the
  migration or backfill.
- A provenance value is stored only when Multica supplied it in issue/run
  metadata. Missing values are `unknown`; rows predating collection are
  `legacy_unknown`.
- No prompt contents, comments, credentials, or tenant-identifying data leave
  the local database through the API.
- The existing `/api/flow?days=7|30|90&project=&lane=` contract remains
  compatible; new fields are additive.

## Data model

Schema v8 adds three append-only tables.

`run_attribution_events` has one row per newly observed run id. It records the
attribution schema version, provenance state, model/runtime/prompt/skills/
harness/governance-bundle revisions, and the linked implementation issue. The
collector reads the revision fields from the run payload first and then from
the matching normalized issue metadata. It never fills a missing revision from
the current agent or runtime catalogue.

`issue_readiness_events` records each observed false→true
`dispatch_ready` transition. It also records one `initial=1` baseline for a
card that was already ready when v8 collection began. Initial baselines are
explicitly legacy/unknown and are excluded from elapsed-time metrics; a later
observed transition creates a trusted event.

`qa_lineage_events` has one row per QA issue after a terminal, well-formed
verdict is observed. It records the implementation issue, immutable candidate,
verdict, reported verdict time, observation time, and for `PASSED` the accepted
candidate and the implementation story points observed at that time. The
primary key makes repeat polls idempotent. An existing terminal QA row first
seen during v8 migration is marked `initial=1` and excluded from period metrics;
a malformed or incomplete QA record does not generate an event.

The normalized issue row carries the attribution metadata needed by the store:
`attribution_schema_version`, `model_revision`, `runtime_revision`,
`prompt_revision`, `skills_revision`, `harness_revision`, and
`governance_bundle_revision`. `run_attribution_events.provenance_state` is
`observed` only with schema version and all six revisions; it is `unknown` for
new incomplete observations and `legacy_unknown` for rows that existed before
v8. This preserves the distinction between absence before collection and an
incomplete new attribution.

## Collection and lineage

At migration, one `legacy_unknown` attribution event is created for every
already-stored raw run, with no invented revision values. This backfill is
`INSERT OR IGNORE`, so it is repeatable and never changes a raw row. Existing
ready cards receive an `initial=1` readiness baseline for the same reason.

`store.upsert_runs` inserts a first-seen run attribution alongside the existing
run upsert. Its value is frozen by `INSERT OR IGNORE`; later catalog changes or
metadata refreshes cannot rewrite it. `store.upsert_issues` inserts a QA
lineage event when its terminal QA metadata is complete and records a rising
readiness transition. For an acceptance, it looks up the implementation issue's
current story points in the same transaction and snapshots the value into the
event.

Because both tables are observed-event logs, attempts are the number of
distinct `run_attribution_events` for an implementation issue, rework is the
number of distinct candidates with a failed/inconclusive terminal outcome,
and accepted candidates/SP are read from `PASSED` lineage events. No metric
queries the mutable current QA fields to reconstruct those values.

## Metrics and API

`flow_metrics.flow` adds a `frontier` block and a `lineage` block.

- **Ready frontier**: current dispatch-ready, non-Jira `todo`/`backlog` cards,
  grouped only through the existing project/lane filters. It returns its count
  and story-point denominator.
- **PM p95**: nearest-rank p95 of observed time from issue creation to its
  first `dispatch_ready` observation. Only cards whose readiness event falls
  in the requested window and whose creation time is valid are measured;
  `measured` is the denominator.
- **Waiting age**: duration from a ready card's first readiness observation to
  `now`. It returns the median and p95 plus the number of currently waiting
  cards, never a synthetic age for a pre-collection card.
- **First-pass QA**: per implementation issue, the earliest terminal lineage
  event is the first QA attempt. A first pass is a `PASSED` outcome. The
  response exposes `passed`, `denominator`, and the rate, with events placed
  in the requested period by terminal verdict time (or left in `unwindowed` if
  no valid reported time exists).

The lineage block also returns attempt count, rework count, accepted candidate
count, accepted story points, and unknown/legacy attribution denominators.
These are all explicit counts; an unavailable ratio is `null`, never zero.

## UI

The existing Flow Metrics panel gets four additive cards: ready frontier, PM
p95, waiting age, and first-pass QA. Their subtitles show the relevant
numerator/denominator or observation count. The existing coverage sentence is
extended with the legacy/unknown provenance count so the dashboard makes the
limit visible rather than silently treating it as complete data.

## Error handling and compatibility

V5–V7 snapshots remain servable. They return the new blocks with null ratios
and zero denominators, `schema_supported` continues to describe the older flow
schema, and `attribution_schema_supported` is false. V8 migrations are
additive. A rerun has no effect, and it does not modify raw run/status rows.

## Verification

Focused tests cover normalization, frozen run attribution, terminal QA event
idempotence, v7→v8 migration preserving raw rows, all metric numerators and
unknown cases, API response shape, and browser rendering of the new empty and
populated card states. The full pytest suite remains the certifying regression
check.
