"""样本外验证: 科技倾斜选股 (TechTiltICTimingStrategy) 分段回测。

用法:
  python oos_techtilt.py --start 2020-01-01 --end 2024-12-31 --tilts 0,0.5,0.75,1.0,1.5
  python oos_techtilt.py --start 2025-01-01 --end 2026-07-31 --tilts 0,0.75
"""
import warnings; warnings.filterwarnings("ignore")
import argparse, os, sys
from pathlib import Path
import pickle

import numpy as np
import pandas as pd

import mlflow
from mlflow.tracking import MlflowClient

SELF_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(SELF_DIR))
import custom_handler  # noqa: F401

from qlib import auto_init
from qlib.utils import init_instance_by_config
from qlib.backtest import backtest
from qlib.backtest.signal import SignalWCache
from qlib.contrib.evaluate import risk_analysis

EXPERIMENT_ID = "731471388521802531"  # rolling_csi300_lgbm_ndrop1 ensemble pred


def load_ensemble_pred():
    client = MlflowClient(tracking_uri=str(SELF_DIR / "mlruns"))
    runs = client.search_runs(EXPERIMENT_ID)
    runs.sort(key=lambda r: r.info.end_time or 0)
    path = client.download_artifacts(runs[-1].info.run_id, "pred.pkl")
    pred = pd.read_pickle(path)
    if isinstance(pred, pd.DataFrame):
        pred = pred.iloc[:, 0]
    return pred


EXECUTOR = {
    "class": "SimulatorExecutor",
    "module_path": "qlib.backtest.executor",
    "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True},
}
EXCHANGE = {
    "close_cost": 0.0015,
    "deal_price": "close",
    "limit_threshold": 0.095,
    "min_cost": 5,
    "open_cost": 0.0005,
}


def run_one(pred, tilt, start, end, strategy_class="TechTiltICTimingStrategy", baijiu_weight=None):
    if strategy_class == "TechTiltICTimingStrategy":
        strat_kwargs = {"tilt": float(tilt)}
    else:  # IndustryAdjustStrategy
        strat_kwargs = {"weight_rules": []}
        if tilt != 0:
            strat_kwargs["baijiu_weight"] = float(tilt)
        else:
            strat_kwargs["baijiu_codes"] = []  # disable default baijiu deweight at tilt 0
    kwargs = {
        "n_drop": 1,
        "topk": 30,
        "signal": SignalWCache(signal=pred),
        "ic_high": 0.06,
        "ic_low": 0.04,
        "ic_window": 60,
        "low_risk": 0.5,
    }
    kwargs.update(strat_kwargs)
    strategy = init_instance_by_config(
        {
            "class": strategy_class,
            "module_path": "custom_handler",
            "kwargs": kwargs,
        }
    )
    portfolio_metric_dict, indicator_dict = backtest(
        start_time=pd.Timestamp(start),
        end_time=pd.Timestamp(end),
        strategy=strategy,
        executor=dict(EXECUTOR),
        account=1000000,
        benchmark="SH000300",
        exchange_kwargs=dict(EXCHANGE),
    )
    return portfolio_metric_dict, indicator_dict


def report(pmd, imd):
    freq = "1day"
    report_normal, _ = pmd[freq]
    ind, _ = imd[freq]
    ra = risk_analysis(report_normal["return"] - report_normal["bench"], freq=freq).to_dict()["risk"]
    ra_cost = risk_analysis(
        (report_normal["return"] - report_normal["cost"]) - report_normal["bench"], freq=freq
    ).to_dict()["risk"]
    def g(d, k):
        v = d.get(k)
        return round(float(v), 4) if v is not None else None
    return {
        "毛IR": g(ra, "information_ratio"),
        "毛年化": g(ra, "annualized_return"),
        "毛回撤": g(ra, "max_drawdown"),
        "净IR": g(ra_cost, "information_ratio"),
        "净年化": g(ra_cost, "annualized_return"),
        "净回撤": g(ra_cost, "max_drawdown"),
        "换手": ind.get("turnover", None),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--tilts", default="0,0.5,0.75,1.0,1.5")
    ap.add_argument("--class", dest="strategy_class", default="TechTiltICTimingStrategy")
    args = ap.parse_args()
    tilts = [float(x) for x in args.tilts.split(",")]

    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")
    pred = load_ensemble_pred()
    print(f"pred range: {pred.index.get_level_values(0).min().date()} -> {pred.index.get_level_values(0).max().date()}")
    print(f"回测区间: {args.start} -> {args.end}  策略: {args.strategy_class}")
    print(f"{'tilt':>6} | {'毛年化':>8} {'毛IR':>6} {'毛回撤':>8} | {'净年化':>8} {'净IR':>6} {'净回撤':>8}")
    print("-" * 75)
    for t in tilts:
        pmd, imd = run_one(pred, t, args.start, args.end, strategy_class=args.strategy_class)
        r = report(pmd, imd)
        print(f"{t:>6.2f} | {r['毛年化']*100:>7.2f}% {r['毛IR']:>6.2f} {r['毛回撤']*100:>7.2f}% | "
              f"{r['净年化']*100:>7.2f}% {r['净IR']:>6.2f} {r['净回撤']*100:>7.2f}%")


if __name__ == "__main__":
    main()