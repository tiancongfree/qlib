"""
集合竞价短线增强因子 (独立可调用, 未接入 sync_to_realtime 主流程)

实现知乎回答 (question/38812088) 中分享的两个集合竞价因子:

  1. 集合竞价买入强度因子  (auction_buy_strength)
     衡量 9:20-9:25 不可撤单时段内主动买入资金流的相对强度。
        net_buy = Σ(下单价 > 卖一价的买单金额) - Σ(下单价 < 买一价的卖单金额)
        factor  = net_buy / 过去5日平均日成交金额

  2. 散户集合竞价卖出强度因子 (retail_sell_strength)
     散户在集合竞价阶段的净卖出强度 (基于"散户是反向指标"的假设)。
        retail_net_sell = Σ(下单价 < 买一价的散户卖单金额) - Σ(下单价 > 卖一价的散户买单金额)
        factor = retail_net_sell / 过去5日平均日成交金额

数据依赖 (重要):
  原文因子需要 9:20-9:25 逐笔委托明细 (Level-2): 每笔的下单价、买卖一价、
  主动方向、是否散户。当前研究环境没有该数据源 (腾讯快照/tushare 均无逐笔
  委托), 因此本模块只实现"因子计算"纯函数 + qlib 5 日均额标准化, 数据采集
  由外部数据源接入后填充。

用法:
    from auction_factors import compute_auction_factors, load_avg_5d_amount

    avg5d = load_avg_5d_amount(["SH600000", "SZ000001"])
    df = compute_auction_factors(orders_df, avg_5d_amount=avg5d)
    # df.columns: instrument, net_buy, retail_net_sell,
    #             auction_buy_strength, retail_sell_strength
"""

import numpy as np
import pandas as pd

_QLIB_DIR = __import__("pathlib").Path.home() / ".qlib/qlib_data/cn_data"

# 必需的逐笔委托字段 (由外部 L2 数据源提供)
REQUIRED_COLUMNS = ["instrument", "price", "amount"]
OPTIONAL_COLUMNS = ["ask1", "bid1", "side", "is_retail"]


def _read_bin_field(inst: str, field: str) -> tuple[int, np.ndarray]:
    """Read a qlib day bin; returns (start_calendar_idx, values)."""
    path = _QLIB_DIR / "features" / inst.lower() / f"{field}.day.bin"
    if not path.exists():
        return 0, np.array([])
    n = np.fromfile(str(path), dtype="<f4")
    if n.size == 0:
        return 0, np.array([])
    return int(n[0]), n[1:]


def load_avg_5d_amount(instruments: list, end_date: str = None) -> pd.Series:
    """过去5个交易日平均日成交金额 (qlib amount 口径, 与分子同比例可约掉).

    注: qlib 的 amount 是 baostock 成交额经恒定的坐标映射后得到的调整值,
    与真实成交金额相差一个恒定比例。因子是"净流入/5日均额"的比值, 分子
    分母同比例约去, 因此用调整值标准化在数学上等价于用真实金额标准化。
    """
    cal = [l.strip() for l in open(_QLIB_DIR / "calendars/day.txt")]
    cal_idx = {d: i for i, d in enumerate(cal)}
    end = end_date or cal[-1]
    idx = cal_idx.get(end, len(cal) - 1)
    start = max(0, idx - 5)
    window = set(range(start, idx))  # 不含 end 当日

    result = {}
    for inst in instruments:
        bin_start, vals = _read_bin_field(inst, "amount")
        if vals.size == 0:
            result[inst] = np.nan
            continue
        # vals 的索引是相对该股 bin 起始日的偏移, 需加回 bin_start 映射到全局日历索引
        days = [vals[i] for i in range(len(vals)) if (bin_start + i) in window]
        days = [v for v in days if np.isfinite(v) and v > 0]
        result[inst] = float(np.mean(days)) if days else np.nan
    return pd.Series(result, dtype=float)


def compute_auction_factors(
    orders: pd.DataFrame,
    avg_5d_amount: dict | pd.Series = None,
    require_unreversible: bool = True,
) -> pd.DataFrame:
    """计算两个集合竞价因子。

    Parameters
    ----------
    orders : pd.DataFrame
        9:20-9:25 逐笔委托明细, 每行一笔委托, 必需列:
          instrument : str   qlib 代码 (如 SH600000)
          price      : float 委托/下单价
          amount     : float 委托金额 (元, 真实成交金额口径)
       判定主动方向的列 (二选一):
          (a) side + 比较价: 可选 ask1/bid1; 优先用价格比较原文口径
          (b) 直接给主动方向列: side ∈ {'buy','sell'} 表示主动买/主动卖
        可选列:
          is_retail : bool   是否散户委托 (默认 False)
          ask1      : float  委托时点卖一价 (给定则按原文价格比较)
          bid1      : float  委托时点买一价
          side      : str    'buy'/'sell' 主动方向 (无 ask1/bid1 时兜底)
    avg_5d_amount : dict | pd.Series, optional
        每只股票过去5日平均日成交金额 (qlib amount 口径)。缺省时自动调用
        load_avg_5d_amount。缺少数值时该股两个因子均为 NaN。
    require_unreversible : bool
        True (默认) 要求用 ask1/bid1 价格比较来判定主动方向 (9:20-9:25
        不可撤单段, 原文口径); 若数据没有 ask1/bid1 且 require=True 会抛错。

    Returns
    -------
    pd.DataFrame indexed by instrument with columns:
        net_buy, retail_net_sell, auction_buy_strength, retail_sell_strength
    """
    df = orders.copy()
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            raise ValueError(f"orders missing required column: {col}")
    for col in ("ask1", "bid1"):
        if col not in df.columns:
            df[col] = np.nan
    if "side" not in df.columns:
        df["side"] = None
    if "is_retail" not in df.columns:
        df["is_retail"] = False

    has_prices = df["ask1"].notna() & df["bid1"].notna()
    if has_prices.any():
        # 原文口径: 下单价 > 卖一价 = 主动买单; 下单价 < 买一价 = 主动卖单
        is_agg_buy = (df["price"] > df["ask1"]).astype(float)
        is_agg_sell = (df["price"] < df["bid1"]).astype(float)
    else:
        is_agg_buy = pd.Series(np.nan, index=df.index)
        is_agg_sell = pd.Series(np.nan, index=df.index)

    has_side = df["side"].isin(["buy", "sell"])
    # 有价格比较就用价格判定 (原文), 否则退回 side 方向
    agg_buy_mask = pd.Series(False, index=df.index)
    agg_sell_mask = pd.Series(False, index=df.index)
    price_rows = has_prices & (is_agg_buy > 0)
    agg_buy_mask[price_rows] = True
    agg_sell_mask[has_prices & (is_agg_sell > 0)] = True
    side_buy = has_side & (df["side"] == "buy")
    side_sell = has_side & (df["side"] == "sell")
    agg_buy_mask |= side_buy & ~has_prices
    agg_sell_mask |= side_sell & ~has_prices

    df["agg_buy"] = agg_buy_mask
    df["agg_sell"] = agg_sell_mask
    df["retail"] = df["is_retail"].fillna(False).astype(bool)

    df["buy_amount"] = np.where(df["agg_buy"], df["amount"], 0.0)
    df["sell_amount"] = np.where(df["agg_sell"], df["amount"], 0.0)
    df["retail_buy"] = np.where(df["retail"] & df["agg_buy"], df["amount"], 0.0)
    df["retail_sell"] = np.where(df["retail"] & df["agg_sell"], df["amount"], 0.0)

    g = df.groupby("instrument").agg(
        buy_amount=("buy_amount", "sum"),
        sell_amount=("sell_amount", "sum"),
        retail_buy=("retail_buy", "sum"),
        retail_sell=("retail_sell", "sum"),
    )
    g["net_buy"] = g["buy_amount"] - g["sell_amount"]
    g["retail_net_sell"] = g["retail_sell"] - g["retail_buy"]

    if avg_5d_amount is None:
        avg_5d_amount = load_avg_5d_amount(list(g.index))
    baseline = pd.Series(avg_5d_amount, dtype=float)
    baseline.index = baseline.index.astype(str)

    g["auction_buy_strength"] = g["net_buy"] / baseline.reindex(g.index)
    g["retail_sell_strength"] = g["retail_net_sell"] / baseline.reindex(g.index)

    cols = ["net_buy", "retail_net_sell",
            "auction_buy_strength", "retail_sell_strength"]
    return g[cols]


# ---------------------------------------------------------------------------
# 腾讯快照近似实现 (无 Level-2 数据时的降级方案)
# ---------------------------------------------------------------------------

# 腾讯 qt.gtimg.cn 返回字段索引 (以 '~' 分隔)
_TX_PRICE = 3   # 现价
_TX_GAP = 5    # 今开
_TX_PRE = 4    # 昨收
_TX_OUT = 7    # 外盘 (主动买成交量)
_TX_IN = 8     # 内盘 (主动卖成交量)
_TX_BID1 = 9   # 买一价
_TX_BIDV1 = 10  # 买一量
_TX_ASK1 = 19  # 卖一价
_TX_ASKV1 = 20  # 卖一量
_TX_LIMIT_UP = 47    # 涨停价 (腾讯直接给出, 不用昨收×比例算)
_TX_LIMIT_DOWN = 48  # 跌停价


def fetch_tencent_snapshot(instruments: list) -> dict:
    """批量抓取腾讯行情快照 (50 只/请求, 全市场一次调用即可).

    返回 {qlib_code: {'price','open','preclose','outer','inner',
                      'bid1','bidv1','ask1','askv1','limit_up','limit_down'}}.
    """
    import urllib.request

    def _tencent_code(inst: str) -> str:
        ex = inst[:2].lower()
        return f"{ex}{inst[2:]}"

    def _f(parts, idx):
        try:
            v = parts[idx]
            return float(v) if v not in ("", "0") else 0.0
        except (IndexError, ValueError):
            return 0.0

    snap = {}
    codes = [_tencent_code(c) for c in instruments]
    for i in range(0, len(codes), 50):
        chunk = codes[i : i + 50]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
            for line in data.strip().split(";"):
                parts = line.split("~")
                if len(parts) < 49:
                    continue
                sym = parts[2]
                qlib_code = None
                for _q in chunk:
                    if _q[2:] == sym:
                        qlib_code = _q.upper()
                        break
                if not qlib_code:
                    continue
                snap[qlib_code] = {
                    "price": _f(parts, _TX_PRICE),
                    "open": _f(parts, _TX_GAP),
                    "preclose": _f(parts, _TX_PRE),
                    "outer": _f(parts, _TX_OUT),     # 外盘 (手)
                    "inner": _f(parts, _TX_IN),      # 内盘 (手)
                    "bid1": _f(parts, _TX_BID1),
                    "bidv1": _f(parts, _TX_BIDV1),   # 买一量 (手)
                    "ask1": _f(parts, _TX_ASK1),
                    "askv1": _f(parts, _TX_ASKV1),   # 卖一量 (手)
                    "limit_up": _f(parts, _TX_LIMIT_UP),
                    "limit_down": _f(parts, _TX_LIMIT_DOWN),
                }
        except Exception:
            continue
    return snap


def compute_tencent_approx_factors(snapshot: dict) -> pd.DataFrame:
    """基于腾讯快照近似两个因子 (无逐笔委托时的降级口径).

    近似逻辑 (相对原文的映射):
      集合竞价买入强度 ≈ 高开幅度 (gap = (open - preclose) / preclose),
      用买卖一挂单失衡 (bid_ratio = bidv1/(bidv1+askv1)) 作同向幅度调整——
      买盘挂单占比越高放大, 越低收窄, 但**不翻转符号** (低开始终为弱)。
          auction_buy_strength = gap * 1e4 * (1 + 0.5*(2*bid_ratio - 1))
      (gap 放大到可读量纲; 0.5 是调整强度系数)
      散户卖出强度 ≈ 内盘占比 (主动卖盘占全部主动成交的比例):
          inner_ratio = inner / (outer + inner)
          retail_sell_strength = inner_ratio - 0.5   # >0 主动卖盘偏多

    注意: 这不是原文的逐笔委托因子, 而是可用数据的合理近似。值只用于
    横截面排序/相对强弱比较, 绝对值无意义。gap 用今开/昨收, 在任何时点
    抓取都是集合竞价的定调结果 (今开全天不变); inner/outer 则反映抓取
    时刻的累计主动买卖, 越小越好(盘中/盘后含义不同)。
    """
    rows = []
    for inst, s in snapshot.items():
        gap = 0.0
        if s["preclose"] > 0:
            gap = (s["open"] - s["preclose"]) / s["preclose"]
        denom = s["bidv1"] + s["askv1"]
        bid_ratio = s["bidv1"] / denom if denom > 0 else 0.5
        # 同向幅度调整, 不翻转符号: gap 方向是主信号
        buy_strength = gap * 1e4 * (1 + 0.5 * (2 * bid_ratio - 1))

        act = s["outer"] + s["inner"]
        inner_ratio = s["inner"] / act if act > 0 else 0.5
        retail_sell = inner_ratio - 0.5

        rows.append({
            "instrument": inst,
            "gap_pct": gap * 100,
            "bid_ask_ratio": bid_ratio,
            "auction_buy_strength": buy_strength,
            "inner_ratio": inner_ratio,
            "retail_sell_strength": retail_sell,
        })
    return pd.DataFrame(rows).set_index("instrument")


if __name__ == "__main__":
    demo = pd.DataFrame([
        # instrument,  price,  amount, ask1, bid1,  is_retail
        {"instrument": "SH600000", "price": 12.10, "amount": 500000.0,
         "ask1": 12.00, "bid1": 11.99, "is_retail": False},   # 主动买
        {"instrument": "SH600000", "price": 12.05, "amount": 300000.0,
         "ask1": 12.00, "bid1": 11.99, "is_retail": False},   # 主动买
        {"instrument": "SH600000", "price": 11.95, "amount": 800000.0,
         "ask1": 12.00, "bid1": 11.99, "is_retail": True},    # 散户主动卖
        {"instrument": "SH600000", "price": 11.98, "amount": 200000.0,
         "ask1": 12.00, "bid1": 11.99, "is_retail": True},    # 散户主动卖
        {"instrument": "SZ000001", "price": 10.00, "amount": 100000.0,
         "ask1": 9.95, "bid1": 9.94, "is_retail": True},      # 散户主动买
        {"instrument": "SZ000001", "price": 9.90, "amount": 600000.0,
         "ask1": 9.95, "bid1": 9.94, "is_retail": True},      # 散户主动卖
    ])
    avg = pd.Series({"SH600000": 20_000_000.0, "SZ000001": 10_000_000.0})
    print(compute_auction_factors(demo, avg_5d_amount=avg))
    print()
    print("=== Tencent snapshot approximate factors ===")
    snap = fetch_tencent_snapshot(["SH600000", "SZ000001", "SH600519"])
    if snap:
        print(compute_tencent_approx_factors(snap))
    else:
        print("(network unavailable)")
