#!/usr/bin/env bash
# Long-running local Multica sync + signed public-host publisher.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${AISTAT_ENV_FILE:-$HOME/.config/aistat/production.env}"
if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

export AISTAT_PUBLISH_URL="${AISTAT_PUBLISH_URL:-https://aistat.app/api/ingest/snapshot}"
export AISTAT_PUBLISH_INTERVAL_SECONDS="${AISTAT_PUBLISH_INTERVAL_SECONDS:-300}"

if [ -z "${AISTAT_INGEST_SECRET:-}" ]; then
  KEYCHAIN_ACCOUNT="${AISTAT_KEYCHAIN_ACCOUNT:-$USER}"
  KEYCHAIN_SERVICE="${AISTAT_KEYCHAIN_SERVICE:-aistat.app ingest}"
  if ! command -v security >/dev/null 2>&1; then
    echo "AISTAT_INGEST_SECRET is unset and macOS Keychain is unavailable" >&2
    exit 1
  fi
  AISTAT_INGEST_SECRET="$(
    security find-generic-password \
      -a "$KEYCHAIN_ACCOUNT" \
      -s "$KEYCHAIN_SERVICE" \
      -w
  )"
  export AISTAT_INGEST_SECRET
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

REQUIREMENTS_STAMP="$(cksum requirements.txt)"
if [ ! -f .venv/.requirements.cksum ] || \
   [ "$(cat .venv/.requirements.cksum)" != "$REQUIREMENTS_STAMP" ]; then
  .venv/bin/pip install -q -r requirements.txt
  printf '%s\n' "$REQUIREMENTS_STAMP" >.venv/.requirements.cksum
fi

mkdir -p data
.venv/bin/python -m aistat.poller >>data/poller.log 2>&1 &
POLLER_PID=$!
cleanup() {
  kill "$POLLER_PID" 2>/dev/null || true
}
trap cleanup EXIT

.venv/bin/python -m aistat.publish --watch
