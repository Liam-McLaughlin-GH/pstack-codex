#!/usr/bin/env bash
# Append one safe, single-line row to a local decision log.
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$script_dir/log.py" "$@"
