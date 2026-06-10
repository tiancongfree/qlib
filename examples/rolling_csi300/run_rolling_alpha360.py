"""
Rolling Retraining for LightGBM on CSI300 with Alpha360 features (2020-2024).

Alpha360 extends Alpha158 with 360 features — adding longer historical
price windows (past 60 days of OHLCV unfolded day by day).

This script runs a rolling retraining identical to run_rolling.py but
with Alpha360 instead of Alpha158, enabling direct comparison.

Usage:
    python run_rolling_alpha360.py
"""
import os
import sys
from pathlib import Path

# Avoid importing local qlib source (which has uncompiled C extensions)
if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

from qlib import auto_init
from qlib.contrib.rolling.base import Rolling
from save_positions import save_positions_to_csv

CONF_PATH = Path(__file__).parent / "rolling_config_alpha360.yaml"


def main(skip_train: bool = False):
    """Run rolling training + backtest with Alpha360 features.

    Parameters
    ----------
    skip_train : bool
        If True, skip model training and only re-run ensemble + backtest
        (useful when models are already trained and you only want to re-export
        positions or re-run portfolio analysis with different parameters).
    """
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    # Clean previous Alpha360 rolling experiments
    mlruns = Path(__file__).parent / "mlruns_alpha360"
    if not mlruns.exists():
        os.makedirs(mlruns, exist_ok=True)

    rolling = Rolling(
        conf_path=CONF_PATH,
        step=60,         # ~3 months per rolling window (quarterly retraining)
        horizon=20,      # 20-day prediction horizon
        exp_name="rolling_csi300_lgbm_alpha360",
    )

    print("=" * 60)
    print("  Rolling Retraining: LightGBM + Alpha360 on CSI300")
    print("=" * 60)
    print(f"  Step: 60 trading days (~quarterly)")
    print(f"  Horizon: 20 days")
    print(f"  Mode: Expanding window (ROLL_EX)")
    print(f"  Features: Alpha360 (360 features)")
    print("=" * 60)

    if skip_train:
        print("\n  [SKIP TRAIN] Models already trained. Only running ensemble + backtest.")
        print("=" * 60)
        rolling._ens_rolling()
        rolling._update_rolling_rec()
    else:
        task_list = rolling.get_task_list()
        print(f"\nTotal rolling tasks to train: {len(task_list)}")
        for i, t in enumerate(task_list):
            segs = t["dataset"]["kwargs"]["segments"]
            print(f"  Task {i+1}: train {segs['train']}, valid {segs['valid']}, test {segs['test']}")

        print("\nStarting rolling training...")
        rolling.run()

    print("\nDone! Results saved in mlruns_alpha360/")

    # Save daily positions to CSV
    save_positions_to_csv(exp_name="rolling_csi300_lgbm_alpha360")


if __name__ == "__main__":
    import fire
    fire.Fire(main)
