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
# The token-handoff worker (encrypted store + pull client) runs only on the
# trusted local machine: its code, its `cryptography` dependency and any key
# or store files must never reach the shared cPanel host.
rm -f "$TARGET/aistat/worker_store.py" "$TARGET/aistat/worker_sync.py"
# The local runtime supervisor, its installer and preflight orchestrate the
# trusted-local contours (poller/publisher/worker/collector). They belong to
# the local machine only and never run on the shared cPanel host.
rm -f "$TARGET/aistat/supervisor.py" "$TARGET/aistat/runtime_install.py" \
      "$TARGET/aistat/preflight.py"
# The shared host serves the dependency-free legacy WSGI entry point (and may
# run the Flask WSGI contour); it never runs the local FastAPI/uvicorn app.
# Ship neither `server.py` nor the FastAPI import it carries to the public
# host — the loopback-only launcher (run.sh) keeps them local.
rm -f "$TARGET/aistat/server.py"
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
