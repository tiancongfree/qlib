"""
Rolling Retraining for LightGBM on CSI300 (2020-2024).

Training window expands over time (ROLL_EX):
  - Initial train: 2008-2018, valid: 2019, test starts at 2020
  - Each step: test window slides forward by ~60 trading days (~3 months)
  - Training window expands to include more recent data
  - Model is retrained from scratch each step

This addresses the "model staleness" issue found in the static model,
where IC degraded from 0.036 (2020) to 0.011 (2023).
"""

import os
import sys
from pathlib import Path

# Avoid importing local qlib source (which has uncompiled C extensions)
if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import mlflow
from ruamel.yaml import YAML
from qlib import auto_init
from qlib.data import D
from qlib.contrib.rolling.base import Rolling
from qlib.utils.pickle_utils import add_safe_class
import custom_handler  # noqa: F401 - register handler for pickle
from save_positions import save_positions_to_csv

# Register custom classes so subprocesses can deserialize them
add_safe_class("custom_handler", "Alpha158Momentum")
add_safe_class("custom_handler", "Alpha158Industry")
add_safe_class("custom_handler", "IndustryProcessor")
add_safe_class("custom_handler", "DvRatioProcessor")
add_safe_class("custom_handler", "Alpha158DvRatio")
add_safe_class("custom_handler", "VolatilityTimingStrategy")
add_safe_class("custom_handler", "IndustryCappedStrategy")

DEFAULT_CONF = Path(__file__).parent / "rolling_config.yaml"


def _find_latest_rolling_exp(handler_class: str = None) -> str:
    """Find the most recent rolling_models_* experiment compatible with the handler.

    Parameters
    ----------
    handler_class : str, optional
        Expected handler class name (e.g. ``Alpha158Industry``). If given, only
        experiments whose runs reference a handler cache of this class are
        considered, preventing --skip_train from reusing models trained with a
        different handler.
    """
    mlruns_dir = Path(__file__).parent / "mlruns"
    if not mlruns_dir.exists():
        return None
    client = mlflow.tracking.MlflowClient(tracking_uri=str(mlruns_dir))
    rolling_exps = [
        e for e in client.search_experiments()
        if e.name.startswith("rolling_models_")
    ]
    if not rolling_exps:
        return None
    # Filter by handler class if requested
    if handler_class is not None:
        compatible = []
        for e in rolling_exps:
            runs = client.search_runs([e.experiment_id])
            if not runs:
                continue
            # All runs in a rolling experiment share the same handler param
            handler_param = runs[0].data.params.get("dataset.kwargs.handler", "")
            if handler_class in handler_param:
                compatible.append(e)
        rolling_exps = compatible
    if not rolling_exps:
        return None
    # Use the one with the latest creation time
    rolling_exps.sort(key=lambda e: e.creation_time or 0)
    return rolling_exps[-1].name


def _expected_handler_class() -> str:
    """Return the handler class name of the current config."""
    with CONF_PATH.open("r") as f:
        yaml = YAML(typ="safe", pure=True)
        conf = yaml.load(f)
    return conf["task"]["dataset"]["kwargs"]["handler"]["class"]


def main(skip_train: bool = False, conf: str = None, exp_name: str = "rolling_csi300_lgbm"):
    """Run rolling training + backtest.

    Parameters
    ----------
    skip_train : bool
        If True, skip model training and only re-run ensemble + backtest
        (useful when models are already trained and you only want to re-export
        positions or re-run portfolio analysis with different parameters).
    conf : str, optional
        Path to the rolling config yaml. Defaults to ``rolling_config.yaml``.
    exp_name : str
        Name of the output (combined) mlflow experiment. Defaults to
        ``rolling_csi300_lgbm``.
    """
    global CONF_PATH
    CONF_PATH = Path(conf) if conf else DEFAULT_CONF
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    # Use latest available date as config end_time
    cal = D.calendar()
    test_end = str(cal[-1].date()) if hasattr(cal[-1], 'date') else str(cal[-1])[:10]
    print(f"  Latest data: {test_end}")

    # Dynamically update config end_time to latest date
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    with CONF_PATH.open("r") as f:
        conf = yaml.load(f)
    for key in ("end_time",):
        conf["data_handler_config"]["end_time"] = test_end
    conf["port_analysis_config"]["backtest"]["end_time"] = test_end
    with CONF_PATH.open("w") as f:
        yaml.dump(conf, f)
    print(f"  Config end_time updated to {test_end}")

    # Clean previous rolling experiments
    mlruns = Path(__file__).parent / "mlruns"
    if not mlruns.exists():
        os.makedirs(mlruns, exist_ok=True)

    # When skipping training, reuse existing rolling_models experiment
    rolling_exp = None
    if skip_train:
        handler_class = _expected_handler_class()
        rolling_exp = _find_latest_rolling_exp(handler_class=handler_class)
        if rolling_exp is None:
            print("ERROR: No existing rolling_models_* experiment found. Run without --skip_train first.")
            sys.exit(1)
        # Remove the old combined experiment so we can write a fresh one
        try:
            from qlib.workflow import R
            R.delete_exp(experiment_name=exp_name)
        except Exception:
            pass
        print(f"  Reusing rolling experiment: {rolling_exp}")

    rolling = Rolling(
        conf_path=CONF_PATH,
        step=60,         # ~3 months per rolling window (quarterly retraining)
        horizon=20,      # 20-day prediction horizon
        exp_name=exp_name,
        rolling_exp=rolling_exp,
        test_end=test_end,
    )

    print("=" * 60)
    print(f"  Rolling Retraining: LightGBM + {Path(CONF_PATH).stem} on CSI300")
    print("=" * 60)
    print(f"  Step: 60 trading days (~quarterly)")
    print(f"  Horizon: 20 days")
    print(f"  Mode: Expanding window (ROLL_EX)")
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

    print("\nDone! Results saved in mlruns/")

    # Save daily positions to CSV
    save_positions_to_csv(exp_name=exp_name)


if __name__ == "__main__":
    import fire
    fire.Fire(main)
