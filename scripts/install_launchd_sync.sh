#!/usr/bin/env bash
# Install the publisher runtime outside macOS-protected Documents.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="${AISTAT_RUNTIME_ROOT:-$HOME/Library/Application Support/AIStat}"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.aistat.sync.plist"
LAUNCH_DOMAIN="gui/$(id -u)"
STAGE="$RUNTIME_ROOT/.install-stage"

rm -rf "$STAGE"
mkdir -p "$STAGE" "$RUNTIME_ROOT/aistat" "$RUNTIME_ROOT/data"
trap 'rm -rf "$STAGE"' EXIT
git -C "$SOURCE_ROOT" archive HEAD -- \
  aistat \
  deploy/com.aistat.sync.plist.example \
  pricing.json \
  requirements.txt \
  sync_to_host.sh |
  tar -x -C "$STAGE"
PLIST_SOURCE="$STAGE/deploy/com.aistat.sync.plist.example"

plutil -lint "$PLIST_SOURCE"
launchctl bootout "$LAUNCH_DOMAIN" "$PLIST_TARGET" 2>/dev/null || true
chmod 700 "$RUNTIME_ROOT"
rsync -a --delete "$STAGE/aistat/" "$RUNTIME_ROOT/aistat/"
install -m 0644 "$STAGE/pricing.json" "$RUNTIME_ROOT/pricing.json"
install -m 0644 "$STAGE/requirements.txt" "$RUNTIME_ROOT/requirements.txt"
install -m 0755 "$STAGE/sync_to_host.sh" "$RUNTIME_ROOT/sync_to_host.sh"

mkdir -p "$HOME/Library/LaunchAgents"
install -m 0644 "$PLIST_SOURCE" "$PLIST_TARGET"
launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST_TARGET"

echo "Installed AIStat sync runtime at $RUNTIME_ROOT"
