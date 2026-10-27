#!/bin/sh
set -eu
export GITNEXUS_REPO_ROOT="${GITNEXUS_REPO_ROOT:-/repositories}"
export GITNEXUS_MCP_READ_ONLY=1
export GITNEXUS_MCP_ALLOWED_REPOS="$GITNEXUS_REPO_ROOT"
# Exact trusted mount; no global Git configuration is written into the checkout.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="$GITNEXUS_REPO_ROOT"
mkdir -p /state/home
case "${1:-serve}" in
  prepare)
    export GITNEXUS_STORAGE_PATH=/state/index
    echo "Waiting for exclusive graph access; stop the gitnexus service before refreshing."
    exec flock --exclusive --no-fork /state/refresh.lock node /opt/codegraph/prepare.mjs
    ;;
  serve)
    # A second container must never write to this volume concurrently.
    exec flock --exclusive --no-fork /state/refresh.lock node /opt/codegraph/manager.mjs
    ;;
  *) echo "Usage: prepare | serve" >&2; exit 2 ;;
esac
