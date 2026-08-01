"""
Update fundamental data (valuation + financial indicators) into qlib format.

Sources (via tushare proxy at http://jiaoch.site):
  - daily_basic     → daily features (pe_ttm, pb, ps_ttm, dv_ttm, total_mv, circ_mv, turnover_rate)
  - fina_indicator  → PIT data (roe, roa, grossprofit_margin, netprofit_margin, ...)

Outputs:
  - ~/.qlib/qlib_data/cn_data/features/<code>/pe_ttm.day.bin  (daily valuation)
  - ~/.qlib/qlib_data/cn_data/financial/<code>/<field>_q.data/.index  (PIT)

Usage:
  python update_fundamentals.py            # full update
  python update_fundamentals.py --limit 5  # test on first 5 stocks
"""

import sys
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"

TOKEN = os.environ.get("TUSHARE_TOKEN")
API_URL = os.environ.get("TUSHARE_API_URL", "http://jiaoch.site")

# daily_basic valuation fields → dumped as daily features
DAILY_BASIC_FIELDS = ["pe_ttm", "pb", "ps_ttm", "dv_ttm", "total_mv", "circ_mv", "turnover_rate"]

# fina_indicator fields → dumped as PIT quarterly data (field name must be the CSV `field` column)
FINA_INDICATOR_FIELDS = [
    "roe",
    "roe_waa",
    "roa",
    "grossprofit_margin",
    "netprofit_margin",
    "debt_to_assets",
    "current_ratio",
    "quick_ratio",
    "or_yoy",
    "netprofit_yoy",
    "q_gr_yoy",
    "q_profit_yoy",
    "ocf_to_or",
    "eps",
]

START_DATE = "20050101"
END_DATE = "20260731"
DELAY = 0.3  # seconds between API calls
MAX_RETRY = 3

sys.path.insert(0, str(Path.home() / "qlib" / "scripts"))
from dump_pit import DumpPitData  # noqa: E402


def init_pro():
    import tushare as ts

    if not TOKEN:
        raise RuntimeError("TUSHARE_TOKEN env var not set")
    pro = ts.pro_api(TOKEN)
    pro._DataApi__token = TOKEN
    pro._DataApi__http_url = API_URL
    return pro


def safe_call(pro, api_name, **kwargs):
    """Call a tushare API with retry + delay."""
    for attempt in range(MAX_RETRY):
        try:
            df = getattr(pro, api_name)(**kwargs)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            if attempt < MAX_RETRY - 1:
                time.sleep(DELAY * (attempt + 1))
            else:
                print(f"  ! {api_name} failed: {str(e)[:100]}")
        time.sleep(DELAY)
    return None


def fetch_fina_indicator(pro, ts_code, fields):
    """Fetch fina_indicator in ~5-year chunks.

    The proxy truncates large-range calls (~55 rows), so split the full range
    into overlapping windows to recover the complete history.
    """
    frames = []
    windows = [
        ("20050101", "20141231"),
        ("20150101", END_DATE),
    ]
    for s, e in windows:
        df = safe_call(
            pro, "fina_indicator",
            ts_code=ts_code, start_date=s, end_date=e, fields=fields,
        )
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["ann_date", "end_date"] + [f for f in FINA_INDICATOR_FIELDS if f in out.columns])
    return out


def to_ts_code(inst: str) -> str:
    """SH600519 -> 600519.SH"""
    ex = inst[:2].lower()
    code = inst[2:]
    suffix = "SH" if ex == "sh" else "SZ" if ex == "sz" else "BJ"
    return f"{code}.{suffix}"


def to_fname(inst: str) -> str:
    """SH600519 -> sh600519"""
    return inst.lower()


def read_instruments():
    inst = pd.read_csv(QLIB_DIR / "instruments/csi300.txt", sep="\t", names=["s", "st", "en"])
    codes = inst["s"].str.strip().str.upper()
    return sorted(codes.unique())


def quarter_of(end_date):
    """end_date YYYYMMDD -> YYYYQ int (e.g. 20231231 -> 202304)"""
    end_date = str(end_date)
    y = int(end_date[:4])
    m = int(end_date[4:6])
    return y * 100 + (m - 1) // 3 + 1


def period_date(ann_date) -> str:
    """ann_date -> str YYYY-MM-DD for the PIT `date` column (keeps it a string in CSV)."""
    d = str(int(ann_date))
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def dump_daily_basic_bins(pro, instrument, code):
    """Fetch daily_basic for one stock and append to features/<code>/<field>.day.bin.

    The bin format: header float32 = start_index (into the global calendar), then
    one value per calendar day from that index onward.
    """
    features_dir = QLIB_DIR / "features" / to_fname(code)
    if not features_dir.exists():
        return

    # Determine where to start fetching (incremental): read the largest end_index
    # among existing daily_basic bins.
    cal = read_calendar()
    start_idx = None
    for field in DAILY_BASIC_FIELDS:
        bin_path = features_dir / f"{field}.day.bin"
        if bin_path.exists():
            arr = np.fromfile(str(bin_path), dtype="<f")
            if len(arr) > 0:
                idx = int(arr[0])
                end = idx + len(arr) - 1
                start_idx = end if start_idx is None else max(start_idx, end)
    if start_idx is not None and start_idx >= len(cal) - 1:
        return  # already up to date

    df = safe_call(
        pro,
        "daily_basic",
        ts_code=to_ts_code(code),
        start_date=START_DATE,
        end_date=END_DATE,
        fields="ts_code,trade_date," + ",".join(DAILY_BASIC_FIELDS),
    )
    if df is None:
        return

    df["date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("date").drop_duplicates("date").set_index("date")

    # Map to global calendar indices
    cal_idx = {ts: i for i, ts in enumerate(cal)}
    pos = df.index.map(cal_idx)
    df = df[pos.notna()]
    pos = pos[pos.notna()].astype(int)
    if df.empty:
        return

    for field in DAILY_BASIC_FIELDS:
        if field not in df.columns:
            continue
        bin_path = features_dir / f"{field}.day.bin"
        series = pd.Series(df[field].values, index=pos)
        first_idx = int(series.index.min())
        last_idx = int(series.index.max())
        # Build contiguous array over [first_idx, last_idx]
        data = np.full(last_idx - first_idx + 1, np.nan, dtype=np.float32)
        for i, v in series.items():
            if pd.notna(v):
                data[int(i) - first_idx] = np.float32(v)
        np.hstack([np.float32(first_idx), data]).astype("<f").tofile(str(bin_path))


def read_calendar():
    cal = pd.read_csv(QLIB_DIR / "calendars/day.txt", header=None).iloc[:, 0].tolist()
    return [pd.Timestamp(d) for d in cal]


def dump_fina_pit(pro, instrument, code, tmp_dir):
    """Fetch fina_indicator for one stock and write a PIT CSV.

    CSV columns (per dump_pit.py): date, period, value, field
    - date   = announcement date (ann_date) as int YYYYMMDD
    - period = end_date as YYYYQ int
    - value  = indicator value
    - field  = indicator name (e.g. "roe")
    """
    df = fetch_fina_indicator(pro, to_ts_code(code), "ts_code,ann_date,end_date," + ",".join(FINA_INDICATOR_FIELDS))
    if df is None or df.empty:
        return

    # Deduplicate identical rows (tushare can return duplicate rows)
    df = df.drop_duplicates()
    df = df.dropna(subset=["ann_date", "end_date"])

    records = []
    for field in FINA_INDICATOR_FIELDS:
        sub = df[["ann_date", "end_date", field]].dropna(subset=[field]).copy()
        if sub.empty:
            continue
        sub["date"] = sub["ann_date"].apply(period_date)
        sub["period"] = sub["end_date"].apply(quarter_of)
        sub["value"] = sub[field].astype(np.float64)
        sub["field"] = field
        records.append(sub[["date", "period", "value", "field"]])

    if not records:
        return
    out = pd.concat(records, ignore_index=True)
    out.to_csv(tmp_dir / f"{to_fname(code)}.csv", index=False)


def main(limit: int = 0):
    print("=" * 60)
    print("  Update fundamental data (tushare proxy)")
    print(f"  Qlib dir: {QLIB_DIR}")
    print("=" * 60)

    pro = init_pro()
    codes = read_instruments()
    if limit > 0:
        codes = codes[:limit]
    print(f"  {len(codes)} instruments")

    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="fund_pit_"))
    print(f"  PIT tmp dir: {tmp_dir}")

    print("\n[1] Dumping daily_basic valuation features...")
    n_ok = 0
    for i, code in enumerate(codes):
        before = {f: (QLIB_DIR / "features" / to_fname(code) / f"{f}.day.bin").exists() for f in DAILY_BASIC_FIELDS}
        dump_daily_basic_bins(pro, code, code)
        after = {f: (QLIB_DIR / "features" / to_fname(code) / f"{f}.day.bin").exists() for f in DAILY_BASIC_FIELDS}
        if any(after.values()):
            n_ok += 1
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(codes)}] ...")
    print(f"  daily_basic done: {n_ok}/{len(codes)} stocks have bins")

    print("\n[2] Building fina_indicator PIT CSVs...")
    for i, code in enumerate(codes):
        dump_fina_pit(pro, code, code, tmp_dir)
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(codes)}] ...")

    print("\n[3] Dumping PIT data into financial/ ...")
    dp = DumpPitData(csv_path=str(tmp_dir), qlib_dir=str(QLIB_DIR), max_workers=8)
    dp.dump(interval="quarterly", overwrite=True)

    print("\nDone!")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
