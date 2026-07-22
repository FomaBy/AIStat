#!/usr/bin/env bash
# Single-command launch: Multica poller + API + dashboard on one port.
#   ./run.sh                 → http://localhost:8787
#   AISTAT_PORT=9000 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

# Load a private, gitignored .env if present so the poller can authenticate with
# a durable Multica PAT (AISTAT_MULTICA_TOKEN + MULTICA_WORKSPACE_ID) instead of
# an interactive `multica login` session in ~/.multica that silently expires and
# freezes collection (FAN-1442). The file is never committed; see .env.example.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PORT="${AISTAT_PORT:-8787}"
VENV=".venv"

if [ -n "${AISTAT_PUBLISH_URL:-}" ] && [ -z "${AISTAT_TENANT_ID:-}" ]; then
  echo "AISTAT_TENANT_ID is required when snapshot publishing is enabled" >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> первый запуск: создаю venv и ставлю зависимости"
  python3 -m venv "$VENV"
fi

REQUIREMENTS_STAMP="$(cksum requirements.txt)"
if [ ! -f "$VENV/.requirements.cksum" ] || \
   [ "$(cat "$VENV/.requirements.cksum")" != "$REQUIREMENTS_STAMP" ]; then
  echo "==> обновляю зависимости"
  "$VENV/bin/pip" install -q -r requirements.txt
  printf '%s\n' "$REQUIREMENTS_STAMP" >"$VENV/.requirements.cksum"
fi

mkdir -p data
echo "==> поллер Multica запущен в фоне (лог: data/poller.log)"
"$VENV/bin/python" -m aistat.poller >>data/poller.log 2>&1 &
POLLER_PID=$!
PUBLISHER_PID=""

if [ -n "${AISTAT_PUBLISH_URL:-}" ]; then
  echo "==> защищённая публикация на хостинг запущена (лог: data/publisher.log)"
  "$VENV/bin/python" -m aistat.publish --watch >>data/publisher.log 2>&1 &
  PUBLISHER_PID=$!
fi

cleanup() {
  kill "$POLLER_PID" 2>/dev/null || true
  if [ -n "$PUBLISHER_PID" ]; then
    kill "$PUBLISHER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "==> дашборд: http://localhost:$PORT"
"$VENV/bin/uvicorn" aistat.server:app --host 127.0.0.1 --port "$PORT"
