"""
Full workflow: update data → rolling backtest → sync to real trading.

Usage:
    # Full pipeline (train models, backtest, sync)
    python run_workflow.py --api-key snf81kqdvb07xgcymu6hi4wterza2jo9

    # Skip training (reuse existing models for ensemble + backtest)
    python run_workflow.py --api-key snf81kqdvb07xgcymu6hi4wterza2jo9 --skip-train

    # Stop after backtest (no trading sync)
    python run_workflow.py --skip-train --no-sync

    # Control what fraction of total assets to invest (default 0.95 = 95%)
    python run_workflow.py --api-key snf81kqdvb07xgcymu6hi4wterza2jo9 --invest-ratio 0.8

    # Only sync (skip data update and training)
    python run_workflow.py --api-key snf81kqdvb07xgcymu6hi4wterza2jo9 --sync-only
"""
import os
os.environ["TQDM_DISABLE"] = "1"

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def step_header(step: int, title: str):
    print(f"\n{'=' * 60}")
    print(f"  STEP {step}: {title}")
    print(f"{'=' * 60}\n")


def run(cmd: list, step_name: str) -> bool:
    print(f"  Running: {' '.join(str(a) for a in cmd)}")
    t0 = time.time()
    result = subprocess.run(cmd, cwd=HERE)
    elapsed = time.time() - t0
    if result.returncode == 0:
        print(f"  [{step_name}] Done in {elapsed:.1f}s\n")
        return True
    print(f"  [{step_name}] FAILED (exit code {result.returncode})")
    return False


def main(
    api_key: str = "",
    host: str = "192.168.11.244",
    port: int = 7648,
    exp_name: str = "rolling_csi300_lgbm",
    skip_train: bool = False,
    sync: bool = True,
    sync_only: bool = False,
    dry_run: bool = False,
    invest_ratio: float = 0.95,
):
    steps = []

    if sync_only:
        # ---- Step 1 (only): sync ----
        step_header(1, "Sync to real trading (skip data & training)")
        cmd = [
            sys.executable, "sync_to_realtime.py",
            "--host", host,
            "--port", str(port),
            "--api-key", api_key,
            "--exp-name", exp_name,
            "--invest-ratio", str(invest_ratio),
        ]
        if dry_run:
            cmd += ["--dry-run", "True"]
        else:
            cmd += ["--dry-run", "False"]
        ok = run(cmd, "sync")
        if not ok:
            sys.exit(1)
        return

    # ---- Step 1: Update data ----
    step_header(1, "Update qlib data from baostock")
    ok = run([sys.executable, "update_baostock.py"], "update_baostock")
    if not ok:
        print("WARNING: Data update failed. Continuing anyway...")

    # ---- Step 2: Rolling backtest ----
    step_header(2, "Rolling backtest")
    cmd = [sys.executable, "run_rolling.py"]
    if skip_train:
        cmd.append("--skip_train")
    ok = run(cmd, "run_rolling")
    if not ok:
        print("ERROR: Rolling backtest failed.")
        sys.exit(1)

    # ---- Step 3: Sync to real trading ----
    if sync:
        step_header(3, "Sync to real trading")
        cmd = [
            sys.executable, "sync_to_realtime.py",
            "--host", host,
            "--port", str(port),
            "--api-key", api_key,
            "--exp-name", exp_name,
            "--invest-ratio", str(invest_ratio),
        ]
        if dry_run:
            cmd += ["--dry-run", "True"]
        else:
            cmd += ["--dry-run", "False"]
        ok = run(cmd, "sync")
        if not ok:
            print("ERROR: Sync failed.")
            sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"  Workflow complete!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    import fire
    fire.Fire(main)
