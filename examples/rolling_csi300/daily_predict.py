"""Daily inference: generate new-day preds using the latest trained rolling model.

Background
----------
The qlib rolling framework generates preds only at training time (each model
predicts its fixed test window once).  Between retrains (now monthly), new
trading days have no preds, so a daily ``run_rolling.py --skip-train`` just
re-concatenates stale preds and the portfolio freezes.

This script fills the gap: it predicts the NEW dates (after the last pred date)
with the newest trained rolling model, and appends them into the combined
experiment's pred.pkl (dedup, keep latest).  Retraining is still done
infrequently (monthly); this only runs inference.

Features are computed ON THE FLY with QlibDataLoader + IndustryProcessor
(verified numerically identical to the handler cache: diff == 0.0), so the
script works even when the 5GB handler cache lags behind the latest data.

Usage:
    python daily_predict.py                                  # auto window
    python daily_predict.py --start 2026-08-24 --end 2026-08-24
    python daily_predict.py --exp-name rolling_csi300_lgbm_ndrop1
    python daily_predict.py --dry-run
"""
import warnings
warnings.filterwarnings("ignore")

import sys
import pickle
from pathlib import Path

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pandas as pd
import mlflow
from qlib import auto_init
from qlib.data.dataset import DatasetH, DataHandlerLP
from qlib.data.dataset.loader import QlibDataLoader
from qlib.utils.pickle_utils import add_safe_class

import custom_handler  # noqa: F401
add_safe_class("custom_handler", "Alpha158Industry")
add_safe_class("custom_handler", "IndustryProcessor")

import tmp_leak_guard

# Guard against qlib Recorder.save_objects leaking /tmp/tmpXXXX dirs on error
# (missing try/finally upstream).  See tmp_leak_guard.py.
tmp_leak_guard.install()

DEFAULT_EXP = "rolling_csi300_lgbm_ndrop1"


def _latest_rolling_exp(client) -> str:
    exps = [e for e in client.search_experiments() if e.name.startswith("rolling_models_")]
    if not exps:
        return None
    exps.sort(key=lambda e: e.creation_time or 0)
    return exps[-1].name


def _load_newest_model_run(client, exp_name: str):
    exp = client.get_experiment_by_name(exp_name)
    if exp is None:
        raise RuntimeError(f"rolling experiment {exp_name} not found")
    runs = client.search_runs([exp.experiment_id], order_by=["attributes.start_time"])
    if not runs:
        raise RuntimeError(f"no runs in {exp_name}")
    run = runs[-1]
    base = Path("mlruns") / exp.experiment_id / run.info.run_id / "artifacts"
    with open(base / "params.pkl", "rb") as f:
        model = pickle.load(f)
    return exp_name, model


def _pred_last_date(pred) -> pd.Timestamp:
    return pd.Timestamp(pred.index.get_level_values("datetime").max())


def _select_run_by_pred_tail(client, experiment_id: str):
    """Return the run whose pred.pkl reaches the FURTHEST date.

    Select by the max datetime actually present in each run's pred.pkl, NOT by
    ``attributes.start_time``.  The start_time ordering is unreliable: a fresh
    daily/ensemble run may be created with a *shorter* tail (e.g. right after a
    retrain the newest rolling window only covers a couple of days) while an
    older run carries the daily-appended tail.  Picking by start_time would then
    read the stale/shorter pred and the target would jump back.

    Returns (run, pred) or (None, None) if no run has a readable pred.pkl.
    """
    runs = client.search_runs([experiment_id], order_by=["attributes.start_time"])
    best_run, best_pred, best_last = None, None, None
    for run in runs:
        p = Path("mlruns") / experiment_id / run.info.run_id / "artifacts" / "pred.pkl"
        if not p.exists():
            continue
        try:
            df = pd.read_pickle(p)
            if isinstance(df, pd.DataFrame):
                df = df.iloc[:, 0]
            last = _pred_last_date(df)
        except Exception:
            continue
        if best_last is None or last > best_last:
            best_run, best_pred, best_last = run, df, last
    return best_run, best_pred


def _compute_features(start, end):
    """Compute features on the fly for the window (no cache dependency).

    Returns DataFrame with MultiIndex columns (feature, <name>) matching the
    model's expected 356 input columns (172 base + 184 ind_*), index
    (datetime, instrument).
    """
    alpha = custom_handler.Alpha158Industry.__new__(custom_handler.Alpha158Industry)
    fields, names = custom_handler.Alpha158Industry.get_feature_config(alpha)
    loader = QlibDataLoader(config=(fields, names))
    from qlib.data import D
    instruments = D.instruments("csi300")
    base = loader.load(instruments, start_time=start, end_time=end)
    base.columns = pd.MultiIndex.from_tuples([("feature", c) for c in base.columns])
    proc = custom_handler.IndustryProcessor()
    out = proc(base)
    feats = [c for c in out.columns if c[0] == "feature"]
    return out[feats]


def main(
    exp_name: str = DEFAULT_EXP,
    start: str = None,
    end: str = None,
    dry_run: bool = False,
    min_rows: int = 50,
    in_place: bool = False,
):
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")
    from qlib.data import D

    client = mlflow.tracking.MlflowClient()

    # ---- current combined pred ----
    comb = client.get_experiment_by_name(exp_name)
    if comb is None:
        raise RuntimeError(f"combined experiment {exp_name} not found")
    # Select the run carrying the FURTHEST pred tail (not the newest start_time):
    # a just-created run may have a shorter tail than an older daily-extended one.
    latest, pred = _select_run_by_pred_tail(client, comb.experiment_id)
    if latest is None:
        raise RuntimeError(f"no readable pred.pkl in experiment {exp_name}")
    comb_dir = Path("mlruns") / comb.experiment_id / latest.info.run_id / "artifacts"
    pred_path = comb_dir / "pred.pkl"
    if isinstance(pred, pd.DataFrame):
        pred = pred.iloc[:, 0]
    last_date = pred.index.get_level_values("datetime").max()
    print(f"[daily_predict] current pred: {pred.index.get_level_values('datetime').min().date()} -> {last_date.date()} ({len(pred)} rows)")

    # ---- target window ----
    cal = D.calendar()
    latest_cal = pd.Timestamp(cal[-1])
    if end is None:
        end = str(latest_cal.date())
    if start is None:
        after = [d for d in cal if d > last_date]
        if not after:
            print("[daily_predict] no new dates, nothing to do.")
            return
        start = str(pd.Timestamp(after[0]).date())
    print(f"[daily_predict] window: {start} -> {end}")

    # ---- model ----
    rolling_exp, model = _load_newest_model_run(client, _latest_rolling_exp(client))
    print(f"[daily_predict] using model from {rolling_exp}")

    # ---- features ----
    feats = _compute_features(start, end)
    print(f"[daily_predict] features: {feats.shape}")
    if feats.empty:
        print("[daily_predict] no features for window, nothing to do.")
        return

    if dry_run:
        print(f"[daily_predict] dry-run, would predict {len(feats)} rows / "
              f"{feats.index.get_level_values(0).nunique()} days")
        return

    # ---- predict ----
    dh = DataHandlerLP.from_df(feats.copy())
    ds = DatasetH(handler=dh, segments={"test": (start, end)})
    new_pred = model.predict(ds, segment="test")
    if isinstance(new_pred, pd.DataFrame):
        new_pred = new_pred.iloc[:, 0]
    new_pred = new_pred.dropna()
    new_pred = new_pred[~new_pred.index.duplicated(keep="last")]
    print(f"[daily_predict] new preds: {len(new_pred)} rows, "
          f"{new_pred.index.get_level_values(0).nunique()} days")
    if len(new_pred) < min_rows:
        print(f"[daily_predict] too few rows (<{min_rows}), abort append.")
        return

    # ---- append ----
    combined = pd.concat([pred, new_pred])
    combined = combined[~combined.index.duplicated(keep="last")]
    combined = combined.sort_index()
    combined = combined.to_frame("score")

    if in_place:
        # Legacy behaviour: overwrite the selected run's pred.pkl.  DANGEROUS -
        # the run's frozen positions were produced by the OLD pred, so the stored
        # pred no longer matches its own backtest.  Kept only for compatibility.
        combined.to_pickle(pred_path)
        print(f"[daily_predict] (in-place) overwrote pred.pkl of run {latest.info.run_id[:12]}")
    else:
        # Default: write a NEW run so historical runs (and their positions) stay
        # immutable.  run_rolling --skip-train then merges this tail via
        # _latest_combined_pred().  This prevents the "same calendar day predicted
        # two different ways across consecutive runs" whipsaw.
        from qlib.workflow import R as _R
        with _R.start(experiment_id=comb.experiment_id):
            _R.save_objects(**{"pred.pkl": combined})
            new_rid = _R.get_recorder().id
        print(f"[daily_predict] saved combined pred into NEW run {new_rid[:12]}")

    print(f"[daily_predict] combined pred saved: {combined.index.get_level_values('datetime').min().date()} "
          f"-> {combined.index.get_level_values('datetime').max().date()} ({len(combined)} rows)")
    print("[daily_predict] done. Re-run `run_rolling.py --skip-train` to backtest.")


if __name__ == "__main__":
    import fire
    fire.Fire(main)
