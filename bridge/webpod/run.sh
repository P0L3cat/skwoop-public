#!/bin/bash
# launchd entry for the web pod sidecar: same env file and venv as the bridge.
cd "$(dirname "$0")/.."
set -a
. "$HOME/.skwoop/env"
set +a
exec ./.venv/bin/python -m webpod.server
