#!/bin/sh
# Compatibility entry point for the six-column Python audit-log helper.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$script_dir/log.py" "$@"
