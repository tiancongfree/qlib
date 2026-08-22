"""IC health monitor for the rolling CSI300 strategy.

Computes the same RankIC the ICTiming strategy uses (forward 20-day return,
shifted 20 trading days for causality) and reports the trailing ``ic_window``-day
mean.  If the realized IC has been below ``ic_low`` for ``min_bad_days`` days in a
row, it prints a WARNING (the strategy's own ICTiming de-risking will already be
cutting exposure, but a sustained factor failure may warrant a pause / retrain).

Designed to run daily right after ``daily_predict.py`` on the trading box.

Usage:
    python monitor_ic.py                                   # IC from latest combined pred
    python monitor_ic.py --exp-name rolling_csi300_lgbm_ndrop1
    python monitor_ic.py --window 60 --ic-low 0.03
"""
import warnings
warnings.filterwarnings("ignore")

import sys
from pathlib import Path

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import numpy as np
import pandas as pd
import mlflow
from qlib import auto_init
from qlib.data import D

DEFAULT_EXP = "rolling_csi300_lgbm_ndrop1"


def main(
    exp_name: str = DEFAULT_EXP,
    window: int = 60,
    ic_low: float = 0.03,
    min_bad_days: int = 10,
):
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    client = mlflow.tracking.MlflowClient()
    comb = client.get_experiment_by_name(exp_name)
    if comb is None:
        print(f"[monitor_ic] experiment {exp_name} not found")
        return 1
    runs = client.search_runs([comb.experiment_id], order_by=["attributes.start_time"])
    latest = runs[-1]
    pred_path = Path("mlruns") / comb.experiment_id / latest.info.run_id / "artifacts" / "pred.pkl"
    if not pred_path.exists():
        print(f"[monitor_ic] no pred.pkl at {pred_path}")
        return 1
    pred = pd.read_pickle(pred_path)
    if isinstance(pred, pd.DataFrame):
        pred = pred.iloc[:, 0]

    # ---- forward 20-day returns (same as ICTiming._precompute_ic) ----
    cal = D.calendar()
    start = str(cal[0].date())
    end = str(cal[-1].date())
    close = D.features(D.instruments("csi300"), ["$close"], start_time=start, end_time=end)
    pct_back = close["$close"].groupby(level="instrument").pct_change(-20)
    fwd = -pct_back / (1 + pct_back)
    fwd = fwd.to_frame("fwd")

    p = pred.to_frame("score")
    m = p.join(fwd, how="inner").dropna()

    def _daily_ic(g):
        if len(g) < 5:
            return np.nan
        return g["score"].corr(g["fwd"], method="spearman")

    daily = m.groupby(level="datetime").apply(_daily_ic).dropna().sort_index()
    if len(daily) < 30:
        print(f"[monitor_ic] only {len(daily)} IC points, too early to assess.")
        return 0

    # causal: IC at date t uses close[t+20]; only usable 20 days later
    usable = daily.shift(20).dropna()
    if len(usable) < window:
        print(f"[monitor_ic] only {len(usable)} usable IC points (< window {window}).")
        usable_window = usable
    else:
        usable_window = usable.iloc[-window:]

    mean_ic = usable_window.mean()
    last_ic = usable_window.iloc[-1]
    # consecutive bad days
    bad_run = 0
    for v in reversed(usable_window):
        if v < ic_low:
            bad_run += 1
        else:
            break

    print(f"[monitor_ic] trailing {len(usable_window)}d mean RankIC = {mean_ic:.4f}")
    print(f"[monitor_ic] last usable IC = {last_ic:.4f} (date {usable_window.index[-1].date()})")
    print(f"[monitor_ic] consecutive days below {ic_low}: {bad_run}")

    if bad_run >= min_bad_days and mean_ic < ic_low:
        print(f"[monitor_ic] WARNING: sustained low IC "
              f"({bad_run}d below {ic_low}, mean {mean_ic:.4f}). "
              f"Consider pausing the strategy or forcing a retrain.")
        return 2
    print("[monitor_ic] IC healthy.")
    return 0


if __name__ == "__main__":
    import fire
    fire.Fire(main)
