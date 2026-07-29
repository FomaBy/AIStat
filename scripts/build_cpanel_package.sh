#!/usr/bin/env bash
set -euo pipefail
umask 022

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_REPOSITORY="${AISTAT_SOURCE_REPOSITORY:-$REPO_DIR}"
TARGET="$REPO_DIR/dist/aistat-cpanel"
ARCHIVE="$REPO_DIR/dist/aistat-cpanel.zip"
EXPECTED_SHA="${1:-${AISTAT_EXPECTED_SHA:-}}"
EXPECTED_TREE="${2:-${AISTAT_EXPECTED_TREE:-}}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
usage() {
  printf 'usage: %s <full-commit-sha> <full-tree-sha>\n' "$0" >&2
  exit 64
}

[ "$#" -le 2 ] || usage
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || usage
[[ "$EXPECTED_TREE" =~ ^[0-9a-f]{40}$ ]] || usage
command -v git >/dev/null 2>&1 || die "git not found"
command -v tar >/dev/null 2>&1 || die "tar not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"

ACTUAL_SHA="$(git -C "$SOURCE_REPOSITORY" rev-parse --verify "${EXPECTED_SHA}^{commit}" 2>/dev/null)" \
  || die "expected commit is not available: $EXPECTED_SHA"
[ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] \
  || die "expected commit did not resolve exactly: $EXPECTED_SHA"
ACTUAL_TREE="$(git -C "$SOURCE_REPOSITORY" rev-parse "${EXPECTED_SHA}^{tree}")" \
  || die "could not resolve tree for $EXPECTED_SHA"
[ "$ACTUAL_TREE" = "$EXPECTED_TREE" ] \
  || die "tree mismatch for $EXPECTED_SHA: expected $EXPECTED_TREE, got $ACTUAL_TREE"

mkdir -p "$REPO_DIR/dist"
BUILD_DIR="$(mktemp -d "$REPO_DIR/dist/.aistat-cpanel-build.XXXXXX")"
SOURCE_DIR="$BUILD_DIR/source"
BUILD_TARGET="$BUILD_DIR/aistat-cpanel"
mkdir -p "$SOURCE_DIR" "$BUILD_TARGET"
cleanup() { rm -rf "$BUILD_DIR"; }
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Export only committed bytes from the exact candidate. The current checkout,
# including every untracked/ignored file beside a tracked input, is never a
# package source.
git -C "$SOURCE_REPOSITORY" archive "$EXPECTED_SHA" -- \
  aistat \
  passenger_wsgi.py \
  aistat.cgi \
  pricing.json \
  deploy/namecheap.htaccess \
  requirements-cpanel.txt \
  | tar -x -C "$SOURCE_DIR" \
  || die "could not export exact candidate $EXPECTED_SHA"

cp -Rp "$SOURCE_DIR/aistat" "$BUILD_TARGET/"
cp -p "$SOURCE_DIR/passenger_wsgi.py" "$SOURCE_DIR/aistat.cgi" \
  "$SOURCE_DIR/pricing.json" "$BUILD_TARGET/"
cp -p "$SOURCE_DIR/deploy/namecheap.htaccess" "$BUILD_TARGET/.htaccess.example"
cp -p "$SOURCE_DIR/requirements-cpanel.txt" "$BUILD_TARGET/requirements.txt"
# The token-handoff worker (encrypted store + pull client) runs only on the
# trusted local machine: its code, its `cryptography` dependency and any key
# or store files must never reach the shared cPanel host.
rm -f "$BUILD_TARGET/aistat/worker_store.py" "$BUILD_TARGET/aistat/worker_sync.py"
# The local runtime supervisor, its installer and preflight orchestrate the
# trusted-local contours. They belong to the local machine only.
rm -f "$BUILD_TARGET/aistat/supervisor.py" \
      "$BUILD_TARGET/aistat/runtime_install.py" \
      "$BUILD_TARGET/aistat/preflight.py"
# The shared host serves only the dependency-free WSGI contours.
rm -f "$BUILD_TARGET/aistat/server.py"
find "$BUILD_TARGET" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$BUILD_TARGET" -type f -name '*.pyc' -delete

# A portable, canonical manifest proves the exact package bytes, sizes and
# executable modes. It names the full source commit/tree but deliberately
# contains no ref, remote, build-host or environment values.
python3 - "$BUILD_TARGET" "$EXPECTED_SHA" "$EXPECTED_TREE" <<'PY'
from __future__ import print_function

import hashlib
import json
import os
import stat
import sys

package, commit_sha, tree_sha = sys.argv[1:]
manifest_name = "PACKAGE-MANIFEST.json"
entries = []
for root, dirs, files in os.walk(package):
    dirs.sort()
    files.sort()
    for name in dirs + files:
        path = os.path.join(root, name)
        if os.path.islink(path):
            raise SystemExit("package manifest refuses symlink: " + path)
    for name in files:
        path = os.path.join(root, name)
        relative = os.path.relpath(path, package).replace(os.sep, "/")
        if relative == manifest_name:
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        info = os.stat(path)
        entries.append({
            "mode": "%04o" % stat.S_IMODE(info.st_mode),
            "path": relative,
            "sha256": digest.hexdigest(),
            "size_bytes": info.st_size,
        })

manifest = os.path.join(package, manifest_name)
with open(manifest, "w") as stream:
    json.dump({
        "files": sorted(entries, key=lambda item: item["path"]),
        "format": "aistat-cpanel-package",
        "format_version": 1,
        "hash_algorithm": "sha256",
        "source_commit_sha": commit_sha,
        "source_tree_sha": tree_sha,
    }, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
PY

# Keep the last good build until the complete new package and manifest exist.
rm -rf "$TARGET" "$ARCHIVE"
mv "$BUILD_TARGET" "$TARGET"

# The daily cPanel deploy only needs the built directory and shared hosts may
# lack `zip`, so allow skipping the archive.
if [ "${AISTAT_SKIP_ZIP:-0}" = "1" ]; then
  printf '%s\n' "$TARGET"
elif command -v zip >/dev/null 2>&1; then
  (
    cd "$REPO_DIR/dist"
    zip -qr aistat-cpanel.zip aistat-cpanel
  )
  printf '%s\n' "$ARCHIVE"
else
  echo "zip not found; leaving built directory without archive" >&2
  printf '%s\n' "$TARGET"
fi
