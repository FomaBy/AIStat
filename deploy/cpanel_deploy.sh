#!/usr/bin/env bash
#
# Exact-candidate cPanel deploy for AIStat.
#
# Normal deploys require the full approved commit and root-tree SHA. The
# package comes only from that commit's git archive, is validated in isolation,
# and is published with one same-filesystem atomic rename. Rollback uses the
# same target validation, host-local lock and atomic switch.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${HOME:?HOME is not set}"
APP_LINK="${AISTAT_APP_ROOT:-$HOME_DIR/aistat_app}"
RELEASES_DIR="${AISTAT_RELEASES_DIR:-$HOME_DIR/aistat_releases}"
LOCK_FILE="${AISTAT_DEPLOY_LOCK_FILE:-$HOME_DIR/aistat-private/cpanel-deploy.lock}"
KEEP_RELEASES="${AISTAT_KEEP_RELEASES:-5}"
MANIFEST_NAME="PACKAGE-MANIFEST.json"

VALIDATION_ROOT=""
NEXT_LINK=""
STAGED_RELEASE=""
PREVIOUS_TARGET=""
RELEASE_TARGET=""

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[aistat-deploy] %s %s\n' "$(ts)" "$*"; }
die() { printf '[aistat-deploy] %s ERROR: %s\n' "$(ts)" "$*" >&2; exit 1; }

usage() {
  cat >&2 <<EOF
usage:
  $0 deploy <full-commit-sha> <full-tree-sha>
  $0 rollback <exact-absolute-release-target>
EOF
  exit 64
}

prev_label() {
  if [ -n "${1-}" ]; then basename "$1"; else printf 'none\n'; fi
}

is_full_sha() { [[ "${1-}" =~ ^[0-9a-f]{40}$ ]]; }

manifest_identity() {
  python3 - "$1/$MANIFEST_NAME" <<'PY'
from __future__ import print_function

import json
import sys

with open(sys.argv[1], "r") as stream:
    value = json.load(stream)
print(value.get("source_commit_sha", ""), value.get("source_tree_sha", ""))
PY
}

manifest_sha256() {
  python3 - "$1/$MANIFEST_NAME" <<'PY'
from __future__ import print_function

import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

verify_manifest() {
  python3 - "$1" "$2" "$3" "$MANIFEST_NAME" <<'PY'
from __future__ import print_function

import hashlib
import json
import os
import posixpath
import stat
import sys

root, expected_commit, expected_tree, manifest_name = sys.argv[1:]
manifest_path = os.path.join(root, manifest_name)
if os.path.islink(manifest_path) or not os.path.isfile(manifest_path):
    raise SystemExit("missing regular package manifest")
with open(manifest_path, "r") as stream:
    manifest = json.load(stream)
if manifest.get("format") != "aistat-cpanel-package":
    raise SystemExit("unexpected manifest format")
if manifest.get("format_version") != 1:
    raise SystemExit("unexpected manifest version")
if manifest.get("hash_algorithm") != "sha256":
    raise SystemExit("unexpected manifest hash algorithm")
if manifest.get("source_commit_sha") != expected_commit:
    raise SystemExit("manifest commit does not match expected commit")
if manifest.get("source_tree_sha") != expected_tree:
    raise SystemExit("manifest tree does not match expected tree")

entries = manifest.get("files")
if not isinstance(entries, list):
    raise SystemExit("manifest files must be a list")
expected_paths = set()
root_real = os.path.realpath(root)
for entry in entries:
    if not isinstance(entry, dict):
        raise SystemExit("invalid manifest entry")
    relative = entry.get("path")
    if not isinstance(relative, str) or not relative:
        raise SystemExit("invalid manifest path")
    if (relative.startswith("/") or posixpath.normpath(relative) != relative
            or relative == manifest_name or relative in expected_paths):
        raise SystemExit("unsafe or duplicate manifest path: " + relative)
    full_path = os.path.join(root, *relative.split("/"))
    if os.path.islink(full_path) or not os.path.isfile(full_path):
        raise SystemExit("manifest path is not a regular file: " + relative)
    if os.path.dirname(os.path.realpath(full_path)) != os.path.dirname(
            os.path.abspath(full_path)):
        raise SystemExit("manifest path escapes package: " + relative)
    if not os.path.realpath(full_path).startswith(root_real + os.sep):
        raise SystemExit("manifest path escapes package: " + relative)
    info = os.stat(full_path)
    if entry.get("size_bytes") != info.st_size:
        raise SystemExit("manifest size mismatch: " + relative)
    if entry.get("mode") != "%04o" % stat.S_IMODE(info.st_mode):
        raise SystemExit("manifest mode mismatch: " + relative)
    digest = hashlib.sha256()
    with open(full_path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if entry.get("sha256") != digest.hexdigest():
        raise SystemExit("manifest digest mismatch: " + relative)
    expected_paths.add(relative)

actual_paths = set()
for current, dirs, files in os.walk(root):
    for name in dirs + files:
        candidate = os.path.join(current, name)
        if os.path.islink(candidate):
            raise SystemExit("package contains symlink: " + candidate)
    for name in files:
        relative = os.path.relpath(os.path.join(current, name), root).replace(
            os.sep, "/"
        )
        if relative != manifest_name:
            actual_paths.add(relative)
if actual_paths != expected_paths:
    raise SystemExit("manifest payload set does not match package")
PY
}

# Sourcing with AISTAT_DEPLOY_LIB_ONLY loads pure helpers for focused tests.
if [ -n "${AISTAT_DEPLOY_LIB_ONLY:-}" ]; then
  # `exit` is the direct-execution fallback for the source-only `return`.
  # shellcheck disable=SC2317
  return 0 2>/dev/null || exit 0
fi

cleanup() {
  [ -z "$VALIDATION_ROOT" ] || rm -rf "$VALIDATION_ROOT"
  [ -z "$NEXT_LINK" ] || rm -f "$NEXT_LINK"
  [ -z "$STAGED_RELEASE" ] || rm -rf "$STAGED_RELEASE"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "$1 not found on host"
}

acquire_lock() {
  local lock_parent previous_umask
  lock_parent="$(dirname "$LOCK_FILE")"
  [ -d "$lock_parent" ] \
    || die "deploy lock parent does not exist: $lock_parent"
  previous_umask="$(umask)"
  umask 077
  exec 9>>"$LOCK_FILE" || die "cannot open deploy lock: $LOCK_FILE"
  umask "$previous_umask"
  python3 - "$$" 9 <<'PY' \
    || die "another deploy/rollback holds the host-local lock: $LOCK_FILE"
from __future__ import print_function

import fcntl
import os
import sys

pid = sys.argv[1]
fd = int(sys.argv[2])
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (IOError, OSError):
    raise SystemExit(1)
os.ftruncate(fd, 0)
os.write(fd, ("pid=%s\n" % pid).encode("ascii"))
PY
  log "host-local lock acquired: $LOCK_FILE"
}

validate_keep_releases() {
  if [ "$KEEP_RELEASES" = "0" ]; then return; fi
  [[ "$KEEP_RELEASES" =~ ^[2-9][0-9]*$ ]] \
    || die "AISTAT_KEEP_RELEASES must be 0 (unlimited) or a canonical integer >= 2"
}

validate_configured_paths() {
  local label value
  for label in APP_LINK RELEASES_DIR LOCK_FILE; do
    case "$label" in
      APP_LINK) value="$APP_LINK" ;;
      RELEASES_DIR) value="$RELEASES_DIR" ;;
      LOCK_FILE) value="$LOCK_FILE" ;;
    esac
    [ "${value#/}" != "$value" ] || die "$label must be an absolute path: $value"
  done
  [ "$APP_LINK" != "$RELEASES_DIR" ] \
    || die "AISTAT_APP_ROOT and AISTAT_RELEASES_DIR must be different paths"
  [ "$APP_LINK" != "$LOCK_FILE" ] \
    || die "AISTAT_APP_ROOT and AISTAT_DEPLOY_LOCK_FILE must be different paths"
}

validate_release_target() {
  local target="$1" target_normalized releases_real parent_real
  [ "${target#/}" != "$target" ] || die "release target must be an absolute path: $target"
  target_normalized="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$target")"
  [ "$target" = "$target_normalized" ] \
    || die "release target must not contain traversal or redundant components: $target"
  [ ! -L "$target" ] || die "release target must not be a symlink: $target"
  [ -d "$target" ] || die "release target does not exist: $target"
  releases_real="$(cd "$RELEASES_DIR" && pwd -P)"
  parent_real="$(cd "$(dirname "$target")" && pwd -P)"
  [ "$parent_real" = "$releases_real" ] \
    || die "release target must be a direct child of $releases_real: $target"
}

capture_live_target() {
  local require_existing="${1:-0}"
  PREVIOUS_TARGET=""
  if [ -L "$APP_LINK" ]; then
    PREVIOUS_TARGET="$(readlink "$APP_LINK")" \
      || die "could not read current live symlink: $APP_LINK"
    validate_release_target "$PREVIOUS_TARGET"
  elif [ -e "$APP_LINK" ]; then
    die "$APP_LINK is not a symlink; use the documented first-migration maintenance procedure"
  elif [ "$require_existing" = "1" ]; then
    die "rollback requires an existing live symlink: $APP_LINK"
  fi
}

assert_live_unchanged() {
  if [ -z "$PREVIOUS_TARGET" ]; then
    [ ! -e "$APP_LINK" ] && [ ! -L "$APP_LINK" ] \
      || die "live path appeared during validation; refusing publish"
  else
    [ -L "$APP_LINK" ] \
      || die "live symlink changed during validation; refusing publish"
    [ "$(readlink "$APP_LINK")" = "$PREVIOUS_TARGET" ] \
      || die "live target drifted during validation; refusing publish"
    validate_release_target "$PREVIOUS_TARGET"
  fi
}

atomic_switch() {
  local target="$1" app_parent app_name
  app_parent="$(dirname "$APP_LINK")"
  app_name="$(basename "$APP_LINK")"
  [ -d "$app_parent" ] || die "application parent does not exist: $app_parent"
  NEXT_LINK="$app_parent/.${app_name}.next.$$"
  [ ! -e "$NEXT_LINK" ] && [ ! -L "$NEXT_LINK" ] \
    || die "temporary publish link already exists: $NEXT_LINK"
  ln -s "$target" "$NEXT_LINK" || die "could not create temporary publish link"
  python3 - "$NEXT_LINK" "$APP_LINK" <<'PY' \
    || die "atomic live-link rename failed"
from __future__ import print_function

import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
  NEXT_LINK=""
  # The commit point has happened. Never let EXIT cleanup remove the now-live
  # release even if the read-back check or later retention/logging fails.
  STAGED_RELEASE=""
  [ -L "$APP_LINK" ] && [ "$(readlink "$APP_LINK")" = "$target" ] \
    || die "published link does not match exact target: $target"
}

candidate_gate() {
  local expected_sha="$1" expected_tree="$2" phase="$3" actual_sha actual_tree
  git -C "$REPO_DIR" fetch --quiet origin \
    '+refs/heads/main:refs/remotes/origin/main' \
    || die "$phase fetch failed — keeping current release"
  actual_sha="$(git -C "$REPO_DIR" rev-parse --verify 'refs/remotes/origin/main^{commit}')" \
    || die "$phase could not resolve fetched origin/main"
  actual_tree="$(git -C "$REPO_DIR" rev-parse 'refs/remotes/origin/main^{tree}')" \
    || die "$phase could not resolve fetched origin/main tree"
  [ "$actual_sha" = "$expected_sha" ] \
    || die "$phase commit drift: expected $expected_sha, fetched $actual_sha"
  [ "$actual_tree" = "$expected_tree" ] \
    || die "$phase tree drift: expected $expected_tree, fetched $actual_tree"
  log "$phase candidate verified: commit=$actual_sha tree=$actual_tree"
}

validate_package_runtime() {
  local package="$1"
  VALIDATION_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/aistat-cpanel-validate.XXXXXX")"
  mkdir -p "$VALIDATION_ROOT/package" "$VALIDATION_ROOT/home" \
    "$VALIDATION_ROOT/data/tenants"
  cp -R "$package"/. "$VALIDATION_ROOT/package/"

  python3 -m compileall -q -f "$VALIDATION_ROOT/package" \
    || die "package failed forced compileall — NOT deploying"
  python3 -m py_compile \
    "$VALIDATION_ROOT/package/aistat.cgi" \
    "$VALIDATION_ROOT/package/passenger_wsgi.py" \
    || die "top-level entry point failed forced compile — NOT deploying"

  (
    cd "$VALIDATION_ROOT/package"
    env \
      HOME="$VALIDATION_ROOT/home" \
      PYTHONDONTWRITEBYTECODE=1 \
      PYTHONPATH="$VALIDATION_ROOT/package" \
      AISTAT_APP_ROOT="$VALIDATION_ROOT/package" \
      AISTAT_CGI_ENV_FILE="$VALIDATION_ROOT/missing.env" \
      AISTAT_DB_PATH="$VALIDATION_ROOT/data/aistat.db" \
      AISTAT_SECURITY_DB_PATH="$VALIDATION_ROOT/data/security.db" \
      AISTAT_TENANTS_DIR="$VALIDATION_ROOT/data/tenants" \
      AISTAT_SESSION_SECRET=validation-session-secret-0000000000000001 \
      AISTAT_INGEST_SECRET=validation-ingest-secret-00000000000000002 \
      AISTAT_ADMIN_USERNAME=validation-admin \
      AISTAT_PASSWORD_HASH=validation-password-hash \
      AISTAT_ALLOWED_HOSTS=localhost \
      python3 -c 'import passenger_wsgi; import runpy; assert callable(passenger_wsgi.application); runpy.run_path("aistat.cgi", run_name="aistat_validation")' \
      || die "isolated cPanel import smoke failed — NOT deploying"
  )
  rm -rf "$VALIDATION_ROOT"
  VALIDATION_ROOT=""
}

stage_release() {
  local package="$1" sha="$2" stamp incoming suffix release
  stamp="$(date '+%Y%m%d-%H%M%S')"
  incoming="$(mktemp -d "$RELEASES_DIR/.incoming-${stamp}-${sha}.XXXXXX")" \
    || die "could not create unique release staging directory"
  STAGED_RELEASE="$incoming"
  cp -R "$package"/. "$incoming/" || die "could not copy package into release staging"
  verify_manifest "$incoming" "$sha" "$3" \
    || die "staged release manifest verification failed"
  suffix="${incoming##*.}"
  release="$RELEASES_DIR/release-${stamp}-${sha}-${suffix}"
  [ ! -e "$release" ] && [ ! -L "$release" ] \
    || die "unique release target already exists: $release"
  mv "$incoming" "$release" || die "could not finalize staged release directory"
  STAGED_RELEASE="$release"
  RELEASE_TARGET="$release"
}

prune_releases() {
  local current="$1" previous="$2"
  [ "$KEEP_RELEASES" != "0" ] || return 0
  python3 - "$RELEASES_DIR" "$current" "$previous" "$KEEP_RELEASES" "$MANIFEST_NAME" <<'PY'
from __future__ import print_function

import json
import os
import shutil
import sys

root, current, previous, keep_raw, manifest_name = sys.argv[1:]
keep = int(keep_raw)
protected = set(path for path in (current, previous) if path)
managed = []
for entry in os.scandir(root):
    if not entry.name.startswith("release-") or not entry.is_dir(follow_symlinks=False):
        continue
    manifest_path = os.path.join(entry.path, manifest_name)
    if os.path.islink(manifest_path) or not os.path.isfile(manifest_path):
        continue
    try:
        with open(manifest_path, "r") as stream:
            manifest = json.load(stream)
    except (IOError, OSError, ValueError):
        continue
    if (manifest.get("format") != "aistat-cpanel-package"
            or manifest.get("format_version") != 1):
        continue
    managed.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
managed.sort(key=lambda item: (item[0], item[1]), reverse=True)
kept = len(protected)
for unused_mtime, path in managed:
    if path in protected:
        continue
    if kept < keep:
        kept += 1
        continue
    shutil.rmtree(path)
PY
}

cmd_deploy() {
  [ "$#" -eq 2 ] || usage
  local expected_sha="$1" expected_tree="$2" head_sha head_tree package
  local release manifest_hash live_identity live_sha live_tree
  is_full_sha "$expected_sha" || die "deploy requires a full lowercase 40-character commit SHA"
  is_full_sha "$expected_tree" || die "deploy requires a full lowercase 40-character tree SHA"
  validate_keep_releases
  acquire_lock
  mkdir -p "$RELEASES_DIR"
  capture_live_target 0

  candidate_gate "$expected_sha" "$expected_tree" "pre-build"
  git -C "$REPO_DIR" reset --hard --quiet "$expected_sha" \
    || die "could not reset checkout to exact candidate"
  head_sha="$(git -C "$REPO_DIR" rev-parse HEAD)"
  head_tree="$(git -C "$REPO_DIR" rev-parse 'HEAD^{tree}')"
  [ "$head_sha" = "$expected_sha" ] && [ "$head_tree" = "$expected_tree" ] \
    || die "checkout identity mismatch after reset"

  if [ -n "$PREVIOUS_TARGET" ] && [ -f "$PREVIOUS_TARGET/$MANIFEST_NAME" ]; then
    live_identity="$(manifest_identity "$PREVIOUS_TARGET")" \
      || die "could not read current live release identity"
    read -r live_sha live_tree <<<"$live_identity"
    verify_manifest "$PREVIOUS_TARGET" "$live_sha" "$live_tree" \
      || die "current live release manifest is invalid"
    if [ "$live_sha" = "$expected_sha" ] && [ "$live_tree" = "$expected_tree" ]; then
      manifest_hash="$(manifest_sha256 "$PREVIOUS_TARGET")"
      log "ALREADY LIVE commit=$expected_sha tree=$expected_tree target=$PREVIOUS_TARGET manifest_sha256=$manifest_hash"
      return 0
    fi
  fi

  log "building immutable package from commit=$expected_sha tree=$expected_tree"
  AISTAT_SKIP_ZIP=1 bash "$REPO_DIR/scripts/build_cpanel_package.sh" \
    "$expected_sha" "$expected_tree" >/dev/null \
    || die "package build failed — keeping current release"
  package="$REPO_DIR/dist/aistat-cpanel"
  [ -f "$package/aistat/legacy_wsgi.py" ] \
    || die "built package missing aistat/legacy_wsgi.py"
  [ -f "$package/pricing.json" ] || die "built package missing pricing.json"
  [ -f "$package/aistat.cgi" ] || die "built package missing aistat.cgi"
  [ -f "$package/passenger_wsgi.py" ] \
    || die "built package missing passenger_wsgi.py"
  verify_manifest "$package" "$expected_sha" "$expected_tree" \
    || die "built package manifest verification failed"
  validate_package_runtime "$package"

  stage_release "$package" "$expected_sha" "$expected_tree"
  release="$RELEASE_TARGET"
  manifest_hash="$(manifest_sha256 "$release")"
  log "package verified: manifest=$release/$MANIFEST_NAME manifest_sha256=$manifest_hash"

  candidate_gate "$expected_sha" "$expected_tree" "pre-publish"
  assert_live_unchanged
  atomic_switch "$release"
  STAGED_RELEASE=""
  log "PUBLISHED app=$APP_LINK previous=${PREVIOUS_TARGET:-none} new=$release commit=$expected_sha tree=$expected_tree manifest_sha256=$manifest_hash"

  if ! prune_releases "$release" "$PREVIOUS_TARGET"; then
    log "WARNING: publish succeeded but retention cleanup failed; live and previous targets were preserved"
  fi
  log "deploy complete: commit=$expected_sha tree=$expected_tree live=$release"
}

cmd_rollback() {
  [ "$#" -eq 1 ] || usage
  local target="$1" target_identity target_sha target_tree manifest_hash
  acquire_lock
  [ -d "$RELEASES_DIR" ] || die "release directory does not exist: $RELEASES_DIR"
  capture_live_target 1
  validate_release_target "$target"
  [ -f "$target/$MANIFEST_NAME" ] \
    || die "rollback target has no verifiable package manifest: $target"
  target_identity="$(manifest_identity "$target")" \
    || die "could not read rollback target identity"
  read -r target_sha target_tree <<<"$target_identity"
  if ! is_full_sha "$target_sha" || ! is_full_sha "$target_tree"; then
    die "rollback target manifest has invalid commit/tree identity"
  fi
  verify_manifest "$target" "$target_sha" "$target_tree" \
    || die "rollback target package manifest verification failed"
  if [ "$target" = "$PREVIOUS_TARGET" ]; then
    log "ALREADY LIVE rollback target=$target commit=$target_sha tree=$target_tree"
    return 0
  fi
  assert_live_unchanged
  manifest_hash="$(manifest_sha256 "$target")"
  atomic_switch "$target"
  log "ROLLED BACK app=$APP_LINK previous=$PREVIOUS_TARGET new=$target commit=$target_sha tree=$target_tree manifest_sha256=$manifest_hash"
}

require_command git
require_command python3
require_command tar
validate_configured_paths

command_name="${1:-}"
[ -n "$command_name" ] || usage
shift
case "$command_name" in
  deploy)   cmd_deploy "$@" ;;
  rollback) cmd_rollback "$@" ;;
  *)        usage ;;
esac
