"""
Regression tests for the "append retrain rewrites past target" bug.

No pytest required. Run directly:
    /home/tc/qlib/.venv/bin/python examples/rolling_csi300/sim/test_sim_merge.py

(Also works under pytest if it happens to be installed: it exposes test_* funcs.)

`test_current_code_rewrites_past` documents the BUG: the current production merge
rule DOES rewrite already-served past days. If someone silently changes the rule,
this test flips and we notice.

`test_option_a_freezes_past` is the contract we want: with Option A the past is
frozen AND the served history still keeps up with "today" (no silent freeze).
"""
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sim_merge import run_timeline  # noqa: E402


def _summarise(churn, progress):
    rewritten_days = sorted({d for _, d in churn})
    lag = [t for t, n, m in progress if n < m]
    return rewritten_days, lag


def test_current_code_rewrites_past():
    # BUG DOCUMENTATION: current merge rule rewrites already-served history.
    churn, _history, progress = run_timeline(fix=False, verbose=False)
    rewritten_days, _lag = _summarise(churn, progress)
    assert rewritten_days, (
        "Expected current code to retroactively rewrite past days, but it "
        "did not. If the merge rule changed, update this test."
    )


def test_option_a_freezes_past():
    # CONTRACT: Option A must (a) never rewrite a served day and
    # (b) still keep the served history up to the simulated "today".
    churn, _history, progress = run_timeline(fix=True, verbose=False)
    rewritten_days, lag = _summarise(churn, progress)
    assert rewritten_days == [], f"Option A still rewrote past days: {rewritten_days}"
    assert lag == [], f"Option A froze the tail (silent freeze) at: {lag}"


def test_fresh_exp_first_run_returns_dataframe():
    # REGRESSION: when there is no previously-served combined pred (a fresh
    # experiment, or the very first append), freeze_served must still return the
    # DataFrame shape downstream expects. SigAnaRecord does `pred.iloc[:, 0]` /
    # `label.iloc[:, label_col]`, which raises "IndexingError: Too many indexers"
    # on a Series (this bit the first append of a brand-new experiment).
    import pandas as pd
    from rolling_append import freeze_served

    idx = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-09-18"), "SH600000")], names=["datetime", "instrument"]
    )
    series = pd.Series([1.5], index=idx, name="score")
    out = freeze_served(series, None)
    assert isinstance(out, pd.DataFrame), f"expected DataFrame, got {type(out).__name__}"
    assert list(out.columns) == ["score"]
    # and with a served pred present it must also be a DataFrame
    out2 = freeze_served(series, series)
    assert isinstance(out2, pd.DataFrame)


def _run_standalone():
    tests = [
        test_current_code_rewrites_past,
        test_option_a_freezes_past,
        test_fresh_exp_first_run_returns_dataframe,
    ]
    failed = 0
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"  [PASS] {name}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {name}: {e}")
        except Exception:
            failed += 1
            print(f"  [ERROR] {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
