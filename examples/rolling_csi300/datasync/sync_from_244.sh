#!/bin/bash
# Pull fresh qlib data FROM the serving machine (244) into the training machine's
# qlib data dir, health-gated on both ends:
#
#   1. push helper scripts to 244
#   2. on 244: health-check ITS data, then pack calendars/instruments + feature
#      bins modified since the last sync into a tarball
#   3. pull the tarball, extract over our cn_data dir
#   4. health-check OUR data after the sync
#
# Any failure exits non-zero and LEAVES THE LOCAL DATA UNTOUCHED (the caller,
# monthly_retrain.sh, then falls back to the local baostock update).  This
# replaces the training machine's slow `timeout 900 update_baostock.py`, which
# could never reach the akshare fallback and left data stuck ~2 weeks behind.
#
# Env overrides: DATA_DIR, KEY, REMOTE, RWDIR_WIN, RWDIR_WSL, MARKER, PY.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # .../examples/rolling_csi300/datasync
EXDIR="$(cd "$HERE/.." && pwd)"                          # .../examples/rolling_csi300

DATA_DIR="${DATA_DIR:-$HOME/.qlib/qlib_data/cn_data}"
KEY="${KEY:-$HOME/.ssh/id_ed25519_new}"
REMOTE="${REMOTE:-tc@192.168.11.244}"
RWDIR_WIN="${RWDIR_WIN:-C:/Users/tc/qlib_datasync}"
RWDIR_WSL="${RWDIR_WSL:-/mnt/c/Users/tc/qlib_datasync}"
MARKER="${MARKER:-$EXDIR/logs/.data_sync_marker}"
PY="${PY:-/home/tc/qlib/.venv/bin/python}"

mkdir -p "$EXDIR/logs"
SSH="ssh -i $KEY -o ConnectTimeout=12 -o StrictHostKeyChecking=no"
SCP="scp -i $KEY -o ConnectTimeout=12 -o StrictHostKeyChecking=no"

if [[ -f "$MARKER" ]]; then SINCE=$(cat "$MARKER"); else SINCE=0; fi

TMPTAR="$(mktemp /tmp/qlib_data_incr.XXXXXX.tar.gz)"
cleanup() { rm -f "$TMPTAR"; }
trap cleanup EXIT

echo "[datasync] remote=$REMOTE since=$SINCE data_dir=$DATA_DIR"

# 1. push helpers to 244 (Windows side, where ssh/scp land)
$SSH "$REMOTE" "wsl -u tc mkdir -p $RWDIR_WSL" >/dev/null 2>&1 || true
$SCP "$HERE/data_health.py" "$HERE/remote_pack.py" "$HERE/run_remote.sh" \
    "$REMOTE:/$RWDIR_WIN/" >/dev/null 2>&1 || { echo "[datasync] FAIL: could not push helpers to 244"; exit 1; }

# 2. health-check + pack on 244
set +e
OUT=$($SSH "$REMOTE" \
    "wsl -u tc bash $RWDIR_WSL/run_remote.sh --data-dir /home/tc/.qlib/qlib_data/cn_data --since $SINCE --out $RWDIR_WSL/data_incr.tar.gz" 2>&1)
RC=$?
set -e
echo "$OUT"
if [[ $RC -ne 0 ]]; then
    echo "[datasync] FAIL: 244 health/pack failed (rc=$RC) -> local data untouched"
    exit 1
fi
NEW_MARKER=$(printf '%s\n' "$OUT" | sed -n 's/^DATA_SYNC_MARKER //p' | tail -1)

# 3. pull the tarball
$SCP "$REMOTE:/$RWDIR_WIN/data_incr.tar.gz" "$TMPTAR" >/dev/null 2>&1 \
    || { echo "[datasync] FAIL: could not pull tarball"; exit 1; }

# 4. extract (archive paths are relative to cn_data)
mkdir -p "$DATA_DIR"
tar xzf "$TMPTAR" -C "$DATA_DIR" || { echo "[datasync] FAIL: extract failed"; exit 1; }

# 5. verify local health AFTER the sync
if ! "$PY" "$HERE/data_health.py" --data-dir "$DATA_DIR"; then
    echo "[datasync] FAIL: post-sync local health check failed"
    exit 1
fi

# 6. success: advance the incremental marker to the pack start time
if [[ -n "$NEW_MARKER" ]]; then
    echo "$NEW_MARKER" > "$MARKER"
fi
echo "[datasync] DONE: synced from 244, marker=${NEW_MARKER:-<none>}"
exit 0
