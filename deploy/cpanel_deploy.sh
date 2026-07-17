#!/usr/bin/env bash
#
# AIStat — daily production deploy for Namecheap cPanel shared hosting.
#
# Intended to run from a cPanel cron job at 05:00 (see
# docs/deployment-namecheap.md, section "6. Автообновление через cPanel Git +
# cron"). On each run it:
#
#   1. fast-forwards this clone to origin/main (public repo, no credentials);
#   2. rebuilds the dependency-free cPanel package;
#   3. syntax-checks the package;
#   4. atomically publishes it as ~/aistat_app via a symlink flip;
#   5. keeps the previous releases so a rollback is a single symlink change.
#
# Safety guarantees:
#   * Every step before the symlink flip can abort the run. If the pull,
#     build or syntax check fails, the live symlink is NOT touched and the
#     currently running release keeps serving traffic.
#   * Only the application code root (~/aistat_app) is changed. Private data
#     (~/aistat-private: env file + SQLite databases) and the web-root shim
#     (~/public_html/cgi-bin/aistat.cgi, ~/public_html/.htaccess) are never
#     written to by this script.
#   * The CGI entry point re-imports the code on every request, so the flip
#     takes effect immediately with no restart. (For a Passenger setup,
#     restart the Python App from cPanel after a deploy.)
#
# Rollback: point the symlink back at the previous release, e.g.
#   ln -sfn ~/aistat_releases/<previous-release> ~/aistat_app
# Available releases are listed by:  ls -1t ~/aistat_releases
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${HOME:?HOME is not set}"
APP_LINK="${AISTAT_APP_ROOT:-$HOME_DIR/aistat_app}"
RELEASES_DIR="${AISTAT_RELEASES_DIR:-$HOME_DIR/aistat_releases}"
KEEP_RELEASES="${AISTAT_KEEP_RELEASES:-5}"

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[aistat-deploy] %s %s\n' "$(ts)" "$*"; }
die() { printf '[aistat-deploy] %s ERROR: %s\n' "$(ts)" "$*" >&2; exit 1; }

# Unambiguous `previous:` value for the PUBLISHED deploy-log line: the prior
# release's basename when one exists, or `none` otherwise. Kept as a pure
# helper so the focused test can exercise it without running a deploy.
prev_label() {
  if [ -n "${1-}" ]; then
    basename "$1"
  else
    printf 'none\n'
  fi
}

# Sourcing this script with AISTAT_DEPLOY_LIB_ONLY=1 loads the helpers above
# (for focused tests) and stops before any deploy side effect.
if [ -n "${AISTAT_DEPLOY_LIB_ONLY:-}" ]; then
  return 0 2>/dev/null || exit 0
fi

command -v git >/dev/null 2>&1     || die "git not found on host"
command -v python3 >/dev/null 2>&1 || die "python3 not found on host"

# 1. Update the source. A network/git failure aborts before anything is
#    published, so production keeps running the previous release.
cd "$REPO_DIR"
log "updating $REPO_DIR from origin/main"
git fetch --quiet origin main            || die "git fetch failed — keeping current release"
git reset --hard --quiet origin/main     || die "git reset failed — keeping current release"
NEW_SHA="$(git rev-parse --short HEAD)"
log "source at $NEW_SHA"

# 2. Build the dependency-free package (AISTAT_SKIP_ZIP: no `zip` needed).
log "building cPanel package"
AISTAT_SKIP_ZIP=1 bash scripts/build_cpanel_package.sh >/dev/null || die "package build failed"
PKG="$REPO_DIR/dist/aistat-cpanel"
[ -f "$PKG/aistat/legacy_wsgi.py" ] || die "built package missing aistat/legacy_wsgi.py"
[ -f "$PKG/pricing.json" ]          || die "built package missing pricing.json"

# 3. Validate the package. A syntax error aborts before publishing.
#    -f forces a fresh compile of every module so the check never trusts a
#    stale .pyc (compileall's timestamp cache could otherwise skip a changed
#    file whose mtime lands in the same second as a previous good build).
log "syntax-checking package"
python3 -m compileall -q -f "$PKG/aistat" || die "package failed py_compile — NOT deploying"

# 4. Stage an immutable, timestamped release.
mkdir -p "$RELEASES_DIR"
RELEASE="$RELEASES_DIR/$(date '+%Y%m%d-%H%M%S')-$NEW_SHA"
log "staging release $(basename "$RELEASE")"
rm -rf "$RELEASE"
cp -R "$PKG" "$RELEASE"

# 5. Publish atomically. `ln -sfn` replaces the symlink with a single rename.
#    On the first run ~/aistat_app is still the real directory from the manual
#    initial deploy; preserve it as a rollback release before switching.
if [ -e "$APP_LINK" ] && [ ! -L "$APP_LINK" ]; then
  LEGACY="$RELEASES_DIR/manual-$(date '+%Y%m%d-%H%M%S')"
  log "existing directory at $APP_LINK — preserving it as $(basename "$LEGACY") for rollback"
  mv "$APP_LINK" "$LEGACY"
fi
PREV="$(readlink "$APP_LINK" 2>/dev/null || true)"
ln -sfn "$RELEASE" "$APP_LINK"
log "PUBLISHED $APP_LINK -> $(basename "$RELEASE") (previous: $(prev_label "$PREV"))"

# 6. Prune old releases, always keeping the live one plus KEEP_RELEASES-1 more.
if [ "${KEEP_RELEASES}" -gt 0 ] 2>/dev/null; then
  CURRENT="$(readlink "$APP_LINK" 2>/dev/null || true)"
  kept=0
  for dir in $(ls -1dt "$RELEASES_DIR"/*/ 2>/dev/null); do
    dir="${dir%/}"
    [ "$dir" = "$CURRENT" ] && continue
    kept=$((kept + 1))
    if [ "$kept" -ge "$KEEP_RELEASES" ]; then
      log "pruning old release $(basename "$dir")"
      rm -rf "$dir"
    fi
  done
fi

log "deploy complete: $NEW_SHA is live at $APP_LINK"
