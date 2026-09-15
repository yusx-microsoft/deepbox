#!/usr/bin/env bash
set -euo pipefail

# Process-local compatibility only: existing Azure settings/resources stay named
# DEEPBOX_*. Never log values or let a legacy value override an explicitly empty
# AGENTBRIDGE_* setting. The server shares this same presence-based precedence.
for legacy_key in "${!DEEPBOX_@}"; do
  canonical_key="AGENTBRIDGE_${legacy_key#DEEPBOX_}"
  if [ "${!canonical_key+x}" != x ]; then
    export "${canonical_key}=${!legacy_key}"
  fi
done
unset legacy_key canonical_key
# Keep Azure data outside extracted wwwroot, including fresh Azure configurations.
# Existing DEEPBOX_DATA_DIR=/home/deepbox remains the selected path unchanged.
export AGENTBRIDGE_DATA_DIR="${AGENTBRIDGE_DATA_DIR-/home/deepbox}"

app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$app_root"
echo "Starting agentbridge from $app_root"
exec python -m gunicorn \
  --chdir "$app_root" \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers 1 \
  --forwarded-allow-ips="*" \
  server.app.main:app
