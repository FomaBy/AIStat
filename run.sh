#!/usr/bin/env bash
# Single-command launch: API + dashboard on one port.
#   ./run.sh                 → http://localhost:8787
#   AISTAT_PORT=9000 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

# Load optional local application settings. Collection belongs to the trusted
# per-user runtime supervisor, never to a dashboard process.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PORT="${AISTAT_PORT:-8787}"
VENV=".venv"

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

echo "==> дашборд: http://localhost:$PORT"
exec "$VENV/bin/uvicorn" aistat.server:app --host 127.0.0.1 --port "$PORT"
