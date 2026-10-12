#!/bin/bash
# Monthly (Plan B: default APPEND) retrain for the rolling CSI300 strategy.
#
# Runs on the RESEARCH machine (本机, 训练机).  After retraining, pushes the new
# rolling_models experiment + combined pred to the trading box (244) so daily
# inference / backtest / sync there uses the fresh model.
#
# Schedule: monthly on the 1st and 15th (see crontab).  Also ~daily catch-up via
# monthly_retrain_catchup.sh.
#
# Plan B (2026-09-07): by default we run run_rolling.py in APPEND mode - only the
# newest rolling window (2008->now, expand-only) is retrained and APPENDED to the
# existing rolling_models_* experiment; older windows stay frozen.  Live target
# only depends on the newest window, so this is the only work actually needed to
# refresh the model.  A full rebuild (retrain every window to refresh the whole
# 2020->today report) is MANUAL:  MODE=full bash monthly_retrain.sh
#
# Flow (APPEND):
#   1. sync qlib data FROM the serving machine (244), health-gated both ends
#      (datasync/sync_from_244.sh).  Fallback = local update_baostock.py.
#   2. append newest window (run_rolling.py --mode append) into latest
#      rolling_models_*, re-ensemble combined pred to latest date
#   3. bundle the reused rolling experiment + updated combined pred
#   4. scp to 244 Windows desktop -> move into WSL qlib dir
#   5. extract on 244 (cache pkl only rebundled on full / when it changed)
#
# NOTE: 244 deploy path uses git bundle for code (daily_predict.py etc.), and
# scp for data (mlruns experiments + cache pkl).  Update AGENTS.md if changed.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY=/home/tc/qlib/.venv/bin/python
# Unbuffered child output: the boot-time "silent death" loses block-buffered
# stdout when the process is signal-killed, so Python must flush every line.
export PYTHONUNBUFFERED=1
SSH_KEY=~/.ssh/id_ed25519_new
REMOTE="tc@192.168.11.244"
EXP=rolling_csi300_lgbm_ndrop1

# MODE: append (default, Plan B) or full (manual - retrain every window).
MODE="${MODE:-append}"
if [[ "$MODE" != "append" && "$MODE" != "full" ]]; then
    echo "Invalid MODE '$MODE' (expected append|full)"; exit 2
fi

LOG_DIR=/home/tc/qlib/examples/rolling_csi300/logs
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/monthly_retrain_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

# Lightweight stage marker.  Written to a dedicated file (NOT through the tee
# pipe), so the last-reached step survives even when the process is
# signal-killed mid-step and the tee buffer is lost.  This is what lets us
# pinpoint the boot-time "silent death" (2026-09/10: every fresh-boot retrain
# died right at the in-run `sudo swapon /swapfile19G` with zero log output).
STAGE_FILE="$LOG_DIR/.retrain_stage"
stage() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" > "$STAGE_FILE" 2>/dev/null || true
    echo "$*"
}

# Signal diagnostics: the boot-time deaths had NO guest OOM/segfault and the
# EXIT trap never ran -> an external signal killed us.  Record which one and the
# process context so the next occurrence is diagnosable.
on_signal() {
    local ctx
    ctx="ppid=$(ps -o ppid= -p $$ 2>/dev/null) pgid=$(ps -o pgid= -p $$ 2>/dev/null) sid=$(ps -o sid= -p $$ 2>/dev/null)"
    printf '%s RECEIVED SIGNAL %s (%s) log=%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$ctx" "$LOG" > "$STAGE_FILE" 2>/dev/null || true
    echo ""
    echo "!! monthly_retrain RECEIVED SIGNAL $1 at $(date) ($ctx) log=$LOG"
    exit 128
}
trap 'on_signal HUP' HUP
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal QUIT' QUIT

# Scratch dir for the deploy bundles; always removed on exit (see trap below).
TMP=""

echo "=============================================="
stage "Monthly rolling retrain started: $(date)"
echo "=============================================="

# ---- 1. sync data from 244 (health-gated), fallback to local update ----
echo ""
stage "[1/5] Syncing qlib data from 244 (health-gated)..."
# The training machine's own baostock download crawls (~1 stock/s) and was killed
# by the old `timeout 900` BEFORE the akshare fallback (which only runs after a
# full baostock pass) -> data stuck ~2 weeks behind.  244 updates nightly with no
# timeout and is the authoritative fresh copy, so pull from there instead.  The
# sync health-checks 244 first and our data after; any failure leaves local data
# untouched and we fall back to the old local update.
if bash "$HERE/datasync/sync_from_244.sh"; then
    stage "  data synced from 244"
else
    stage "  WARN: 244 sync failed or 244 unhealthy -> falling back to local update_baostock.py"
    timeout 900 "$PY" update_baostock.py || echo "  WARN: data update failed, continuing with existing data"
fi

# ---- 1b. pull the serving-machine served tail (append guard) ----
# The append path refuses to run when this (training) machine's combined pred tail
# lags the last date the serving machine already served (2026-09-28 incident:
# a manual append on the training machine rewrote 09-21..24 that 244 had served).
# We pull 244's authoritative logs/.served_tail so the guard compares against the
# true serving tail.  Non-fatal: if the pull fails we keep the existing local file.
echo ""
stage "[1b] Pulling served-tail marker from 244 (append guard)..."
SERVED_TAIL_REMOTE='/mnt/c/Users/tc/qlib_served_tail'
if ssh -i "$SSH_KEY" -o ConnectTimeout=10 "$REMOTE" \
        "wsl -u tc bash -lc 'cp /home/tc/qlib/examples/rolling_csi300/logs/.served_tail /mnt/c/Users/tc/qlib_served_tail 2>/dev/null'" \
        >/dev/null 2>&1; then
    if scp -i "$SSH_KEY" -o ConnectTimeout=10 \
            "$REMOTE:/C:/Users/tc/qlib_served_tail" "$LOG_DIR/.served_tail" \
            >/dev/null 2>&1; then
        echo "  pulled served tail: $(cat "$LOG_DIR/.served_tail" 2>/dev/null)"
    else
        echo "  WARN: could not scp served tail from 244 (keeping local)"
    fi
else
    echo "  WARN: could not read served tail on 244 (keeping local)"
fi

# ---- retrain swap window (on-demand, torn down when retrain exits) ----
# The heavy run_rolling.py train peaks ~13GB anon in one subprocess.  The host
# has only a 4GiB /dev/sdc swap active by default, which is not enough -> OOM
# (BrokenProcessPool, see AGENTS 2026-09-07).  We do NOT keep this swapfile
# always-on (it would reserve ~20GB disk permanently); instead we activate it
# for the duration of this retrain run and deactivate before it exits.
RETRAIN_SWAP=${RETRAIN_SWAP:-/swapfile19G}
SWAP_DID_ON=""

swapon_has() { swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$1"; }

enable_retrain_swap() {
    if swapon_has "$RETRAIN_SWAP"; then
        stage "retrain swap already active: $RETRAIN_SWAP"
        return
    fi
    # Mark BEFORE the swapon call: the boot-time deaths happened exactly here
    # (the stage file still records this line even if we are killed mid-call).
    stage "activating retrain swap: $RETRAIN_SWAP"
    if [[ -f "$RETRAIN_SWAP" ]] && sudo -n swapon "$RETRAIN_SWAP" 2>/dev/null; then
        SWAP_DID_ON=1
        SZ=$(swapon --show=NAME,SIZE --bytes --ifexists 2>/dev/null | awk -v s="$RETRAIN_SWAP" '$1==s{printf "%.1fGB", $2/1e9}' || true)
        stage "enabled retrain swap: $RETRAIN_SWAP ($SZ)"
    else
        stage "WARN: could not enable retrain swap $RETRAIN_SWAP - training may OOM"
    fi
}

disable_retrain_swap() {
    if [[ -n "$SWAP_DID_ON" ]] && swapon_has "$RETRAIN_SWAP"; then
        sudo -n swapoff "$RETRAIN_SWAP" 2>/dev/null || true
        echo "retrain swap deactivated: $RETRAIN_SWAP"
    fi
}

cleanup_retrain() {
    disable_retrain_swap
    if [[ -n "$TMP" && -d "$TMP" ]]; then
        rm -rf "$TMP"
    fi
}

# ---- 2. retrain (append-only by default) ----
echo ""
if [[ "$MODE" == "append" ]]; then
    stage "[2/5] APPEND: retraining ONLY the newest rolling window..."
else
    stage "[2/5] FULL: retraining all rolling windows..."
fi
trap 'cleanup_retrain' EXIT   # tear swap + scratch dir down no matter how the run ends
enable_retrain_swap
"$PY" run_rolling.py --mode "$MODE" --exp-name "$EXP"

# ---- 3. locate (reused/appended or newly-created) rolling experiment ----
echo ""
stage "[3/5] Locating rolling experiment..."
# Prefer the experiment whose pred-bearing run count is the highest (an interrupted
# full retrain leaves only a few window preds and must not win over the exp we just
# appended to).  Tie-break -> newest creation.  This mirrors run_rolling's
# _find_latest_rolling_exp so both the trainer and the deploy bundler agree.
NEW_EXP=$("$PY" - << 'PY'
import mlflow, pathlib, os
client = mlflow.tracking.MlflowClient()
def completed(run_dir):
    a = pathlib.Path(run_dir) / "artifacts" / "pred.pkl"
    return a.exists()
exps = [e for e in client.search_experiments()
        if e.name.startswith("rolling_models_") and e.lifecycle_stage == "active"]
best = None
for e in exps:
    runs = client.search_runs([e.experiment_id])
    if not runs:
        continue
    base = pathlib.Path("mlruns") / e.experiment_id
    n = sum(1 for r in runs if completed(base / r.info.run_id))
    if best is None or n > best["n"] or (n == best["n"] and (e.creation_time or 0) > (best["e"].creation_time or 0)):
        best = {"e": e, "n": n}
if best is None:
    raise SystemExit("no active rolling_models_* experiment found")
print(best["e"].name)
PY
)
echo "  chosen rolling experiment: $NEW_EXP"
export NEW_EXP   # needed by step [4/5] python (os.environ lookup)
# combined pred last date (select the run with the FURTHEST tail, not newest
# start_time: daily_predict.py now writes the daily-appended tail into a new run,
# and an ensemble run created later may carry a shorter tail)
"$PY" - << 'PY'
import pandas as pd, glob, os, mlflow, pathlib
client = mlflow.tracking.MlflowClient()
comb = client.get_experiment_by_name("rolling_csi300_lgbm_ndrop1")
runs = client.search_runs([comb.experiment_id], order_by=["attributes.start_time"])
best = None
for r in runs:
    f = f"mlruns/{comb.experiment_id}/{r.info.run_id}/artifacts/pred.pkl"
    if not os.path.exists(f):
        continue
    p = pd.read_pickle(f)
    if isinstance(p, pd.DataFrame):
        p = p.iloc[:, 0]
    last = p.index.get_level_values("datetime").max()
    if best is None or last > best[0]:
        best = (last, r.info.run_id)
print(f"  combined pred last date: {best[0].date()} (run {best[1][:12]})")
PY

# ---- 4. bundle rolling experiment + pred, scp to 244 ----
echo ""
stage "[4/5] Bundling rolling experiment + pred -> 244..."
EXP_ID=$("$PY" << PY
import mlflow, os
client = mlflow.tracking.MlflowClient()
e = client.get_experiment_by_name(os.environ["NEW_EXP"])
if e is None:
    raise SystemExit(f"experiment {os.environ['NEW_EXP']} not found")
print(e.experiment_id)
PY
)
echo "  rolling exp id: $EXP_ID"

# bundle rolling experiment (43MB) and combined experiment pred
COMB_ID=$("$PY" - << 'PY'
import mlflow
client = mlflow.tracking.MlflowClient()
e = client.get_experiment_by_name("rolling_csi300_lgbm_ndrop1")
print(e.experiment_id)
PY
)
RUN_ID=$("$PY" - << 'PY'
import mlflow, os, pandas as pd
client = mlflow.tracking.MlflowClient()
e = client.get_experiment_by_name("rolling_csi300_lgbm_ndrop1")
runs = client.search_runs([e.experiment_id], order_by=["attributes.start_time"])
best = None
for r in runs:
    f = f"mlruns/{e.experiment_id}/{r.info.run_id}/artifacts/pred.pkl"
    if not os.path.exists(f):
        continue
    p = pd.read_pickle(f)
    if isinstance(p, pd.DataFrame):
        p = p.iloc[:, 0]
    last = p.index.get_level_values("datetime").max()
    if best is None or last > best[0]:
        best = (last, r.info.run_id)
print(best[1])
PY
)

TMP=/tmp/retrain_deploy_$$
mkdir -p "$TMP"
tar czf "$TMP/rolling_exp.tar.gz" "mlruns/$EXP_ID"
tar czf "$TMP/combined_pred.tar.gz" "mlruns/$COMB_ID/$RUN_ID"
# handler cache (5.4GB) only needs re-syncing on a FULL rebuild (a new handler cache
# was written there).  APPEND keeps reusing the existing cache -> skip the big scp.
if [[ "$MODE" == "full" ]]; then
    CACHE=$(ls Alpha158Industry.*.pkl | head -1)
    echo "  cache (full rebuild): $CACHE"
    SEND_CACHE=1
else
    SEND_CACHE=0
    echo "  cache: unchanged on append (skipping 5.4GB scp)"
fi

# scp to 244 windows desktop
scp -i "$SSH_KEY" "$TMP/rolling_exp.tar.gz" "$TMP/combined_pred.tar.gz" "$REMOTE:/C:/Users/tc/Desktop/" || echo "  WARN: scp experiments failed"
if [[ "$SEND_CACHE" == "1" ]]; then
    # cache is big; scp separately (about 1-3 min on LAN)
    scp -i "$SSH_KEY" "$CACHE" "$REMOTE:/C:/Users/tc/Desktop/" || echo "  WARN: scp cache failed"
fi

# ---- 5. extract on 244 (WSL) ----
echo ""
stage "[5/5] Extracting on 244..."
cat > "$TMP/deploy.sh" << 'EOF'
#!/bin/bash
cd /home/tc/qlib/examples/rolling_csi300
cp /mnt/c/Users/tc/Desktop/rolling_exp.tar.gz /tmp/ 2>/dev/null
cp /mnt/c/Users/tc/Desktop/combined_pred.tar.gz /tmp/ 2>/dev/null
tar xzf /mnt/c/Users/tc/Desktop/rolling_exp.tar.gz -C . 
tar xzf /mnt/c/Users/tc/Desktop/combined_pred.tar.gz -C .
# remove the scratch copies so they don't accumulate in /tmp on the trading box
rm -f /tmp/rolling_exp.tar.gz /tmp/combined_pred.tar.gz
if ls /mnt/c/Users/tc/Desktop/Alpha158Industry.*.pkl >/dev/null 2>&1; then
    cp /mnt/c/Users/tc/Desktop/Alpha158Industry.*.pkl .
    md5sum Alpha158Industry.*.pkl
fi
echo "--- 244 deployed rolling experiment(s) ---"
ls -d mlruns/$(grep -l "rolling_models_" mlruns/*/meta.yaml | head -1 | sed 's|mlruns/||;s|/meta.yaml||') 2>/dev/null || true
EOF
scp -i "$SSH_KEY" "$TMP/deploy.sh" "$REMOTE:/C:/Users/tc/Desktop/deploy_retrain.sh" > /dev/null
timeout 120 ssh -i "$SSH_KEY" "$REMOTE" "wsl -u tc bash /mnt/c/Users/tc/Desktop/deploy_retrain.sh" || echo "  WARN: 244 deploy step failed (check manually)"

echo ""
echo "=============================================="
echo "Monthly rolling retrain finished: $(date)"
echo "Log: $LOG"
echo "=============================================="

# ---- health guard: only mark success if artifacts are actually fresh/usable ----
# "exited 0" is NOT proof of success: earlier steps tolerate failures with
# `|| echo WARN` (data update, scp, 244 deploy) and a silent OOM death writes
# nothing at all.  Checks that the newest window trained, the combined pred+label
# exist, pred tail reaches the calendar, and indices align; on failure it writes
# logs/.retrain_health.json and emails an alert, and we DO NOT advance the state
# so monthly_retrain_catchup.sh retries on its next scheduled run.
echo ""
stage "[guard] Verifying retrain artifacts..."
if "$PY" retrain_guard.py --exp "$EXP"; then
    STATE="${STATE:-$LOG_DIR/.last_retrain}"
    printf '%s %s\n' "$(date '+%Y%m%d %H:%M:%S')" "$(date '+%Y-%m-%d %H:%M')" > "$STATE"
    stage "Wrote last-retrain state: $(cat "$STATE")"
    echo ""
    echo "=============================================="
    echo "Monthly retrain done (guard passed): $(date)"
    echo "=============================================="
else
    echo "HEALTH GUARD FAILED -> NOT writing $LOG_DIR/.last_retrain (catch-up will retry)"
    exit 1
fi
