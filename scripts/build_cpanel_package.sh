#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="dist/aistat-cpanel"
ARCHIVE="dist/aistat-cpanel.zip"
rm -rf "$TARGET" "$ARCHIVE"
mkdir -p "$TARGET"

cp -R aistat "$TARGET/"
cp passenger_wsgi.py pricing.json "$TARGET/"
cp requirements-cpanel.txt "$TARGET/requirements.txt"
find "$TARGET" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$TARGET" -type f -name '*.pyc' -delete

(
  cd dist
  zip -qr aistat-cpanel.zip aistat-cpanel
)

echo "$ARCHIVE"
