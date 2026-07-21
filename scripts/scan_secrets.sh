#!/usr/bin/env bash
# FAN-1185: prove AIStat logs and shippable artifacts carry no secret material.
#
# The app is designed never to log a token, password or Authorization header
# (sync/poller errors pass through aistat.handoff.safe_sync_error's finite safe
# vocabulary, and a per-user PAT reaches the official CLI only via stdin — never
# argv, env, a log line or an exception). This script is the reusable check that
# proves it stayed that way: run it after a deploy or on a schedule.
#
# Scans, for concrete secret VALUE signatures:
#   * runtime log files (data/*.log locally and in the runtime root),
#   * the built cPanel package (dist/),
#   * every git-tracked file (docs and this script excluded).
#
# Exit 0 when clean, 1 when a potential secret is found, 2 on a usage error.
#
# Usage: scripts/scan_secrets.sh [extra-log-dir ...]
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 2

# Secret VALUE shapes that must never appear in a log or a shippable artifact.
VALUE_PATTERNS='(-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer [A-Za-z0-9._~+/=-]{20,}|Authorization: *(Bearer|Basic) [A-Za-z0-9]|ghp_[0-9A-Za-z]{20,}|github_pat_[0-9A-Za-z_]{20,}|xox[baprs]-[0-9A-Za-z-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})'
# Logs additionally must not carry a secret-ish key assigned a real value.
LOG_ASSIGN='(password|passwd|api[_-]?key|access[_-]?token|session[_-]?secret|ingest[_-]?secret|worker[_-]?secret)["'"'"' ]*[:=]["'"'"' ]*[^"'"'"' 	]{8,}'

LOG_DIRS=("$@")
if [ "${#LOG_DIRS[@]}" -eq 0 ]; then
  LOG_DIRS=("$ROOT/data")
  RUNTIME_LOGS="$HOME/Library/Application Support/AIStat/data"
  [ -d "$RUNTIME_LOGS" ] && LOG_DIRS+=("$RUNTIME_LOGS")
fi

matches="$(mktemp)"
trap 'rm -f "$matches"' EXIT

scan_logs() {
  local d
  for d in "${LOG_DIRS[@]}"; do
    [ -d "$d" ] || continue
    find "$d" -type f -name '*.log' -print0 2>/dev/null \
      | xargs -0 -r grep -aEnH "$VALUE_PATTERNS|$LOG_ASSIGN" 2>/dev/null
  done
}

scan_dist() {
  [ -d "$ROOT/dist" ] && grep -rIEnH "$VALUE_PATTERNS" "$ROOT/dist" 2>/dev/null
  return 0
}

scan_tracked() {
  local f
  while IFS= read -r -d '' f; do
    case "$f" in
      scripts/scan_secrets.sh|docs/*) continue ;;
    esac
    grep -IEnH "$VALUE_PATTERNS" "$f" 2>/dev/null
  done < <(git ls-files -z 2>/dev/null)
  return 0
}

{ scan_logs; scan_dist; scan_tracked; } >"$matches" 2>/dev/null

if [ -s "$matches" ]; then
  echo "FAIL: potential secret material found (file:line — value redacted):" >&2
  # Print only the location; never re-echo the matched value.
  awk -F: '{ print "  " $1 ":" $2 }' "$matches" | sort -u >&2
  exit 1
fi

echo "OK: no secret patterns in logs or shippable artifacts."
exit 0
