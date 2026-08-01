"""Re-dump fina_indicator PIT data (with chunked fetching to avoid proxy truncation)."""
import sys
import time
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"
TOKEN = "6bc84c24d67b2b5a3168353a298309abae23298e388a34952f0fd2eec7f0"
API_URL = "http://jiaoch.site"
FINA_INDICATOR_FIELDS = [
    "roe", "roe_waa", "roa", "grossprofit_margin", "netprofit_margin",
    "debt_to_assets", "current_ratio", "quick_ratio", "or_yoy", "netprofit_yoy",
    "q_gr_yoy", "q_profit_yoy", "ocf_to_or", "eps",
]
END_DATE = "20260731"
DELAY = 0.3
MAX_RETRY = 3

sys.path.insert(0, str(Path.home() / "qlib" / "scripts"))
from dump_pit import DumpPitData  # noqa: E402


def init_pro():
    import tushare as ts
    pro = ts.pro_api(TOKEN)
    pro._DataApi__token = TOKEN
    pro._DataApi__http_url = API_URL
    return pro


def safe_call(pro, api_name, **kwargs):
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
    frames = []
    windows = [
        ("20050101", "20141231"),
        ("20150101", END_DATE),
    ]
    for s, e in windows:
        df = safe_call(pro, "fina_indicator", ts_code=ts_code, start_date=s, end_date=e, fields=fields)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    drop_cols = [f for f in FINA_INDICATOR_FIELDS if f in out.columns]
    out = out.drop_duplicates(subset=["ann_date", "end_date"] + drop_cols)
    return out


def to_ts_code(inst: str) -> str:
    ex = inst[:2].lower()
    code = inst[2:]
    suffix = "SH" if ex == "sh" else "SZ" if ex == "sz" else "BJ"
    return f"{code}.{suffix}"


def to_fname(inst: str) -> str:
    return inst.lower()


def quarter_of(end_date):
    end_date = str(end_date)
    y = int(end_date[:4])
    m = int(end_date[4:6])
    return y * 100 + (m - 1) // 3 + 1


def period_date(ann_date) -> str:
    d = str(int(ann_date))
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def read_instruments():
    inst = pd.read_csv(QLIB_DIR / "instruments/csi300.txt", sep="\t", names=["s", "st", "en"])
    return sorted(inst["s"].str.strip().str.upper().unique())


def dump_fina_pit(pro, code, tmp_dir):
    df = fetch_fina_indicator(pro, to_ts_code(code), "ts_code,ann_date,end_date," + ",".join(FINA_INDICATOR_FIELDS))
    if df is None or df.empty:
        return 0
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
        return 0
    out = pd.concat(records, ignore_index=True)
    out.to_csv(tmp_dir / f"{to_fname(code)}.csv", index=False)
    return len(out)


def main(limit: int = 0):
    pro = init_pro()
    codes = read_instruments()
    if limit > 0:
        codes = codes[:limit]
    print(f"  {len(codes)} instruments")

    tmp_dir = Path(tempfile.mkdtemp(prefix="fund_pit2_"))
    print(f"  PIT tmp dir: {tmp_dir}")

    n_csv = 0
    n_rows = 0
    for i, code in enumerate(codes):
        rows = dump_fina_pit(pro, code, tmp_dir)
        if rows > 0:
            n_csv += 1
            n_rows += rows
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(codes)}] ...")

    print(f"  {n_csv} CSVs, {n_rows} rows")

    print("[3] Dumping PIT data...")
    dp = DumpPitData(csv_path=str(tmp_dir), qlib_dir=str(QLIB_DIR), max_workers=8)
    dp.dump(interval="quarterly", overwrite=True)
    print("Done!")


if __name__ == "__main__":
    import fire
    fire.Fire(main)
