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
#   1. update qlib data from baostock/akshare
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

echo "=============================================="
echo "Monthly rolling retrain started: $(date)"
echo "=============================================="

# ---- 1. update data ----
echo ""
echo "[1/5] Updating qlib data..."
timeout 900 "$PY" update_baostock.py || echo "  WARN: data update failed, continuing with existing data"

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
        echo "retrain swap already active: $RETRAIN_SWAP"
        return
    fi
    if [[ -f "$RETRAIN_SWAP" ]] && sudo -n swapon "$RETRAIN_SWAP" 2>/dev/null; then
        SWAP_DID_ON=1
        SZ=$(swapon --show=NAME,SIZE --bytes --ifexists 2>/dev/null | awk -v s="$RETRAIN_SWAP" '$1==s{printf "%.1fGB", $2/1e9}')
        echo "enabled retrain swap: $RETRAIN_SWAP ($SZ)"
    else
        echo "WARN: could not enable retrain swap $RETRAIN_SWAP - training may OOM"
    fi
}

disable_retrain_swap() {
    if [[ -n "$SWAP_DID_ON" ]] && swapon_has "$RETRAIN_SWAP"; then
        sudo -n swapoff "$RETRAIN_SWAP" 2>/dev/null || true
        echo "retrain swap deactivated: $RETRAIN_SWAP"
    fi
}

# ---- 2. retrain (append-only by default) ----
echo ""
if [[ "$MODE" == "append" ]]; then
    echo "[2/5] APPEND: retraining ONLY the newest rolling window..."
else
    echo "[2/5] FULL: retraining all rolling windows..."
fi
trap 'disable_retrain_swap' EXIT   # tear swap back down no matter how the run ends
enable_retrain_swap
"$PY" run_rolling.py --mode "$MODE" --exp-name "$EXP"

# ---- 3. locate (reused/appended or newly-created) rolling experiment ----
echo ""
echo "[3/5] Locating rolling experiment..."
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
# combined pred last date
"$PY" - << 'PY'
import pandas as pd, glob, os, mlflow
client = mlflow.tracking.MlflowClient()
comb = client.get_experiment_by_name("rolling_csi300_lgbm_ndrop1")
runs = client.search_runs([comb.experiment_id], order_by=["attributes.start_time"])
latest = runs[-1]
f = f"mlruns/{comb.experiment_id}/{latest.info.run_id}/artifacts/pred.pkl"
p = pd.read_pickle(f)
print(f"  combined pred last date: {p.index.get_level_values('datetime').max().date()}")
PY

# ---- 4. bundle rolling experiment + pred, scp to 244 ----
echo ""
echo "[4/5] Bundling rolling experiment + pred -> 244..."
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
import mlflow
client = mlflow.tracking.MlflowClient()
e = client.get_experiment_by_name("rolling_csi300_lgbm_ndrop1")
runs = client.search_runs([e.experiment_id], order_by=["attributes.start_time"])
print(runs[-1].info.run_id)
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
echo "[5/5] Extracting on 244..."
cat > "$TMP/deploy.sh" << 'EOF'
#!/bin/bash
cd /home/tc/qlib/examples/rolling_csi300
cp /mnt/c/Users/tc/Desktop/rolling_exp.tar.gz /tmp/ 2>/dev/null
cp /mnt/c/Users/tc/Desktop/combined_pred.tar.gz /tmp/ 2>/dev/null
tar xzf /mnt/c/Users/tc/Desktop/rolling_exp.tar.gz -C . 
tar xzf /mnt/c/Users/tc/Desktop/combined_pred.tar.gz -C .
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
echo "Monthly retrain done: $(date)"
echo "Log: $LOG"
echo "=============================================="

# Record last successful retrain time (for monthly_retrain_catchup.sh)
STATE="${STATE:-$LOG_DIR/.last_retrain}"
printf '%s %s\n' "$(date '+%Y%m%d %H:%M:%S')" "$(date '+%Y-%m-%d %H:%M')" > "$STATE"
echo "Wrote last-retrain state: $STATE"
echo "$(cat "$STATE")"
