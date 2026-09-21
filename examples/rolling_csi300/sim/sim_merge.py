"""
Fast simulator for the "append retrain rewrites past target" bug.

This deliberately does NOT train any model and does NOT run the qlib backtest.
It reproduces the exact pred-merge machinery that causes the bug:

  1. a `rolling_models_*` experiment full of per-window pred.pkl runs,
  2. the real `qlib.model.ens.ensemble.RollingEnsemble` (concat + keep=last),
  3. the real `_ens_rolling` style merge with `extra_pred` (daily tail),
  4. the daily_predict tail-append step.

It then asks the one question that matters:

    For a fixed PAST trading day D, does the merged score vector for D
    change when a later append retrain adds a new (overlapping) window?

If yes, the target for D is retroactively rewritten -> live account chases it.

The score for a past day is the direct cause of the target, so this is a
faithful, deterministic proxy for the full backtest target.

Usage:
    python sim/sim_merge.py            # show the bug (current code)
    python sim/sim_merge.py --fix      # apply candidate fix, expect stability
"""
import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROLL = HERE.parent
sys.path.insert(0, str(ROLL))

from qlib.model.ens.ensemble import RollingEnsemble  # noqa: E402

# Exercise the REAL production freeze rule (Option A), not a copy.
from rolling_append import freeze_served  # noqa: E402


def _stable_seed(*parts) -> int:
    import hashlib
    h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:8], 16)

# ---------------------------------------------------------------------------
# Synthetic market: deterministic so runs are reproducible.
# ---------------------------------------------------------------------------
N_INST = 40
INSTS = [f"SH{600000 + i:06d}" for i in range(N_INST)]


def _trading_days(start="2026-06-01", n=120):
    return pd.bdate_range(start=start, periods=n)


DAYS = _trading_days()


def _make_window_pred(win_start_day: pd.Timestamp, win_end_day: pd.Timestamp,
                      model_seed: int, n_days_full: int = None):
    """Build a window pred covering [win_start_day, win_end_day].

    Score for (seed, day, instrument) is a PURE function of the three - not of
    the draw order - so a window pred and a single-day 'daily_predict' pred for
    the same (seed, day) agree exactly.  Different seeds give slightly different
    scores, mimicking a retrain that used a different handler cache.
    """
    days = [d for d in DAYS if win_start_day <= d <= win_end_day]
    rows = []
    base_rng = np.random.RandomState(_stable_seed("base", model_seed))
    base = base_rng.normal(size=N_INST)
    for d in days:
        day_rng = np.random.RandomState(_stable_seed(model_seed, d.date()))
        day_noise = day_rng.normal(scale=0.30, size=N_INST)
        score = base + day_noise
        mi = pd.MultiIndex.from_product([[d], INSTS], names=["datetime", "instrument"])
        rows.append(pd.Series(score, index=mi, name="score"))
    return pd.concat(rows)


# ---------------------------------------------------------------------------
# A fake "rolling_models" store: list of windows, newest last.
# ---------------------------------------------------------------------------
class WindowStore:
    def __init__(self):
        self.windows = []  # list of dicts {run_id, start_time, pred}

    def add_window(self, win_start, win_end, model_seed, start_time):
        pred = _make_window_pred(pd.Timestamp(win_start), pd.Timestamp(win_end), model_seed)
        run_id = f"run{len(self.windows):02d}_{model_seed}"
        self.windows.append(dict(run_id=run_id, start_time=start_time, pred=pred, seed=model_seed,
                                 ws=pd.Timestamp(win_start), we=pd.Timestamp(win_end)))
        return self.windows[-1]

    def as_ensemble_dict_newest_first(self):
        """Mimic RecorderCollector: mlflow list_recorders returns start_time DESC."""
        ordered = sorted(self.windows, key=lambda w: w["start_time"], reverse=True)
        return {w["run_id"]: w["pred"] for w in ordered}


# ---------------------------------------------------------------------------
# The merge (mirrors run_rolling.py::_ens_rolling + daily_predict tail merge).
# ---------------------------------------------------------------------------
def _series(x):
    return x.iloc[:, 0] if hasattr(x, "columns") else x


def merge_combined(store: WindowStore, extra_pred=None, fix=False, served=None):
    """Reproduce _ens_rolling(extra_pred=...) exactly.

    fix=False -> current production behaviour (newest wins on duplicates).
    fix=True  -> Option A: first-writer-wins.  Any (datetime, instrument) that
                 already exists in the previously-served combined pred keeps its
                 old score; only genuinely new dates/instruments come from the
                 latest pipeline.  This is the "no look-ahead / point-in-time"
                 rule: a decision already made cannot be revised by a later model.
    """
    ens = RollingEnsemble()(store.as_ensemble_dict_newest_first())
    ens = _series(ens).rename("score")
    if extra_pred is None:
        merged = ens
    else:
        extra = _series(extra_pred).rename("score")
        merged = pd.concat([ens, extra])
        merged = merged[~merged.index.duplicated(keep="last")]
        merged = merged.sort_index()
    out = merged.to_frame("score")

    if fix and served is not None:
        out = freeze_served(out, served)
    return out


# ---------------------------------------------------------------------------
# Driver: simulate the Sep-2026 timeline that caused the churn.
# ---------------------------------------------------------------------------
def snapshot_scores(combined, day):
    day = pd.Timestamp(day)
    return combined.xs(day, level="datetime")["score"].sort_index()


def run_timeline(fix=False, verbose=True):
    """Simulate: base window -> daily appends -> overlapping append retrains."""
    store = WindowStore()

    # Baseline rolling windows (before the daily period).  These are disjoint.
    store.add_window("2026-06-01", "2026-07-31", model_seed=1, start_time=100)
    store.add_window("2026-08-01", "2026-09-07", model_seed=2, start_time=200)

    sim_days = [d for d in DAYS if pd.Timestamp("2026-09-08") <= d <= pd.Timestamp("2026-09-21")]
    sim_days = sim_days[:10]  # 10 trading days

    frozen_pred = None
    history = {}  # date -> score series at the time it was first served
    churn = []
    progress = []  # (today, n_days_served_upto_today) after each run_rolling call
    state = {"today": None}

    def serve(combined, label, today):
        # Record/compare the score for every day already present.  A day is
        # "served" the first time it appears with a score; any later change to
        # that day's score is a retroactive rewrite (the real-world churn).
        state["today"] = today
        present = set(combined.index.get_level_values("datetime"))
        upto = [d for d in sim_days if d <= today]
        n_served = len([d for d in upto if d in present])
        progress.append((today, n_served, len(upto)))
        for d in sim_days:
            if d not in combined.index.get_level_values("datetime"):
                continue
            cur = snapshot_scores(combined, d)
            if d not in history:
                history[d] = cur
            else:
                prev = history[d]
                cur_al = cur.reindex(prev.index)
                if not np.allclose(prev.values, cur_al.values, equal_nan=True):
                    churn.append((label, d))
                    if verbose:
                        n = int((~np.isclose(prev.values, cur_al.values)).sum())
                        print(f"    !! past day {d.date()} REWRITTEN by {label} ({n} instruments changed)")

    def daily_tail(days, seed):
        """daily_predict: predict `days` with the *newest* model (seed)."""
        parts = [_make_window_pred(pd.Timestamp(d), pd.Timestamp(d), model_seed=seed) for d in days]
        return pd.concat(parts)

    # ------------------------------------------------------------------
    # Timeline mirroring the real 244 incident (2026-09-08 .. 09-21):
    #   - a frozen old window + append#1 (Sep 14, model A) covering 09-08->12-08
    #   - daily_predict runs each day with the newest model
    #   - append#2 (Sep 18, SAME window but model B) arrives mid-stream
    # ------------------------------------------------------------------
    store.add_window("2026-06-15", "2026-09-07", model_seed=2, start_time=200)  # frozen
    store.add_window("2026-09-08", "2026-12-08", model_seed=7, start_time=300)  # append#1 (model A)

    def build(extra, label):
        """One run_rolling --skip-train invocation (ens + daily tail)."""
        nonlocal frozen_pred
        combined = merge_combined(store, extra_pred=extra, fix=fix, served=frozen_pred)
        frozen_pred = combined
        return combined

    # --- Sep 8..15: daily_predict with model A; skip-train merges the tail ---
    for i, d in enumerate(sim_days):
        if d > pd.Timestamp("2026-09-15"):
            break
        days = [x for x in sim_days if x <= d]
        tail = daily_tail(days, seed=7)
        combined = build(tail, f"A-daily({d.date()})")
        serve(combined, f"A-daily({d.date()})", today=d)

    # --- Sep 16: append#2, SAME window, model B (different handler cache) ---
    store.add_window("2026-09-08", "2026-12-08", model_seed=9, start_time=400)  # append#2 (model B)

    # --- Sep 16..21: daily_predict now uses model B (newest), tail covers 09-08.. ---
    for i, d in enumerate(sim_days):
        if d <= pd.Timestamp("2026-09-15"):
            continue
        days = [x for x in sim_days if x <= d]
        tail = daily_tail(days, seed=9)
        combined = build(tail, f"B-daily({d.date()})")
        serve(combined, f"B-daily({d.date()})", today=d)

    return churn, history, progress


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="apply candidate fix")
    args = ap.parse_args()

    print("=" * 68)
    print(f"  SIMULATION  ({'WITH FIX' if args.fix else 'CURRENT CODE'})")
    print("=" * 68)
    churn, history, progress = run_timeline(fix=args.fix)
    print("-" * 68)

    # --- progress check: at every step, ALL days up to the simulated "today"
    # must have a served score.  If a "fix" froze things, some recent day would
    # be missing -> silent freeze (the Aug bug: target stops updating).  PASS
    # requires BOTH no rewrite AND the served history keeping up with today.
    lag = [(t, n, m) for t, n, m in progress if n < m]
    if progress:
        t0 = progress[0][0]
        tN = progress[-1][0]
        print(f"  served history: today {t0.date()} -> {tN.date()}; "
              f"steps lagging behind today = {len(lag)}")
    else:
        print("  served history: (no steps)")

    ok = True
    if churn:
        uniq_days = sorted({d for _, d in churn})
        print(f"  [FAIL] {len(churn)} retroactive rewrites across "
              f"{len(uniq_days)} past days: {[str(d.date()) for d in uniq_days]}")
        ok = False
    else:
        print("  [OK] no past day was retroactively rewritten")
    if lag:
        print(f"  [FAIL] {len(lag)} step(s) had no served score for a day that had "
              f"already happened (silent freeze!): "
              f"{[str(t.date()) for t, _, _ in lag]}")
        ok = False
    else:
        print("  [OK] served history always kept up with today (no silent freeze)")

    print("-" * 68)
    print("  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
