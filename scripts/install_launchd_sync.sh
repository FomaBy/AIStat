#!/usr/bin/env bash
# RETIRED (FAN-1412). The legacy com.aistat.sync generation (poller +
# publisher via sync_to_host.sh) is replaced by the com.aistat.runtime
# supervisor. Recreating the legacy job here would run duplicate contours
# next to the supervisor, so this entry point now refuses to install.
#
# `deploy/aistat_runtime.sh install` migrates an existing com.aistat.sync
# job automatically (verified bootout before the supervisor starts, shared
# data/ preserved). Upgrade/rollback path: docs/runtime-supervisor.md.
set -euo pipefail

echo "error: scripts/install_launchd_sync.sh is retired (FAN-1412)." >&2
echo "The com.aistat.sync launchd job was replaced by com.aistat.runtime." >&2
echo "Run: deploy/aistat_runtime.sh install  (migrates com.aistat.sync automatically)" >&2
echo "See docs/runtime-supervisor.md for the upgrade/rollback path." >&2
exit 64
