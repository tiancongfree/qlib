"""
Analyze the auction-filter observation log to measure win rate.

Reads auction_observe_log.csv (date/code/auction_buy_strength/retail_sell_strength
/would_cut/held), computes each stock's NEXT-Day forward return from qlib day
bins, and compares the outcome of the would-be-cut group vs the kept group.

The article's claim: stocks ranked at the BOTTOM by auction_buy_strength (weak
open) tend to underperform. So a good filter would have would_cut=True stocks
ending up with LOWER forward returns than the kept ones.

Forward return horizon defaults to the next trading day (off the open price,
matching the "buy at open, sell at close" intent), but can be set to N days.

Usage:
    python auction_win_rate.py                               # default 1-day
    python auction_win_rate.py --horizon 3 --top_cut 5
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from auction_factors import _read_bin_field

QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"
DEFAULT_LOG = Path(__file__).parent / "auction_observe_log.csv"


def load_calendar() -> list:
    return [l.strip() for l in open(QLIB_DIR / "calendars/day.txt")]


def forward_return(inst: str, date: str, cal_size: int, horizon: int, cal_idx: dict) -> float | None:
    """Return horizon-day forward return from the OPEN on `date`.

    Uses the open-to-close (/cut) path actually traded: open[date+t] - open[date]
    for the auction-decided names. qlib stores adjusted prices so returns are
    split/dividend adjusted (consistent for both groups).
    """
    bin_start, opn = _read_bin_field(inst, "open")
    if opn.size == 0:
        return None
    i = cal_idx.get(date)
    if i is None:
        return None
    rel = i - bin_start
    if rel < 0 or rel + horizon >= len(opn):
        return None
    p0 = opn[rel]
    p1 = opn[rel + horizon]
    if not np.isfinite(p0) or not np.isfinite(p1) or p0 <= 0:
        return None
    return float(p1 / p0 - 1.0)


def _summary(group: pd.DataFrame, label: str, horizon: int) -> dict:
    r = group["fwd_return"].dropna()
    return {
        "label": label,
        "n": len(r),
        "n_orig": len(group),
        "win_rate": float((r > 0).mean()),
        "mean_fwd": float(r.mean()),
        "median_fwd": float(r.median()),
    }


def main(log: str = str(DEFAULT_LOG), horizon: int = 1, top_cut: int = 5,
         retail_exempt_threshold: float = None):
    if not Path(log).exists():
        print(f"观察日志不存在: {log}")
        print("先运行 sync (观察模式) 积累样本后再分析。")
        return

    df = pd.read_csv(log)
    df["fwd_return"] = None

    cal = load_calendar()
    cal_idx = {d: i for i, d in enumerate(cal)}

    # 用 obs 中出现的最大日期对齐下一个交易日: 取每个 (date,code) 独立算
    dates = sorted(df["date"].astype(str).unique())
    cnt = 0
    for inst, date in zip(df["code"], df["date"].astype(str)):
        r = forward_return(inst, date, len(cal), horizon, cal_idx)
        df.loc[(df["code"] == inst) & (df["date"].astype(str) == date), "fwd_return"] = r
        if r is not None:
            cnt += 1
    df = df[df["fwd_return"].notna()].copy()
    print(f"样本: {len(df)} 行 (有下{horizon}日收益), 覆盖日期 {dates[0] if dates else '?'} ~ {dates[-1] if dates else '?'}")

    # 简化: 假设 top_cut 与运行时一致, 直接按 would_cut 分组。
    # (若运行时用了豁免, 这里可选再套 retail_exempt_threshold 剔除豁免股)
    if retail_exempt_threshold is not None:
        df = df[df["retail_sell_strength"] <= retail_exempt_threshold]

    cut_group = df[df["would_cut"] == True]
    keep_group = df[df["would_cut"] == False]

    print(f"\n{'=' * 60}")
    print(f"  集合竞价因子胜率分析 (前向 {horizon} 日, open->open)")
    print(f"{'=' * 60}")
    rows = []
    for g, lab in ((cut_group, "裁剪(would_cut=T)"), (keep_group, "保留(would_cut=F)")):
        s = _summary(g, lab, horizon)
        rows.append(s)
        print(f"\n  [{lab}]  n={s['n_orig']} (有收益 {s['n']})")
        print(f"    胜率(收益>0):  {s['win_rate']:.1%}")
        print(f"    平均前向收益:  {s['mean_fwd']:+.4f}")
        print(f"    中位数前向收益: {s['median_fwd']:+.4f}")

    if len(rows) == 2 and rows[0]["n"] and rows[1]["n"]:
        delta = rows[1]["mean_fwd"] - rows[0]["mean_fwd"]
        print(f"\n  超额: 保留组 − 裁剪组 = {delta:+.4f}")
        print(f"  结论方向: {'过滤器有效 (被裁股确实弱)' if delta > 0 else '过滤器反着做 (被裁股反而强)'}")

    # 可选: 高/低因子分组前向收益对比
    if len(df):
        q = df["auction_buy_strength"].rank(pct=True)
        low = df[q <= 0.2]
        high = df[q >= 0.8]
        print(f"\n{'=' * 60}")
        print(f"  按因子分位切分 (纯因子视角, 非 must-cut):")
        for g, lab in ((low, "因子最低20%"), (high, "因子最高20%")):
            if len(g):
                s = _summary(g, lab, horizon)
                print(f"  [{lab}] n={s['n_orig']} 胜率 {s['win_rate']:.1%}  均 {s['mean_fwd']:+.4f}")


if __name__ == "__main__":
    fire_cli = argparse.ArgumentParser()
    fire_cli.add_argument("--log", default=str(DEFAULT_LOG))
    fire_cli.add_argument("--horizon", type=int, default=1)
    fire_cli.add_argument("--top-cut", type=int, default=5)
    fire_cli.add_argument("--retail-exempt-threshold", type=float, default=None)
    args = fire_cli.parse_args()
    main(log=args.log, horizon=args.horizon, top_cut=args.top_cut,
         retail_exempt_threshold=args.retail_exempt_threshold)