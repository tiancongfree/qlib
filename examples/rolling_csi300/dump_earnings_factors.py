"""
Build earnings-momentum / surprise factors from PIT data into daily features.

Factors (all point-in-time, no lookahead - value mapped to first trading day
on/after ann_date, ffill between announcements):

  np_accel     = netprofit_yoy - lag1(netprofit_yoy)   (earnings growth acceleration)
  qprof_accel  = q_profit_yoy  - lag1(q_profit_yoy)    (single-quarter profit momentum)
  roe_chg      = roe_waa       - lag1(roe_waa)          (ROE change)
  margin_chg   = netprofit_margin - lag1(netprofit_margin)  (margin inflection)
  eps_yoy      = eps           - lag4(eps)              (YoY EPS growth, ~4 quarters)
  NP_2POS      = 1 if netprofit_yoy > 0 AND lag1 > 0 AND np_accel > 0 else 0  (persistent + accelerating)

Notes:
  - 'netprofit_yoy'/'q_profit_yoy' are already YoY growth rates from tushare
    fina_indicator, so acceleration = diff of consecutive announcements.
  - eps YoY uses period delta of 4 quarters (lag4 by period, not by record count).
  - Values are winsorized at 1%/99% cross-sectionally at dump time is NOT done
    here (lightgbm + existing processors handle outliers); we only store raw.

Output: ~/.qlib/qlib_data/cn_data/features/<code>/<factor>.day.bin
Usage:  python dump_earnings_factors.py [--limit N]
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

QLIB_DIR = Path.home() / ".qlib/qlib_data/cn_data"

# factor -> (source PIT field, transform)
# transform: "yoy_diff" = value - lag1 (for fields already YoY rates: acceleration)
#            "lag4_diff" = value - value_same_q_last_year (for cumulative fields: YoY change)
FACTORS = {
    "np_accel":     ("netprofit_yoy", "yoy_diff"),
    "qprof_accel":  ("q_profit_yoy", "yoy_diff"),
    "roe_chg":      ("roe_waa", "lag4_diff"),
    "margin_chg":   ("netprofit_margin", "lag4_diff"),
    "eps_yoy":      ("eps", "lag4_diff"),
}


def to_fname(inst: str) -> str:
    return inst.lower()


def read_calendar():
    cal = pd.read_csv(QLIB_DIR / "calendars/day.txt", header=None).iloc[:, 0].tolist()
    return [str(d) for d in cal]


def read_pit(inst, field):
    """Read PIT .data -> list of (ann_date, period, value) sorted by (period, ann_date)."""
    raw = QLIB_DIR / "financial" / to_fname(inst) / f"{field}_q.data"
    if not raw.exists():
        return []
    raw = raw.read_bytes()
    n_rec = len(raw) // 20
    out = []
    for i in range(n_rec):
        rec = raw[i * 20:(i + 1) * 20]
        d = np.frombuffer(rec[0:4], dtype="<u4")[0]
        period = np.frombuffer(rec[4:8], dtype="<u4")[0]
        val = np.frombuffer(rec[8:16], dtype="<f8")[0]
        if d <= 0 or np.isnan(val):
            continue
        y, m, day = d // 10000, d // 100 % 100, d % 100
        out.append((f"{y:04d}-{m:02d}-{day:02d}", int(period), float(val)))
    # dedupe: for same period keep latest ann_date record
    by_period = {}
    for ann, period, val in sorted(out, key=lambda x: (x[1], x[0])):
        by_period[period] = (ann, val)
    # sort by period for lag computation
    return sorted([(ann, period, val) for period, (ann, val) in by_period.items()],
                  key=lambda x: x[1])


def compute_factors(inst):
    """Return {factor: [(ann_date, value)]} with transforms applied at PIT level."""
    cal = read_calendar()
    raw = {}
    for factor, (field, transform) in FACTORS.items():
        raw[factor] = read_pit(inst, field)

    results = {}
    for factor, (field, transform) in FACTORS.items():
        recs = raw[factor]
        if len(recs) < 2:
            results[factor] = []
            continue
        periods = [r[1] for r in recs]
        vals = [r[2] for r in recs]
        if transform == "yoy_diff":
            # consecutive announcement diff
            diff = [np.nan] + [vals[i] - vals[i - 1] for i in range(1, len(vals))]
            results[factor] = [(recs[i][0], diff[i]) for i in range(len(recs)) if not np.isnan(diff[i])]
        elif transform == "lag4_diff":
            # compare to the same quarter 1 year earlier (period like 202503 -> 202403)
            diff = [np.nan] * len(vals)
            prev_lookup = {}
            for j, p in enumerate(periods):
                prev_lookup[p] = j
            for i in range(len(periods)):
                year = periods[i] // 100
                q = periods[i] % 100
                prev_period = (year - 1) * 100 + q
                if prev_period in prev_lookup:
                    diff[i] = vals[i] - vals[prev_lookup[prev_period]]
            results[factor] = [(recs[i][0], diff[i]) for i in range(len(recs)) if not np.isnan(diff[i])]
    return results


def dump_daily(inst):
    factors = compute_factors(inst)
    features_dir = QLIB_DIR / "features" / to_fname(inst)
    if not features_dir.exists():
        return 0
    cal = read_calendar()
    n_written = 0
    for factor, pts in factors.items():
        if not pts:
            continue
        ann_date_arr = np.array([ts for ts, _ in pts], dtype=object)
        ann_val = np.array([v for _, v in pts], dtype=np.float32)
        next_idx = np.searchsorted(cal, ann_date_arr, side="left")
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
            str(features_dir / f"{factor}.day.bin")
        )
        n_written += 1
    return n_written


def main(limit: int = 0):
    inst = pd.read_csv(QLIB_DIR / "instruments/csi300.txt", sep="\t", names=["s", "st", "en"])
    codes = sorted(inst["s"].str.strip().str.upper().unique())
    if limit > 0:
        codes = codes[:limit]
    print(f"  {len(codes)} instruments, factors: {list(FACTORS.keys())}")

    n_ok = 0
    for i, code in enumerate(codes):
        w = dump_daily(code)
        if w > 0:
            n_ok += 1
        if (i + 1) % 100 == 0:
            print(f"  [{i+1}/{len(codes)}] ...")
    print(f"  done: {n_ok}/{len(codes)} stocks have earnings-momentum factors")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
