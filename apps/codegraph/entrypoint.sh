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
    echo "Waiting for exclusive graph access; stop the gitnexus service before refreshing."
    exec flock --exclusive --no-fork /state/refresh.lock node /opt/codegraph/prepare.mjs
    ;;
  serve)
    if [ -z "${GITNEXUS_MCP_AUTH_TOKEN:-}" ]; then
      echo "Set IC_GITNEXUS_AUTH_TOKEN before starting the code-graph profile." >&2
      exit 1
    fi
    # Readers cannot overlap a refresh, including another preparation container.
    exec flock --shared --no-fork /state/refresh.lock sh -c 'node /opt/codegraph/prepare.mjs --check && exec gitnexus mcp --http --host 0.0.0.0 --port 3111'
    ;;
  *) echo "Usage: prepare | serve" >&2; exit 2 ;;
esac
