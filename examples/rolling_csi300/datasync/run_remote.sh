#!/bin/bash
# 244-side entrypoint invoked over ssh as:
#   wsl -u tc bash /mnt/c/Users/tc/qlib_datasync/run_remote.sh <args...>
# Keeps the ssh command free of nested quotes (Windows cmd mangles them).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-/home/tc/qlib/.venv/bin/python}"
exec "$PY" -u "$HERE/remote_pack.py" "$@"
