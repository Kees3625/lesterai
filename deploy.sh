#!/bin/bash
# Deploy the latest code from GitHub to this container.
# Usage (on the app container): /opt/capvac-chatbot/deploy.sh
set -e
cd "$(dirname "$0")"
echo "=== pulling latest from GitHub ==="
git pull --ff-only
echo "=== rebuilding + restarting app (caddy untouched) ==="
docker compose up -d --build app
echo "=== done ==="
docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null || docker compose ps
