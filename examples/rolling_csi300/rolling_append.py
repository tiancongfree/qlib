"""
Append-only rolling: retrain just the newest window instead of the whole 27-window
history every month (Plan B).

Rationale
---------
- The live/serving target only ever consumes the prediction of the *newest* rolling
  window (RollingEnsemble concats + dedups keep-latest across windows; it does NOT
  average).  The other (historical) windows only reproduce the 2020->today backtest
  path used for equity/IC reports.
- A monthly from-scratch retrain needs the newest window anyway (LightGBM has no true
  online/row-wise weight update; refreshing the model to latest data == re-fitting the
  newest window on its expanding 2008->now train set).
- So instead of re-running all 27 windows each month (wasted work + OOM/swap pressure),
  we keep the rolling_models_* experiment and TRAIN ONLY the freshly sliding newest
  window, then re-ensemble.  Old windows' pred.pkl stay frozen and are reused for the
  historical backtest.

Upgrade safety
--------------
This subclasses qlib.contrib.rolling.base.Rolling and overrides nothing inherited
except adding one append-only trainer method.  It composes only public qlib names, so
it needs NO edit to qlib source (unlike e.g. the position.py sort patch).  For a
"full" rebuild of every window, call the normal Rolling.run() path / --mode full.
"""

from typing import List

import pandas as pd

from qlib.contrib.rolling.base import Rolling
from qlib.model.ens.ensemble import RollingEnsemble
from qlib.model.trainer import TrainerR
from qlib.workflow import R
from qlib.workflow.task.collect import RecorderCollector


def _series(x):
    return x.iloc[:, 0] if hasattr(x, "columns") else x


def freeze_served(new_pred, served_pred):
    """Option A: an already-served (datetime, instrument) score is FINAL.

    The combined pred that the live pipeline already served (backtest target /
    sync orders) is the source of truth for every index it contains.  A later
    append retrain may only ADD rows (new dates / new instruments); it may never
    overwrite one that was already served.  Without this, a retrain that produces
    a new "newest model" (different handler cache) rewrites past scores -> the live
    account chases a retroactively-changed target (the Sep-2026 7-buy/7-sell churn).

    Implemented as concat([served, new]) + keep="first": served wins on overlap,
    genuinely-new rows survive.
    """
    new = _series(new_pred).rename("score")
    if served_pred is None:
        # Nothing served yet (first run / fresh experiment): nothing to freeze,
        # but still hand downstream the DataFrame shape it expects
        # (SigAnaRecord does pred.iloc[:, label_col]).
        return new.sort_index().to_frame("score")
    served = _series(served_pred).rename("score")
    merged = pd.concat([served, new])
    merged = merged[~merged.index.duplicated(keep="first")]
    merged = merged.sort_index().to_frame("score")
    return merged


class AppendRolling(Rolling):
    """Same rolling setup as :class:`Rolling`, but train_append() only trains the
    newest sliding window (the final task of get_task_list()) and never deletes /
    recreates the rolling experiment, so prior windows' pred.pkl are preserved.
    """

    def train_append(self):
        """Train only the newest window task, appending it to the existing rolling exp.

        Mirrors qlib's ``_train_rolling_tasks`` but (a) does NOT call ``R.delete_exp``
        and (b) only schedules the final task in ``task_list``.  Uses the same
        per-window subprocess training (``call_in_subproc=True``) to release memory.
        """
        task_l = self.get_task_list()
        if not task_l:
            raise RuntimeError("no rolling tasks generated")
        self.logger.info("Append-only: training ONLY the newest rolling window")
        trainer = TrainerR(
            experiment_name=self.rolling_exp,
            call_in_subproc=True,
        )
        trainer([task_l[-1]])

    def _ens_rolling(self, extra_pred=None, served_pred=None):
        """Same as the parent, but the previously-served combined pred is frozen.

        Two retroactive-rewrite points are neutralised by ``freeze_served``:
          1. ``RollingEnsemble`` re-concats every rolling window; a newly appended
             window (new model) would overwrite an old window's scores for the same
             dates.
          2. ``extra_pred`` (the daily-appended tail) is itself repredicted by the
             newest model and overlaps already-served past dates.
        Passing ``served_pred`` (the combined pred already consumed by the live
        pipeline) freezes both: only genuinely new dates survive from the fresh run.
        """
        rc = RecorderCollector(
            experiment=self.rolling_exp,
            artifacts_key=["pred", "label"],
            process_list=[RollingEnsemble()],
            artifacts_path={"pred": "pred.pkl", "label": "label.pkl"},
        )
        res = rc()
        merged = _series(res["pred"]).rename("score")
        if extra_pred is not None:
            # Extend with the daily-appended tail (keeps the pred advancing past
            # the last rolling test window).
            extra = _series(extra_pred).rename("score")
            merged = pd.concat([merged, extra])
            merged = merged[~merged.index.duplicated(keep="last")]
            merged = merged.sort_index()
        # FREEZE: never let a later run revise an already-served score.
        res["pred"] = freeze_served(merged, served_pred)
        with R.start(experiment_name=self.exp_name):
            R.log_params(exp_name=self.rolling_exp)
            R.save_objects(**{"pred.pkl": res["pred"], "label.pkl": res["label"]})
            self._rid = R.get_recorder().id
