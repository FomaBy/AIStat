#!/usr/bin/env bash
#
# AIStat — local dual-branch deployment manager (macOS / launchd).
#
# Keeps two always-on local dashboards side by side, each tracking a git branch:
#   * dev  — auto-updates from origin/dev on a timer   → http://127.0.0.1:8788
#   * main — release-on-request (see `release`)        → http://127.0.0.1:8789
#
# Both serve the app's dashboard (API + UI) with their own port and SQLite DB
# through this script's own `serve` subcommand — never the checkout's ./run.sh,
# which on an older branch also starts a poller/publisher. Collection belongs
# solely to com.aistat.runtime.
#
# The operator's working copy (this repo in ~/Documents/AIStat) is never
# touched: each deployment is a dedicated clone under
#   ~/Library/Application Support/AIStat/local/{dev,main}
# kept OUTSIDE macOS-protected ~/Documents so the launchd agents can read it
# (same reason the runtime supervisor lives there, see docs/runtime-supervisor.md).
#
# Subcommands:
#   install                       clone/prepare both deployments and load launchd agents
#   serve <dev|main>              run one deployment's dashboard in the foreground
#                                 (what the launchd agents execute; not for manual use)
#   uninstall [--purge]           unload agents (--purge also removes checkouts + data)
#   sync <dev|main> [--ref R] [--force]
#                                 fetch + reset a deployment to origin/<branch> (or R),
#                                 restart it if HEAD changed (the dev timer calls `sync dev`)
#   release [--from <ref>] [--no-promote] [--force]
#                                 push a release to origin/main (default from origin/dev),
#                                 build + validate it, then refresh the main deployment
#   status                        launchd state, HEAD sha and health for each deployment
#   start|stop|restart <dev|main>
#
# Config via env (defaults shown):
#   AISTAT_LOCAL_ROOT                    ~/Library/Application Support/AIStat/local
#   AISTAT_REPO_URL                      origin URL of this repo
#   AISTAT_DEV_PORT                      8788
#   AISTAT_MAIN_PORT                     8789
#   AISTAT_DEV_UPDATE_INTERVAL_SECONDS   120   (how often the dev timer checks origin/dev)
#
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ROOT="${AISTAT_LOCAL_ROOT:-$HOME/Library/Application Support/AIStat/local}"
DEV_PORT="${AISTAT_DEV_PORT:-8788}"
MAIN_PORT="${AISTAT_MAIN_PORT:-8789}"
DEV_UPDATE_INTERVAL="${AISTAT_DEV_UPDATE_INTERVAL_SECONDS:-120}"

LA_DIR="$HOME/Library/LaunchAgents"
LAUNCH_DOMAIN="gui/$(id -u)"
LABEL_PREFIX="com.aistat.local"
SELF_INSTALLED="$LOCAL_ROOT/local_deploy.sh"
LOG_DIR="$LOCAL_ROOT/logs"

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[aistat-local] %s %s\n' "$(ts)" "$*"; }
die() { printf '[aistat-local] %s ERROR: %s\n' "$(ts)" "$*" >&2; exit 1; }

resolve_repo_url() {
  if [ -n "${AISTAT_REPO_URL:-}" ]; then printf '%s' "$AISTAT_REPO_URL"; return; fi
  git -C "$SOURCE_ROOT" remote get-url origin 2>/dev/null \
    || die "cannot determine repo URL; set AISTAT_REPO_URL"
}

branch_guard() {
  case "${1:-}" in
    dev|main) : ;;
    *) die "unknown deployment '${1:-}' (expected: dev | main)" ;;
  esac
}
deploy_dir() { printf '%s/%s' "$LOCAL_ROOT" "$1"; }
label_for()  { printf '%s.%s' "$LABEL_PREFIX" "$1"; }
port_for()   { case "$1" in dev) printf '%s' "$DEV_PORT";; main) printf '%s' "$MAIN_PORT";; esac; }

restart_agent() {
  branch_guard "$1"
  launchctl kickstart -k "$LAUNCH_DOMAIN/$(label_for "$1")" 2>/dev/null \
    || die "could not restart '$1' — is it installed? (run: local_deploy.sh install)"
}

# venv + deps, mirroring run.sh so the first launchd start is instant and any
# requirements change is applied on the next sync/release without extra flags.
ensure_deps() {
  local dir="$1" stamp
  [ -x "$dir/.venv/bin/python" ] || python3 -m venv "$dir/.venv"
  stamp="$(cksum "$dir/requirements.txt")"
  if [ ! -f "$dir/.venv/.requirements.cksum" ] \
     || [ "$(cat "$dir/.venv/.requirements.cksum")" != "$stamp" ]; then
    log "installing deps in $(basename "$dir")"
    "$dir/.venv/bin/pip" install -q -r "$dir/requirements.txt"
    printf '%s\n' "$stamp" > "$dir/.venv/.requirements.cksum"
  fi
}

prepare_checkout() {
  local branch="$1" url dir
  branch_guard "$branch"
  url="$(resolve_repo_url)"
  dir="$(deploy_dir "$branch")"
  if [ ! -d "$dir/.git" ]; then
    log "cloning origin/$branch -> $dir"
    rm -rf "$dir"
    mkdir -p "$(dirname "$dir")"
    git clone --quiet --branch "$branch" "$url" "$dir" \
      || die "clone of origin/$branch failed (does the branch exist on origin?)"
  else
    log "updating $dir to origin/$branch"
    git -C "$dir" fetch --quiet origin "$branch" || die "fetch origin/$branch failed"
    git -C "$dir" checkout --quiet -B "$branch" "origin/$branch" || die "checkout $branch failed"
    git -C "$dir" reset --hard --quiet "origin/$branch"
  fi
  ensure_deps "$dir"
  mkdir -p "$dir/data"
}

# The dashboard entry point, owned by this script instead of by the checkout.
# A deployment tracks a branch, and an older branch's ./run.sh also starts
# `aistat.poller` (plus the publisher when configured) next to the dashboard —
# extra writers that would duplicate the canonical com.aistat.runtime collector.
# Running the dashboard from here keeps exactly one automatic polling/publish
# path no matter how old the tracked branch is.
cmd_serve() {
  local branch="${1:-}" dir port
  branch_guard "$branch"
  dir="$(deploy_dir "$branch")"
  port="${AISTAT_PORT:-$(port_for "$branch")}"
  [ -d "$dir/.git" ] || die "$branch deployment not installed; run: local_deploy.sh install"
  ensure_deps "$dir"
  mkdir -p "$dir/data"
  cd "$dir"
  # Optional local application settings, exactly as the app's own run.sh reads
  # them. Collection stays with the runtime supervisor whatever the file says.
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi
  log "$branch dashboard → http://127.0.0.1:$port"
  exec "$dir/.venv/bin/uvicorn" aistat.server:app --host 127.0.0.1 --port "$port"
}

write_server_plist() {
  local branch="$1" dir port label plist
  branch_guard "$branch"
  dir="$(deploy_dir "$branch")"
  port="$(port_for "$branch")"
  label="$(label_for "$branch")"
  plist="$LA_DIR/$label.plist"
  mkdir -p "$LA_DIR" "$LOG_DIR"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SELF_INSTALLED</string>
    <string>serve</string>
    <string>$branch</string>
  </array>
  <key>WorkingDirectory</key><string>$dir</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>AISTAT_PORT</key><string>$port</string>
    <key>AISTAT_LOCAL_ROOT</key><string>$LOCAL_ROOT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/$branch.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$branch.err.log</string>
</dict>
</plist>
PLIST
  plutil -lint "$plist" >/dev/null || die "generated plist invalid: $plist"
}

write_updater_plist() {
  local label="$LABEL_PREFIX.dev-update" plist
  plist="$LA_DIR/$label.plist"
  mkdir -p "$LA_DIR" "$LOG_DIR"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SELF_INSTALLED</string>
    <string>sync</string>
    <string>dev</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>AISTAT_LOCAL_ROOT</key><string>$LOCAL_ROOT</string>
    <key>AISTAT_DEV_PORT</key><string>$DEV_PORT</string>
    <key>AISTAT_MAIN_PORT</key><string>$MAIN_PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>$DEV_UPDATE_INTERVAL</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/dev-update.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/dev-update.err.log</string>
</dict>
</plist>
PLIST
  plutil -lint "$plist" >/dev/null || die "generated plist invalid: $plist"
}

cmd_install() {
  mkdir -p "$LOCAL_ROOT" "$LOG_DIR"
  # Copy this script OUTSIDE ~/Documents so the launchd dev-update timer can run
  # it (launchd cannot read macOS-protected ~/Documents).
  install -m 0755 "${BASH_SOURCE[0]}" "$SELF_INSTALLED"
  prepare_checkout dev
  prepare_checkout main
  write_server_plist dev
  write_server_plist main
  write_updater_plist
  local label
  for label in "$LABEL_PREFIX.dev" "$LABEL_PREFIX.main" "$LABEL_PREFIX.dev-update"; do
    launchctl bootout "$LAUNCH_DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$LAUNCH_DOMAIN" "$LA_DIR/$label.plist" || die "failed to load $label"
  done
  log "installed."
  log "  dev  → http://127.0.0.1:$DEV_PORT   (auto-updates from origin/dev every ${DEV_UPDATE_INTERVAL}s)"
  log "  main → http://127.0.0.1:$MAIN_PORT   (release with: $SELF_INSTALLED release)"
  log "  logs: $LOG_DIR ; status: $SELF_INSTALLED status"
}

cmd_uninstall() {
  local purge=0 label
  [ "${1:-}" = "--purge" ] && purge=1
  for label in "$LABEL_PREFIX.dev" "$LABEL_PREFIX.main" "$LABEL_PREFIX.dev-update"; do
    launchctl bootout "$LAUNCH_DOMAIN/$label" 2>/dev/null || true
    rm -f "$LA_DIR/$label.plist"
  done
  log "launchd agents removed"
  if [ "$purge" = 1 ]; then
    rm -rf "$LOCAL_ROOT"
    log "purged $LOCAL_ROOT"
  else
    log "checkouts + data kept under $LOCAL_ROOT (use: uninstall --purge to remove)"
  fi
}

cmd_sync() {
  local branch="${1:-}"; branch_guard "$branch"; shift
  local ref="origin/$branch" force=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --ref)   ref="${2:?--ref needs a value}"; shift 2;;
      --force) force=1; shift;;
      *) die "sync: unknown option '$1'";;
    esac
  done
  local dir; dir="$(deploy_dir "$branch")"
  [ -d "$dir/.git" ] || die "$branch deployment not installed; run: local_deploy.sh install"
  git -C "$dir" fetch --quiet origin "$branch" || die "fetch origin/$branch failed"
  local old new
  old="$(git -C "$dir" rev-parse HEAD)"
  git -C "$dir" reset --hard --quiet "$ref"
  new="$(git -C "$dir" rev-parse HEAD)"
  ensure_deps "$dir"
  if [ "$old" != "$new" ] || [ "$force" = 1 ]; then
    log "$branch ${old:0:7} -> ${new:0:7}; restarting"
    restart_agent "$branch"
  else
    log "$branch already at ${new:0:7}; no restart"
  fi
}

cmd_release() {
  local from="origin/dev" promote=1 force=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --from)       from="${2:?--from needs a value}"; shift 2;;
      --no-promote) promote=0; shift;;
      --force)      force=1; shift;;
      *) die "release: unknown option '$1'";;
    esac
  done
  local mdir; mdir="$(deploy_dir main)"
  [ -d "$mdir/.git" ] || die "main deployment not installed; run: local_deploy.sh install"

  # The main deployment clone is the git base for the whole release: it carries
  # origin (push credentials are host-scoped, so it can push) and, after a
  # fetch, every branch a release might promote from.
  git -C "$mdir" fetch --quiet origin || die "fetch origin failed"

  # 1. Resolve the release candidate to an immutable SHA.
  local candidate sha tree
  if [ "$promote" = 1 ]; then candidate="$from"; else candidate="origin/main"; fi
  sha="$(git -C "$mdir" rev-parse --verify "${candidate}^{commit}" 2>/dev/null)" \
    || die "cannot resolve release candidate '$candidate'"
  tree="$(git -C "$mdir" rev-parse "${sha}^{tree}" 2>/dev/null)" \
    || die "cannot resolve release candidate tree '$candidate'"
  if [ "$promote" = 1 ] && [ "$force" != 1 ] \
     && git -C "$mdir" rev-parse --verify --quiet origin/main >/dev/null; then
    git -C "$mdir" merge-base --is-ancestor origin/main "$sha" \
      || die "origin/main is not an ancestor of '$from' — refusing non-fast-forward release (use --force)"
  fi

  # 2. Build + validate the candidate in a THROWAWAY staging tree, before any
  #    publish and before touching the live main deployment. If staging fails,
  #    origin/main and the running main dashboard are left completely untouched
  #    (this is the whole point of the release safety guarantee).
  local stage; stage="$LOCAL_ROOT/.release-stage.$$"
  rm -rf "$stage"; mkdir -p "$stage"
  # shellcheck disable=SC2064
  trap "rm -rf '$stage'" EXIT
  # Full commit+tree everywhere below: the cPanel approval gate (§7 of
  # docs/deployment-namecheap.md) needs this exact pair, so the operator must
  # be able to copy it straight from the release log.
  log "staging + validating candidate commit=$sha tree=$tree"
  git -C "$mdir" archive "$sha" | tar -x -C "$stage" \
    || die "could not export candidate — nothing published, main untouched"
  ( cd "$stage" && \
    AISTAT_SOURCE_REPOSITORY="$mdir" AISTAT_SKIP_ZIP=1 \
      bash scripts/build_cpanel_package.sh "$sha" "$tree" >/dev/null ) \
    || die "package build failed for candidate — origin/main and main deployment left untouched"
  python3 -m compileall -q -f "$stage/dist/aistat-cpanel" \
    || die "candidate failed py_compile — origin/main and main deployment left untouched"
  python3 -m py_compile \
    "$stage/dist/aistat-cpanel/aistat.cgi" \
    "$stage/dist/aistat-cpanel/passenger_wsgi.py" \
    || die "candidate entry point failed py_compile — origin/main and main deployment left untouched"
  rm -rf "$stage"; trap - EXIT
  log "candidate validated"

  # 3. Only now publish: push the validated SHA to origin/main (if promoting).
  if [ "$promote" = 1 ]; then
    log "promoting $from (commit=$sha tree=$tree) -> origin/main"
    if [ "$force" = 1 ]; then
      git -C "$mdir" push --force-with-lease origin "$sha:refs/heads/main" || die "push to origin/main failed"
    else
      git -C "$mdir" push origin "$sha:refs/heads/main" || die "push to origin/main failed"
    fi
  fi

  # 4. Move the live main deployment to the validated commit and restart it.
  log "updating main deployment to commit=$sha tree=$tree"
  git -C "$mdir" reset --hard --quiet "$sha"
  ensure_deps "$mdir"
  restart_agent main
  log "release complete: origin/main at commit=$(git -C "$mdir" rev-parse HEAD) tree=$(git -C "$mdir" rev-parse 'HEAD^{tree}'); dashboard http://127.0.0.1:$MAIN_PORT"
}

cmd_status() {
  printf 'AIStat local deployments (root: %s)\n' "$LOCAL_ROOT"
  local b dir label port sha loaded health
  for b in dev main; do
    dir="$(deploy_dir "$b")"; label="$(label_for "$b")"; port="$(port_for "$b")"
    if [ -d "$dir/.git" ]; then
      sha="$(git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo '?')@$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    else
      sha="(not installed)"
    fi
    launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1 && loaded="loaded" || loaded="not-loaded"
    curl -sf -m 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && health="serving :$port" || health="down :$port"
    printf '  %-4s  %-10s  %-13s  %s\n' "$b" "$loaded" "$health" "$sha"
  done
  local ulabel="$LABEL_PREFIX.dev-update"
  if launchctl print "$LAUNCH_DOMAIN/$ulabel" >/dev/null 2>&1; then
    printf '  dev-updater: loaded (checks origin/dev every %ss)\n' "$DEV_UPDATE_INTERVAL"
  else
    printf '  dev-updater: not-loaded\n'
  fi
}

cmd_start() {
  branch_guard "${1:-}"
  launchctl bootstrap "$LAUNCH_DOMAIN" "$LA_DIR/$(label_for "$1").plist" 2>/dev/null \
    || launchctl kickstart "$LAUNCH_DOMAIN/$(label_for "$1")" \
    || die "could not start '$1' (installed?)"
  log "$1 started"
}

cmd_stop() {
  branch_guard "${1:-}"
  launchctl bootout "$LAUNCH_DOMAIN/$(label_for "$1")" 2>/dev/null || true
  log "$1 stopped"
}

main() {
  local cmd="${1:-status}"; shift || true
  case "$cmd" in
    install)   cmd_install "$@";;
    serve)     cmd_serve "$@";;
    uninstall) cmd_uninstall "$@";;
    sync)      cmd_sync "$@";;
    release)   cmd_release "$@";;
    status)    cmd_status;;
    start)     cmd_start "$@";;
    stop)      cmd_stop "$@";;
    restart)   branch_guard "${1:-}"; restart_agent "$1"; log "$1 restarted";;
    *) die "unknown command '$cmd' (install|serve|uninstall|sync|release|status|start|stop|restart)";;
  esac
}

# Run only when executed directly; sourcing (e.g. from tests) loads the
# functions without dispatching a command.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
