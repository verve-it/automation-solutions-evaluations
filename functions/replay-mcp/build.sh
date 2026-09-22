#!/usr/bin/env bash
# Thin wrapper. The build itself is build.py, so that bash and PowerShell run
# the same code rather than two implementations that drift.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/build.py" "$@"
