# End-to-end lineage and pipeline SLOs (FAN-3460)

Two read-only views over the rows the poller already collects: `/api/lineage`
reconstructs one delivery chain from a correlation id, and `/api/slo` reports
the pipeline's objectives, error budgets and dedupe-ready breach events. Both
follow the flow-metrics truthfulness contract — every value comes from an
observed row, nothing is interpolated, backdated or inferred from a sibling
card, and no external tracing SaaS is involved.

## Correlation id

The correlation id is the **implementation issue id**. Every other node in the
chain names it, so no stage is joined on a guessed key:

| Stage | Source | Join |
| --- | --- | --- |
| issue | `issues` | the correlation id itself |
| run | `runs`, `run_attribution_events` | `runs.issue_id` |
| candidate | `issues.candidate_sha`, `qa_lineage_events.candidate` | mirrored / reviewed |
| QA | `qa_lineage_events` | `implementation_issue_id` |
| integration | `lineage_stage_events` (`stage='integration'`) | `implementation_issue_id` |
| release | `lineage_stage_events` (`stage='release'`) | `implementation_issue_id` |

`GET /api/lineage?trace=<id>` accepts the implementation issue id, its human
identifier (`FAN-3460`, case-insensitive), or the id of a QA/DevOps child — a
child resolves to the same chain through `qa_for_issue_id` and the response
reports `requested_issue_id` and `requested_is_root` so the redirection is
visible rather than implicit.

The payload exposes identifiers, SHAs, release versions and run ids for
drill-down. Issue titles, comment bodies, prompt content and tenant secrets stay
out of it, exactly as in `/api/flow`.

### Stage states

- `observed` — an immutable event or a mirrored value exists.
- `missing` — the pipeline declared the stage expected and nothing was observed:
  a card that names `qa_issue_id` with no terminal QA event, or a QA-accepted
  candidate with `post_qa_integration_required` and no integration event.
- `stale` — the current mirrored metadata diverges from the immutable first
  observation. Both values are returned (`integration_sha` vs
  `mirrored_integration_sha`, `release_version` vs `mirrored_release_version`);
  neither is preferred and the divergence is not reconciled.
- `not_expected` — the gate is legitimately closed (QA never accepted a
  candidate, or the card never required integration). A failed QA card therefore
  does **not** report a missing integration.

`gaps` lists every stage in `missing`/`stale`, and `complete` is true only when
that list is empty.

## Collection (schema v10)

Schema v10 is additive: it adds `lineage_stage_events` plus mirrored issue
columns (`candidate_sha`, `qa_issue_id`, `integration_required`,
`integration_issue_id`, `integration_outcome`, `integration_sha`,
`integration_ci_status`, `release_version`). `normalize.normalize_issue` fills
them from the observed pipeline metadata vocabulary in precedence order
(`integration_result_sha` → `integration_sha` → `integrated_target_sha` →
`final_integrated_candidate_sha`, and so on); when every key is absent the
column stays NULL. Only the terminal outcomes `INTEGRATED/PASSED/FAILED/BLOCKED`
and the known CI conclusions are accepted — an unrecognised word is "not
observed", never guessed into a bucket.

`store.upsert_issues` writes at most one `lineage_stage_events` row per
(implementation issue, stage) with `INSERT OR IGNORE`, so repeated cycles are
idempotent and a later metadata rewrite cannot restate closed history: it
surfaces as a `stale` link instead. A stage whose outcome was already present
the first time the card was seen is a collection baseline (`initial = 1`).

`MIN_SERVABLE_SCHEMA_VERSION` stays 5. An older admitted snapshot keeps serving
every existing aggregate; `/api/lineage` and `/api/slo` report
`schema_supported: false` with unmeasured objectives instead of failing. The
migration is `init_db` itself — idempotent, in place, raw-row preserving.

## SLOs

`GET /api/slo?days=7|30|90` (any other value is a 422). Every objective states
its window, numerator, denominator, threshold, owner and error budget, and lists
the exact subjects behind a breach (up to 20, with `subjects_truncated`).

| SLO | Owner | SLI | Threshold | Objective |
| --- | --- | --- | --- | --- |
| `pm_readiness_latency` | pm | issue created → first observed `dispatch_ready` | 48 h | 0.90 |
| `dispatch_latency` | dispatcher | first observed `dispatch_ready` → first run created | 1 h | 0.90 |
| `production_data_freshness` | ops | poll cycles finished with every source healthy | 900 s data age | 0.99 |
| `ci_green` | devops | observed integration CI conclusions that are `success` | — | 0.90 |
| `release_gate` | devops | QA PASSED → post-QA integration observed | 48 h | 0.90 |

Denominators only ever contain measurable subjects:

- readiness baselines (`initial = 1`) and unparseable/negative timestamps are
  excluded and counted in `excluded`, never estimated;
- a card that became ready or passed QA *less than* its threshold ago and has no
  run/integration yet is censored, not counted as a failure — once it passes the
  threshold it becomes a breaching subject with its elapsed time;
- an empty window is `measured: false` with `ratio: null`, not a 0 % breach.

Error budget: `allowed_failures = (1 - objective) × denominator` and
`budget_remaining = 1 - failures / allowed_failures`, clamped to `[0, 1]`.

### Alert events

A breach (or a budget below 25 %) produces one event per objective:

```json
{"dedupe_key": "ci_green|30d|breach", "slo": "ci_green", "severity": "breach",
 "owner": "devops", "window_days": 30, "objective": 0.9, "ratio": 0.5,
 "budget_remaining": 0.0,
 "subjects": [{"issue_id": "...", "identifier": "FAN-4001",
               "ci_status": "failure", "integration_sha": "2222..."}]}
```

The `dedupe_key` is the stable identity of the condition: recomputing the same
window re-emits the same key, so the delivery path deduplicates instead of
re-notifying. Currently stale served data emits its own
`production_data_freshness|<days>d|stale_data` event even in a window that
recorded no poll cycle at all — an unmeasured ratio must not hide a stale
snapshot. **Delivering** these events (owner notification, escalation) belongs
to the control-plane alert path; AIStat only produces them.

## Dashboard

The «SLO конвейера и сквозной след» panel renders the objective table (owner,
threshold, objective, actual, numerator/denominator, error budget; breached rows
highlighted), the breach events with their dedupe keys and subjects, and an
on-demand trace lookup that prints the six stages with their state and exact
FAN/SHA/run ids. The panel hides itself on a deployment whose host does not
serve the endpoints, rather than rendering an invented state.

## Tests

`tests/test_lineage.py` drives five replay fixtures through the real ingest path
(`normalize.normalize_issue` + `store.upsert_issues`, one poll cycle at a time):
`replay_success`, `replay_rework`, `replay_failed_qa`, `replay_stale_deploy` and
`replay_missing_lineage`, plus the SLO numerators/denominators, censoring,
dedupe-key stability, the legacy-snapshot path and the API surface.
`tests/test_dashboard_browser.py::test_slo_panel_and_lineage_drilldown_render`
covers the panel and the drill-down in a real browser.
