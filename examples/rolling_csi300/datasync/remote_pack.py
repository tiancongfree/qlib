#!/usr/bin/env python3
"""Serving-machine (244) side of the qlib-data sync.

Health-checks the local data dir, then packs an incremental tarball for the
training machine to pull:

  * calendars/    (whole)
  * instruments/  (whole)
  * features/**.day.bin  with mtime > --since

The tarball paths are relative to the data dir, so the training machine extracts
it with `tar xzf <tar> -C <its cn_data>`.

Runs INSIDE 244's WSL.  Exit 0 = packed, 2 = unhealthy (abort), 1 = error.

Prints:
  HEALTH OK|FAIL
  REMOTE_PACK OK files=N bytes=B since=S pack_start=P
  DATA_SYNC_MARKER <pack_start>
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_health  # noqa: E402


def iter_feature_bins(data_dir, since):
    froot = os.path.join(data_dir, "features")
    if not os.path.isdir(froot):
        return
    for code in sorted(os.listdir(froot)):
        cdir = os.path.join(froot, code)
        if not os.path.isdir(cdir):
            continue
        for fn in sorted(os.listdir(cdir)):
            if not fn.endswith(".day.bin"):
                continue
            fp = os.path.join(cdir, fn)
            try:
                if os.path.getmtime(fp) <= since:
                    continue
            except OSError:
                continue
            yield fp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.expanduser("~/.qlib/qlib_data/cn_data"))
    ap.add_argument("--since", type=float, default=0.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lag-max", type=int, default=5)
    ap.add_argument("--no-require-fresh", action="store_true")
    ap.add_argument("--skip-health", action="store_true")
    a = ap.parse_args()

    d = os.path.expanduser(a.data_dir)
    pack_start = time.time()

    if not a.skip_health:
        h = data_health.check(d, a.lag_max, require_fresh=not a.no_require_fresh)
        print("HEALTH " + ("OK" if h["ok"] else "FAIL"))
        for k, v in h["info"].items():
            print(f"  {k}: {v}")
        for w in h["warnings"]:
            print(f"  WARN: {w}")
        for r in h["reasons"]:
            print(f"  FAIL: {r}")
        if not h["ok"]:
            print("REMOTE_PACK ABORT: unhealthy")
            return 2

    out_dir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    with tarfile.open(a.out, "w:gz") as tf:
        for sub in ("calendars", "instruments"):
            p = os.path.join(d, sub)
            if os.path.isdir(p):
                tf.add(p, arcname=sub)
        for fp in iter_feature_bins(d, a.since):
            tf.add(fp, arcname=os.path.relpath(fp, d))
            n += 1
    size = os.path.getsize(a.out)
    print(f"REMOTE_PACK OK files={n} bytes={size} since={int(a.since)} pack_start={int(pack_start)}")
    print(f"DATA_SYNC_MARKER {int(pack_start)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
