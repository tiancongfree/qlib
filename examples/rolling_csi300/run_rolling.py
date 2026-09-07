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
from rolling_append import AppendRolling
from save_positions import save_positions_to_csv

# Register custom classes so subprocesses can deserialize them
add_safe_class("custom_handler", "Alpha158Momentum")
add_safe_class("custom_handler", "Alpha158Industry")
add_safe_class("custom_handler", "Alpha158Earnings")
add_safe_class("custom_handler", "IndustryProcessor")
add_safe_class("custom_handler", "DvRatioProcessor")
add_safe_class("custom_handler", "Alpha158DvRatio")
add_safe_class("custom_handler", "VolatilityTimingStrategy")
add_safe_class("custom_handler", "IndustryCappedStrategy")
add_safe_class("custom_handler", "MinTradeValueStrategy")
add_safe_class("custom_handler", "ICTimingStrategy")

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

    def _exp_completed_runs(e) -> int:
        """Number of runs in experiment `e` that have actually produced a
        pred.pkl artifact (i.e. a fully trained rolling window).  A rolling
        retrain is only 'complete' when every window turned into a pred; an
        aborted/interrupted train leaves several windows without pred.pkl."""
        if os.path.isdir(Path(mlruns_dir) / e.experiment_id):
            done = 0
            for run in client.search_runs([e.experiment_id]):
                if os.path.exists(
                    Path(mlruns_dir) / e.experiment_id / run.info.run_id / "artifacts" / "pred.pkl"
                ):
                    done += 1
            return done
        return 0

    rolling_exps = [
        e for e in client.search_experiments()
        if e.name.startswith("rolling_models_") and e.lifecycle_stage == "active"
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

    # Favour a *complete* experiment: one whose pred-bearing run count reaches the
    # maximum seen across candidate experiments (an interrupted retrain leaves a
    # few windows without pred.pkl, well below the full window count).  Picking the
    # newest-by-creation-time complete experiment avoids ensembling on a partial /
    # aborted rolling_models_* that would only cover early dates.
    counted = [(e, _exp_completed_runs(e)) for e in rolling_exps]
    max_done = max(c[1] for c in counted) if counted else 0
    complete = [e for e, n in counted if max_done and n >= max_done]
    if complete:
        complete.sort(key=lambda e: e.creation_time or 0)
        chosen = complete[-1]
    else:
        rolling_exps.sort(key=lambda e: e.creation_time or 0)
        chosen = rolling_exps[-1]
        print(f"WARNING: no fully-trained rolling experiment found; falling back to "
              f"{chosen.name} with fewer than a complete set of window preds.")
    return chosen.name


def _expected_handler_class() -> str:
    """Return the handler class name of the current config."""
    with CONF_PATH.open("r") as f:
        yaml = YAML(typ="safe", pure=True)
        conf = yaml.load(f)
    return conf["task"]["dataset"]["kwargs"]["handler"]["class"]


def _latest_combined_pred(exp_name: str):
    """Return the pred of the newest run of the combined experiment, or None.

    In the daily flow, daily_predict.py appends new-day predictions to this run's
    pred.pkl so that it extends past the last rolling-test window.  Returning it
    lets the skip-train path merge those extra dates back into the freshly
    re-ensembled pred instead of dropping them (which would freeze the target).
    """
    import pandas as _pd
    from pathlib import Path as _Path
    mlruns_dir = _Path(__file__).parent / "mlruns"
    client = mlflow.tracking.MlflowClient(tracking_uri=str(mlruns_dir))
    try:
        exp = client.get_experiment_by_name(exp_name)
        if exp is None:
            return None
        runs = client.search_runs([exp.experiment_id], order_by=["attributes.start_time"])
        if not runs:
            return None
        run = runs[-1]
        pred_path = mlruns_dir / exp.experiment_id / run.info.run_id / "artifacts" / "pred.pkl"
        if not pred_path.exists():
            return None
        return _pd.read_pickle(pred_path)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"WARNING: could not load existing combined pred for merge: {exc}")
        return None


def main(skip_train: bool = False, mode: str = "append", conf: str = None,
         exp_name: str = "rolling_csi300_lgbm"):
    """Run rolling training + backtest.

    Parameters
    ----------
    skip_train : bool
        If True, skip model training and only re-run ensemble + backtest
        (useful when models are already trained and you only want to re-export
        positions or re-run portfolio analysis with different parameters).
    mode : str
        One of "append" (default) or "full".
        - full   : the old behaviour - retrain EVERY rolling window from scratch
                   (all ~27 wrapping the 2020->today path). Use to refresh the full
                   2020->today equity/IC/backtest report.
        - append : only train the NEWEST rolling window (expand-only), reusing the
                   existing rolling_models_* experiment without deleting/recreating
                   it. Old windows' pred stay frozen and keep the historical backtest
                   continuous. Live target only depends on the newest window, so this
                   is sufficient for daily serving (see AppendRolling).
        Ignored when skip_train=True.
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

    # Resolve the rolling_models_* experiment.  append mode (like skip_train) MUST
    # target an already-existing experiment, because it adds the newest window to the
    # previously-trained windows rather than rebuilding all of them from scratch.
    rolling_exp = None
    if skip_train or (not skip_train and mode == "append"):
        handler_class = _expected_handler_class()
        rolling_exp = _find_latest_rolling_exp(handler_class=handler_class)
        if rolling_exp is None:
            print("ERROR: No existing rolling_models_* experiment found."
                  + (" Run without --skip_train first." if skip_train
                     else " Run one full (--mode full) retrain first, then appends "
                          "(--mode append) can extend it."))
            sys.exit(1)
        print(f"  Reusing rolling experiment: {rolling_exp}")

    if not skip_train and mode == "full":
        # full rebuild -> start fresh, do not reuse stale windows
        rolling_exp = None

    _RollingCls = Rolling if mode == "full" else AppendRolling
    rolling = _RollingCls(
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
        # IMPORTANT (frozen-pred fix): do NOT delete the combined experiment. The
        # latest combined-run pred.pkl is extended daily by daily_predict.py to the
        # latest trading day.  Re-ensembling from the rolling (training) models alone
        # would reset the pred back to the last rolling test window (the retrain
        # date) and throw away those daily-apended new dates -> the backtest target
        # freezes and live rebalancing stops.  Instead we merge the rolling ensemble
        # with any existing (already-extended) combined pred (extra_pred) so the
        # combined pred keeps advancing.
        tail_pred = _latest_combined_pred(exp_name)
        rolling._ens_rolling(extra_pred=tail_pred)
        rolling._update_rolling_rec()
    else:
        if mode == "append":
            # Plan B: retrain only the newest rolling window (expand-only), appending
            # to the existing rolling_models_* experiment.  The newest window's pred
            # is what the live target uses, so this is the only work actually needed
            # to refresh the model each month; the older windows stay frozen and keep
            # the 2020->today historical report continuous.
            task_list = rolling.get_task_list()
            print(f"\nTotal tasks if full: {len(task_list)} | append: "
                  f"1 (newest window only)")
            last = task_list[-1]
            segs = last["dataset"]["kwargs"]["segments"]
            print(f"  Appending window: train {segs['train']}, "
                  f"valid {segs['valid']}, test {segs['test']}")
            rolling.train_append()
            # Re-ensemble with any existing (already daily-extended) combined pred so
            # the daily/appended tail is preserved (frozen-pred fix).  train_append()
            # only trains; it does NOT ensemble, so do it here.
            tail_pred = _latest_combined_pred(exp_name)
            rolling._ens_rolling(extra_pred=tail_pred)
            rolling._update_rolling_rec()
        else:  # full -> old behaviour: train every window + ensemble + report.
            task_list = rolling.get_task_list()
            print(f"\nTotal rolling tasks to train: {len(task_list)}")
            for i, t in enumerate(task_list):
                segs = t["dataset"]["kwargs"]["segments"]
                print(f"  Task {i+1}: train {segs['train']}, "
                      f"valid {segs['valid']}, test {segs['test']}")
            print("\nStarting rolling training...")
            rolling.run()  # internally ensembles + updates rolling rec for full

    print("\nDone! Results saved in mlruns/")

    # Save daily positions to CSV
    save_positions_to_csv(exp_name=exp_name)


if __name__ == "__main__":
    import fire
    fire.Fire(main)
