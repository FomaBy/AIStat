# AIStat agent context

AIStat measures agent token usage and work efficiency. Keep this file as a
lightweight repository map; workspace estimation, dispatch, ownership, QA, Git,
and completion transactions live in the bound
`multica-workspace-governance` skill.

## Before changing code

- Work only the Multica issue explicitly assigned to the current agent. Re-read
  its acceptance criteria, dependencies, owner, active runs, and locked paths.
- Inspect the affected code and tests before loading broad documentation.
- Match the surrounding Python/TypeScript/operations patterns and preserve
  backward compatibility unless the issue explicitly changes a contract.

## Load context on demand

- Usage and efficiency semantics: `docs/metrics-efficiency.md`
- Per-user collection and privacy boundaries: `docs/per-user-collection.md`
- Runtime lifecycle and supervision: `docs/runtime-supervisor.md`
- Operator recovery: `docs/operations-runbook.md`
- Local deployment: `docs/deployment-local.md`
- Namecheap deployment: `docs/deployment-namecheap.md`
- Secret/token transfer: `docs/secure-token-handoff.md`

Read only the references needed by the assigned scope. Do not copy transient
quota state, incident details, worker IDs, or task-specific exceptions into this
file.

## Repository gotchas

- Usage/efficiency claims require observed data; do not invent provider
  denominators, reset times, savings, or performance gains.
- Treat tokens, credentials, tenant data, and per-user telemetry as sensitive.
  Keep secrets out of commands, logs, fixtures, commits, and screenshots.
- Preserve data compatibility and migration safety. A schema or deployment
  change needs the relevant negative/recovery check, not only a happy-path test.
- Keep generated environments, caches, local databases, and `.opencode`
  dependencies out of task-owned commits unless the issue explicitly owns them.

## Verification

Run the focused tests for the changed contract, then the smallest relevant
regression/security checks. Inspect the complete diff and report exact commands,
results, pushed SHA, residual risk, and cleanup. Never call an unexecuted check
PASS.
