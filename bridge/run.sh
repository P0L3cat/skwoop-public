#!/usr/bin/env bash
# Start the bridge with secrets from ~/.skwoop/env (never from this repo).
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE="${SKWOOP_ENV_FILE:-$HOME/.skwoop/env}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy .env.example there, chmod 600)"; exit 1; }
set -a; . "$ENV_FILE"; set +a
exec ./.venv/bin/python skwoop_bridge.py
