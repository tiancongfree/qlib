"""Determinism guard for the rolling backtest pipeline.

Two independent protections, both motivated by real incidents in AGENTS.md:

1. ``assert_pos_type_sorted(conf)`` -- fail fast if the backtest config does not
   resolve ``pos_type`` to our sorted ``Position`` subclass.  A missing/unset
   ``pos_type`` silently falls back to qlib's base ``Position``, whose
   ``get_stock_list()`` iterates a set (order = PYTHONHASHSEED) and made the
   n_drop=1 backtest non-deterministic (see AGENTS.md 3b / the 2026-08-04
   wrong-order incident).  This is the cheap, always-on guard.

2. ``record_and_check_positions(exp_name)`` -- after each backtest, hash the
   daily target (held-name set per date) and compare against the previous run's
   record.  If a date that was *already served* by an earlier run now has a
   different target, the live account will be whipsawed to chase a rewritten
   history; we print a loud warning (and optionally raise).  This catches the
   class of "retroactive target rewrite" bugs (the Sep-2026 churn) even when the
   root cause is not obvious up front.

State lives in ``logs/.determinism_state.json``.
"""

import hashlib
import json
from pathlib import Path

# Avoid importing local qlib source (uncompiled C extensions)
_HERE = Path(__file__).parent
if str(_HERE.parent.parent) in __import__("sys").path:
    __import__("sys").path.remove(str(_HERE.parent.parent))

_STATE_DIR = _HERE / "logs"
_STATE_PATH = _STATE_DIR / ".determinism_state.json"


def assert_pos_type_sorted(conf: dict) -> str:
    """Verify the backtest ``pos_type`` resolves to a sorted Position subclass.

    Returns the resolved dotted path.  Raises ``RuntimeError`` if the resolved
    class is missing or is qlib's non-deterministic base ``Position``.
    """
    try:
        pos_type = conf["port_analysis_config"]["backtest"]["pos_type"]
    except (KeyError, TypeError):
        pos_type = None

    if not pos_type:
        raise RuntimeError(
            "DETERMINISM GUARD: backtest.pos_type is not set. Without it the "
            "backtest uses qlib's base Position (set-ordered get_stock_list) and "
            "becomes non-deterministic for n_drop=1 (AGENTS.md 3b). Set "
            "`pos_type: custom_handler.SortedPosition`."
        )

    module_path, _, cls_name = pos_type.rpartition(".")
    if not module_path:
        # e.g. pos_type: Position -> qlib base, not our sorted subclass
        raise RuntimeError(
            f"DETERMINISM GUARD: pos_type='{pos_type}' does not point at our "
            f"sorted subclass. Expected 'custom_handler.SortedPosition'."
        )

    import importlib
    import inspect

    from qlib.backtest.position import Position

    mod = importlib.import_module(module_path)
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise RuntimeError(
            f"DETERMINISM GUARD: pos_type '{pos_type}' cannot be imported "
            f"(module '{module_path}' has no '{cls_name}')."
        )
    if cls is Position or not issubclass(cls, Position):
        raise RuntimeError(
            f"DETERMINISM GUARD: pos_type '{pos_type}' is not a sorted Position "
            f"subclass."
        )
    # The whole point of our subclass is to sort get_stock_list; assert it.
    src = inspect.getsource(cls.get_stock_list)
    if "sorted(" not in src:
        raise RuntimeError(
            f"DETERMINISM GUARD: {pos_type}.get_stock_list does not sort its "
            f"result (source has no `sorted(`) -> non-deterministic. Refusing to "
            f"run."
        )
    return pos_type


def _positions_to_target_map(positions: dict) -> dict:
    """Map date -> sorted held instrument list (the intraday target names)."""
    out = {}
    for d, pos in positions.items():
        position = getattr(pos, "position", None)
        if not isinstance(position, dict):
            continue
        names = sorted(k for k, v in position.items() if isinstance(v, dict))
        out[d] = names
    return out


def _hash_target(names) -> str:
    return hashlib.md5("|".join(names).encode()).hexdigest()[:12]


def record_and_check_positions(exp_name: str, raise_on_rewrite: bool = False) -> dict:
    """Hash the current backtest's daily targets and diff against the last record.

    Returns the new record dict.  Prints a loud warning (or raises) if a date
    present in the *previous* record now has a different target set, which means
    history was rewritten and the live account would be whipsawed.
    """
    from qlib.workflow import R

    exp = R.get_exp(experiment_name=exp_name)
    recorders = exp.list_recorders()
    if not recorders:
        print("[det-guard] no recorders, skip position check.")
        return {}

    positions_key = "portfolio_analysis/positions_normal_1day.pkl"
    best, best_max = None, None
    for rid in recorders:
        try:
            pos = R.get_recorder(experiment_name=exp_name, recorder_id=rid).load_object(
                positions_key
            )
            if pos:
                mx = max(pos.keys())
                if best_max is None or mx > best_max:
                    best, best_max = pos, mx
        except Exception:
            continue
    if best is None:
        print("[det-guard] could not load positions, skip position check.")
        return {}

    target_map = _positions_to_target_map(best)
    run_record = {str(d.date() if hasattr(d, "date") else d): _hash_target(n)
                  for d, n in target_map.items()}
    last_date = max(target_map.keys())

    # ---- diff against previous record ----
    prev = {}
    if _STATE_PATH.exists():
        try:
            prev = json.loads(_STATE_PATH.read_text())
        except Exception:
            prev = {}
    prev_runs = prev.get("runs", {})

    # Only compare this experiment against its OWN previous record.  Different
    # experiments (probe runs, other configs) legitimately have different
    # targets and must not trigger a false "rewrite" alarm.
    rewritten = []
    rec = prev_runs.get(exp_name)
    if rec:
        common = set(rec.get("targets", {})) & set(run_record)
        for d in sorted(common):
            if rec["targets"][d] != run_record[d]:
                rewritten.append((exp_name, d, rec["targets"][d], run_record[d]))

    status = "OK"
    if rewritten:
        status = "REWRITE_DETECTED"
        print("\n" + "!" * 70)
        print(f"[det-guard] TARGET REWRITE DETECTED: {len(rewritten)} date(s) differ "
              f"from a previously-served run. Live account WILL churn if these are "
              f"the target source.")
        for exp_key, d, old_h, new_h in rewritten[:20]:
            print(f"[det-guard]   {d}: prev={old_h} now={new_h} (vs run {exp_key[:12]})")
        if len(rewritten) > 20:
            print(f"[det-guard]   ... and {len(rewritten) - 20} more")
        print("!" * 70 + "\n")

    # deterministic path hash over the whole served curve
    path_hash = hashlib.md5(
        "|".join(f"{d}:{run_record[d]}" for d in sorted(run_record)).encode()
    ).hexdigest()[:16]
    print(f"[det-guard] positions: {len(run_record)} days, last={last_date.date()}, "
          f"path_hash={path_hash}, status={status}")

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    # keep only this experiment's own key fresh; retain the other experiments' recs
    prev_runs[exp_name] = {
        "last_date": str(last_date.date() if hasattr(last_date, "date") else last_date),
        "path_hash": path_hash,
        "targets": run_record,
    }
    prev["runs"] = prev_runs
    _STATE_PATH.write_text(json.dumps(prev))

    if rewritten and raise_on_rewrite:
        raise RuntimeError(
            f"DETERMINISM GUARD: retroactive target rewrite on {len(rewritten)} "
            f"date(s); refusing to continue."
        )
    return {"path_hash": path_hash, "rewritten": rewritten, "status": status}


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    import fire
    from qlib import auto_init

    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")
    fire.Fire(record_and_check_positions)
