#!/bin/bash
# Catch-up wrapper: if a scheduled retrain was MISSED (e.g. machine was off at
# its scheduled time), run it now.
#
# Scheduled EVERY day (see crontab: 01:00 and 09:00).  It checks the timestamp
# of the last successful retrain (state file logs/.last_retrain, written by
# monthly_retrain.sh).  If the last successful retrain is >= MAX_AGE_DAYS old,
# the previous scheduled run was missed -> trigger monthly_retrain.sh now.
#
# 2026-09-18: retrain cadence changed from "1st/15th monthly" to EVERY 2 DAYS
# (to reproduce/validate the daily-pred fix faster).  MAX_AGE_DAYS default is
# therefore 2 -- the catchup IS the scheduler now; it fires as soon as the last
# successful retrain is >= 2 days old.  Raise it back for a monthly cadence.
#
# Plan B (2026-09-07): a missed retrain is re-run in the DEFAULT mode of
# monthly_retrain.sh, i.e. APPEND (only the newest window is retrained - quick).
# Full 27-window rebuilds are manual (MODE=full), never scheduled here.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

MAX_AGE_DAYS="${MAX_AGE_DAYS:-2}"
STATE="${STATE:-$HERE/logs/.last_retrain}"
LOCK="${LOCK:-$HERE/logs/.retrain.lock}"

mkdir -p "$(dirname "$STATE")"

# Re-entrancy guard: multiple triggers (host 30-min task, WSL cron, .profile)
# may fire at once; never let two catch-up retrains run concurrently.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "another catch-up already running (lock $LOCK held) -> skip"
    exit 0
fi

if [[ ! -f "$STATE" ]]; then
    echo "state=$STATE missing (first run) -> initialize as stale so a catch-up is considered"
    echo "20260101 00:00:00" > "$STATE"
fi

LAST=$(head -1 "$STATE" | cut -d' ' -f1)
LAST=$(printf '%s' "$LAST" | tr -d '-')
LAST_SECONDS=$(date -d "${LAST:0:8}" '+%s')
NOW_SECONDS=$(date '+%s')
AGE_DAYS=$(( (NOW_SECONDS - LAST_SECONDS) / 86400 ))

echo "$(date '+%Y%m%d %H:%M:%S') last=$LAST age=${AGE_DAYS}d max_ok=$MAX_AGE_DAYS"

if (( AGE_DAYS >= MAX_AGE_DAYS )); then
    echo "missed periodic retrain (age=${AGE_DAYS}d) -> starting catch-up now"
    bash "$HERE/monthly_retrain.sh" || {
        echo "catch-up retrain FAILED"
        exit 1
    }
    echo "catch-up retrain done"
else
    echo "age within limit, nothing to catch up"
fi
