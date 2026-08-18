"""
Sync qlib last-day positions to real trading account via easyths.

Reads the last-day portfolio from the rolling backtest, queries current
holdings from the easyths service, and submits market orders to align
the real account with the target portfolio.

Usage:
    python sync_to_realtime.py                                          \\
        --host 192.168.11.244 --port 7648                               \\
        --api-key snf81kqdvb07xgcymu6hi4wterza2jo9                     \\
        --exp-name rolling_csi300_lgbm                                  \\
        --dry-run

    # Dry-run first to see what would be traded, then remove --dry-run to execute.
"""

import os
import sys
import time as _time
from datetime import datetime as _dt, time as _dtime
from pathlib import Path

if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', write_through=True)

import pandas as pd
from qlib import auto_init
from easyths import TradeClient, TradeClientError

from auction_factors import compute_tencent_approx_factors, fetch_tencent_snapshot
from save_positions import save_last_day_positions

API_KEY_ENV = "EASYTHS_API_KEY"


def _qlib_to_ths(code: str) -> str:
    """Convert qlib instrument code (e.g. SH600000) to plain code (600000)."""
    return code[2:] if code.startswith(("SH", "SZ")) else code


def _ths_to_qlib(code: str) -> str:
    """Detect market from the first digit and prepend SH/SZ."""
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return f"SH{code}"
    return f"SZ{code}"


def load_target_positions(exp_name: str, last_positions_csv: str = None) -> pd.DataFrame:
    """Load target positions from CSV or by re-running save_last_day_positions."""
    if last_positions_csv and Path(last_positions_csv).exists():
        df = pd.read_csv(last_positions_csv, index_col=[0, 1])
        df.index.names = ["instrument", "datetime"]
        return df
    return save_last_day_positions(exp_name=exp_name)


def get_actual_holdings(client: TradeClient) -> dict | None:
    """Query current holdings from easyths, return {stock_code: shares}.

    Returns ``None`` when the query itself failed or returned a payload that
    cannot be trusted (e.g. easyths reported success=True but parsed an empty
    broker response into an empty DataFrame — a known failure mode that must
    NOT be mistaken for a genuinely empty account, otherwise sync would
    re-buy every held stock).  An empty ``{}`` is only returned for a
    verifiably empty account (no holdings payload at all).
    """
    result = client.query_holdings(return_type="json")
    if not result.get("success"):
        print(f"ERROR: Failed to query holdings: {result.get('message')}")
        return None
    data = result.get("data", {})
    # data can be either {"holdings": [...]} or a list directly
    if isinstance(data, list):
        holdings_list = data
    elif isinstance(data, dict):
        if "holdings" in data or "data" in data:
            holdings_list = data.get("holdings", data.get("data", []))
        elif not data:
            # Empty dict with no holdings/data key: easyths reported success=True
            # but failed to parse the broker response into a DataFrame (e.g.
            # "No columns to parse from file" -> holding={}).  This is NOT a
            # trustworthy empty account -> treat as query failure.
            print("ERROR: Holdings payload empty/unparseable (easyths parsed an empty "
                  "broker response as success=True). Refusing to treat as empty account.")
            return None
        else:
            holdings_list = data
    else:
        holdings_list = []
    holdings = {}
    for pos in holdings_list:
        if not isinstance(pos, dict):
            continue
        code = (
            str(pos.get("证券代码", "") or pos.get("股票代码", "") or
                 pos.get("code", "") or pos.get("stock_code", ""))
        )
        qty = float(
            pos.get("股票余额", 0) or pos.get("可用余额", 0) or
            pos.get("持仓数量", 0) or pos.get("quantity", 0) or
            pos.get("amount", 0)
        )
        if qty > 0 and _is_stock(code):
            holdings[_ths_to_qlib(code)] = qty
    return holdings


def _is_stock(code: str) -> bool:
    """Keep only A-share stock codes; exclude bonds, cash funds, repos etc.

    The account may hold non-stock instruments (现金宝 131990, 可转债 113708,
    etc.).  These must never be treated as positions to buy/sell against the
    qlib stock target, otherwise sync would emit sell orders for them (or
    worse, a re-buy).  A-share stock codes: SH 6xxxxx (60x/68x/605/601/603),
    SZ 0xxxxx (000/001/002/003) and 3xxxxx (300/301).
    """
    c = str(code).strip()
    if not c.isdigit() or len(c) > 6:
        return False
    c = c.zfill(6)
    return c.startswith(("600", "601", "603", "605", "688", "689",
                         "000", "001", "002", "003", "300", "301"))


def _to_lots(shares: float) -> int:
    """Round shares down to nearest 100 (1 lot = 100 shares for A-shares)."""
    return max(0, int(shares / 100) * 100)


_QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"

def _load_real_prices(instruments: list) -> dict:
    """Query current real-time prices from Tencent quote API (batched).

    Thin wrapper over :func:`fetch_tencent_snapshot`, returning only
    {qlib_code: current_price}. Batch query: <=50 codes per HTTP request.
    """
    return {
        inst: s["price"] for inst, s in fetch_tencent_snapshot(instruments).items()
        if s.get("price", 0) > 0
    }


def _load_factor(inst: str, end_date: str = None) -> float:
    """Load the qlib adj-factor for a stock on the latest trading day.

    qlib stores amount in adjusted (前复权) shares.  real_shares = amount * factor.
    """
    import numpy as np
    cal = [l.strip() for l in open(_QLIB_DIR / "calendars/day.txt")]
    cal_idx = {d: i for i, d in enumerate(cal)}
    if end_date is None:
        end_date = cal[-1]
    path = _QLIB_DIR / "features" / inst.lower() / "factor.day.bin"
    if not path.exists():
        return None
    n = np.fromfile(str(path), dtype="<f4")
    start = int(n[0])
    vals = n[1:]
    i = cal_idx.get(end_date, 0) - start
    if 0 <= i < len(vals):
        v = float(vals[i])
        return v if np.isfinite(v) and v > 0 else None
    return None


def _parse_target_stocks(
    target: pd.DataFrame,
    max_total: float = None,
    snapshot: dict = None,
) -> dict:
    result = {}
    instruments = [idx[0] for idx in target.index]
    # Reuse an already-fetched snapshot when available (avoids a second HTTP call).
    if snapshot is None:
        snapshot = fetch_tencent_snapshot(instruments)
    real_prices = {
        inst: s["price"] for inst, s in snapshot.items() if s.get("price", 0) > 0
    }

    # Convert qlib target positions to real shares.
    # qlib stores amount in adjusted (前复权) shares and price in adjusted price.
    # The MARKET VALUE is exact and self-consistent:  value = amount * price.
    #   real_shares = value / real_market_price
    # Do NOT use factor here: qlib's factor is unreliable (systematically off by
    # up to 100%+ for some stocks due to historical adjustment bugs), so
    # amount*factor*real_price double-counts the error.
    items = []
    for idx, row in target.iterrows():
        instrument = idx[0]
        amount = row.get("amount")
        if not amount:
            continue
        real_price = real_prices.get(instrument)
        if not real_price or real_price <= 0:
            continue
        qlib_price = row.get("price")
        if not qlib_price or qlib_price <= 0:
            continue
        value = amount * qlib_price  # real market value (adjusted basis is exact)
        real_shares = value / real_price
        items.append((instrument, real_shares, value))

    # Scale based on REAL market value so that total buy value <= max_total.
    # Using adjusted value (amount*qlib_price) as the denominator is wrong when
    # factor is off: it makes scale too big/small vs actual RMB value.
    total_real_value = sum(rs * real_prices[inst] for inst, rs, _ in items)
    scale = max_total / total_real_value if (max_total and total_real_value) else 1.0

    for instrument, real_shares, value in items:
        scaled_shares = real_shares * scale
        qty = _to_lots(scaled_shares)
        if qty > 0:
            result[instrument] = qty
    return result


def _append_obs_log(
    obs_log: str,
    target: pd.DataFrame,
    cut_codes: list,
    strengths: dict,
    retail_sells: dict,
    pool: set,
    actual_set: set,
):
    """Append one row per candidate stock to the observation CSV.

    Columns: date, code, auction_buy_strength, retail_sell_strength, would_cut,
    held. Used later to measure the win rate of the would-be filter vs. the
    actual next-day return of each stock.
    """
    if not obs_log:
        return
    try:
        import datetime as _dt

        date = str(target.index[0][1]) if len(target.index) else _dt.date.today()
        records = []
        for c in sorted(pool):
            records.append({
                "date": date,
                "code": c,
                "auction_buy_strength": round(strengths.get(c, 0.0), 4),
                "retail_sell_strength": round(retail_sells.get(c, 0.0), 4),
                "would_cut": c in set(cut_codes),
                "held": c in actual_set,
            })
        new_df = pd.DataFrame(records)
        path = Path(obs_log)
        header = not path.exists()
        new_df.to_csv(path, mode="a", header=header, index=False)
        print(f"  观察日志已追加 {len(records)} 行 → {obs_log}")
    except Exception as e:  # never let logging break the sync
        print(f"  WARNING: 观察日志写入失败: {e}")


def _wait_for_auction_snapshot(wait_until: str = "09:24:30", enabled: bool = True):
    """Sleep until the 集合竞价不可撤单段末 before grabbing the Tencent snapshot.

    The scheduled daily task starts at 09:19; the preceding rolling backtest +
    model loading take a couple of minutes, so by the time we reach the snapshot
    fetch it is usually ~09:21-09:23. The auction factors (gap, bid-balance) are
    only meaningful when the snapshot is taken inside the 9:20-9:25 non-cancellable
    window — the opening auction closes at 9:25 and the open price is fixed then.
    So we wait until ``wait_until`` (default 09:24:30) unless it is already past
    (manual run / after-market / non-trading day → snapshot fetched immediately).

    Only sleeps when the current time is between 09:00 and ``wait_until``; never
    across midnight or on days where the clock has already passed the target.
    """
    if not enabled or not wait_until:
        return
    try:
        now = _dt.now()
        h, m, s = (int(x) for x in wait_until.split(":"))
        target = now.replace(hour=h, minute=m, second=s, microsecond=0)
        if now < target and now.hour >= 9:
            delay = (target - now).total_seconds()
            print(f"\n  集合竞价等待: 当前 {now:%H:%M:%S}, 等待 {delay:.0f}s 至 "
                  f"{wait_until} (9:20-9:25 不可撤单段末) 再抓快照...", flush=True)
            _time.sleep(delay)
            print(f"  已到 {wait_until}, 开始抓取腾讯快照。", flush=True)
        else:
            print(f"  当前 {now:%H:%M:%S} 已过 {wait_until}, 不等待直接抓快照。", flush=True)
    except Exception as e:
        print(f"  WARNING: 集合竞价等待失败, 直接抓快照: {e}", flush=True)


def sync_positions(
    target: pd.DataFrame,
    actual: dict,
    client: TradeClient,
    dry_run: bool = True,
    max_total: float = None,
    price_slippage: float = 0.002,
    auction_filter: bool = False,
    auction_observe: bool = True,
    auction_cut_threshold: float = -50.0,
    retail_exempt_threshold: float = None,
    obs_log: str = None,
    auction_wait_until: str = "09:24:30",
    auction_wait: bool = True,
):
    """
    Compare target vs actual and place buy/sell orders, with an optional
    集合竞价(collection-bidding) short-term filter that TRIMS the target list.

    Filter logic (only when auction_filter=True):
      - candidate pool = all qlib target stocks
      - cut every stock whose auction_buy_strength is BELOW ``auction_cut_threshold``
        (absolute-threshold rule, no fixed cut count)
      - rationale: the factor ≈ gap (open-1) scaled by a bid-balance tone, so its
        sign carries the market regime — on a broadly-up day most strengths are
        positive (few/no cuts), on a broadly-down day many low-open names fall
        below the threshold and get trimmed. A fixed bottom-N would instead waste
        trades on "least strong" names even in a strong market.
      - retail exemption: a below-threshold stock whose retail_sell_strength is
        above ``retail_exempt_threshold`` is EXEMPT (held position kept, new buy
        kept), because the original article treats heavy retail selling as a
        contrarian bullish signal
      - remaining cut stocks:
          * if currently held  -> SELL the whole position
          * if not held        -> skip (no BUY order)
      - all other target stocks -> normal buy/add/trim
    qlib's unconditional sells (held but not in target) are unaffected by the filter.

    Observation mode (auction_observe=True): factors are computed and the full
    "would-cut" decision is PRINTED and appended to ``obs_log`` CSV, but the
    target list is NOT modified — normal qlib orders are placed regardless.
    This is meant for collecting win-rate statistics before enabling the filter
    for real.

    Parameters
    ----------
    target : pd.DataFrame
        Last-day positions from qlib (index: instrument, datetime).
    actual : dict
        Current holdings from easyths, {qlib_code: shares_in_lots}.
    client : TradeClient
        Connected easyths client.
    dry_run : bool
        If True, only print what would be done.
    max_total : float, optional
        Cap total position value. If None, use target total.
    auction_filter : bool
        Enable the collection-bidding short-term filter that TRIMS the target
        list (see above). Default False (not enabled yet).
    auction_observe : bool
        Compute/print/log the would-be filter decision WITHOUT applying it.
        Default True.
    auction_cut_threshold : float
        Absolute-strength threshold: every target stock with auction_buy_strength
        BELOW this value is cut (no fixed count). Default -50.0. On a broadly-up
        day few names fall below it (little/nothing cut); on a broadly-down day
        many low-open names do (trimmed accordingly).
    retail_exempt_threshold : float, optional
        If set, a bottom-cut stock with retail_sell_strength above this value is
        exempt from being cut (contrarian bullish). Default None = no exemption.
    obs_log : str, optional
        Path to a CSV that observation decisions are appended to (one row per
        stock per run). Used for post-hoc win-rate statistics.
    auction_wait_until : str
        Wall-clock time (HH:MM:SS) to wait for before fetching the Tencent
        snapshot. Default "09:24:30" = end of the 9:20-9:25 non-cancellable
        auction window. See :func:`_wait_for_auction_snapshot`.
    auction_wait : bool
        Enable the wait. Default True (scheduled runs start 09:19; the snapshot
        must be taken inside the auction window for the factors to be valid).
    """
    # Wait for the auction window to be over before fetching real-time quotes.
    _wait_for_auction_snapshot(auction_wait_until, enabled=auction_wait)

    # ---- One batched Tencent snapshot for everything ----
    # Fetch real prices AND auction factors in a single batch (<=50 codes per
    # request) covering all target + actual stocks. Never call the quote API
    # per-stock, and never fetch twice in one sync run.
    all_instruments = sorted(set(idx[0] for idx in target.index) | set(actual.keys()))
    snapshot = fetch_tencent_snapshot(all_instruments)

    # SAFETY GUARD: if the quote API failed (network/ban), parsing target with
    # an empty snapshot yields an empty target list -> every held stock would
    # be treated as "qlib removed" and SOLD.  That is a catastrophic mis-order
    # on a data-source outage, so abort the sync instead.
    if not snapshot:
        print("=" * 60)
        print("  SAFETY GUARD: Tencent quote API returned no snapshot for any "
              "instrument.")
        print("  Without real prices the target list cannot be built and every "
              "held stock would be sold as 'qlib removed'.")
        print("  ABORTING sync. Re-run when the quote API is reachable.")
        print("=" * 60)
        return

    target_stocks = _parse_target_stocks(target, max_total, snapshot=snapshot)

    # Approximate collection-bidding factors from the SAME snapshot.
    # Computed whenever we need them: real filter (auction_filter) or observation
    # mode (auction_observe).
    need_factors = auction_filter or auction_observe
    auction_factors = pd.DataFrame()
    strengths = {}
    retail_sells = {}
    if need_factors and snapshot:
        auction_factors = compute_tencent_approx_factors(snapshot)
        if not auction_factors.empty:
            for idx, row in auction_factors.iterrows():
                v = row.get("auction_buy_strength")
                if pd.notna(v):
                    strengths[idx] = float(v)
                r = row.get("retail_sell_strength")
                if pd.notna(r):
                    retail_sells[idx] = float(r)

    def _strength(code: str) -> float:
        return strengths.get(code, 0.0)

    def _retail(code: str) -> float:
        return retail_sells.get(code, 0.0)

    # Target weights {code: weight} for priority ordering of orders.
    # BUY orders are placed heavy-weight first so that if T+1 available cash
    # runs out, the largest target positions still get bought first; SELL orders
    # are placed light-weight first so small positions are cleaned up before the
    # bigger ones (their proceeds only free up next day anyway).
    target_weights = {}
    if "weight" in target.columns:
        for idx, row in target.iterrows():
            w = row.get("weight")
            if w is not None and w > 0:
                target_weights[idx[0]] = float(w)

    def _weight(code: str) -> float:
        return target_weights.get(code, 0.0)

    target_set_all = set(target_stocks.keys())
    actual_set = set(actual.keys())

    # ---- 集合竞价短线过滤器: 从 target 全集中裁剪绝对弱势股 ----
    # candidate pool = all target stocks; sort by auction_buy_strength ASC.
    # 方案A (绝对阈值, 无上限): 裁掉所有 auction_buy_strength < threshold 的股票。
    # 因子 ≈ gap 缩放(低开->负, 高开->正), 符号携带市场强弱 → 普涨日多数为正
    # 少裁/不裁, 普跌日低开股多则多裁; 不再用固定 bottom-N (普涨日末5也常是正因子,
    # 裁掉=浪费; 普跌日末5反而漏掉同池更弱的一批)。
    # 保留: 散户豁免(散户反向看多) + 观察模式(算+打印+记日志, 不实际裁剪)。
    cut_codes = []
    if need_factors and target_set_all:
        ranked = sorted(target_set_all, key=lambda c: _strength(c))
        below = [c for c in ranked if _strength(c) < auction_cut_threshold]
        mode = "实际裁剪" if auction_filter else "观察模式(不实际裁剪)"
        print(f"\n  集合竞价短线过滤器 [{mode}]: 候选 {len(target_set_all)} 只, "
              f"绝对阈值 {auction_cut_threshold:.2f}, 低于阈值 {len(below)} 只")
        print(f"  {'code':<10}{'strength':>10}{'retail':>10}  {'hold?':>6}")
        for c in ranked:
            star = "*" if c in set(below) else " "
            print(f"  {star}{c:<9}{_strength(c):>10.2f}{_retail(c):>10.2f}  "
                  f"{'HOLD' if c in actual_set else 'none':>6}")
        for c in below:
            exempt = False
            if retail_exempt_threshold is not None and _retail(c) > retail_exempt_threshold:
                exempt = True
            if exempt:
                print(f"  → {c} 豁免 (retail_sell_strength {_retail(c):.2f} "
                      f"> {retail_exempt_threshold}, 散户反向看多)")
                continue
            cut_codes.append(c)
            action = "全卖" if c in actual_set else "不下单(无仓)"
            print(f"  → 裁剪 {c} (strength {_strength(c):.2f}): {action}")
        if cut_codes:
            print(f"  裁剪 {len(cut_codes)} 只: {', '.join(sorted(cut_codes))}")
        else:
            print("  本次无裁剪")
        print()
        # 观察日志: 记录决策到 CSV (无论是否实际裁剪)
        _append_obs_log(obs_log, target, cut_codes, strengths, retail_sells,
                        target_set_all, actual_set)
        # 只有实际裁剪模式才从目标名单中剔除被裁剪的股票
        if auction_filter:
            for c in cut_codes:
                target_stocks.pop(c, None)

    target_set = set(target_stocks.keys())

    def _buy_key(code: str):
        """BUY order placement key: heavy-weight first (T+1 cash-shortage
        still buys the big positions first)."""
        return (-_weight(code), code)

    stocks_to_sell = actual_set - target_set
    stocks_to_buy = target_set - actual_set
    stocks_in_both = target_set & actual_set

    # Sell: light weight first (absent weights -> plain code sort)
    stocks_to_sell_sorted = sorted(stocks_to_sell, key=lambda c: (_weight(c), c))
    # Buy: heavy weight first
    stocks_to_buy_sorted = sorted(stocks_to_buy, key=_buy_key)

    print(f"\n{'=' * 60}")
    print(f"  Target stocks (after filter): {len(target_set)}")
    print(f"  Actual stocks: {len(actual_set)}")
    print(f"  To sell: {len(stocks_to_sell)}")
    print(f"  To buy:  {len(stocks_to_buy)}")
    print(f"  To adjust (quantity change): {len(stocks_in_both)}")
    if dry_run:
        print(f"  >>> DRY RUN - no orders will be placed <<<")
    print(f"{'=' * 60}\n")

    if need_factors and not auction_factors.empty:
        print("  集合竞价近似因子 (Tencent snapshot, 横截面相对值, 当前已裁剪名单):")
        show_codes = sorted(set(target_set) | set(stocks_to_sell))
        show = auction_factors.reindex(show_codes).dropna(how="all").round(4)
        if not show.empty:
            print(show.to_string())
        print()

    def _sanitize(text):
        if not isinstance(text, str):
            return str(text)
        return text.replace("\r", "").replace("\n", " ").strip()

    def do(name, ths_code, qty, limit_price=None):
        print(f"  {name:6s} {ths_code} x {qty}", flush=True)
        if not dry_run:
            try:
                # easyths at this broker rejects market orders (市价委托) for most
                # stocks, so we place LIMIT orders instead.  For buys we set the
                # limit slightly above the reference price and for sells slightly
                # below, to maximize the chance of immediate execution.
                if name.startswith("SELL"):
                    if limit_price:
                        r = client.sell(stock_code=ths_code, price=limit_price, quantity=qty)
                    else:
                        r = client.market_sell(stock_code=ths_code, quantity=qty)
                else:
                    if limit_price:
                        r = client.buy(stock_code=ths_code, price=limit_price, quantity=qty)
                    else:
                        r = client.market_buy(stock_code=ths_code, quantity=qty)
                status = "OK" if r.get("success") else f"FAIL: {_sanitize(r.get('message', ''))}"
                print(f"    -> {status}", flush=True)
            except TradeClientError as e:
                print(f"    -> ERROR: {e}", flush=True)

    # Reference prices for limit orders, from the SAME snapshot (no extra call).
    ref_prices = {
        inst: s["price"] for inst, s in snapshot.items() if s.get("price", 0) > 0
    }

    # ---- Sell stocks not in target ----
    # 已裁剪名单 (实际持有但被因子裁掉的) 单独标注, 便于复盘
    cut_held = set(cut_codes) & actual_set
    for code in stocks_to_sell_sorted:
        qty = int(actual[code])
        ths = _qlib_to_ths(code)
        rp = ref_prices.get(code) or ref_prices.get(ths)
        # sell slightly below reference to fill; if no price, fall back to market
        limit_price = round(rp * (1 - price_slippage), 2) if rp else None
        reason = "因子裁剪" if code in cut_held else "qlib调出"
        print(f"  # 卖出原因: {reason}", flush=True)
        do("SELL", ths, qty, limit_price)

    # ---- Buy stocks in target but not in actual ----
    for code in stocks_to_buy_sorted:
        ths = _qlib_to_ths(code)
        rp = ref_prices.get(code) or ref_prices.get(ths)
        # buy slightly above reference to fill
        limit_price = round(rp * (1 + price_slippage), 2) if rp else None
        do("BUY", ths, target_stocks[code], limit_price)

    # ---- Adjust quantities for stocks in both ----
    # BUY+ (add) heavy-weight first; SELL- (trim) light-weight first, so that
    # when T+1 cash is short the heavy positions get topped up preferentially.
    for code in sorted(stocks_in_both, key=lambda c: (-_weight(c), c)):
        target_qty = target_stocks[code]
        actual_qty = int(actual[code])
        diff = target_qty - actual_qty
        ths = _qlib_to_ths(code)
        rp = ref_prices.get(code) or ref_prices.get(ths)
        if diff > 0:
            limit_price = round(rp * (1 + price_slippage), 2) if rp else None
            do("BUY+", ths, diff, limit_price)
        elif diff < 0:
            limit_price = round(rp * (1 - price_slippage), 2) if rp else None
            do("SELL-", ths, abs(diff), limit_price)

    # ---- 过滤器决策摘要 (便于复盘) ----
    if need_factors and cut_codes:
        held_cut = sorted(cut_held & actual_set)
        skip_cut = sorted(set(cut_codes) - actual_set)
        applied = "实际执行" if auction_filter else "仅观察(未执行)"
        print(f"\n{'=' * 60}")
        print(f"  集合竞价过滤器决策摘要 [{applied}]:")
        print(f"    候选(可买 target): {len(target_set_all)} 只, 裁剪 {len(cut_codes)} 只")
        if held_cut:
            print(f"    已持仓→全卖: {', '.join(held_cut)}")
        if skip_cut:
            print(f"    无仓位→跳过: {', '.join(skip_cut)}")
        exempted = []
        if retail_exempt_threshold is not None:
            for c in sorted(target_set_all):
                if _retail(c) > retail_exempt_threshold:
                    exempted.append(c)
        if exempted:
            print(f"    豁免(散户反向): {', '.join(exempted)}")
        print(f"    保留并正常调仓: {len(target_set)} 只")
        print(f"{'=' * 60}\n")

    if dry_run:
        print(f"\n{'=' * 60}")
        print(f"  This was a DRY RUN. Re-run with --dry-run False to execute.")
        print(f"{'=' * 60}")


def main(
    host: str = "192.168.11.244",
    port: int = 7648,
    api_key: str = None,
    exp_name: str = "rolling_csi300_lgbm",
    last_positions_csv: str = None,
    dry_run: bool = True,
    invest_ratio: float = 0.95,
    price_slippage: float = 0.002,
    allow_empty_holdings: bool = False,
    auction_filter: bool = False,
    auction_observe: bool = True,
    auction_cut_threshold: float = -50.0,
    retail_exempt_threshold: float = None,
    obs_log: str = None,
    auction_wait_until: str = "09:24:30",
    auction_wait: bool = True,
):
    print("=" * 60)
    print("  Qlib → EasyTHS Real-time Sync")
    print("=" * 60)

    # Init qlib before loading positions from mlflow
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    # Load target positions
    target = load_target_positions(exp_name, last_positions_csv)
    if target.empty:
        print("ERROR: No target positions found.")
        sys.exit(1)

    # Observation log defaults to a file next to this script unless a path is given.
    if auction_observe and not obs_log:
        obs_log = str(Path(__file__).parent / "auction_observe_log.csv")

    # Resolve API key: CLI arg > env var
    resolved_key = api_key or os.environ.get(API_KEY_ENV, "")
    if not resolved_key:
        print("ERROR: API key required. Set --api-key or EASYTHS_API_KEY env var.")
        sys.exit(1)

    # Patch easyths client to disable keep-alive (prevents CLOSE_WAIT pileup)
    import httpx as _httpx
    _orig_get_client = TradeClient._get_client
    def _patched_get_client(self):
        if self._client is None:
            self._client = _httpx.Client(
                base_url=self._base_url,
                timeout=self.timeout,
                limits=_httpx.Limits(max_keepalive_connections=0, max_connections=1),
            )
        return self._client
    TradeClient._get_client = _patched_get_client

    # Connect to easyths (with retries)
    # Before each attempt, force-RST any lingering connections so the
    # server can clean up CLOSE_WAIT sockets.
    import socket as _socket
    import struct as _struct
    import time as _time

    def _send_rst():
        """Force-RST stale connections so server can recover from CLOSE_WAIT."""
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(1)
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_LINGER, _struct.pack('ii', 1, 0))
            s.connect((host, port))
            s.close()
        except Exception:
            pass

    # Connect with retries
    print(f"\nConnecting to easyths at {host}:{port} ...")
    last_err = None
    client = None
    elapsed = 0
    for attempt in range(3):
        try:
            _send_rst()
            client = TradeClient(host=host, port=port, api_key=resolved_key, timeout=60)
            health = client.health_check()
            if health.get("success"):
                print(f"  easyths status: OK ({health.get('message')})")
                break
            last_err = health.get("message")
        except Exception as e:
            last_err = str(e)
            client = None
            _send_rst()
        if attempt < 2:
            wait = (attempt + 1) * 10
            print(f"  Connection failed ({attempt+2}/3, 已过{elapsed}s/共30s), retrying in {wait}s... ({last_err})")
            _time.sleep(wait)
            elapsed += wait
    else:
        print(f"  Connection unstable after 3 attempts: {last_err}")
        sys.exit(1)

    with client:
        print("  Querying account funds...", end="", flush=True)
        funds = client.query_funds()
        if not funds.get("success"):
            print(" FAILED")
            print(f"ERROR: Failed to query funds: {funds.get('message')}")
            sys.exit(1)
        print(" OK", flush=True)
        total_assets = float(funds["data"].get("总资产", 0))
        # 全部资产投入 (2026-08-13): 已清仓, 转入模拟盘观察, 不再保留现金垫。
        max_total = total_assets
        print(f"  Account total assets: {total_assets:.2f}")
        print(f"  Invest all: -> target portfolio: {max_total:.2f}")

        actual = get_actual_holdings(client)
        if actual is None:
            print("ERROR: Holdings query failed. Aborting sync to avoid placing orders "
                  "on an unknown account state (would re-buy every target stock).")
            sys.exit(1)
        if not actual and not allow_empty_holdings:
            print("=" * 60)
            print("  SAFETY GUARD: holdings query returned EMPTY account.")
            print("  This is usually a transient easyths/broker failure (the broker")
            print("  returned empty text and easyths parsed it into an empty DataFrame")
            print("  while still reporting success=True), NOT a genuinely empty account.")
            print("  Proceeding would re-buy every target stock (duplicate orders).")
            print("  ABORTING. If the account is truly empty (first-time setup), re-run")
            print("  with --allow-empty-holdings True to force it through.")
            print("=" * 60)
            sys.exit(1)
        print(f"  Current holdings: {len(actual)} stocks")

        if not dry_run:
            cancel_result = client.cancel_order()
            if cancel_result.get("success"):
                print(f"  Cancelled existing pending orders")
            else:
                print(f"  No pending orders to cancel or cancel failed: {cancel_result.get('message')}")

        sync_positions(
            target, actual, client,
            dry_run=dry_run,
            max_total=max_total,
            price_slippage=price_slippage,
            auction_filter=auction_filter,
            auction_observe=auction_observe,
            auction_cut_threshold=auction_cut_threshold,
            retail_exempt_threshold=retail_exempt_threshold,
            obs_log=obs_log,
            auction_wait_until=auction_wait_until,
            auction_wait=auction_wait,
        )


if __name__ == "__main__":
    import fire
    fire.Fire(main)
