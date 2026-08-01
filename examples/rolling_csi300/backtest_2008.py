import warnings; warnings.filterwarnings('ignore')
import sys, pickle
sys.path.insert(0, '.')
sys.path.insert(0, '/home/tc/qlib')

import pandas as pd
from qlib import auto_init
from qlib.utils import init_instance_by_config
from qlib.backtest import backtest, executor
from qlib.contrib.evaluate import risk_analysis
from qlib.contrib.strategy import TopkDropoutStrategy
from qlib.backtest.signal import Signal

auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

pred = pd.read_pickle('/tmp/opencode/pred2008.pkl')
print("pred range:", pred.index.get_level_values('datetime').min(), "->", pred.index.get_level_values('datetime').max(), "rows:", len(pred))

start = '2008-01-02'
end = '2008-12-31'

from qlib.backtest.signal import SignalWCache
signal = SignalWCache(pred)
strategy = TopkDropoutStrategy(signal=signal, topk=30, n_drop=1)

exch_kwargs = {
    "close_cost": 0.0015,
    "open_cost": 0.0005,
    "min_cost": 5,
    "deal_price": "close",
    "limit_threshold": 0.095,
}
executor_config = {
    "class": "SimulatorExecutor",
    "module_path": "qlib.backtest.executor",
    "kwargs": {
        "time_per_step": "day",
        "generate_portfolio_metrics": True,
    },
}
print("Running backtest 2008...")
portfolio_metric_dict, indicator_dict = backtest(
    start, end,
    strategy,
    executor_config,
    benchmark="SH000300",
    account=1000000,
    exchange_kwargs=exch_kwargs,
)
print("Done.")

report_normal, positions_normal = portfolio_metric_dict.get("1day", (None, None))
if report_normal is None:
    report_normal = portfolio_metric_dict[list(portfolio_metric_dict.keys())[0]][0]
    positions_normal = portfolio_metric_dict[list(portfolio_metric_dict.keys())[0]][1]

report_normal.to_pickle('/tmp/opencode/report2008.pkl')
print("report saved, rows:", len(report_normal))

analysis = dict()
for name, ind in indicator_dict.items():
    analysis[name] = {"risk": risk_analysis(report_normal[["return"]], freq="day")}

import numpy as np

acct = report_normal["account"]
final = acct.iloc[-1]
n = len(report_normal)
cum = final / acct.iloc[0]
ann = cum ** (252 / n) - 1
bench = report_normal["bench"]
bench_cum = (1 + bench).cumprod().iloc[-1]
bench_ann = bench_cum ** (252 / n) - 1
mdd = (acct / acct.cummax() - 1).min()
turnover = report_normal["turnover"].mean() * 252
cost = report_normal["cost"].sum()

print("=" * 50)
print("2008 Backtest Result (model trained 2008-2018, predicting 2008 - look-ahead)")
print("=" * 50)
print(f"Final account:   {final:,.0f}  (start 1,000,000)")
print(f"Cumulative:      {cum-1:+.2%}")
print(f"Annualized:      {ann:+.2%}")
print(f"Benchmark ann:   {bench_ann:+.2%}")
print(f"Max drawdown:    {mdd:.2%}")
print(f"Turnover/yr:     {turnover:.0f}")
print(f"Total cost:      {cost:,.0f}")
# yearly
rep = report_normal.copy()
rep['cum'] = (1+rep['return']).cumprod()
print("\nYearly returns (strategy / bench):")
for y in rep.index.year.unique():
    m = rep.index.year == y
    sr = (1+rep.loc[m,'return']).prod()-1
    br = (1+rep.loc[m,'bench']).prod()-1
    print(f"  {y}: strategy {sr:+.2%}  bench {br:+.2%}")
