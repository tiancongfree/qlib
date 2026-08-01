"""Backfill missing OHLCV data for stocks whose bins lag behind the calendar.

Root cause: the 2026-07-31 update_baostock.py run was interrupted (only ~145 of
~6082 stocks got 6-15..7-31 data appended). This script finds stocks whose last
bin row is before calendar[-1] and appends the missing dates using the same
map_and_scale + DumpDataUpdate pipeline.
"""
import sys
import os
import socket
import time
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import baostock as bs

sys.path.insert(0, str(Path.home() / "qlib"))
from scripts.dump_bin import DumpDataUpdate

QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"
OVERLAP = timedelta(days=10)

def read_calendar():
    cal = pd.read_csv(QLIB_DIR / "calendars/day.txt", header=None).iloc[:, 0].tolist()
    return [pd.Timestamp(d) for d in cal]

def fname_to_bs(fname):
    return f"{fname[:2]}.{fname[2:]}"

def stock_last_date(calendar):
    """Return {fname: (last_cal_idx, last_close)} for each stock bin."""
    out = {}
    for d in sorted((QLIB_DIR / "features").iterdir()):
        if not d.is_dir():
            continue
        adj_p = d / "adjclose.day.bin"
        if not adj_p.exists():
            continue
        adj = np.fromfile(str(adj_p), dtype="<f")
        if len(adj) < 3:
            continue
        start = int(adj[0])
        last_idx = len(adj) - 1
        last_cal_idx = start + last_idx - 1
        if last_cal_idx >= len(calendar):
            continue
        close = np.fromfile(str(d / "close.day.bin"), dtype="<f")
        out[d.name] = (last_cal_idx, float(close[-1]) if len(close) == len(adj) else 0.0)
    return out

def main():
    print("=" * 60)
    print("Backfilling missing stock data (interrupted 7-31 update)")
    print("=" * 60)
    calendar = read_calendar()
    cal_last = len(calendar) - 1
    target = calendar[cal_last]
    print(f"  Calendar last: {target.date()} ({len(calendar)} days)")

    last_dates = stock_last_date(calendar)
    # Only backfill stocks whose last bin row is recent (within ~90 trading days).
    # Old last dates => delisted / long-suspended; nothing to backfill.
    recent_threshold = cal_last - 90
    lagging = []
    for fname, (last_idx, _) in last_dates.items():
        # skip 北交所 and index-like instruments
        if fname.startswith("bj") or fname[:2] not in ("sh", "sz"):
            continue
        if recent_threshold <= last_idx < cal_last:
            lagging.append((fname, last_idx))
    lagging.sort()
    print(f"  Lagging stocks (within {cal_last - recent_threshold} days): {len(lagging)}")
    for f in lagging[:8]:
        print(f"    {f[0]}: last={calendar[f[1]].date()}")
    print("  ...")

    if not lagging:
        print("  Nothing to do.")
        return

    # Optional pilot mode for testing the pipeline on a subset
    pilot = os.environ.get("BACKFILL_PILOT")
    if pilot:
        n = int(pilot)
        print(f"  PILOT MODE: only first {n} stocks")
        lagging = lagging[:n]

    # Optional chunked mode: process a slice [start, end)
    bstart = int(os.environ.get("BACKFILL_START", "0"))
    bend = int(os.environ.get("BACKFILL_END", str(len(lagging))))
    if bstart > 0 or bend < len(lagging):
        print(f"  CHUNK MODE: [{bstart}, {bend}) of {len(lagging)}")
        lagging = lagging[bstart:bend]

    socket.setdefaulttimeout(30)
    all_data = []
    fail = 0
    total = len(lagging)
    end_dt = target.strftime("%Y-%m-%d")
    BATCH_LOGIN = 400
    lg = None
    for i, (fname, last_idx) in enumerate(lagging):
        # Re-login periodically to avoid long-lived connection issues
        if i % BATCH_LOGIN == 0:
            if lg is not None:
                try:
                    bs.logout()
                except Exception:
                    pass
            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"baostock login failed: {lg.error_msg}")
            print(f"  (re-login @ {i+1})", flush=True)
        bs_sym = fname_to_bs(fname)
        # Per-stock download window: a few days before its own last date
        start_dt = (calendar[last_idx] - OVERLAP).strftime("%Y-%m-%d")
        if i % 50 == 0 or i == total - 1:
            print(f"  [{i+1}/{total}] {fname} ({calendar[last_idx].date()}->{end_dt})", flush=True)
        try:
            rs = bs.query_history_k_data_plus(
                bs_sym,
                "date,open,high,low,close,volume,amount,preclose,pctChg",
                start_date=start_dt,
                end_date=end_dt,
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
            df["symbol"] = fname
            all_data.append(df)
        except Exception:
            fail += 1
        if i % 200 == 199:
            time.sleep(0.5)
    try:
        bs.logout()
    except Exception:
        pass
    print(f"  Downloaded {len(all_data)} stocks, {fail} failures")

    if not all_data:
        print("  Nothing downloaded, aborting.")
        return

    # Build last values from each bin's actual last row
    existing = {}
    for fname, last_idx in lagging:
        feat_dir = QLIB_DIR / "features" / fname
        adj = np.fromfile(str(feat_dir / "adjclose.day.bin"), dtype="<f")
        close = np.fromfile(str(feat_dir / "close.day.bin"), dtype="<f")
        factor = np.fromfile(str(feat_dir / "factor.day.bin"), dtype="<f")
        volume = np.fromfile(str(feat_dir / "volume.day.bin"), dtype="<f")
        amount = np.fromfile(str(feat_dir / "amount.day.bin"), dtype="<f")
        bin_idx = len(adj) - 1
        # first_adjclose = first valid (finite, >0) adjclose after header
        fa = adj[1]
        if not (np.isfinite(fa) and fa > 0):
            valid = np.where(np.isfinite(adj[1:]) & (adj[1:] > 0))[0]
            fa = adj[1 + valid[0]] if len(valid) else np.nan
        existing[fname] = {
            "first_adjclose": fa,
            "last_adjclose": adj[bin_idx],
            "last_close": close[bin_idx],
            "last_factor": factor[bin_idx],
            "last_volume": volume[bin_idx],
            "last_amount": amount[bin_idx],
        }

    # Reuse map_and_scale from update_baostock
    sys.path.insert(0, str(Path(__file__).parent))
    import update_baostock as ub

    result = ub.map_and_scale(all_data, existing, calendar)

    # Filter: only keep rows strictly after each stock's last bin date
    new_rows = []
    for fname, grp in result.groupby("symbol"):
        if fname not in last_dates:
            continue
        last_idx, _ = last_dates[fname]
        last_date = calendar[last_idx]
        grp = grp[grp["date"] > last_date]
        new_rows.append(grp)
    new_data = pd.concat(new_rows, ignore_index=True) if new_rows else pd.DataFrame()
    print(f"  New rows to append: {len(new_data)}")

    if new_data.empty:
        print("  Nothing to append.")
        return

    tmpdir = Path(tempfile.mkdtemp(prefix="qlib_backfill_"))
    csv_dir = tmpdir / "csv"
    csv_dir.mkdir(parents=True)
    for fname, grp in new_data.groupby("symbol"):
        grp.to_csv(csv_dir / f"{fname}.csv", index=False)
    print(f"  Saved {len(list(csv_dir.glob('*.csv')))} CSVs")

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
    print("  DumpDataUpdate done.")

    # Verify
    last_dates2 = stock_last_date(calendar)
    still = sum(1 for f, (idx, _) in last_dates2.items() if idx < cal_last and f[:2] in ("sh", "sz"))
    print(f"  Stocks still lagging after backfill: {still}")
    print("Done!")

if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    main()
