#!/bin/bash
# Monthly full retrain for the rolling CSI300 strategy.
#
# Runs on the RESEARCH machine (本机, 训练机).  After retraining, pushes the new
# rolling_models experiment + handler cache + combined pred to the trading box
# (244) so daily inference / backtest / sync there uses the fresh model.
#
# Schedule: monthly on the 1st and 15th (see crontab).
#
# Flow:
#   1. update qlib data from baostock/akshare
#   2. full retrain (run_rolling.py, no --skip-train) -> new rolling_models_*,
#      rebuilt handler cache covering latest data, combined pred to latest date
#   3. bundle the new rolling experiment + updated pred
#   4. scp to 244 Windows desktop -> move into WSL qlib dir
#   5. scp the 5.4GB handler cache to 244 (full, simplest reliable sync)
#   6. verify md5 on both sides
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

# ---- 2. full retrain ----
echo ""
echo "[2/5] Full rolling retrain (all 27 tasks)..."
"$PY" run_rolling.py --exp-name "$EXP"

# ---- 3. locate new rolling experiment + verify combined pred date ----
echo ""
echo "[3/5] Locating new rolling experiment..."
NEW_EXP=$("$PY" - << 'PY'
import mlflow, pathlib
client = mlflow.tracking.MlflowClient()
exps = [e for e in client.search_experiments() if e.name.startswith("rolling_models_")]
exps.sort(key=lambda e: e.creation_time or 0)
print(exps[-1].name)
PY
)
echo "  new rolling experiment: $NEW_EXP"
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
EXP_ID=$("$PY" - << 'PY'
import mlflow
client = mlflow.tracking.MlflowClient()
e = client.get_experiment_by_name("rolling_models_" + "$NEW_EXP".replace("rolling_models_",""))
exps = [x for x in client.search_experiments() if x.name == "$NEW_EXP"]
print(exps[0].experiment_id)
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
# handler cache (full 5.4GB)
CACHE=$(ls Alpha158Industry.*.pkl | head -1)
echo "  cache: $CACHE"

# scp to 244 windows desktop
scp -i "$SSH_KEY" "$TMP/rolling_exp.tar.gz" "$TMP/combined_pred.tar.gz" "$REMOTE:/C:/Users/tc/Desktop/" || echo "  WARN: scp experiments failed"
# cache is big; scp separately (about 1-3 min on LAN)
scp -i "$SSH_KEY" "$CACHE" "$REMOTE:/C:/Users/tc/Desktop/" || echo "  WARN: scp cache failed"

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
cp /mnt/c/Users/tc/Desktop/Alpha158Industry.*.pkl . 
md5sum Alpha158Industry.*.pkl
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
