"""
attr_daily_tracking.py -- 实盘 vs qlib 目标「当日跟踪误差」金额归因 (元口径).

用途: 每当发现 qlib HTML 日收益 与 实盘真实日收益 对不上时, 跑这个脚本把差异拆成:
  (a) 高价股缺配 (整手化买不进)
  (b) 共同持仓权重漂移 (整手取整)
  (c) 残差 (停牌/口径/舍入)
并顺带判断当日是否零成交 (若是, 则 sync 速度与差异无关).

数据源 (read-only):
  - 实盘账户: easyths (host 必须 192.168.11.244:7648, WSL2 独立网络)
  - qlib 目标权重: mlruns/<exp>/<rid>/artifacts/portfolio_analysis/positions_normal_1day.pkl

运行 (必须在 examples/rolling_csi300/ 下, 因 pkl 含 custom_handler 类):
  python attr_daily_tracking.py --date 2026-09-11 --exp rolling_csi300_lgbm_ndrop1

依赖: easyths 服务在 244 Windows 上运行; 本机跑需能连到 192.168.11.244:7648.
"""
import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path.cwd()  # 必须在 examples/rolling_csi300/ 下运行 (mlruns 相对 cwd)
sys.path.insert(0, str(HERE))
os.chdir(HERE)

EASYTHS_HOST = "192.168.11.244"
EASYTHS_PORT = 7648


def to_qlib_code(code):
    """broker 6位代码 -> qlib instrument (SH/SZ 前缀)."""
    code = str(code).zfill(6)
    return ("SH" if code[0] == "6" or code[0] == "5" else "SZ") + code


def load_positions_pkl(exp_name, date):
    """返回该日 qlib target: dict instrument -> weight (相对总资产), 及 recorder id."""
    from qlib import auto_init
    from qlib.workflow import R

    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")
    os.environ.setdefault("MLFLOW_TRACKING_URI", str(HERE / "mlruns"))
    exp = R.get_exp(experiment_name=exp_name)
    best = None
    for rid in exp.list_recorders():
        p = Path("mlruns") / exp.id / rid / "artifacts" / "portfolio_analysis" / "positions_normal_1day.pkl"
        if not p.exists():
            continue
        try:
            d = pd.read_pickle(p)
            if d and max(d.keys()) > (best[0] if best else pd.Timestamp.min):
                best = (max(d.keys()), rid, d)
        except Exception:
            continue
    if best is None:
        raise RuntimeError(f"no positions pkl for exp {exp_name}")
    _, rid, d = best
    dd = pd.Timestamp(date)
    if dd not in d:
        raise RuntimeError(f"{date} not in positions pkl (max {max(d.keys()).date()})")
    pos = d[dd].position
    wq = {k: float(v["weight"]) for k, v in pos.items() if isinstance(v, dict)}
    return wq, rid


def query_account():
    """读实盘账户 (read-only). 返回 funds dict + list of holding dicts."""
    from easyths import TradeClient

    c = TradeClient(host=EASYTHS_HOST, port=EASYTHS_PORT)
    with c:
        f = c.query_funds().get("data", {})
        h = c.query_holdings(return_type="json")
        data = h.get("data", {})
        hl = data.get("holdings", data) if isinstance(data, dict) else data
    return f, hl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="归因交易日, e.g. 2026-09-11")
    ap.add_argument("--exp", default="rolling_csi300_lgbm_ndrop1")
    args = ap.parse_args()

    # ---- qlib target weights ----
    wq, rid = load_positions_pkl(args.exp, args.date)
    print(f"[qlib] exp={args.exp} recorder={rid[:8]} target={len(wq)} names")

    # ---- actual account ----
    funds, holdings = query_account()
    a_total = float(funds.get("总资产", 0))
    mv_eod_total = float(funds.get("股票市值", 0))
    cash = float(funds.get("资金余额", 0))
    print(f"[account] total={a_total:,.0f} mv={mv_eod_total:,.0f} cash={cash:,.0f} "
          f"utilization={mv_eod_total/a_total:.1%}")

    # parse holdings; r = 当日盈亏比(%)/100 (close-to-close ground truth)
    actual = {}
    tot_buy = tot_sell = 0.0
    for p in holdings:
        s = to_qlib_code(p.get("证券代码"))
        mv = float(p.get("市值", 0) or 0)
        r = float(p.get("当日盈亏比(%)", 0) or 0) / 100.0
        actual[s] = {"mv_eod": mv, "r": r, "mv_start": mv / (1 + r)}
        tot_buy += float(p.get("当日买入", 0) or 0)
        tot_sell += float(p.get("当日卖出", 0) or 0)
    actual_pnl = sum(v["mv_eod"] - v["mv_start"] for v in actual.values())
    print(f"[account] holdings={len(actual)} 当日买入={tot_buy:.0f} 当日卖出={tot_sell:.0f} "
          f"-> {'ZERO TRADE (sync速度无关)' if tot_buy == 0 and tot_sell == 0 else 'HAS TRADES'}")

    # ---- returns for qlib-only names from qlib data ----
    from qlib import auto_init
    from qlib.data import D
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")
    prev = (pd.Timestamp(args.date) - pd.Timedelta(days=1))
    missing = [s for s in wq if s not in actual]
    r_miss = {}
    if missing:
        df = D.features(missing, ["$close"], start_time=str(prev.date()),
                        end_time=args.date)["$close"].unstack(level=0)
        for s in missing:
            try:
                r_miss[s] = float(df.loc[args.date, s] / df.loc[prev, s] - 1)
            except Exception:
                r_miss[s] = np.nan

    # ---- qlib target P&L on actual capital ----
    q_pnl = 0.0
    for s, w in wq.items():
        r = actual[s]["r"] if s in actual else r_miss.get(s, np.nan)
        if np.isnan(r):
            r = 0.0
        q_pnl += a_total * w * r

    diff = actual_pnl - q_pnl

    # (a) missing names
    miss_pnl = sum(a_total * wq[s] * (r_miss.get(s, 0.0) or 0.0) for s in missing)
    # (b) weight drift on common names
    common = [s for s in wq if s in actual]
    dev_pnl = sum((actual[s]["mv_start"] / a_total - wq[s]) * a_total * actual[s]["r"] for s in common)
    resid = diff - (-miss_pnl) - dev_pnl

    print("\n================ 金额归因 (元) ================")
    print(f"  qlib 目标 P&L on 实盘资金 : {q_pnl:>12,.0f}")
    print(f"  实盘实际 P&L              : {actual_pnl:>12,.0f}")
    print(f"  差异 (actual - qlib)      : {diff:>12,.0f}")
    print(f"    (a) 高价股缺配 (没吃到) : {-miss_pnl:>12,.0f}  ({-miss_pnl/diff*100 if diff else 0:.0f}%)")
    print(f"    (b) 共同持仓权重漂移    : {dev_pnl:>12,.0f}  ({dev_pnl/diff*100 if diff else 0:.0f}%)")
    print(f"    (c) 残差(停牌/口径/舍入): {resid:>12,.0f}")

    if missing:
        print("\n--- 缺配股明细 ---")
        for s in sorted(missing, key=lambda x: -abs(a_total * wq[x] * (r_miss.get(x, 0) or 0))):
            r = r_miss.get(s, np.nan)
            print(f"  {s}: w={wq[s]:.2%} r={r:+.2%} 本应盈亏={a_total*wq[s]*(r if not np.isnan(r) else 0):+,.0f}")

    print("\n--- 权重漂移 top (共同持仓) ---")
    dev_rows = []
    for s in common:
        wa = actual[s]["mv_start"] / a_total
        c = (wa - wq[s]) * a_total * actual[s]["r"]
        dev_rows.append((s, wq[s], wa, wa - wq[s], actual[s]["r"], c))
    dev_rows.sort(key=lambda x: x[5])
    for s, wq_i, wa, dev, r, c in dev_rows[:10]:
        print(f"  {s}: w_qlib={wq_i:.2%} w_actual={wa:.2%} dev={dev:+.2%} r={r:+.2%} -> {c:+,.0f}")
    print(f"  ... (共 {len(common)} 只, 合计 {dev_pnl:+,.0f})")


if __name__ == "__main__":
    main()
