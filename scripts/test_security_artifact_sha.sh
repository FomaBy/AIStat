#!/usr/bin/env bash
# FAN-3513: every candidate-addressable security artifact must be keyed by
# the exact PR head SHA on pull_request runs and by github.sha on push runs
# (the gitleaks artifact is the reference behavior). This is a static check
# of the workflow expression itself, run outside any GitHub Actions context.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow="$repo_root/.github/workflows/security.yml"

expected='${{ github.event.pull_request.head.sha || github.sha }}'

fail() {
  printf '%s\n' "security artifact SHA self-test failed: $1" >&2
  exit 1
}

[ -f "$workflow" ] || fail "workflow file not found: $workflow"

check_artifact() {
  local prefix="$1"
  local line
  line="$(grep -E "^ +name: ${prefix}-" "$workflow" || true)"
  [ -n "$line" ] || fail "no artifact name found for prefix '$prefix' (missing source SHA binding)"
  [ "$(printf '%s\n' "$line" | wc -l)" -eq 1 ] || fail "expected exactly one '$prefix' artifact, found several"
  case "$line" in
    *"$expected") ;;
    *) fail "'$prefix' artifact does not end in the exact head/push SHA expression: $line" ;;
  esac
}

for prefix in pip-audit bandit sbom-cyclonedx gitleaks-redacted; do
  check_artifact "$prefix"
done

# Regression guard: a bare github.sha (no PR head fallback) on any artifact
# name line is exactly the FAN-3499 defect — the merge SHA leaks in instead
# of the source candidate SHA on pull_request runs. Fail closed on it.
if grep -E "^ +name: (pip-audit|bandit|sbom-cyclonedx|gitleaks-redacted)-\\\$\{\{ github\.sha \}\}" "$workflow"; then
  fail "an artifact is keyed by bare github.sha with no PR head SHA fallback"
fi

printf '%s\n' "security artifact SHA self-test: pass"
