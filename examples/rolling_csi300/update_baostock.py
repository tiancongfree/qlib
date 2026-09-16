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
MAX_LAG_LOOKBACK = 90  # only self-heal bins lagging within this many calendar days
# Stocks we already tried to self-heal but whose bin did NOT advance (suspended /
# delisted / no upstream data).  Stored as {code: bin_last_date_at_attempt}.  A
# stock is skipped only while its bin still shows that exact same last date, so
# it self-re-enables the moment real data appears (bin advances -> date differs).
LAG_ATTEMPTS_FILE = Path(__file__).parent / ".lag_attempts.json"

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

def _read_lag_attempts() -> dict:
    """Return {code: last_bin_date_str} for stocks whose self-heal already failed."""
    if LAG_ATTEMPTS_FILE.exists():
        try:
            import json
            return json.loads(LAG_ATTEMPTS_FILE.read_text())
        except Exception:
            return {}
    return {}

def _write_lag_attempts(d: dict):
    import json
    # Drop entries whose bin has since advanced past the recorded date, so the
    # file does not grow unbounded and stocks re-enter self-heal when revived.
    LAG_ATTEMPTS_FILE.write_text(json.dumps(d, sort_keys=True))

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
        if len(adj) < 2 or len(close) != len(adj):
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
            # calendar date of the bin's actual last valid row. bin_idx is a
            # FILE index (0=header, 1=first data row) and must be translated
            # through the bin's start calendar index.
            "last_date": (
                calendar[bin_start + (bin_idx - 1)]
                if 0 <= bin_start + (bin_idx - 1) < len(calendar) else None
            ),
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


# Sentinel results for _query_one_stock, distinguishing the failure modes so the
# caller can decide whether to retry (timeout/failed) or accept (empty symbol).
_TIMEOUT = object()
_FAILED = object()
_EMPTY = object()


def _query_one_stock(bs_sym: str, start: str, end: str):
    """Query one stock, returning a DataFrame or a sentinel.

    - DataFrame        : success (possibly 0-row -> _EMPTY)
    - _TIMEOUT         : per-stock wall-clock timeout (socket hung)
    - _FAILED          : baostock returned error_code != "0" (dead/poisoned session)
    - _EMPTY           : query ok but no rows in range (delisted / suspended)
    """
    rs = _query_stock_timeout(bs_sym, start, end, per_stock_timeout=30)
    if rs is None:
        return _TIMEOUT
    if getattr(rs, "error_code", "1") != "0":
        return _FAILED
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return _EMPTY
    return pd.DataFrame(rows, columns=rs.fields)


def _retry_missing_baostock(missing, start, end, all_data):
    """Retry a list of symbols on baostock after a fresh login.

    Returns the list of symbols that are still missing (to hand to the akshare
    fallback). Appends successes to ``all_data`` in place.
    """
    if not missing:
        return []
    _safe_relogin()
    still = []
    for fname in missing:
        bs_sym = fname_to_bs(fname)
        try:
            df = _query_one_stock(bs_sym, start, end)
        except BaseException as e:  # noqa: BLE001
            print(f"    [retry] {fname} ERROR({type(e).__name__})")
            still.append(fname)
            continue
        if df is _EMPTY:
            continue
        if df is _TIMEOUT or df is _FAILED or df is None:
            still.append(fname)
            continue
        df["symbol"] = fname.lower()
        all_data.append(df)
    print(f"    baostock retry recovered {len(missing) - len(still)}/{len(missing)}")
    return still


def download(stock_list, start, end):
    """Download baostock data for all stocks.

    Falls back to the akshare Sina source (stock_zh_a_daily, adjust=qfq) if the
    baostock login fails. The Sina qfq series is numerically identical to
    baostock's adjustflag=2 (verified across several stocks), so the returned
    data is in the same forward-adjusted coordinate system and map_and_scale
    needs no changes.
    """
    lg = _login_with_timeout(60)
    if lg is None or lg.error_code != "0":
        msg = getattr(lg, "error_msg", "login timed out/returned None (socket reset)")
        print(f"  baostock login failed: {msg}; falling back to akshare Sina...")
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
        # Connection-health watchdog. A single server reset used to poison the
        # baostock session: the following queries all returned error_code != "0"
        # (printed "FAIL"), which did NOT trigger a re-login, so every remaining
        # stock failed with "Broken pipe" and the whole nightly update was lost
        # (2026-09-14: one reset on sz002696 -> 1795/5561 stocks failed, akshare
        # backfill crawled delisted codes and the run was killed before dump).
        # Detect a dead session by consecutive failures and self-heal instead.
        DEAD_CONN_STREAK = 10
        # A half-degraded session (many timeouts but occasional OKs) never hit
        # the consecutive streak threshold, yet throughput collapses and the
        # update crawls for hours (2026-09-15). Also refresh the session when
        # the recent failure rate is high even if not strictly consecutive.
        DEAD_CONN_WINDOW = 20      # look at the last N completed stocks
        DEAD_CONN_WINDOW_HITS = 8  # relogin if >= this many failed in-window
        MAX_RELOGIN_RETRY = 3
        streak = 0
        recent = []  # rolling outcomes: True=success, False=failure
        relogins = 0
        for i in range(0, total, chunk):
            batch = stock_list[i : i + chunk]
            batch_id = i // chunk + 1
            for fname in batch:
                bs_sym = fname_to_bs(fname)
                print(f"    [{batch_id}] {fname} ({bs_sym}) ...", end="", flush=True)
                # Per-stock attempt with connection-health retry. A stock is
                # retried after a detected dead connection (bounded), so a single
                # reset can no longer cascade into losing the rest of the run.
                df = None
                for attempt in range(MAX_RELOGIN_RETRY + 1):
                    try:
                        df = _query_one_stock(bs_sym, start, end)
                    except BaseException as e:  # noqa: BLE001
                        df = _FAILED
                        print(f" ERROR({type(e).__name__})", end="", flush=True)
                    if df is None or df is _FAILED or df is _TIMEOUT:
                        streak += 1
                        recent.append(False)
                        del recent[:-DEAD_CONN_WINDOW]
                        window_hits = recent.count(False)
                        # A long run of failures == the session is dead, not the
                        # individual symbols. Re-login and retry this same stock.
                        dead = streak >= DEAD_CONN_STREAK
                        degraded = (
                            len(recent) >= DEAD_CONN_WINDOW
                            and window_hits >= DEAD_CONN_WINDOW_HITS
                        )
                        if (dead or degraded) and attempt < MAX_RELOGIN_RETRY:
                            tag = "relogin" if dead else "relogin(window)"
                            print(f" [{tag}]", end="", flush=True)
                            if _safe_relogin():
                                relogins += 1
                                streak = 0
                                recent.clear()
                                continue
                        break
                    # success
                    streak = 0
                    recent.append(True)
                    del recent[:-DEAD_CONN_WINDOW]
                    break
                if df is _TIMEOUT:
                    timeout += 1
                    fail += 1
                    print(" TIMEOUT(skip)", flush=True)
                    continue
                if df is _FAILED or df is None:
                    print(" FAIL", flush=True)
                    fail += 1
                    continue
                if df is _EMPTY:
                    print(" empty", flush=True)
                    continue
                df["symbol"] = fname.lower()
                all_data.append(df)
                print(" OK", flush=True)
            progress = min(i + chunk, total)
            print(f"  Batch {batch_id}: {progress}/{total} ({fail} failed, {timeout} timed out, {relogins} relogins)", flush=True)
            time.sleep(0.3)
        if not all_data:
            raise RuntimeError(f"No data downloaded ({fail} failures)")
        print(f"  Downloaded {len(all_data)} stocks, {fail} failures ({timeout} timed out, {relogins} relogins)")
        # Backfill the losers. Retry baostock first (it is much faster than the
        # akshare Sina path and most "failures" are connection blips), then fall
        # back to akshare only for whatever is still missing.
        if fail > 0:
            ok_set = {d["symbol"].iloc[0] for d in all_data}
            missing = [s for s in stock_list if s not in ok_set]
            if missing:
                print(f"  {len(missing)} stocks missing; retrying on baostock first...")
                still = _retry_missing_baostock(missing, start, end, all_data)
                if still:
                    print(f"  {len(still)} stocks still missing; backfilling via akshare Sina...")
                    fill = download_akshare_sina(still, start, end)
                    all_data.extend(fill)
                    print(f"  akshare backfill added {len(fill)} stocks")
        _safe_logout()
        return all_data
    finally:
        _safe_logout()


def _call_with_timeout(fn, timeout: float):
    """Run ``fn`` in a daemon thread, abandoning it if it blocks past timeout.

    baostock's ``login``/``logout`` are unguarded blocking socket calls: when the
    server is half-dead they can block indefinitely inside ``recv`` (observed
    2026-09: a relogin froze the entire update for 3+ hours with the process
    stuck in ``bs.logout``). ``try/except`` cannot interrupt a blocked socket, so
    run the call on a throwaway thread and give up waiting after ``timeout``.

    Returns ``(completed, result)`` -- ``completed`` is False on timeout.
    """
    import threading

    box = {}

    def _run():
        try:
            box["val"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None
    if "err" in box:
        raise box["err"]
    return True, box.get("val")


def _safe_logout(timeout: float = 30):
    """bs.logout() that neither raises nor hangs (socket may be dead/reset)."""
    try:
        _call_with_timeout(bs.logout, timeout)
    except BaseException:  # noqa: BLE001
        pass


def _login_with_timeout(timeout: float = 60):
    """bs.login() guarded by a wall-clock timeout.

    Returns the login result object, or None on timeout/exception.
    """
    try:
        _ok, lg = _call_with_timeout(bs.login, timeout)
    except BaseException:  # noqa: BLE001
        return None
    return lg if _ok else None


def _safe_relogin():
    """bs.logout()+login() recovery that never raises and never hangs.

    Returns True if a fresh baostock session was established. A hung/reset
    socket can make baostock's layer print "接收数据异常" and either return None
    or block inside login; the timeout keeps the per-stock loop alive.
    """
    _safe_logout()
    lg = _login_with_timeout(60)
    return lg is not None and getattr(lg, "error_code", "1") == "0"


def _akshare_sina(sym: str, start: str, end: str, max_retry: int = 2) -> pd.DataFrame:
    """Fetch one stock's daily bars from the akshare Sina source (qfq).

    Returns a DataFrame with baostock-compatible columns (same forward-adjusted
    coordinate system). Empty DataFrame on repeated network failure.

    Retries are kept low (delisted/index codes return "No value to decode"
    deterministically, so retrying them is pure waste) and the backoff is short.
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
            msg = str(e)
            # A code with no bars is a permanent condition; don't retry it.
            if "No value to decode" in msg or "'date'" in msg:
                return pd.DataFrame()
            print(f"      akshare {sym} retry {retries}: {msg[:80]}")
        retries += 1
        time.sleep(1.0 * retries)
    return pd.DataFrame()


def download_akshare_sina(stock_list, start, end, max_seconds: float = 900.0):
    """Download data for ``stock_list`` via akshare Sina source (qfq fallback).

    Returns the same structure as :func:`download`: a list of per-stock
    DataFrames each with baostock-style columns
    (date, open, high, low, close, volume, amount, preclose, pctChg) + symbol.
    The Sina qfq prices match baostock adjustflag=2 exactly, so these frames plug
    straight into map_and_scale.

    Only the A-share (non-北交所) subset is requested; symbol codes are converted
    SH600519 -> sh600519 for the Sina API. A wall-clock deadline bounds the whole
    fallback so it can never run for hours and get killed before dump_update
    (2026-09-14: killed at 107/2080 after ~40min, losing the whole update).
    """
    import akshare as ak  # noqa: F401  (imported for its public API below)

    a_share_list = [s for s in stock_list if not s.startswith("bj")]
    bj_skipped = len(stock_list) - len(a_share_list)
    if bj_skipped:
        print(f"  [akshare] skipped {bj_skipped} 北交所 stocks")
    total = len(a_share_list)
    deadline = time.time() + max_seconds
    print(f"  [akshare] fetching {total} stocks from Sina (qfq -> {start}..{end}, "
          f"deadline {max_seconds:.0f}s)")
    all_data = []
    fail = 0
    done = 0
    for i, fname in enumerate(a_share_list):
        if time.time() > deadline:
            print(f"  [akshare] deadline hit after {done}/{total}; "
                  f"returning {len(all_data)} fetched", flush=True)
            break
        done += 1
        sym = fname.lower()  # sh600519 works for stock_zh_a_daily
        print(f"    [{i + 1}/{total}] {fname} ...", end="", flush=True)
        df = _akshare_sina(sym, start.replace("-", ""), end.replace("-", ""))
        if df.empty:
            fail += 1
            print(" FAILED", flush=True)
            time.sleep(0.2)
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
    lg = _login_with_timeout(60)
    if lg is None or lg.error_code != "0":
        msg = getattr(lg, "error_msg", "login timed out")
        print(f"  baostock login failed: {msg}, skip")
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
    lg = _login_with_timeout(60)
    if lg is None or lg.error_code != "0":
        msg = getattr(lg, "error_msg", "login timed out")
        print(f"  baostock login failed: {msg}, skip")
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
        lg = _login_with_timeout(60)
        if lg is None or lg.error_code != "0":
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
    lg = _login_with_timeout(60)
    if lg is None or lg.error_code != "0":
        msg = getattr(lg, "error_msg", "login timed out")
        print(f"  baostock login failed: {msg}, skip new-stock download")
        return pd.DataFrame()
    try:
        streak = 0
        for info in new_stocks:
            bs_sym, start_dt = info["bs_sym"], info["start_dt"]
            try:
                rs = _bs_query_rows(bs_sym, start_dt, end_date)
                if rs.error_code != "0":
                    streak += 1
                    print(f"    {info['fname']}: query failed ({rs.error_msg}), skip")
                    # Same dead-session watchdog as download(): a few consecutive
                    # failures mean the socket is poisoned, so re-login to keep the
                    # remaining new stocks from cascading into failure.
                    if streak >= 3 and _safe_relogin():
                        streak = 0
                    continue
                streak = 0
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
        # Anchor the ratio on THIS bin's actual last date (fallback to calendar
        # end). Bins can lag the calendar, and using the wrong anchor would
        # scale new prices by a stale/incorrect ratio.
        anchor = exist.get("last_date") or last_cal
        overlap = grp[grp["date"] <= anchor]
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


# ---------------------------------------------------------------------------
# Correct bin appender (replaces qlib's DumpDataUpdate for existing stocks)
#
# qlib's DumpDataUpdate has two defects that silently drop/lose data:
#   A) It decides "which dates to append" from all.txt's end_datetime, and it
#      advances that end BEFORE the bin write succeeds.  Once all.txt claims a
#      date the bin does not actually contain, every later run computes an
#      empty append set and the stock is frozen forever (dump still logs
#      0 errors).  Observed 2026-09: all.txt end=09-09 but bin ended 09-07,
#      for ~5858/6115 stocks.
#   B) In UPDATE mode _data_to_bin blindly appends to the file tail, assuming
#      the bin's last row is exactly the day before the first appended date.
#      When that assumption is broken the appended values are shifted by a day.
# Both are fixed here by treating the BIN CONTENT as the source of truth:
#   - determine each stock's actual last calendar index from its own bin,
#   - append only rows strictly after it, filling any calendar gap with NaN,
#   - write field-by-field so every field stays row-aligned,
#   - update all.txt end ONLY after a successful append.
# ---------------------------------------------------------------------------

DUMP_FIELDS_ORDER = [
    "open", "high", "low", "close", "volume", "amount",
    "adjclose", "factor", "change", "vwap",
]


def _read_bin_last_index(feat_dir: Path, fname: str):
    """Return (start_index, last_idx) of a stock's bins.

    ``start_index`` is the calendar index the bin starts at (stored as the
    first float32).  ``last_idx`` is the calendar index of the last stored
    row.  Returns None if the stock has no usable bin yet.
    """
    close_path = feat_dir / "close.day.bin"
    if not close_path.exists():
        return None
    arr = np.fromfile(str(close_path), dtype="<f")
    if len(arr) < 2:
        return None
    start = int(arr[0])
    return start, start + (len(arr) - 1) - 1  # last row index


def append_new_data_bins(all_new: pd.DataFrame, calendar) -> dict:
    """Append new rows to existing stocks' bins, aligned and validated.

    Parameters
    ----------
    all_new : DataFrame
        Long-format rows with columns symbol/date/open/high/low/close/volume/
        amount/adjclose/factor/change/vwap (symbol lower-case).
    calendar : list[pd.Timestamp]
        Current calendar (AFTER the new dates have been appended).

    Returns
    -------
    dict
        ``{"appended": {fname: last_date_str}, "failed": {fname: reason}}``.
        Only stocks in ``appended`` have had their all.txt end advanced.
    """
    cal_index = {pd.Timestamp(d): i for i, d in enumerate(calendar)}
    appended: dict = {}
    failed: dict = {}

    for fname, grp in all_new.groupby("symbol"):
        fname = str(fname).lower()
        feat_dir = QLIB_DIR / "features" / fname
        info = _read_bin_last_index(feat_dir, fname)
        if info is None:
            failed[fname] = "no close bin"
            continue
        start_idx, last_idx = info
        last_date = calendar[last_idx] if 0 <= last_idx < len(calendar) else None

        grp = grp.sort_values("date")
        grp = grp[grp["date"].notna()]
        # de-duplicate by date (keep the latest row) so the appended block has
        # exactly one row per calendar day
        grp = grp.drop_duplicates(subset=["date"], keep="last")
        # rows to write: strictly newer than the bin's actual last day
        if last_date is not None:
            new_rows = grp[grp["date"] > last_date]
        else:
            new_rows = grp
        if new_rows.empty:
            continue

        # index in the calendar where appending begins / ends. Use the actual
        # last new date (not len(new_rows)) so interior gaps from suspensions
        # keep the block calendar-aligned.
        append_from = cal_index.get(pd.Timestamp(new_rows["date"].min()))
        append_to = cal_index.get(pd.Timestamp(new_rows["date"].max()))
        if append_from is None or append_to is None:
            failed[fname] = "new date not in calendar"
            continue
        if append_to < append_from:
            failed[fname] = "inverted date range"
            continue
        expected_start = last_idx + 1
        if append_from != expected_start:
            # gap between bin tail and first new date -> must pad with NaN so
            # the written rows stay calendar-aligned (never shift the tail).
            gap = append_from - expected_start
        else:
            gap = 0

        # build the appended block, calendar-aligned across the full span
        span = [calendar[i] for i in range(append_from, append_to + 1)]
        block = new_rows.set_index("date").reindex(span)
        # interior all-NaN rows are legitimate (suspension days); only require
        # that the block spans exactly the calendar slot we computed
        if len(block) != (append_to - append_from + 1):
            failed[fname] = "calendar/row date mismatch"
            continue

        try:
            expected_total = (last_idx - start_idx + 1) + 1
            # pre-validate ALL field bins before writing ANY of them, so a
            # mismatch can never leave a partially-appended stock
            to_write = []  # (bin_path, vals)
            for field in DUMP_FIELDS_ORDER:
                bin_path = feat_dir / f"{field}.day.bin"
                if not bin_path.exists():
                    continue
                cur = np.fromfile(str(bin_path), dtype="<f")
                if len(cur) != expected_total:
                    raise ValueError(
                        f"{field}.day.bin length {len(cur)} != expected "
                        f"{expected_total}"
                    )
                if field in block.columns:
                    vals = block[field].to_numpy(dtype="<f")
                else:
                    vals = np.full(len(block), np.nan, dtype="<f")
                if gap > 0:
                    vals = np.concatenate([np.full(gap, np.nan, dtype="<f"), vals])
                to_write.append((bin_path, vals))
            for bin_path, vals in to_write:
                with bin_path.open("ab") as fp:
                    vals.astype("<f").tofile(fp)
            appended[fname] = str(new_rows["date"].max().date())
        except Exception as e:  # noqa: BLE001
            failed[fname] = f"{type(e).__name__}: {e}"

    return {"appended": appended, "failed": failed}


def _update_all_txt_end(appended: dict):
    """Advance all.txt end ONLY for stocks whose bins were actually written."""
    p = QLIB_DIR / "instruments" / "all.txt"
    if not p.exists() or not appended:
        return
    df = pd.read_csv(p, sep="\t", names=["s", "st", "en"], dtype=str)
    df["_key"] = df["s"].str.strip().str.lower()
    mask = df["_key"].isin(appended.keys())
    df.loc[mask, "en"] = df.loc[mask, "_key"].map(appended)
    df = df.drop(columns=["_key"])
    df.to_csv(p, sep="\t", header=False, index=False)


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

    # Existing-stock incremental update (overlap + new dates). Also runs when
    # the calendar is already current but some bins lag (a previous update
    # dropped rows) so the corruption can self-heal on a plain re-run.
    new_data = pd.DataFrame()
    inst = pd.read_csv(QLIB_DIR / "instruments/all.txt", sep="\t", names=["s", "st", "en"])
    stock_list = inst["s"].str.strip().str.lower().tolist()

    # Self-heal detection: find bins that lag the calendar. Delisted stocks
    # (data frozen 2001-2002, etc.) are ignored via MAX_LAG_LOOKBACK so we
    # never widen the window to 25 years of history.  Additionally, a stock
    # whose bin was already self-healed once at the SAME last date is skipped:
    # that means upstream has no new data for it (suspended / delisted), so
    # retrying every day only re-downloads 6000 stocks and stalls the morning
    # run.  The skip clears automatically if the bin later advances.
    cutoff = calendar[-1] - timedelta(days=MAX_LAG_LOOKBACK)
    lag_attempts = _read_lag_attempts()
    lagging = []  # (fname, bin_last_date)
    worst_lag = None
    skipped_stuck = 0
    attempted_now = {}
    for fname in stock_list:
        # 北交所 (bj.*) is never downloaded (download() filters it out), so it
        # must not be treated as a self-heal target either.
        if fname.startswith("bj"):
            continue
        info = _read_bin_last_index(QLIB_DIR / "features" / fname, fname)
        if info is None:
            continue
        _, last_idx = info
        if not (0 <= last_idx < len(calendar)):
            continue
        d = calendar[last_idx]
        if d < cutoff:
            continue  # delisted / long-dead
        if d < calendar[-1]:
            if lag_attempts.get(fname) == d.strftime("%Y-%m-%d"):
                skipped_stuck += 1
                attempted_now[fname] = d.strftime("%Y-%m-%d")
                continue
            lagging.append((fname, d))
            attempted_now[fname] = d.strftime("%Y-%m-%d")
            if worst_lag is None or d < worst_lag:
                worst_lag = d
    if skipped_stuck:
        print(f"  Skipping {skipped_stuck} stuck bins (heal already tried at same date)")
    _write_lag_attempts(attempted_now)

    if has_new_day or lagging:
        print(f"  {len(stock_list)} instruments")
        if lagging:
            worst_s = worst_lag.date() if worst_lag is not None else "-"
            print(f"  Detected {len(lagging)} lagging bins (worst recent: {worst_s}) -> self-heal")

        print("  Reading last bin values...")
        existing = build_existing_last_values(stock_list, calendar)
        print(f"  Got last values for {len(existing)} stocks")

        # 2. Download normal window for all stocks
        print(f"\n[2] Downloading baostock data ({start_str} -> {end_str})...")
        all_dfs = download(stock_list, start_str, end_str)
        # Targeted second pass for lagging stocks (widened range). This keeps
        # the heavy normal window small while still recovering old gaps.
        if lagging and worst_lag is not None:
            lag_codes = [c for c, _ in lagging]
            lag_start = (worst_lag - timedelta(days=2)).strftime("%Y-%m-%d")
            print(f"  Re-downloading {len(lag_codes)} lagging stocks ({lag_start} -> {end_str})...")
            all_dfs = all_dfs + download(lag_codes, lag_start, end_str)
        # 3. Map & scale
        print("\n[3] Mapping and scaling...")
        result_df = map_and_scale(all_dfs, existing, calendar)
        print(f"  Total rows: {len(result_df)}")
        print(f"  Stocks: {result_df['symbol'].nunique()}")
        if not result_df.empty:
            print(f"  Date range: {result_df['date'].min().date()} -> {result_df['date'].max().date()}")
        # Keep the whole overlap range: the per-stock appender filters by each
        # bin's ACTUAL last day, so a stock lagging further back than
        # calendar[-1] still gets all of its missing days (not just the newest).
        new_data = result_df[result_df["date"] > calendar[-1] - timedelta(days=MAX_LAG_LOOKBACK)]
        strict_new = result_df[result_df["date"] > calendar[-1]]
        print(f"  New rows (after {calendar[-1].date()}): {len(strict_new)}")
        if not strict_new.empty:
            print(f"  New dates: {sorted(strict_new['date'].unique())[:5]} ...")
    else:
        print(f"  No new trading day (calendar: {calendar[-1].date()}), no lagging bins")

    # New-stock full history (IPO -> end) mapped into qlib coordinates
    new_stock_data = pd.DataFrame()
    if new_stocks:
        print(f"\n[3b] Downloading full history for {len(new_stocks)} new stocks...")
        new_stock_data = download_new_stocks(new_stocks, end_str)
        if not new_stock_data.empty:
            print(f"  New-stock rows: {len(new_stock_data)}, stocks: {new_stock_data['symbol'].nunique()}")

    if new_data.empty and new_stock_data.empty:
        print("No new data and no new stocks. Already up to date!")
        _write_last_update(end)
        print(f"  Last update record saved: {end.strftime('%Y-%m-%d')}")
        return

    # 5. Extend the calendar first (bins are addressed by calendar index), then
    #    append existing stocks' rows with our aligned/validated appender.
    #    New stocks keep using qlib's DumpDataUpdate (they have no bin yet, so
    #    its append-only path is correct for them).
    if not new_data.empty:
        print("\n[4] Extending calendar...")
        cal_new = sorted(set(calendar) | set(pd.Timestamp(d) for d in new_data["date"].unique()))
        np.savetxt(str(QLIB_DIR / "calendars/day.txt"), [d.strftime("%Y-%m-%d") for d in cal_new],
                   fmt="%s", encoding="utf-8")
        print(f"  Calendar: {calendar[-1].date()} -> {cal_new[-1].date()} ({len(cal_new)} days)")
        calendar = cal_new

        print("\n[5] Appending existing stocks' bins (content-aligned)...")
        res = append_new_data_bins(new_data, calendar)
        _update_all_txt_end(res["appended"])
        print(f"  Appended: {len(res['appended'])} stocks")
        if res["failed"]:
            print(f"  FAILED: {len(res['failed'])} stocks (all.txt end NOT advanced)")
            for k, v in list(res["failed"].items())[:10]:
                print(f"    - {k}: {v}")

    if not new_stock_data.empty:
        print("\n[5b] Dumping new stocks (full history) via DumpDataUpdate...")
        tmpdir = Path(tempfile.mkdtemp(prefix="qlib_upd_"))
        csv_dir = tmpdir / "csv"
        csv_dir.mkdir(parents=True)
        for fname, grp in new_stock_data.groupby("symbol"):
            grp.to_csv(csv_dir / f"{fname}.csv", index=False)
        print(f"  Saved {len(list(csv_dir.glob('*.csv')))} CSVs to {csv_dir}")
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

    # 6. Verify
    print("\n[6] Verification...")
    cal_new = read_calendar()
    print(f"  Calendar now ends: {cal_new[-1].date()} ({len(cal_new)} days)")

    # independence check: how many relevant stocks' close bin reaches the
    # calendar end? bj.* is excluded (never downloaded) and delisted stocks
    # are excluded (data legitimately frozen long ago).
    last_ts = cal_new[-1]
    reach = 0
    total = 0
    recent_miss = 0
    for feat_dir in (QLIB_DIR / "features").iterdir():
        if not feat_dir.is_dir():
            continue
        fname = feat_dir.name
        if fname.startswith("bj"):
            continue
        info = _read_bin_last_index(feat_dir, fname)
        if info is None:
            continue
        total += 1
        start_idx, last_idx = info
        if not (0 <= last_idx < len(cal_new)):
            continue
        d = cal_new[last_idx]
        if d == last_ts:
            reach += 1
        elif d >= last_ts - timedelta(days=MAX_LAG_LOOKBACK):
            recent_miss += 1
    print(f"  Stocks with bin reaching {last_ts.date()}: {reach}/{total} (non-bj)")
    if recent_miss:
        print(f"  WARNING: {recent_miss} non-bj stocks still lag within {MAX_LAG_LOOKBACK}d")

    # Save last update date
    _write_last_update(end)
    print(f"  Last update record saved: {end.strftime('%Y-%m-%d')}")
    print("\nDone!")
if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    try:
        main()
    except KeyboardInterrupt:
        # A killed run must exit non-zero so the scheduler/monitor sees the
        # failure instead of logging "completed successfully" (2026-09-14: the
        # run was interrupted during akshare backfill yet the ps1 wrapper
        # reported success because the exception was swallowed).
        print("\n[update_baostock] INTERRUPTED before completion", file=sys.stderr, flush=True)
        sys.exit(130)
    except BaseException as e:  # noqa: BLE001
        print(f"\n[update_baostock] FAILED: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        sys.exit(1)