#!/usr/bin/env bash
set -euo pipefail

app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$app_root"
echo "Starting deepbox from $app_root"
exec python -m gunicorn \
  --chdir "$app_root" \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers 1 \
  --forwarded-allow-ips="*" \
  server.app.main:app
