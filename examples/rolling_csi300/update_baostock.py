"""Update qlib CN daily data using baostock.
Computes per-stock empirical ratios from overlap dates and applies them to new dates.
Supports incremental update: only fetches a few overlap days + new dates.
"""
import sys
import time
import tempfile
import warnings
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import baostock as bs
sys.path.insert(0, str(Path.home() / "qlib"))
from scripts.dump_bin import DumpDataUpdate
QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"
LAST_UPDATE_FILE = Path(__file__).parent / ".last_update.txt"
OVERLAP_DAYS = 5  # enough for ratio computation

def _latest_trading_day() -> datetime:
    """Return the most recent probable trading day (date only, time=midnight)."""
    d = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    if d.weekday() == 5:   # Saturday -> Friday
        d -= timedelta(days=1)
    elif d.weekday() == 6:  # Sunday -> Friday
        d -= timedelta(days=2)
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
    """For each stock, read last adjclose, close, factor, volume, amount from bin."""
    last_cal_idx = len(calendar) - 1
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
        bin_start = int(adj[0])
        bin_idx = last_cal_idx - bin_start + 1
        if bin_idx < 1 or bin_idx >= len(adj):
            continue
        results[fname] = {
            "first_adjclose": adj[1],
            "last_adjclose": adj[bin_idx],
            "last_close": close[bin_idx],
            "last_factor": factor[bin_idx],
            "last_volume": volume[bin_idx],
            "last_amount": amount[bin_idx],
        }
    return results
def download(stock_list, start, end):
    """Download baostock data for all stocks."""
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    all_data = []
    total = len(stock_list)
    fail = 0
    chunk = 200
    for i in range(0, total, chunk):
        batch = stock_list[i : i + chunk]
        batch_id = i // chunk + 1
        for fname in batch:
            bs_sym = fname_to_bs(fname)
            try:
                # adjustflag=2: forward-adjusted (all prices at current level)
                rs = bs.query_history_k_data_plus(
                    bs_sym,
                    "date,open,high,low,close,volume,amount,preclose,pctChg",
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="2",
                )
                if rs.error_code != "0":
                    fail += 1
                    continue
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    continue
                df = pd.DataFrame(rows, columns=rs.fields)
                df["symbol"] = fname.lower()
                all_data.append(df)
            except Exception:
                fail += 1
        progress = min(i + chunk, total)
        print(f"  Batch {batch_id}: {progress}/{total} ({fail} failed)")
        time.sleep(0.3)
    bs.logout()
    if not all_data:
        raise RuntimeError(f"No data downloaded ({fail} failures)")
    print(f"  Downloaded {len(all_data)} stocks, {fail} failures")
    return all_data
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
            # Scale all forward-adjusted prices
            open_val = row["open"] * adj_ratio if pd.notna(row["open"]) else np.nan
            high_val = row["high"] * adj_ratio if pd.notna(row["high"]) else np.nan
            low_val = row["low"] * adj_ratio if pd.notna(row["low"]) else np.nan
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

    # Quick check: if no new trading day, skip entirely
    if end.date() <= calendar[-1].date():
        print(f"  Already up to date (calendar: {calendar[-1].date()}, target: {end.date()})")
        _write_last_update(end)
        return

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
    print(f"  Download range: {start_str} -> {end_str}")

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
    # 4. Filter new dates only
    new_data = result_df[result_df["date"] > calendar[-1]]
    print(f"  New rows (after {calendar[-1].date()}): {len(new_data)}")
    if not new_data.empty:
        print(f"  New dates: {sorted(new_data['date'].unique())[:5]} ...")
    if new_data.empty:
        print("No new data. Already up to date!")
        _write_last_update(end)
        print(f"  Last update record saved: {end.strftime('%Y-%m-%d')}")
        return
    # 5. Save CSVs
    print("\n[4] Saving CSVs for dump_update...")
    tmpdir = Path(tempfile.mkdtemp(prefix="qlib_upd_"))
    csv_dir = tmpdir / "csv"
    csv_dir.mkdir(parents=True)
    for fname, grp in new_data.groupby("symbol"):
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