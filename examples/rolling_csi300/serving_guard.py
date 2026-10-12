"""Serving-machine guard for the rolling append / skip-train paths.

Motivation (2026-10-12)
-----------------------
The ``AppendRolling._ens_rolling``/``freeze_served`` protection is *machine-local*:
it freezes the combined pred that **this machine** last produced.  That is correct
on the serving machine (244, hostname ``DESKTOP-6MURESA``), because only there does
``daily_predict.py`` advance the combined pred every day.  But the *research*
machine does NOT run ``daily_predict`` between retrains, so its combined pred tail
lags behind the dates 244 has already served.

Consequence (the 2026-09-28 incident, run ``84dcd7c2``): a manual
``run_rolling.py --mode append`` on the research machine froze only up to its own
stale tail (2026-09-18), so the dates 2026-09-21..24 were treated as "new" and
written fresh.  When that experiment was pushed to 244 -- where those dates had
already been served -- the appended pred **overwrote already-served scores**, i.e.
exactly the retroactive rewrite ``freeze_served`` was meant to prevent.

Guard (option 1 chosen by the user): an append must only run on a SANCTIONED
machine (the training machine, which legitimately retrains + pushes to 244, or the
serving machine 244 itself), AND the local combined pred must not be behind the
last date the serving pipeline already served.

NOTE on the production topology (important): ``monthly_retrain.sh`` runs on the
TRAINING machine (env1, ``DESKTOP-JTV5CLG``) every ~2 days in append mode and then
pushes the result to the serving machine (244).  So "append on the research machine"
is the *normal* flow -- 2026-09-28 was only a problem because that run's local
combined tail (09-18) lagged what 244 had already served (09-24), so it rewrote
09-21..24.  Hence the whitelist is the *sanctioned origins* (training + serving),
and the real gate is the served-tail check below.

Two checks, both cheap and fail-fast:
  1. ``assert_serving_machine`` -- hostname must be in the sanctioned whitelist
     (default ``{DESKTOP-JTV5CLG, DESKTOP-6MURESA}``; override with env
     ``QLIB_SERVING_HOSTS`` or a ``logs/.serving_host`` file).  Stops an arbitrary
     machine (or a mis-scheduled task) from producing an append that later gets
     pushed into the serving pipeline.
  2. ``assert_served_tail_not_ahead`` -- if ``logs/.served_tail`` records the last
     date 244 served, the local combined pred tail must be >= that date minus a
     small grace; a lagging local tail means an append would rewrite already-served
     history.  ``monthly_retrain.sh`` pulls ``.served_tail`` from 244 before running
     so the training machine compares against the true serving tail.

Maintenance: ``mark_served_tail`` writes ``logs/.served_tail`` ONLY on the serving
machine (``SERVING_HOSTS``), so a training-machine run cannot corrupt the
authoritative served-tail file.

No qlib source is touched (same upgrade-safe stance as the rest of this repo).
"""

import os
import socket
from pathlib import Path

_HERE = Path(__file__).parent
_STATE_DIR = _HERE / "logs"
_SERVED_TAIL_PATH = _STATE_DIR / ".served_tail"
_SERVING_HOST_PATH = _STATE_DIR / ".serving_host"

# Sanctioned origins allowed to run append: the training machine (env1, retrains
# and pushes to 244) and the serving machine (244).  Overridable via env
# QLIB_SERVING_HOSTS (comma-separated) so a machine move needs only an env change.
_DEFAULT_SERVING_HOSTS = {"DESKTOP-JTV5CLG", "DESKTOP-6MURESA"}

# Host(s) whose daily pipeline actually CONSUMES the target (serves trades) and
# therefore owns ``logs/.served_tail``.  Only these may write the served tail.
_DEFAULT_CONSUMER_HOSTS = {"DESKTOP-6MURESA"}


def serving_hosts() -> set:
    """Return the set of sanctioned append-origin hostnames (env > file > default)."""
    env = os.environ.get("QLIB_SERVING_HOSTS")
    if env:
        hosts = {h.strip() for h in env.split(",") if h.strip()}
        if hosts:
            return hosts
    if _SERVING_HOST_PATH.exists():
        txt = _SERVING_HOST_PATH.read_text().strip()
        hosts = {h.strip() for h in txt.replace(",", "\n").splitlines() if h.strip()}
        if hosts:
            return hosts
    return set(_DEFAULT_SERVING_HOSTS)


def consumer_hosts() -> set:
    """Return the host(s) that consume the target and own ``.served_tail``."""
    env = os.environ.get("QLIB_CONSUMER_HOSTS")
    if env:
        hosts = {h.strip() for h in env.split(",") if h.strip()}
        if hosts:
            return hosts
    return set(_DEFAULT_CONSUMER_HOSTS)


def current_host() -> str:
    try:
        return socket.gethostname()
    except Exception:  # pragma: no cover - defensive
        return ""


def assert_serving_machine(force: bool = False) -> str:
    """Refuse to run the append/skip path on a non-serving machine.

    Returns the current hostname.  Raises ``RuntimeError`` when the host is not in
    the serving whitelist and ``force`` is False.
    """
    host = current_host()
    allowed = serving_hosts()
    if force:
        if host not in allowed:
            print(f"[serving-guard] FORCED append on non-serving host '{host}' "
                  f"(allowed: {sorted(allowed)}). Proceed at your own risk.")
        return host
    if host not in allowed:
        raise RuntimeError(
            f"SERVING GUARD: this host ('{host}') is not a sanctioned append "
            f"origin (allowed: {sorted(allowed)}). The append/skip path must run "
            f"on the training machine or the serving machine; an append from "
            f"elsewhere risks overwriting already-served dates in the serving "
            f"pipeline (2026-09-28 retroactive-rewrite incident). Run this on the "
            f"training/serving machine, set QLIB_SERVING_HOSTS, or pass "
            f"--force-append to override."
        )
    return host


def served_tail() -> str | None:
    """Return the recorded last-served date (ISO str) or None if unknown."""
    if not _SERVED_TAIL_PATH.exists():
        return None
    txt = _SERVED_TAIL_PATH.read_text().strip()
    return txt or None


def mark_served_tail(date_str) -> None:
    """Record the last-served date.

    Only the consuming (serving) machine should call this -- it owns the
    authoritative ``.served_tail``.  The caller (run_rolling) gates on
    ``consumer_hosts()``.
    """
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    if hasattr(date_str, "date"):
        date_str = date_str.date()
    _SERVED_TAIL_PATH.write_text(str(date_str))


def assert_served_tail_not_ahead(local_tail, force: bool = False, grace_days: int = 3) -> None:
    """Refuse when the local combined pred is behind the recorded served tail.

    ``local_tail`` is the max datetime present in the local combined pred.  If the
    recorded served tail is more than ``grace_days`` ahead, this machine's combined
    pred is stale relative to what the serving pipeline already consumed, so an
    append here would rewrite already-served dates -> refuse.
    """
    served = served_tail()
    if served is None or local_tail is None:
        return
    import pandas as _pd
    try:
        served_ts = _pd.Timestamp(served)
        local_ts = _pd.Timestamp(local_tail)
        if getattr(local_ts, "tz", None) is not None:
            local_ts = local_ts.tz_localize(None)
    except Exception:
        return
    lag = (served_ts - local_ts).days
    if lag > grace_days:
        msg = (
            f"SERVING GUARD: local combined pred tail ({local_ts.date()}) lags the "
            f"last-served date ({served_ts.date()}) by {lag} days (> grace "
            f"{grace_days}). An append here would rewrite already-served history "
            f"on the serving machine. Refresh the local combined pred (run the "
            f"daily flow on the serving machine) first, or pass --force-append."
        )
        if force:
            print(f"[serving-guard] WARNING (forced): {msg}")
            return
        raise RuntimeError(msg)
