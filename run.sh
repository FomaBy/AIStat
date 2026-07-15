#!/usr/bin/env bash
# Single-command launch: Multica poller + API + dashboard on one port.
#   ./run.sh                 → http://localhost:8787
#   AISTAT_PORT=9000 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

PORT="${AISTAT_PORT:-8787}"
VENV=".venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> первый запуск: создаю venv и ставлю зависимости"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r requirements.txt
fi

mkdir -p data
echo "==> поллер Multica запущен в фоне (лог: data/poller.log)"
"$VENV/bin/python" -m aistat.poller >>data/poller.log 2>&1 &
POLLER_PID=$!
trap 'kill "$POLLER_PID" 2>/dev/null || true' EXIT

echo "==> дашборд: http://localhost:$PORT"
"$VENV/bin/uvicorn" aistat.server:app --host 127.0.0.1 --port "$PORT"
