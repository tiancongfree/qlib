"""
Dump 分红融资比 (dividend/financing ratio) into qlib PIT format.

Definition (cashflow approximation, rolling cumulative, point-in-time):
  dividend  = c_pay_dist_dpcp_int_exp  (分配股利/利润/偿付利息支付的现金)
  financing = c_recp_cap_contrib       (吸收投资收到的现金 = equity financing)
            + c_recp_borrow            (取得借款收到的现金 = debt financing)

For every annual report of each stock (sorted by ann_date), compute the ratio
over the trailing N years using ONLY reports whose ann_date <= that report's
ann_date (no lookahead). Dump as PIT records:

  date   = ann_date of the report (YYYY-MM-DD)
  period = end_date quarter (YYYYQ)
  value  = ratio at that point
  field  = "dv_ratio_q"  (equity-only)  or "dv_ratio_all_q" (equity + debt)

Usage:
  python dump_dv_ratio.py --limit 5   # test
  python dump_dv_ratio.py             # full
"""

import sys
import time
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"
TOKEN = "6bc84c24d67b2b5a3168353a298309abae23298e388a34952f0fd2eec7f0"
API_URL = "http://jiaoch.site"
START_DATE = "20050101"
END_DATE = "20260731"
DELAY = 0.25
MAX_RETRY = 3
WINDOW_YEARS = 10

DIV_COL = "c_pay_dist_dpcp_int_exp"
FIN_EQUITY_COL = "c_recp_cap_contrib"
FIN_DEBT_COL = "c_recp_borrow"
FETCH_FIELDS = "ts_code,ann_date,end_date," + ",".join([DIV_COL, FIN_EQUITY_COL, FIN_DEBT_COL])

sys.path.insert(0, str(Path.home() / "qlib" / "scripts"))
from dump_pit import DumpPitData  # noqa: E402


def init_pro():
    import tushare as ts

    pro = ts.pro_api(TOKEN)
    pro._DataApi__token = TOKEN
    pro._DataApi__http_url = API_URL
    return pro


def safe_call(pro, **kwargs):
    for attempt in range(MAX_RETRY):
        try:
            df = pro.cashflow(**kwargs)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            if attempt < MAX_RETRY - 1:
                time.sleep(DELAY * (attempt + 1))
            else:
                print(f"  ! cashflow failed: {str(e)[:100]}")
        time.sleep(DELAY)
    return None


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


def fetch_cashflow(pro, ts_code):
    df = safe_call(pro, ts_code=ts_code, start_date=START_DATE, end_date=END_DATE, fields=FETCH_FIELDS)
    if df is None or df.empty:
        return None
    df = df.drop_duplicates(subset=["end_date", "ann_date"])
    # annual reports only (year-end), since the cashflow figures are year-to-date
    df = df[df["end_date"].str.endswith("1231")].copy()
    for c in [DIV_COL, FIN_EQUITY_COL, FIN_DEBT_COL]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ann"] = pd.to_datetime(df["ann_date"], format="%Y%m%d")
    df = df.sort_values("ann").reset_index(drop=True)
    return df


def build_ratio_records(df):
    """Rolling trailing-WINDOW_YEARS cumulative ratio, computed point-in-time.

    For each report i (sorted by ann_date), only reports j with ann_date[j] <=
    ann_date[i] and end_date within WINDOW_YEARS of end_date[i] are used.
    """
    records = []
    vals = df[["ann", "ann_date", "end_date", DIV_COL, FIN_EQUITY_COL, FIN_DEBT_COL]].copy()
    end_dates = pd.to_datetime(vals["end_date"], format="%Y%m%d")

    for i in range(len(vals)):
        ann_i = vals["ann"].iloc[i]
        end_i = end_dates.iloc[i]
        cutoff = end_i - pd.DateOffset(years=WINDOW_YEARS)
        # point-in-time: only reports already announced
        win = vals[(vals["ann"] <= ann_i) & (end_dates >= cutoff)]
        div = win[DIV_COL].sum()
        eq = win[FIN_EQUITY_COL].sum()
        debt = win[FIN_DEBT_COL].sum()
        if div is np.nan or div == 0:
            continue
        records.append(
            {
                "date": period_date(vals["ann_date"].iloc[i]),
                "period": quarter_of(vals["end_date"].iloc[i]),
                "dv_ratio": (div / eq) if eq and eq > 0 else np.nan,
                "dv_ratio_all": (div / (eq + debt)) if (eq + debt) and (eq + debt) > 0 else np.nan,
            }
        )
    return records


def dump_stock(pro, code, tmp_dir):
    ts_code = to_ts_code(code)
    df = fetch_cashflow(pro, ts_code)
    if df is None or df.empty:
        return 0
    recs = build_ratio_records(df)
    if not recs:
        return 0
    out = pd.DataFrame(recs)
    frames = []
    for field in ["dv_ratio", "dv_ratio_all"]:
        sub = out[["date", "period", field]].dropna(subset=[field]).copy()
        if sub.empty:
            continue
        sub = sub.rename(columns={field: "value"})
        sub["field"] = field
        frames.append(sub[["date", "period", "value", "field"]])
    if not frames:
        return 0
    res = pd.concat(frames, ignore_index=True)
    res.to_csv(tmp_dir / f"{to_fname(code)}.csv", index=False)
    return len(res)


def main(limit: int = 0):
    print("=" * 60)
    print("  Dump 分红融资比 into qlib PIT (cashflow rolling cumulative)")
    print(f"  Qlib dir: {QLIB_DIR}")
    print("=" * 60)

    pro = init_pro()
    codes = read_instruments()
    if limit > 0:
        codes = codes[:limit]
    print(f"  {len(codes)} instruments")

    tmp_dir = Path(tempfile.mkdtemp(prefix="dv_ratio_pit_"))
    print(f"  PIT tmp dir: {tmp_dir}")

    n_csv = 0
    n_rows = 0
    t0 = time.time()
    for i, code in enumerate(codes):
        rows = dump_stock(pro, code, tmp_dir)
        if rows > 0:
            n_csv += 1
            n_rows += rows
        if (i + 1) % 100 == 0:
            print(f"  [{i+1}/{len(codes)}] {time.time()-t0:.0f}s")

    print(f"\n  {n_csv} CSVs, {n_rows} rows, {time.time()-t0:.0f}s")

    print("\n[3] Dumping PIT data into financial/ ...")
    dp = DumpPitData(csv_path=str(tmp_dir), qlib_dir=str(QLIB_DIR), max_workers=8)
    dp.dump(interval="quarterly", overwrite=True)
    print("Done!")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
