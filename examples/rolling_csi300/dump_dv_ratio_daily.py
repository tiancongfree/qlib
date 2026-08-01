"""
Convert dv_ratio PIT (quarterly) features into daily features (ffill, no lookahead).

The PIT record (ann_date, period, value) is mapped onto the trading calendar:
  - on the ann_date itself, the value is available (point-in-time)
  - between ann_dates, the latest announced value is carried forward (ffill)
  - before the first ann_date: NaN

Output: ~/.qlib/qlib_data/cn_data/features/<code>/<field>.day.bin
  field: dv_ratio, dv_ratio_all

This makes the handler feature `$dv_ratio` (daily) instead of `P($$dv_ratio_q)`
(PIT), which builds ~4x faster in rolling training.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"
FIELDS = ["dv_ratio", "dv_ratio_all"]


def to_fname(inst: str) -> str:
    return inst.lower()


def read_calendar():
    cal = pd.read_csv(QLIB_DIR / "calendars/day.txt", header=None).iloc[:, 0].tolist()
    return [str(d) for d in cal]


def read_pit(inst, field):
    """Read PIT .data file -> list of (ann_date, value) sorted by ann_date.

    Record layout: date(uint32) + period(uint32) + value(float64) + _next(uint32)
    = 20 bytes per record.
    """
    raw = (QLIB_DIR / "financial" / to_fname(inst) / f"{field}_q.data")
    if not raw.exists():
        return []
    raw = raw.read_bytes()
    rec_size = 4 + 4 + 8 + 4
    n_rec = len(raw) // rec_size
    out = []
    for i in range(n_rec):
        rec = raw[i * rec_size:(i + 1) * rec_size]
        d, period, val_bits, _next = (
            np.frombuffer(rec[0:4], dtype="<u4")[0],
            np.frombuffer(rec[4:8], dtype="<u4")[0],
            rec[8:16],
            np.frombuffer(rec[16:20], dtype="<u4")[0],
        )
        val = np.frombuffer(val_bits, dtype="<f8")[0]
        if d <= 0 or np.isnan(val):
            continue
        y, m, day = d // 10000, d // 100 % 100, d % 100
        out.append((f"{y:04d}-{m:02d}-{day:02d}", val))
    out.sort(key=lambda x: x[0])
    return out


def dump_daily(inst):
    cal = read_calendar()
    recs = {f: read_pit(inst, f) for f in FIELDS}

    features_dir = QLIB_DIR / "features" / to_fname(inst)
    if not features_dir.exists():
        return 0

    n_written = 0
    for field in FIELDS:
        pts = recs[field]
        if not pts:
            continue
        # map ann_dates to the NEXT trading day index (PIT semantics: the value
        # becomes available on the first trading day on/after the announcement)
        ann_date_arr = np.array([ts for ts, _ in pts], dtype=object)
        ann_val = np.array([v for _, v in pts], dtype=np.float32)
        next_idx = np.searchsorted(cal, ann_date_arr, side="left")
        # dedupe: if two announcements map to the same day, keep the latest
        dedup = {}
        for idx, v in zip(next_idx, ann_val):
            dedup[idx] = v
        pts_daily = sorted(dedup.items())
        if not pts_daily:
            continue
        ann_idx = np.array([i for i, _ in pts_daily], dtype=np.int64)
        ann_vals = np.array([v for _, v in pts_daily], dtype=np.float32)
        first_idx = int(ann_idx[0])
        last_idx = len(cal) - 1
        data = np.full(last_idx - first_idx + 1, np.nan, dtype=np.float32)
        pos = np.searchsorted(ann_idx, np.arange(first_idx, last_idx + 1), side="right") - 1
        valid = pos >= 0
        data[valid] = ann_vals[pos[valid]]
        np.hstack([np.float32(first_idx), data]).astype("<f").tofile(
            str(features_dir / f"{field}.day.bin")
        )
        n_written += 1
    return n_written


def main(limit: int = 0):
    inst_dir = QLIB_DIR / "instruments/csi300.txt"
    inst = pd.read_csv(inst_dir, sep="\t", names=["s", "st", "en"])
    codes = sorted(inst["s"].str.strip().str.upper().unique())
    if limit > 0:
        codes = codes[:limit]
    print(f"  {len(codes)} instruments")

    n_ok = 0
    for i, code in enumerate(codes):
        w = dump_daily(code)
        if w > 0:
            n_ok += 1
        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{len(codes)}] ...")
    print(f"  done: {n_ok}/{len(codes)} stocks have daily dv_ratio features")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
