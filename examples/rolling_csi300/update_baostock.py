"""Update qlib CN daily data using baostock.
Computes per-stock empirical ratios from overlap dates and applies them to new dates.
Supports incremental update: only fetches a few overlap days + new dates.
"""
import sys
import time
import tempfile
import warnings
import calendar as _cal
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import baostock as bs  # noqa: F401 (imported for its public API below)
sys.path.insert(0, str(Path.home() / "qlib"))
from scripts.dump_bin import DumpDataUpdate
QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"
LAST_UPDATE_FILE = Path(__file__).parent / ".last_update.txt"
OVERLAP_DAYS = 5  # enough for ratio computation
CSI300_MV_THRESHOLD = 500e8  # 500亿流通市值: 大概率进 CSI300 (当前成分股最小约212亿)

def _latest_trading_day() -> datetime:
    """Return the most recent probable trading day (date only, time=midnight).

    A-share daily bars are only finalized after the 15:00 close.  Before close
    (e.g. the 09:30 morning task) today's bar does not exist yet, so fall back
    to the previous trading day.  This prevents the morning run from treating
    "today" as a new day and re-downloading all stocks for nothing (the evening
    task already updated to the last completed trading day).
    """
    now = datetime.now()
    d = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour < 16:  # before market close + buffer, today's bar not published
        d -= timedelta(days=1)
    while d.weekday() >= 5:  # Saturday/Sunday -> Friday
        d -= timedelta(days=1)
    return d

def _read_last_update() -> datetime | None:
    if LAST_UPDATE_FILE.exists():
        try:
            return datetime.strptime(LAST_UPDATE_FILE.read_text().strip(), "%Y-%m-%d")
        except Exception:
            return None
    return None

def _write_last_update(d: datetime):
    LAST_UPDATE_FILE.write_text(d.strftime("%Y-%m-%d"))
def read_calendar():
    cal = pd.read_csv(QLIB_DIR / "calendars/day.txt", header=None).iloc[:, 0].tolist()
    return [pd.Timestamp(d) for d in cal]
def fname_to_bs(fname: str) -> str:
    ex = fname[:2]
    code = fname[2:]
    return f"{ex}.{code}"
def bs_to_fname(bs_sym: str) -> str:
    return bs_sym.replace(".", "").lower()
def build_existing_last_values(stock_list, calendar):
    """For each stock, read last adjclose, close, factor, volume, amount from bin.

    Uses each bin's actual last valid row (not calendar[-1]), because some bins
    may lag behind the calendar if a previous update was interrupted.
    """
    results = {}
    for fname in stock_list:
        feat_dir = QLIB_DIR / "features" / fname.lower()
        adjclose_path = feat_dir / "adjclose.day.bin"
        if not adjclose_path.exists():
            continue
        adj = np.fromfile(str(adjclose_path), dtype='<f')
        close = np.fromfile(str(feat_dir / "close.day.bin"), dtype='<f')
        factor = np.fromfile(str(feat_dir / "factor.day.bin"), dtype='<f')
        volume = np.fromfile(str(feat_dir / "volume.day.bin"), dtype='<f')
        amount = np.fromfile(str(feat_dir / "amount.day.bin"), dtype='<f')
        if len(adj) < 3 or len(close) != len(adj):
            continue
        bin_start = int(adj[0])
        bin_idx = len(adj) - 1  # actual last row of this bin
        if bin_idx < 1:
            continue
        # The last row may be a suspension-filled row; step back to find a valid close
        last_adjclose = adj[bin_idx]
        last_close = close[bin_idx]
        while bin_idx > 1 and (not np.isfinite(last_adjclose) or last_adjclose <= 0):
            bin_idx -= 1
            last_adjclose = adj[bin_idx]
            last_close = close[bin_idx]
        # first_adjclose = first valid (finite, >0) adjclose after header
        fa = adj[1]
        if not (np.isfinite(fa) and fa > 0):
            valid = np.where(np.isfinite(adj[1:]) & (adj[1:] > 0))[0]
            fa = adj[1 + valid[0]] if len(valid) else np.nan
        results[fname] = {
            "first_adjclose": fa,
            "last_adjclose": last_adjclose,
            "last_close": last_close,
            "last_factor": factor[bin_idx],
            "last_volume": volume[bin_idx],
            "last_amount": amount[bin_idx],
        }
    return results


def _query_stock_timeout(bs_sym: str, start: str, end: str, per_stock_timeout: float = 30):
    """Query one stock's k-data with a per-stock wall-clock timeout.

    baostock's ``query_history_k_data_plus`` is a blocking call that can hang
    indefinitely when the server is unstable (observed 2026-08: the whole
    update stalled on a single stock until the outer 600s timeout killed it).
    Run the query + row-drain in a thread and abandon it if it exceeds the
    timeout, so one bad stock can no longer stall the entire update.

    Returns the ``rs`` result object on success, or ``None`` on timeout.
    """
    import threading

    result = {}

    def _run():
        try:
            result["rs"] = bs.query_history_k_data_plus(
                bs_sym,
                "date,open,high,low,close,volume,amount,preclose,pctChg",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="2",
            )
        except Exception as e:
            result["exc"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(per_stock_timeout)
    if t.is_alive():
        # Abandon this thread (daemon); connection is poisoned, caller re-logins.
        return None
    if "exc" in result:
        raise result["exc"]
    return result.get("rs")


def download(stock_list, start, end):
    """Download baostock data for all stocks.

    Falls back to the akshare Sina source (stock_zh_a_daily, adjust=qfq) if the
    baostock login fails. The Sina qfq series is numerically identical to
    baostock's adjustflag=2 (verified across several stocks), so the returned
    data is in the same forward-adjusted coordinate system and map_and_scale
    needs no changes.
    """
    lg = bs.login()
    if lg.error_code != "0":
        print(f"  baostock login failed: {lg.error_msg}; falling back to akshare Sina...")
        return download_akshare_sina(stock_list, start, end)
    try:
        # Pre-filter: skip 北交所 stocks (bj.*) which are never selected
        a_share_list = [s for s in stock_list if not s.startswith("bj")]
        bj_skipped = len(stock_list) - len(a_share_list)
        print(f"  Filtered out {bj_skipped} 北交所 stocks, {len(a_share_list)} remaining")
        stock_list = a_share_list

        all_data = []
        total = len(stock_list)
        fail = 0
        timeout = 0
        chunk = 200
        for i in range(0, total, chunk):
            batch = stock_list[i : i + chunk]
            batch_id = i // chunk + 1
            for fname in batch:
                bs_sym = fname_to_bs(fname)
                print(f"    [{batch_id}] {fname} ({bs_sym}) ...", end="", flush=True)
                try:
                    # adjustflag=2: forward-adjusted (all prices at current level)
                    rs = _query_stock_timeout(bs_sym, start, end, per_stock_timeout=30)
                    if rs is None:
                        timeout += 1
                        fail += 1
                        print(" TIMEOUT(skip)", flush=True)
                        # A hung socket poisons the baostock session; re-login to recover.
                        try:
                            bs.logout()
                        except Exception:
                            pass
                        lg = bs.login()
                        if lg.error_code != "0":
                            raise RuntimeError(f"baostock re-login failed: {lg.error_msg}")
                        continue
                    if rs.error_code != "0":
                        fail += 1
                        print(" FAIL", flush=True)
                        continue
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    if not rows:
                        print(" empty", flush=True)
                        continue
                    df = pd.DataFrame(rows, columns=rs.fields)
                    df["symbol"] = fname.lower()
                    all_data.append(df)
                    print(" OK", flush=True)
                except Exception:
                    fail += 1
                    print(" ERROR", flush=True)
            progress = min(i + chunk, total)
            print(f"  Batch {batch_id}: {progress}/{total} ({fail} failed, {timeout} timed out)", flush=True)
            time.sleep(0.3)
        bs.logout()
        if not all_data:
            raise RuntimeError(f"No data downloaded ({fail} failures)")
        print(f"  Downloaded {len(all_data)} stocks, {fail} failures ({timeout} timed out)")
        # If too many single-stock failures, fall back to akshare for the losers.
        if fail > 0:
            print(f"  {fail} stocks failed on baostock; backfilling them via akshare Sina...")
            ok_set = {d["symbol"].iloc[0] for d in all_data}
            missing = [s for s in stock_list if s not in ok_set]
            if missing:
                fill = download_akshare_sina(missing, start, end)
                all_data.extend(fill)
                print(f"  akshare backfill added {len(fill)} stocks")
        bs.logout()
        return all_data
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def _akshare_sina(sym: str, start: str, end: str, max_retry: int = 5) -> pd.DataFrame:
    """Fetch one stock's daily bars from the akshare Sina source (qfq).

    Returns a DataFrame with baostock-compatible columns (same forward-adjusted
    coordinate system). Empty DataFrame on repeated network failure.
    """
    import akshare as ak

    retries = 0
    while retries < max_retry:
        try:
            df = ak.stock_zh_a_daily(
                symbol=sym, start_date=start, end_date=end, adjust="qfq"
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"      akshare {sym} retry {retries}: {str(e)[:80]}")
        retries += 1
        time.sleep(1.5 * retries)
    return pd.DataFrame()


def download_akshare_sina(stock_list, start, end):
    """Download data for ``stock_list`` via akshare Sina source (qfq fallback).

    Returns the same structure as :func:`download`: a list of per-stock
    DataFrames each with baostock-style columns
    (date, open, high, low, close, volume, amount, preclose, pctChg) + symbol.
    The Sina qfq prices match baostock adjustflag=2 exactly, so these frames plug
    straight into map_and_scale.

    Only the A-share (non-北交所) subset is requested; symbol codes are converted
    SH600519 -> sh600519 for the Sina API.
    """
    import akshare as ak  # noqa: F401  (imported for its public API below)

    a_share_list = [s for s in stock_list if not s.startswith("bj")]
    bj_skipped = len(stock_list) - len(a_share_list)
    if bj_skipped:
        print(f"  [akshare] skipped {bj_skipped} 北交所 stocks")
    total = len(a_share_list)
    print(f"  [akshare] fetching {total} stocks from Sina (qfq -> {start}..{end})")
    all_data = []
    fail = 0
    for i, fname in enumerate(a_share_list):
        sym = fname.lower()  # sh600519 works for stock_zh_a_daily
        print(f"    [{i + 1}/{total}] {fname} ...", end="", flush=True)
        df = _akshare_sina(sym, start.replace("-", ""), end.replace("-", ""))
        if df.empty:
            fail += 1
            print(" FAILED", flush=True)
            time.sleep(0.5)
            continue
        # drop any time component -> date string, and coerce to numeric
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        df["open"] = pd.to_numeric(df["open"], errors="coerce")
        df["high"] = pd.to_numeric(df["high"], errors="coerce")
        df["low"] = pd.to_numeric(df["low"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        ok = df.dropna(subset=["close"])
        if ok.empty:
            fail += 1
            print(" NO VALID", flush=True)
            time.sleep(0.5)
            continue
        # state: qfq series uses sina's forward-adjusted coord == baostock flag=2
        ok = ok.copy()
        # preclose = previous day's close; pctChg = today's change %
        ok["preclose"] = ok["close"].shift(1)
        ok["pctChg"] = (ok["close"] / ok["preclose"] - 1.0) * 100.0
        ok["symbol"] = fname.lower()
        all_data.append(ok)
        print(f" OK ({len(ok)} rows)", flush=True)
        time.sleep(0.4)  # be gentle with the Sina API
    if fail:
        print(f"  [akshare] {fail} stocks failed")
    return all_data

def read_all_instruments():
    """Read all.txt as {code_lower: (start, end)}."""
    p = QLIB_DIR / "instruments" / "all.txt"
    if not p.exists():
        return {}
    df = pd.read_csv(p, sep="\t", names=["s", "st", "en"], dtype=str)
    return {row.s.strip().lower(): (row.st.strip(), row.en.strip()) for _, row in df.iterrows()}

def estimate_float_mv(bs_sym: str, end_date: str) -> float | None:
    """Estimate float market value (元) from baostock k-data.

    Uses turn (换手率%) and volume (股) to back out float shares:
        float_shares = volume / (turn/100)
        float_mv = close * float_shares
    Returns None if insufficient data.
    """
    start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(
        bs_sym, "date,close,volume,turn",
        start_date=start, end_date=end_date, frequency="d", adjustflag="2",
    )
    if rs.error_code != "0":
        return None
    best = None
    while rs.next():
        r = rs.get_row_data()
        try:
            close = float(r[1]); vol = float(r[2]); turn = float(r[3])
        except (ValueError, TypeError):
            continue
        if turn > 0 and vol > 0:
            float_shares = vol / (turn / 100.0)
            best = close * float_shares
    return best

def _last_trading_day(year: int, month: int, calendar) -> pd.Timestamp:
    """Last trading day in a given (year, month) from qlib calendar.

    Falls back to the nominal month-end day if that month is not in the
    calendar yet (future month not downloaded).
    """
    sub = [d for d in calendar if d.year == year and d.month == month]
    if sub:
        return max(sub)
    return pd.Timestamp(year, month, _cal.monthrange(year, month)[1])

def _rebalance_dates_between(start, end, calendar):
    """Last trading days of Jun/Dec strictly after start and <= end."""
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    by_ym = {}
    for d in calendar:
        if start_ts < d <= end_ts:
            key = d.strftime("%Y-%m")
            cur = by_ym.get(key)
            if cur is None or d > cur:
                by_ym[key] = d
    return [d for k, d in sorted(by_ym.items()) if d.month in (6, 12)]

def _trading_day_before(d, calendar):
    """Trading day strictly before d, or None."""
    cand = [x for x in calendar if x < pd.Timestamp(d)]
    return cand[-1] if cand else None

def _next_rebalance_date(end_date, calendar) -> pd.Timestamp:
    """Next CSI300 rebalancing date (last trading day of Jun/Dec) after end_date."""
    t = pd.Timestamp(end_date)
    if t.month < 6:
        return _last_trading_day(t.year, 6, calendar)
    if t.month < 12:
        return _last_trading_day(t.year, 12, calendar)
    return _last_trading_day(t.year + 1, 6, calendar)

def sync_csi300_instruments(end_date: str) -> bool:
    """Maintain csi300.txt segments per CSI300 semi-annual rebalance rules.

    CSI300 rebalances on the last trading day of June and December. The
    universe file stores one segment per period: [segment_start, segment_end].

    - If a rebalance date has passed since the current segment started: close
      the old segment at the trading day before the rebalance, and open a new
      segment [rebalance_date, end_date] using baostock's current constituent
      list (query_hs300_stocks).
    - Otherwise: just extend the current segment's end to end_date.
    """
    print("\n[0.6] Syncing csi300 constituents (semi-annual rebalance)...")
    lg = bs.login()
    if lg.error_code != "0":
        print(f"  baostock login failed: {lg.error_msg}, skip")
        return False
    try:
        return _sync_csi300_logged_in(end_date)
    finally:
        bs.logout()

def _sync_csi300_logged_in(end_date: str) -> bool:
    p = QLIB_DIR / "instruments" / "csi300.txt"
    if not p.exists():
        print(f"  {p.name} not found, skip")
        return False
    df = pd.read_csv(p, sep="\t", names=["s", "st", "en"], dtype=str)
    df["s"] = df["s"].str.strip()
    df["st"] = df["st"].str.strip()
    df["en"] = df["en"].str.strip()
    en_prev = df["en"].max()
    cur_mask = df["en"] == en_prev
    st_prev = df.loc[cur_mask, "st"].min()
    cal = read_calendar()
    print(f"  Current segment: {st_prev} -> {en_prev} ({int(cur_mask.sum())} stocks)")

    rs = bs.query_hs300_stocks()
    if rs.error_code != "0":
        print(f"  query_hs300_stocks failed: {rs.error_msg}, skip")
        return False
    curr = set()
    upd = None
    while rs.next():
        row = rs.get_row_data()
        upd = upd or row[0]
        curr.add(row[1].replace(".", "").upper())
    print(f"  Baostock current HS300: {len(curr)} stocks (updateDate={upd})")

    rebal = _rebalance_dates_between(st_prev, end_date, cal)
    if not rebal:
        if end_date > en_prev:
            df.loc[cur_mask, "en"] = end_date
            df.to_csv(p, sep="\t", header=False, index=False)
            print(f"  No rebalance since {st_prev}; extended segment end to {end_date}")
        else:
            print(f"  Already up to date (end={en_prev})")
        return True

    r_first, r_last = rebal[0], rebal[-1]
    prev_day = _trading_day_before(r_first, cal)
    if prev_day is None:
        print(f"  No trading day before rebalance {r_first.date()}, skip")
        return False
    prev_end = prev_day.strftime("%Y-%m-%d")
    r_last_s = r_last.strftime("%Y-%m-%d")
    # close old segment at the trading day before the first rebalance
    df.loc[cur_mask, "en"] = prev_end
    # drop any stale pre-registered lines that this new segment supersedes
    df = df[df["st"] != r_last_s]
    # append new segment with current constituents
    new_rows = pd.DataFrame({"s": sorted(curr), "st": r_last_s, "en": end_date})
    df = pd.concat([df, new_rows], ignore_index=True)
    df = df.sort_values(["st", "s"]).reset_index(drop=True)
    df.to_csv(p, sep="\t", header=False, index=False)
    print(f"  Rebalance {r_first.date()}: closed old segment at {prev_end}")
    print(f"  New segment {r_last_s} -> {end_date}: {len(curr)} stocks")
    return True

def sync_new_stocks(end_date: str):
    """Discover newly-listed stocks (IPO discovery only).

    Pulls the full trading universe via bs.query_all_stock and returns the
    stocks missing from all.txt. Registration of the bins + all.txt entry is
    done by DumpDataUpdate's native "new stock" branch during dump_update
    (it full-dumps from the CSVs and saves all.txt). This avoids pre-registering
    codes that have no bins yet.

    Returns list of dicts: {fname, bs_sym, start_dt, mv}.
    """
    print("\n[0.5] Syncing new stocks (IPO discovery)...")
    lg = bs.login()
    if lg.error_code != "0":
        print(f"  baostock login failed: {lg.error_msg}, skip")
        return []
    try:
        return _sync_new_stocks_logged_in(end_date)
    finally:
        bs.logout()

def _sync_new_stocks_logged_in(end_date: str):
    rs = bs.query_all_stock(day=end_date)
    if rs.error_code != "0":
        print(f"  query_all_stock failed: {rs.error_msg}, skip")
        return []
    live = []
    while rs.next():
        row = rs.get_row_data()
        code, status, name = row[0], row[1], row[2]
        # keep A-shares only (sh/sz). Exclude indices: sh.000xxx, sz.399xxx.
        if status != "1":
            continue
        sym = code.lower()
        if sym.startswith("sz.399"):
            continue
        if sym.startswith(("sh.6", "sh.688", "sz.0", "sz.3")):
            live.append(sym)
    live = sorted(set(live))
    all_inst = read_all_instruments()

    new_codes = [s for s in live if s.replace(".", "").lower() not in all_inst]
    print(f"  Live A-shares: {len(live)}, already in all.txt: {len(live) - len(new_codes)}, new: {len(new_codes)}")
    if not new_codes:
        print("  No new stocks.")
        return []

    results = []
    for i, bs_sym in enumerate(new_codes):
        fname = bs_sym.replace(".", "").lower()
        # estimate float MV (informational; csi300 membership is handled by
        # sync_csi300_instruments using baostock's official HS300 list)
        mv = estimate_float_mv(bs_sym, end_date)
        start_dt = None
        # find IPO date via query_stock_basic
        try:
            rb = bs.query_stock_basic(code=bs_sym)
            if rb.error_code == "0" and rb.next():
                start_dt = rb.get_row_data()[2]  # ipoDate
        except Exception:
            pass
        if not start_dt:
            start_dt = end_date
        results.append({"fname": fname, "bs_sym": bs_sym, "start_dt": start_dt, "mv": mv})
        if i % 20 == 0 or i == len(new_codes) - 1:
            print(f"  [{i+1}/{len(new_codes)}] {fname}: mv={mv/1e8:.0f}亿" if mv else f"  [{i+1}/{len(new_codes)}] {fname}: mv=N/A")

    print(f"  Discovered {len(results)} new stocks (bins registered by DumpDataUpdate)")
    return results

def _bs_query_rows(bs_sym, start_date, end_date):
    """Query one stock's k-data, re-logging in once if the session died.

    baostock long-lived sessions get killed in this environment (Bad file
    descriptor / 接收数据异常), so a single retry with a fresh login covers it.
    """
    def _query():
        return bs.query_history_k_data_plus(
            bs_sym,
            "date,open,high,low,close,volume,amount,pctChg",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",
        )
    rs = _query()
    if rs.error_code != "0":
        try:
            bs.logout()
        except Exception:
            pass
        lg = bs.login()
        if lg.error_code != "0":
            return rs
        rs = _query()
    return rs

def download_new_stocks(new_stocks, end_date: str):
    """Download full history (IPO -> end_date) for newly-listed stocks.

    New stocks have no existing bins so map_and_scale can't be used; we fetch
    the whole history and map it into qlib coordinates directly (a fresh IPO has
    no corporate actions yet, so forward-adjusted close == raw close).
    Returns a DataFrame with the same schema as map_and_scale output.
    """
    rows_out = []
    lg = bs.login()
    if lg.error_code != "0":
        print(f"  baostock login failed: {lg.error_msg}, skip new-stock download")
        return pd.DataFrame()
    try:
        for info in new_stocks:
            bs_sym, start_dt = info["bs_sym"], info["start_dt"]
            try:
                rs = _bs_query_rows(bs_sym, start_dt, end_date)
                if rs.error_code != "0":
                    print(f"    {info['fname']}: query failed ({rs.error_msg}), skip")
                    continue
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    print(f"    {info['fname']}: empty, skip")
                    continue
                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount", "pctChg"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"]).sort_values("date")
                if df.empty or (df["close"] <= 0).all():
                    print(f"    {info['fname']}: no valid close, skip")
                    continue
                first_adjclose = df["close"].iloc[0]
                for _, row in df.iterrows():
                    adjclose = row["close"]
                    close_val = adjclose / first_adjclose
                    fac = 1.0 / first_adjclose
                    vol_val = row["volume"] if pd.notna(row["volume"]) and row["volume"] > 0 else 0
                    amt_val = row["amount"] if pd.notna(row["amount"]) and row["amount"] > 0 else 0
                    rows_out.append({
                        "symbol": info["fname"].lower(),
                        "date": pd.to_datetime(row["date"]),
                        "open": row["open"] * fac if pd.notna(row["open"]) else np.nan,
                        "high": row["high"] * fac if pd.notna(row["high"]) else np.nan,
                        "low": row["low"] * fac if pd.notna(row["low"]) else np.nan,
                        "close": close_val,
                        "volume": vol_val,
                        "amount": amt_val,
                        "adjclose": adjclose,
                        "factor": fac,
                        "change": (row["pctChg"] / 100.0) if pd.notna(row["pctChg"]) else 0,
                        "vwap": (amt_val / vol_val) if vol_val > 0 else close_val,
                    })
                print(f"    {info['fname']}: {len(df)} rows (IPO {start_dt})")
            except Exception as exc:
                print(f"    {info['fname']}: ERROR {exc}, skip")
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    result = pd.DataFrame(rows_out)
    if not result.empty:
        result = result.dropna(subset=["date", "close"])
    return result

def map_and_scale(all_dfs, existing, calendar):
    """Map baostock data to qlib format using per-stock empirical ratios."""
    combined = pd.concat(all_dfs, ignore_index=True)
    for col in ["open", "high", "low", "close", "volume", "amount", "preclose", "pctChg"]:
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors="coerce")
    combined["date"] = pd.to_datetime(combined["date"])
    last_cal = calendar[-1]
    rows_out = []
    stocks_done = 0
    skipped = 0
    for fname, grp in combined.groupby("symbol"):
        grp = grp.sort_values("date").copy()
        if fname not in existing:
            skipped += 1
            continue
        exist = existing[fname]
        first_adjclose = exist["first_adjclose"]
        if first_adjclose is None or first_adjclose <= 0:
            skipped += 1
            continue
        # Find overlap row closest to last_cal
        overlap = grp[grp["date"] <= last_cal]
        if overlap.empty:
            skipped += 1
            continue
        last_overlap_idx = overlap["date"].idxmax()
        last_row = grp.loc[last_overlap_idx]
        adj_fwd = last_row["close"]
        vol_fwd = last_row["volume"]
        if pd.isna(adj_fwd) or adj_fwd <= 0:
            skipped += 1
            continue
        # Empirical ratios
        adj_ratio = exist["last_adjclose"] / adj_fwd
        vol_ratio = exist["last_volume"] / vol_fwd if pd.notna(vol_fwd) and vol_fwd > 0 else 1.0
        amt_ratio = exist["last_amount"] / last_row["amount"] if pd.notna(last_row["amount"]) and last_row["amount"] > 0 else vol_ratio
        for _, row in grp.iterrows():
            d = row["date"]
            adjclose = row["close"] * adj_ratio
            if pd.isna(adjclose) or adjclose <= 0:
                continue
            close_val = adjclose / first_adjclose
            # Scale all forward-adjusted prices to the same (forward-adjusted) basis.
            # open/high/low must be scaled identically to close (they were previously
            # only multiplied by adj_ratio, producing a ~first_adjclose mismatch).
            adj_fac = adj_ratio / first_adjclose
            open_val = row["open"] * adj_fac if pd.notna(row["open"]) else np.nan
            high_val = row["high"] * adj_fac if pd.notna(row["high"]) else np.nan
            low_val = row["low"] * adj_fac if pd.notna(row["low"]) else np.nan
            # Volume and amount: apply empirical ratios directly
            vol = row["volume"]
            vol_val = vol * vol_ratio if pd.notna(vol) and vol > 0 else 0
            amt = row["amount"]
            amt_val = amt * amt_ratio if pd.notna(amt) and amt > 0 else 0
            # vwap = amount / volume
            vwap_val = amt_val / vol_val if vol_val > 0 else close_val
            # change = pctChg / 100
            pct = row["pctChg"] if pd.notna(row["pctChg"]) else 0
            change_val = pct / 100.0
            # factor: scale from adjclose ratio
            # factor_yahoo = adjclose_yahoo / raw_close_yahoo
            # Baostock forward-adjusted = raw_close / (cumulative adjustment)
            # We approximate factor using the existing relationship
            factor_val = exist["last_factor"] if exist["last_factor"] > 0 else 1.0
            rows_out.append({
                "symbol": fname.lower(),
                "date": d,
                "open": open_val,
                "high": high_val,
                "low": low_val,
                "close": close_val,
                "volume": vol_val,
                "amount": amt_val,
                "adjclose": adjclose,
                "factor": factor_val,
                "change": change_val,
                "vwap": vwap_val,
            })
        stocks_done += 1
    print(f"  Mapped {stocks_done} stocks, skipped {skipped}")
    result = pd.DataFrame(rows_out)
    result = result.dropna(subset=["date", "close"])
    return result
def main():
    print("=" * 60)
    print("Updating qlib CN data (baostock)")
    print(f"  Qlib dir: {QLIB_DIR}")
    print("=" * 60)
    # 1. Read existing
    print("\n[1] Reading existing data...")
    calendar = read_calendar()
    print(f"  Calendar: {calendar[0].date()} -> {calendar[-1].date()} ({len(calendar)} days)")

    # Compute date range: incremental from last update
    end = _latest_trading_day()
    start = calendar[-1] - timedelta(days=OVERLAP_DAYS)

    # Discover newly-listed stocks BEFORE the early-exit check
    new_stocks = sync_new_stocks(end.strftime("%Y-%m-%d"))
    # Maintain csi300 segments (semi-annual rebalance) BEFORE the early-exit check
    sync_csi300_instruments(end.strftime("%Y-%m-%d"))

    # If last update was long ago, use full overlap for accurate ratio
    last_upd = _read_last_update()
    last_cal = calendar[-1]
    if last_upd is None or (last_cal.date() - last_upd.date()).days > 60:
        start = end - timedelta(days=35)
        print("  Large gap since last update, using full 35-day overlap")
    else:
        print(f"  Last update: {last_upd.date()}")

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    has_new_day = end.date() > calendar[-1].date()
    print(f"  Download range: {start_str} -> {end_str} (new trading day: {has_new_day})")

    # Existing-stock incremental update (overlap + new dates)
    new_data = pd.DataFrame()
    if has_new_day:
        inst = pd.read_csv(QLIB_DIR / "instruments/all.txt", sep="\t", names=["s", "st", "en"])
        stock_list = inst["s"].str.strip().str.lower().tolist()
        print(f"  {len(stock_list)} instruments")
        print("  Reading last bin values...")
        existing = build_existing_last_values(stock_list, calendar)
        print(f"  Got last values for {len(existing)} stocks")
        # 2. Download
        print(f"\n[2] Downloading baostock data ({start_str} -> {end_str})...")
        all_dfs = download(stock_list, start_str, end_str)
        # 3. Map & scale
        print("\n[3] Mapping and scaling...")
        result_df = map_and_scale(all_dfs, existing, calendar)
        print(f"  Total rows: {len(result_df)}")
        print(f"  Stocks: {result_df['symbol'].nunique()}")
        if not result_df.empty:
            print(f"  Date range: {result_df['date'].min().date()} -> {result_df['date'].max().date()}")
        new_data = result_df[result_df["date"] > calendar[-1]]
        print(f"  New rows (after {calendar[-1].date()}): {len(new_data)}")
        if not new_data.empty:
            print(f"  New dates: {sorted(new_data['date'].unique())[:5]} ...")
    else:
        print(f"  No new trading day (calendar: {calendar[-1].date()}), only processing new stocks")

    # New-stock full history (IPO -> end) mapped into qlib coordinates
    new_stock_data = pd.DataFrame()
    if new_stocks:
        print(f"\n[3b] Downloading full history for {len(new_stocks)} new stocks...")
        new_stock_data = download_new_stocks(new_stocks, end_str)
        if not new_stock_data.empty:
            print(f"  New-stock rows: {len(new_stock_data)}, stocks: {new_stock_data['symbol'].nunique()}")

    all_new = pd.concat([new_data, new_stock_data], ignore_index=True)
    if all_new.empty:
        print("No new data and no new stocks. Already up to date!")
        _write_last_update(end)
        print(f"  Last update record saved: {end.strftime('%Y-%m-%d')}")
        return
    # 5. Save CSVs
    print("\n[4] Saving CSVs for dump_update...")
    tmpdir = Path(tempfile.mkdtemp(prefix="qlib_upd_"))
    csv_dir = tmpdir / "csv"
    csv_dir.mkdir(parents=True)
    for fname, grp in all_new.groupby("symbol"):
        fpath = csv_dir / f"{fname}.csv"
        grp.to_csv(fpath, index=False)
    n_files = len(list(csv_dir.glob("*.csv")))
    print(f"  Saved {n_files} CSVs to {csv_dir}")
    # 6. Run dump_update
    print("\n[5] Running dump_update...")
    du = DumpDataUpdate(
        data_path=str(csv_dir),
        qlib_dir=str(QLIB_DIR),
        freq="day",
        max_workers=8,
        date_field_name="date",
        file_suffix=".csv",
        symbol_field_name="symbol",
        exclude_fields="symbol,date",
    )
    du.dump()
    # 7. Verify
    print("\n[6] Verification...")
    cal_new = read_calendar()
    print(f"  Old: {calendar[-1].date()} ({len(calendar)} days)")
    print(f"  New: {cal_new[-1].date()} ({len(cal_new)} days)")
    print(f"  Added: {len(cal_new) - len(calendar)} days")
    # Quick load test
    sys.path.insert(0, str(Path.home() / "qlib"))
    from qlib import auto_init
    from qlib.data import D
    auto_init(provider_uri=str(QLIB_DIR), region="cn")
    cal = D.calendar()
    print(f"  Qlib calendar: {cal[0].date()} -> {cal[-1].date()} ({len(cal)} days)")
    # Save last update date
    _write_last_update(end)
    print(f"  Last update record saved: {end.strftime('%Y-%m-%d')}")
    print("\nDone!")
if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    main()