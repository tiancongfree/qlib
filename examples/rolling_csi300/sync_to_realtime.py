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
from pathlib import Path

if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', write_through=True)

import pandas as pd
from qlib import auto_init
from easyths import TradeClient, TradeClientError

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
        if qty > 0:
            holdings[_ths_to_qlib(code)] = qty
    return holdings


def _to_lots(shares: float) -> int:
    """Round shares down to nearest 100 (1 lot = 100 shares for A-shares)."""
    return max(0, int(shares / 100) * 100)


_QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"

def _load_real_prices(instruments: list) -> dict:
    """Query current real-time prices from Tencent quote API.

    Returns {qlib_code: current_price}. Much faster and more reliable than
    baostock (which needs a login session and hangs when the server is down).
    Batch query is supported (comma-separated codes), so 30 stocks = 1 request.
    """
    import urllib.request

    def _tencent_code(inst: str) -> str:
        ex = inst[:2].lower()
        return f"{ex}{inst[2:]}"

    prices = {}
    codes = [_tencent_code(c) for c in instruments]
    if not codes:
        return prices
    # Batch in chunks of 50
    for i in range(0, len(codes), 50):
        chunk = codes[i : i + 50]
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
            for line in data.strip().split(";"):
                parts = line.split("~")
                if len(parts) > 4:
                    # parts[2]=code, parts[3]=current price
                    sym = parts[2]
                    try:
                        price = float(parts[3])
                    except ValueError:
                        continue
                    if price > 0:
                        # map back to qlib code: tencent returns 6-digit sym without
                        # market prefix, but we requested ex+sym, so rebuild from the
                        # request code (codes list) rather than parsing parts[2].
                        qlib_code = None
                        for _q in chunk:
                            if _q[2:] == sym:
                                qlib_code = _q.upper()
                                break
                        if qlib_code:
                            prices[qlib_code] = price
        except Exception:
            continue
    return prices


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


def _parse_target_stocks(target: pd.DataFrame, max_total: float = None) -> dict:
    result = {}
    instruments = [idx[0] for idx in target.index]
    real_prices = _load_real_prices(instruments)

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


def sync_positions(
    target: pd.DataFrame,
    actual: dict,
    client: TradeClient,
    dry_run: bool = True,
    max_total: float = None,
    price_slippage: float = 0.002,
):
    """
    Compare target vs actual and place buy/sell orders.

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
    """
    target_stocks = _parse_target_stocks(target, max_total)

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

    target_set = set(target_stocks.keys())
    actual_set = set(actual.keys())

    stocks_to_sell = actual_set - target_set
    stocks_to_buy = target_set - actual_set
    stocks_in_both = target_set & actual_set

    # Sell: light weight first (absent weights -> plain code sort)
    stocks_to_sell_sorted = sorted(stocks_to_sell, key=lambda c: (_weight(c), c))
    # Buy: heavy weight first (absent weights -> plain code sort)
    stocks_to_buy_sorted = sorted(stocks_to_buy, key=lambda c: (-_weight(c), c))

    print(f"\n{'=' * 60}")
    print(f"  Target stocks: {len(target_set)}")
    print(f"  Actual stocks: {len(actual_set)}")
    print(f"  To sell: {len(stocks_to_sell)}")
    print(f"  To buy:  {len(stocks_to_buy)}")
    print(f"  To adjust (quantity change): {len(stocks_in_both)}")
    if dry_run:
        print(f"  >>> DRY RUN - no orders will be placed <<<")
    print(f"{'=' * 60}\n")

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

    # Reference prices (real latest close from baostock) for limit orders.
    # Query both target (buy) and to-sell stocks so sells also get a limit price.
    price_query_codes = set(target_stocks.keys()) | set(stocks_to_sell)
    ref_prices = _load_real_prices(list(price_query_codes))

    # ---- Sell stocks not in target ----
    for code in stocks_to_sell_sorted:
        qty = int(actual[code])
        ths = _qlib_to_ths(code)
        rp = ref_prices.get(code) or ref_prices.get(ths)
        # sell slightly below reference to fill; if no price, fall back to market
        limit_price = round(rp * (1 - price_slippage), 2) if rp else None
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
        # 固定现金垫方案 (分批建仓阶段, 2026-08-04): 保留 40w 现金垫, 其余投入目标组合。
        # 相比 `total_assets * invest_ratio`: 比例方案会随盈利把 scale 拉回固定比例,
        # 导致盈利部分被强制兑现 (稀释); 固定垫方案让超出现金垫的部分自然跟涨, 不稀释。
        #  现金垫 40w → 400000 | 现金垫 20w → 200000 | 最终投满 → total_assets * invest_ratio
        max_total = max(0.0, total_assets - 400000)
        print(f"  Account total assets: {total_assets:.2f}")
        print(f"  Cash reserve: 400000.00 -> target portfolio: {max_total:.2f}")

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

        sync_positions(target, actual, client, dry_run=dry_run, max_total=max_total, price_slippage=price_slippage)


if __name__ == "__main__":
    import fire
    fire.Fire(main)
