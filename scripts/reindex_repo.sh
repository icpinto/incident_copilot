#!/usr/bin/env bash
set -euo pipefail
python apps/cli/main.py reindex --repo-root "$1" --compose-path "$2"
