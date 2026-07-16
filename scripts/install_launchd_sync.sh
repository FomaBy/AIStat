#!/usr/bin/env bash
# Install the publisher runtime outside macOS-protected Documents.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="${AISTAT_RUNTIME_ROOT:-$HOME/Library/Application Support/AIStat}"
PLIST_SOURCE="$SOURCE_ROOT/deploy/com.aistat.sync.plist.example"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.aistat.sync.plist"
LAUNCH_DOMAIN="gui/$(id -u)"

mkdir -p "$RUNTIME_ROOT/aistat" "$RUNTIME_ROOT/data"
chmod 700 "$RUNTIME_ROOT"
rsync -a --delete "$SOURCE_ROOT/aistat/" "$RUNTIME_ROOT/aistat/"
install -m 0644 "$SOURCE_ROOT/pricing.json" "$RUNTIME_ROOT/pricing.json"
install -m 0644 "$SOURCE_ROOT/requirements.txt" "$RUNTIME_ROOT/requirements.txt"
install -m 0755 "$SOURCE_ROOT/sync_to_host.sh" "$RUNTIME_ROOT/sync_to_host.sh"

mkdir -p "$HOME/Library/LaunchAgents"
plutil -lint "$PLIST_SOURCE"
launchctl bootout "$LAUNCH_DOMAIN" "$PLIST_TARGET" 2>/dev/null || true
install -m 0644 "$PLIST_SOURCE" "$PLIST_TARGET"
launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST_TARGET"

echo "Installed AIStat sync runtime at $RUNTIME_ROOT"
