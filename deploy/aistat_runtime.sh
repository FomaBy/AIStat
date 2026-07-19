#!/usr/bin/env bash
# Install and manage the trusted local AIStat runtime supervisor (one launchd
# job keeping owner poller, owner publisher, worker_sync --watch and the
# per-user collector alive). Runtime root and every plist path derive from the
# real $HOME — no hard-coded username. Secrets stay in the owner-only env file
# and never touch the plist, argv or this script's output.
#
# Usage:
#   deploy/aistat_runtime.sh install
#   deploy/aistat_runtime.sh preflight
#   deploy/aistat_runtime.sh status
#   deploy/aistat_runtime.sh restart
#   deploy/aistat_runtime.sh rollback
#   deploy/aistat_runtime.sh uninstall [--purge]
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="${AISTAT_RUNTIME_ROOT:-$HOME/Library/Application Support/AIStat}"
ENV_FILE="${AISTAT_ENV_FILE:-$HOME/.config/aistat/production.env}"
VENV="$RUNTIME_ROOT/.venv"
VENV_PY="$VENV/bin/python"
STAGE="$RUNTIME_ROOT/code.incoming"

log() { printf '==> %s\n' "$*"; }

pick_python() {
  if [ -x "$VENV_PY" ]; then printf '%s' "$VENV_PY"; else printf '%s' "python3"; fi
}

require_safe_env_file() {
  # A private env file, if present, must be owner-only (no group/other bits).
  [ -e "$ENV_FILE" ] || return 0
  local mode
  mode="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")"
  # Normalise to a 3-digit octal and check the low two digits are 0.
  local go="${mode: -2}"
  if [ "$go" != "00" ]; then
    echo "error: $ENV_FILE must be mode 0600 (owner-only); found $mode" >&2
    exit 2
  fi
}

load_env_file() {
  # Source secrets/config for preflight without ever echoing them.
  [ -r "$ENV_FILE" ] || return 0
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
}

ensure_venv() {
  mkdir -p "$RUNTIME_ROOT"
  chmod 700 "$RUNTIME_ROOT" 2>/dev/null || true
  if [ ! -x "$VENV_PY" ]; then
    log "creating runtime virtualenv"
    python3 -m venv "$VENV"
  fi
  local stamp
  stamp="$(cksum "$STAGE/requirements.txt")"
  if [ ! -f "$VENV/.requirements.cksum" ] || \
     [ "$(cat "$VENV/.requirements.cksum")" != "$stamp" ]; then
    log "installing runtime dependencies"
    "$VENV_PY" -m pip install -q -r "$STAGE/requirements.txt"
    printf '%s\n' "$stamp" >"$VENV/.requirements.cksum"
  fi
}

stage_code() {
  rm -rf "$STAGE"
  mkdir -p "$STAGE"
  git -C "$SOURCE_ROOT" archive HEAD -- \
    aistat \
    pricing.json \
    requirements.txt \
    deploy/com.aistat.runtime.plist.template | tar -x -C "$STAGE"
}

do_install() {
  require_safe_env_file
  stage_code
  trap 'rm -rf "$STAGE"' EXIT
  ensure_venv

  # Lint the rendered manifest before touching the live runtime.
  local tmp_plist
  tmp_plist="$(mktemp -t aistat_runtime.XXXXXX).plist"
  AISTAT_RUNTIME_ROOT="$RUNTIME_ROOT" AISTAT_ENV_FILE="$ENV_FILE" \
    "$VENV_PY" -m aistat.runtime_install render \
      --runtime-root "$RUNTIME_ROOT" --python "$VENV_PY" \
      --env-file "$ENV_FILE" >"$tmp_plist"
  plutil -lint "$tmp_plist"
  rm -f "$tmp_plist"

  # Full preflight against the freshly staged code + runtime venv.
  log "running preflight"
  load_env_file
  ( cd "$STAGE" && AISTAT_ENV_FILE="$ENV_FILE" "$VENV_PY" -m aistat.preflight )

  log "installing runtime from staged code"
  AISTAT_RUNTIME_ROOT="$RUNTIME_ROOT" "$VENV_PY" -m aistat.runtime_install install \
    --stage "$STAGE" --runtime-root "$RUNTIME_ROOT" \
    --python "$VENV_PY" --env-file "$ENV_FILE"
  trap - EXIT
  rm -rf "$STAGE"
  log "runtime installed at $RUNTIME_ROOT"
}

do_passthrough() {
  local py
  py="$(pick_python)"
  AISTAT_RUNTIME_ROOT="$RUNTIME_ROOT" "$py" -m aistat.runtime_install "$@" \
    --runtime-root "$RUNTIME_ROOT" --env-file "$ENV_FILE"
}

do_preflight() {
  require_safe_env_file
  load_env_file
  local py
  py="$(pick_python)"
  AISTAT_ENV_FILE="$ENV_FILE" "$py" -m aistat.preflight
}

cmd="${1:-}"
shift || true
case "$cmd" in
  install)   do_install ;;
  preflight) do_preflight ;;
  status)    do_passthrough status ;;
  restart)   do_passthrough restart ;;
  rollback)  do_passthrough rollback ;;
  uninstall) do_passthrough uninstall "$@" ;;
  *)
    echo "usage: $0 {install|preflight|status|restart|rollback|uninstall [--purge]}" >&2
    exit 64
    ;;
esac
