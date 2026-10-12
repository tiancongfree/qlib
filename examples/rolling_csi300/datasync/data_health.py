#!/usr/bin/env python3
"""qlib cn_data health check.

Used to gate the "pull fresh qlib data FROM the serving machine (244)" step: we
must never overwrite the training machine's data dir with a broken / stale /
read-only snapshot.  Also runnable standalone on either machine.

Checks
------
1. calendars/day.txt present, parseable, strictly increasing, last date fresh
   (within --lag-max days; weekends/holidays covered by the default 5).
2. instruments/all.txt + instruments/csi300.txt present, non-empty; the newest
   csi300 segment holds a plausible number of codes.
3. features/<code>/<field>.day.bin well-formed for a sample of newest-segment
   csi300 codes (header start-index + length consistent with the calendar; the
   liquid names reach near the calendar end).
4. data dir is writable (catches the ext4-remounted-read-only incident).
5. enough free disk space.

Exit 0 = healthy, 1 = unhealthy, 2 = the check itself failed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import struct
import sys

FIELD_MIN = ("open", "high", "low", "close", "volume")
BIN_EXT = ".day.bin"


def check(data_dir, lag_max=5, sample=40, min_free_gb=2.0, require_fresh=True):
    res = {"data_dir": data_dir, "ok": False, "reasons": [], "warnings": [], "info": {}}

    def bad(msg):
        res["reasons"].append(msg)

    def warn(msg):
        res["warnings"].append(msg)

    data_dir = os.path.expanduser(data_dir)
    res["data_dir"] = data_dir

    cal_path = os.path.join(data_dir, "calendars", "day.txt")
    if not os.path.isfile(cal_path):
        bad(f"missing calendar {cal_path}")
        return res
    dates = []
    try:
        with open(cal_path) as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    dates.append(dt.date.fromisoformat(ln))
    except Exception as e:  # noqa: BLE001
        bad(f"calendar parse error: {e}")
        return res
    if not dates:
        bad("calendar empty")
        return res
    if any(dates[i] >= dates[i + 1] for i in range(len(dates) - 1)):
        bad("calendar not strictly increasing")
    last = dates[-1]
    cal_len = len(dates)
    res["info"]["calendar_last"] = last.isoformat()
    res["info"]["calendar_len"] = cal_len
    lag = (dt.date.today() - last).days
    res["info"]["calendar_lag_days"] = lag
    if lag > lag_max:
        (bad if require_fresh else warn)(f"calendar stale: last {last} is {lag}d old (> {lag_max}d)")

    for name in ("all.txt", "csi300.txt"):
        p = os.path.join(data_dir, "instruments", name)
        if not os.path.isfile(p) or os.path.getsize(p) == 0:
            bad(f"missing/empty instruments/{name}")

    newest_end = None
    try:
        segs = {}
        with open(os.path.join(data_dir, "instruments", "csi300.txt")) as f:
            for ln in f:
                parts = ln.split()
                if len(parts) >= 3:
                    segs[parts[2]] = segs.get(parts[2], 0) + 1
        if segs:
            newest_end = max(segs)
            res["info"]["csi300_newest_segment_end"] = newest_end
            res["info"]["csi300_newest_segment_n"] = segs[newest_end]
            if segs[newest_end] < 200:
                bad(f"newest csi300 segment has only {segs[newest_end]} codes")
            if newest_end != last.isoformat():
                warn(f"csi300 newest segment end {newest_end} != calendar last {last}")
    except Exception as e:  # noqa: BLE001
        bad(f"csi300 parse error: {e}")

    codes = []
    try:
        with open(os.path.join(data_dir, "instruments", "csi300.txt")) as f:
            for ln in f:
                p = ln.split()
                if len(p) >= 3 and p[2] == newest_end:
                    codes.append(p[0].lower())
    except Exception:  # noqa: BLE001
        pass
    if not codes:
        bad("no codes in newest csi300 segment")
        return res

    step = max(1, len(codes) // sample)
    picked = codes[::step][:sample]
    n_bad = n_short = n_ok = 0
    details = []
    for c in picked:
        d = os.path.join(data_dir, "features", c)
        if not os.path.isdir(d):
            n_bad += 1
            details.append(f"{c}:missing_dir")
            continue
        sizes = {}
        for fld in FIELD_MIN:
            fp = os.path.join(d, fld + BIN_EXT)
            if not os.path.isfile(fp):
                break
            sizes[fld] = os.path.getsize(fp)
        if len(sizes) < len(FIELD_MIN):
            n_bad += 1
            details.append(f"{c}:missing_field")
            continue
        sz = sizes["close"]
        if sz < 8 or (sz - 4) % 4 != 0 or len(set(sizes.values())) != 1:
            n_bad += 1
            details.append(f"{c}:bad_bin(size={sz})")
            continue
        with open(os.path.join(d, "close" + BIN_EXT), "rb") as f:
            start = int(struct.unpack("<f", f.read(4))[0])
        ndays = (sz - 4) // 4
        last_idx = start + ndays - 1
        if start < 0 or last_idx > cal_len - 1:
            n_bad += 1
            details.append(f"{c}:idx_oob(start={start},last={last_idx},cal={cal_len})")
            continue
        if last_idx < cal_len - 1 - lag_max:
            n_short += 1
            details.append(f"{c}:short(last_idx={last_idx})")
            continue
        n_ok += 1
    res["info"]["features_sampled"] = len(picked)
    res["info"]["features_ok"] = n_ok
    res["info"]["features_bad"] = n_bad
    res["info"]["features_short"] = n_short
    if n_bad > 0:
        bad(f"{n_bad}/{len(picked)} sampled feature dirs malformed: {details[:6]}")
    if n_short > max(1, len(picked) // 4):
        bad(f"{n_short}/{len(picked)} sampled liquid codes lag calendar end")
    elif n_short:
        warn(f"{n_short}/{len(picked)} sampled codes lag calendar end (suspensions?)")

    try:
        t = os.path.join(data_dir, ".health_wtest")
        with open(t, "w") as f:
            f.write("x")
        os.remove(t)
    except Exception as e:  # noqa: BLE001
        bad(f"data dir not writable (read-only fs?): {e}")

    try:
        st = os.statvfs(data_dir)
        free_gb = st.f_bavail * st.f_frsize / 1e9
        res["info"]["free_gb"] = round(free_gb, 1)
        if free_gb < min_free_gb:
            bad(f"low free space: {free_gb:.1f}GB < {min_free_gb}GB")
    except Exception:  # noqa: BLE001
        pass

    res["ok"] = not res["reasons"]
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.expanduser("~/.qlib/qlib_data/cn_data"))
    ap.add_argument("--lag-max", type=int, default=5)
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--min-free-gb", type=float, default=2.0)
    ap.add_argument("--no-require-fresh", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        res = check(a.data_dir, a.lag_max, a.sample, a.min_free_gb, not a.no_require_fresh)
    except Exception as e:  # noqa: BLE001
        print(f"HEALTH CHECK ERROR: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        for k, v in res["info"].items():
            print(f"  {k}: {v}")
        for w in res["warnings"]:
            print(f"  WARN: {w}")
        for r in res["reasons"]:
            print(f"  FAIL: {r}")
        print("HEALTH OK" if res["ok"] else "HEALTH FAIL")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
