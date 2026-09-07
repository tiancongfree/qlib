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

from qlib.contrib.rolling.base import Rolling
from qlib.model.trainer import TrainerR


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

    # Everything else (basic_task / get_task_list / _ens_rolling / _update_rolling_rec /
    # run) is inherited unchanged from the parent Rolling.
