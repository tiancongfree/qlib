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


def get_actual_holdings(client: TradeClient) -> dict:
    """Query current holdings from easyths, return {stock_code: shares}."""
    result = client.query_holdings(return_type="json")
    if not result.get("success"):
        print(f"ERROR: Failed to query holdings: {result.get('message')}")
        return {}
    data = result.get("data", {})
    # data can be either {"holdings": [...]} or a list directly
    if isinstance(data, list):
        holdings_list = data
    elif isinstance(data, dict):
        holdings_list = data.get("holdings", data.get("data", []))
        if not holdings_list:
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
    """Query latest close prices from baostock for the given instruments.
    Only queries ~20-30 stocks so it's fast (~2s).
    Returns {qlib_code: price}."""
    import baostock as _bs
    _bs.login()
    prices = {}
    for inst in instruments:
        ex = inst[:2].lower()
        code = inst[2:]
        bs_sym = f"{ex}.{code}"
        try:
            rs = _bs.query_history_k_data_plus(
                bs_sym, "close",
                start_date="2026-05-29", end_date="2026-05-29",
                frequency="d", adjustflag="2",
            )
            if rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                close = float(row[0])
                if close > 0:
                    prices[inst] = close
        except Exception:
            pass
    _bs.logout()
    return prices


def _parse_target_stocks(target: pd.DataFrame, max_total: float = None) -> dict:
    result = {}
    total_amount = target["amount"].sum()
    cash = target["cash"].iloc[0] if "cash" in target.columns else 0
    total_portfolio = total_amount + cash
    scale = max_total / total_portfolio if (max_total and total_portfolio) else 1.0

    instruments = [idx[0] for idx in target.index]
    real_prices = _load_real_prices(instruments)

    for idx, row in target.iterrows():
        instrument = idx[0]
        amount = row.get("amount")
        if not amount:
            continue
        real_price = real_prices.get(instrument)
        if not real_price or real_price <= 0:
            continue
        scaled_amount = amount * scale
        shares = scaled_amount / real_price
        qty = _to_lots(shares)
        if qty > 0:
            result[instrument] = qty
    return result


def sync_positions(
    target: pd.DataFrame,
    actual: dict,
    client: TradeClient,
    dry_run: bool = True,
    max_total: float = None,
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

    target_set = set(target_stocks.keys())
    actual_set = set(actual.keys())

    stocks_to_sell = actual_set - target_set
    stocks_to_buy = target_set - actual_set
    stocks_in_both = target_set & actual_set

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

    def do(name, ths_code, qty):
        print(f"  {name:6s} {ths_code} x {qty}", flush=True)
        if not dry_run:
            try:
                if name.startswith("SELL"):
                    r = client.market_sell(stock_code=ths_code, quantity=qty)
                else:
                    r = client.market_buy(stock_code=ths_code, quantity=qty)
                status = "OK" if r.get("success") else f"FAIL: {_sanitize(r.get('message', ''))}"
                print(f"    -> {status}", flush=True)
            except TradeClientError as e:
                print(f"    -> ERROR: {e}", flush=True)

    # ---- Sell stocks not in target ----
    for code in sorted(stocks_to_sell):
        qty = int(actual[code])
        do("SELL", _qlib_to_ths(code), qty)

    # ---- Buy stocks in target but not in actual ----
    for code in sorted(stocks_to_buy):
        do("BUY", _qlib_to_ths(code), target_stocks[code])

    # ---- Adjust quantities for stocks in both ----
    for code in sorted(stocks_in_both):
        target_qty = target_stocks[code]
        actual_qty = int(actual[code])
        diff = target_qty - actual_qty
        ths_code = _qlib_to_ths(code)
        if diff > 0:
            do("BUY+", ths_code, diff)
        elif diff < 0:
            do("SELL-", ths_code, abs(diff))

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
        max_total = total_assets * invest_ratio
        print(f"  Account total assets: {total_assets:.2f}")
        print(f"  Invest ratio: {invest_ratio:.0%} -> target portfolio: {max_total:.2f}")

        actual = get_actual_holdings(client)
        print(f"  Current holdings: {len(actual)} stocks")

        if not dry_run:
            cancel_result = client.cancel_order()
            if cancel_result.get("success"):
                print(f"  Cancelled existing pending orders")
            else:
                print(f"  No pending orders to cancel or cancel failed: {cancel_result.get('message')}")

        sync_positions(target, actual, client, dry_run=dry_run, max_total=max_total)


if __name__ == "__main__":
    import fire
    fire.Fire(main)
