#!/usr/bin/env bash
# Long-running local Multica sync + signed public-host publisher.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${AISTAT_ENV_FILE:-$HOME/.config/aistat/production.env}"
if [ ! -r "$ENV_FILE" ]; then
  echo "AIStat production env is missing or unreadable: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

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
