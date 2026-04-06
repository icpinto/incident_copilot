#!/usr/bin/env bash
set -euo pipefail
python apps/cli/main.py diagnose --repo-root "$1" --compose-path "$2" --log-file "$3"
