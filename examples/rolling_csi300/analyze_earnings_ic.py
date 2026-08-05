"""
Analyze earnings-momentum factors: daily Rank IC by year + ICIR stability.

Checks whether each factor has stable cross-period alpha (the key rejection
criterion for dvratio was style-beta: strong in 2022-2024, negative in 2025-26).
Here we require IC to be consistently positive in BOTH 2020-2024 (in-sample)
and 2025-2026 (out-of-sample).

Usage:
    python analyze_earnings_ic.py [--factor np_accel] [--all]
"""
import warnings
warnings.filterwarnings("ignore")
import sys
sys.path.insert(0, ".")
import numpy as np
import pandas as pd
import qlib
from qlib.constant import REG_CN
from qlib.data import D

qlib.init(provider_uri="~/.qlib/qlib_data/cn_data", region=REG_CN)

FACTORS = ["np_accel", "qprof_accel", "roe_chg", "margin_chg", "eps_yoy"]
LABEL_EXPR = "$close/Ref($close,20)-1"  # 20-day forward return


def load_factor_series(factor: str):
    inst = D.instruments("csi300")
    df = D.features(inst, [f"${factor}", LABEL_EXPR],
                    start_time="2019-01-01", end_time="2026-07-31")
    df.columns = ["factor", "fwd_ret"]
    df = df.dropna()
    return df


def daily_ic(df):
    ics = []
    for dt, grp in df.groupby(level="datetime"):
        if len(grp) < 10:
            continue
        ic = grp["factor"].corr(grp["fwd_ret"], method="spearman")
        if np.isfinite(ic):
            ics.append((dt, ic))
    s = pd.Series(dict(ics)).sort_index()
    s.index = pd.to_datetime(s.index)
    return s


def main(factor: str = None, all_factors: bool = False):
    facs = FACTORS if (all_factors or factor is None) else [factor]
    print("=" * 70)
    print("  Earnings-Momentum Factor IC Analysis")
    print("=" * 70)
    for fac in facs:
        df = load_factor_series(fac)
        if df.empty:
            print(f"\n{fac}: NO DATA")
            continue
        ic = daily_ic(df)
        # yearly stats
        yearly = ic.groupby(ic.index.year).agg(["mean", "std", "count"])
        yearly["ICIR"] = yearly["mean"] / yearly["std"]
        print(f"\n=== {fac} (n_days={len(ic)}, overall IC={ic.mean():.4f}, ICIR={ic.mean()/ic.std():.2f}) ===")
        for yr, row in yearly.iterrows():
            if int(yr) >= 2020:
                print(f"  {yr}: IC={row['mean']:+.4f}  ICIR={row['ICIR']:+.2f}  n={int(row['count'])}")
        # in-sample (2020-2024) vs OOS (2025-2026)
        ins = ic[(ic.index.year >= 2020) & (ic.index.year <= 2024)]
        oos = ic[ic.index.year >= 2025]
        if len(ins) and len(oos):
            print(f"  INS 2020-24: IC={ins.mean():+.4f} ICIR={ins.mean()/ins.std():+.2f}  |  "
                  f"OOS 2025-26: IC={oos.mean():+.4f} ICIR={oos.mean()/oos.std():+.2f}")
            verdict = "PASS" if ins.mean() > 0.005 and oos.mean() > 0.005 else "FAIL(stable alpha)"
            print(f"  >> {verdict}")
        # coverage
        print(f"  平均每日覆盖股票数: {df.groupby(level=0).size().mean():.0f}")
    print("\n=== DONE ===")


if __name__ == "__main__":
    import fire
    fire.Fire(main)
