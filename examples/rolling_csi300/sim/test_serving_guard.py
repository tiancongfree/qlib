"""Regression tests for serving_guard (append must run on the serving machine).

No pytest required. Run directly:
    /home/tc/qlib/.venv/bin/python examples/rolling_csi300/sim/test_serving_guard.py

Documents the 2026-10-12 incident: a manual ``run_rolling.py --mode append`` on the
research machine froze only up to its own (stale) combined tail, so it treated dates
the serving machine had already served (2026-09-21..24) as "new" and overwrote them.
"""
import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))


def _reload_env(host, serving_hosts_env=None, served_tail=None, serving_host_file=None):
    """Reload serving_guard with a patched environment / state files."""
    import os

    for k in ("QLIB_SERVING_HOSTS",):
        os.environ.pop(k, None)
    if serving_hosts_env is not None:
        os.environ["QLIB_SERVING_HOSTS"] = serving_hosts_env

    sg = importlib.import_module("serving_guard")
    importlib.reload(sg)
    # stub hostname
    sg.current_host = lambda: host
    # point state files at temp
    tmp = ROOT / "logs"
    tmp.mkdir(exist_ok=True)
    sg._SERVED_TAIL_PATH = tmp / ".served_tail.test"
    sg._SERVING_HOST_PATH = tmp / ".serving_host.test"
    for p in (sg._SERVED_TAIL_PATH, sg._SERVING_HOST_PATH):
        if p.exists():
            p.unlink()
    if served_tail is not None:
        sg._SERVED_TAIL_PATH.write_text(served_tail)
    if serving_host_file is not None:
        sg._SERVING_HOST_PATH.write_text(serving_host_file)
    return sg


def test_unknown_host_refused():
    # An arbitrary machine (neither the training env1 nor the serving 244) may not
    # run append.
    sg = _reload_env("SOME-RANDOM-BOX")
    try:
        sg.assert_serving_machine()
        assert False, "expected RuntimeError on unknown host"
    except RuntimeError as e:
        assert "not a sanctioned append origin" in str(e)


def test_training_host_allowed_by_default():
    # env1 (training machine) legitimately runs append (monthly_retrain.sh) and
    # pushes to 244 -> it must be sanctioned by default.
    sg = _reload_env("DESKTOP-JTV5CLG")
    assert sg.assert_serving_machine() == "DESKTOP-JTV5CLG"


def test_serving_host_allowed_by_default():
    sg = _reload_env("DESKTOP-6MURESA")
    assert sg.assert_serving_machine() == "DESKTOP-6MURESA"


def test_env_override():
    sg = _reload_env("DESKTOP-JTV5CLG", serving_hosts_env="SOME-OTHER-BOX")
    try:
        sg.assert_serving_machine()
        assert False, "env override should exclude the default host"
    except RuntimeError:
        pass


def test_force_overrides_host():
    sg = _reload_env("SOME-RANDOM-BOX", serving_hosts_env="DESKTOP-6MURESA")
    assert sg.assert_serving_machine(force=True) == "SOME-RANDOM-BOX"


def test_consumer_hosts_default():
    sg = _reload_env("DESKTOP-6MURESA")
    # only the serving machine owns the served tail
    assert sg.consumer_hosts() == {"DESKTOP-6MURESA"}
    assert "DESKTOP-JTV5CLG" not in sg.consumer_hosts()


def test_served_tail_ahead_refused():
    # training machine local combined tail (09-18) lags 244's served tail (09-24)
    # by 6 days > grace 3 -> refuse (this is the 2026-09-28 incident shape).
    sg = _reload_env("DESKTOP-JTV5CLG", served_tail="2026-09-24")
    import pandas as pd
    try:
        sg.assert_served_tail_not_ahead(pd.Timestamp("2026-09-18"))
        assert False, "expected RuntimeError when local tail lags served tail"
    except RuntimeError as e:
        assert "lags the last-served date" in str(e)


def test_served_tail_current_ok():
    sg = _reload_env("DESKTOP-JTV5CLG", served_tail="2026-09-24")
    import pandas as pd
    # local tail within grace (1 day behind) -> OK
    sg.assert_served_tail_not_ahead(pd.Timestamp("2026-09-23"))


def test_served_tail_force_ok():
    sg = _reload_env("DESKTOP-JTV5CLG", served_tail="2026-09-24")
    import pandas as pd
    sg.assert_served_tail_not_ahead(pd.Timestamp("2026-09-18"), force=True)


def test_no_served_tail_is_ok():
    sg = _reload_env("DESKTOP-JTV5CLG")
    import pandas as pd
    sg.assert_served_tail_not_ahead(pd.Timestamp("2026-09-18"))


def test_serving_host_file():
    sg = _reload_env("DESKTOP-X", serving_host_file="DESKTOP-X")
    sg.assert_serving_machine()


def _run_standalone():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception:
            failed += 1
            print(f"  [ERROR] {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
