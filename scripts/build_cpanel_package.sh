#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="dist/aistat-cpanel"
ARCHIVE="dist/aistat-cpanel.zip"
rm -rf "$TARGET" "$ARCHIVE"
mkdir -p "$TARGET"

cp -R aistat "$TARGET/"
cp passenger_wsgi.py aistat.cgi pricing.json "$TARGET/"
cp deploy/namecheap.htaccess "$TARGET/.htaccess.example"
cp requirements-cpanel.txt "$TARGET/requirements.txt"
find "$TARGET" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$TARGET" -type f -name '*.pyc' -delete

# The daily cPanel deploy (deploy/cpanel_deploy.sh) only needs the built
# directory and shared hosts may lack `zip`, so allow skipping the archive.
if [ "${AISTAT_SKIP_ZIP:-0}" = "1" ]; then
  echo "$TARGET"
elif command -v zip >/dev/null 2>&1; then
  (
    cd dist
    zip -qr aistat-cpanel.zip aistat-cpanel
  )
  echo "$ARCHIVE"
else
  echo "zip not found; leaving built directory without archive" >&2
  echo "$TARGET"
fi
