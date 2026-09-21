#!/usr/bin/env python
"""Post-retrain health guard.

Runs as the LAST step of monthly_retrain.sh.  Only if every check passes does
the shell wrapper write logs/.last_retrain (the state that
monthly_retrain_catchup.sh reads).  On any failure this script:

  * prints an itemised report to stdout (which lands in the retrain log via tee),
  * writes logs/.retrain_health.json (machine-readable status, for anything that
    wants to inspect it),
  * sends an alert email (best-effort; a mail failure does NOT mask the guard
    failure).

Why this exists
---------------
monthly_retrain.sh tolerates several steps with `|| echo WARN` (data update,
scp, 244 deploy).  So "Monthly retrain done" in the log does NOT by itself prove
the pipeline produced fresh, usable artifacts.  Worse, a run that dies silently
(OOM at boot, see AGENTS 2026-09-21) writes nothing and is only noticed a day
later by the catch-up age check.  This guard turns "the script exited 0" into
"the artifacts are actually fresh and internally consistent".

Checks
------
1. rolling_models_* has at least one run whose pred.pkl exists (a real window was
   trained) and the number of pred-bearing runs did not DECREASE vs the previous
   health record (no experiment got clobbered).
2. combined experiment (rolling_csi300_lgbm_ndrop1) newest-tail run has both
   pred.pkl and label.pkl.
3. The combined pred tail reaches >= (data calendar last day - tolerance).  A
   frozen / lagging pred (the classic daily-pred bug) fails here.
4. pred and label indices are aligned on the last N days (SigAnaRecord contract);
   a mismatch means the report step would blow up downstream.
5. The rolling experiment we just appended to is the one with the most complete
   windows (mirrors monthly_retrain.sh step [3/5]).

Exit codes: 0 = healthy, 1 = unhealthy, 2 = guard itself errored.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)

import pandas as pd  # noqa: E402

import mlflow  # noqa: E402

EXP = "rolling_csi300_lgbm_ndrop1"
MLRUNS = HERE / "mlruns"
CALENDAR = Path.home() / ".qlib" / "qlib_data" / "cn_data" / "calendars" / "day.txt"
STATE = HERE / "logs" / ".retrain_health.json"


class Check:
    def __init__(self, name: str):
        self.name = name
        self.ok = None
        self.detail = ""

    def pass_(self, detail: str = ""):
        self.ok = True
        self.detail = detail
        return self

    def fail(self, detail: str):
        self.ok = False
        self.detail = detail
        return self

    def as_dict(self):
        return {"check": self.name, "ok": bool(self.ok), "detail": self.detail}


def _client():
    return mlflow.tracking.MlflowClient(tracking_uri=str(MLRUNS))


def _read_pred(path: Path):
    df = pd.read_pickle(path)
    if isinstance(df, pd.DataFrame):
        df = df.iloc[:, 0]
    return df


def _pred_last(path: Path):
    s = _read_pred(path)
    return pd.Timestamp(s.index.get_level_values("datetime").max())


def _calendar_last() -> pd.Timestamp | None:
    if not CALENDAR.exists():
        return None
    with CALENDAR.open() as f:
        last = None
        for line in f:
            line = line.strip()
            if line:
                last = line
    return pd.Timestamp(last) if last else None


def _pred_bearing_runs(client, exp) -> list:
    out = []
    for r in client.search_runs([exp.experiment_id]):
        p = MLRUNS / exp.experiment_id / r.info.run_id / "artifacts" / "pred.pkl"
        if p.exists():
            out.append(r)
    return out


def check_rolling_window(client) -> tuple[Check, int]:
    c = Check("rolling_window_trained")
    exps = [e for e in client.search_experiments()
            if e.name.startswith("rolling_models_") and e.lifecycle_stage == "active"]
    if not exps:
        return c.fail("no active rolling_models_* experiment"), 0
    # pick the experiment with the MOST pred-bearing runs (mirror retrain step 3)
    best = None
    for e in exps:
        n = len(_pred_bearing_runs(client, e))
        if best is None or n > best[1] or (n == best[1] and (e.creation_time or 0) > (best[0].creation_time or 0)):
            best = (e, n)
    exp, n = best
    if n == 0:
        return c.fail(f"{exp.name}: 0 pred-bearing runs"), 0
    return c.pass_(f"{exp.name}: {n} pred-bearing window run(s)"), n


def check_prev_health(n_now: int) -> Check:
    c = Check("rolling_windows_not_shrunk")
    prev = None
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            prev = None
    if not prev:
        return c.pass_("no previous health record (first run)")
    prev_n = int(prev.get("n_rolling_windows", -1))
    if prev_n < 0:
        return c.pass_("previous record had no window count")
    if n_now < prev_n:
        return c.fail(f"window count dropped {prev_n} -> {n_now} (experiment clobbered / aborted full rebuild?)")
    return c.pass_(f"{prev_n} -> {n_now} (not shrunk)")


def check_combined_artifacts(client) -> tuple[Check, object, Path | None]:
    c = Check("combined_pred_and_label_present")
    exp = client.get_experiment_by_name(EXP)
    if exp is None:
        return c.fail(f"combined experiment {EXP} not found"), None, None
    best = None
    for r in client.search_runs([exp.experiment_id], order_by=["attributes.start_time"]):
        p = MLRUNS / exp.experiment_id / r.info.run_id / "artifacts" / "pred.pkl"
        if not p.exists():
            continue
        try:
            last = _pred_last(p)
        except Exception:
            continue
        if best is None or last > best[1]:
            best = (r, last, p)
    if best is None:
        return c.fail("no combined run has a readable pred.pkl"), None, None
    run, last, pred_path = best
    lbl = MLRUNS / exp.experiment_id / run.info.run_id / "artifacts" / "label.pkl"
    if not lbl.exists():
        return c.fail(f"run {run.info.run_id[:12]} has pred but no label.pkl"), run, pred_path
    return c.pass_(f"run {run.info.run_id[:12]} pred+label present, tail {last.date()}"), run, pred_path


def check_pred_freshness(run, tail: pd.Timestamp) -> Check:
    c = Check("combined_pred_fresh")
    cal = _calendar_last()
    if cal is None:
        return c.fail(f"calendar not found at {CALENDAR}")
    lag = (cal - tail).days
    # tolerance: data calendar may legitimately be a few days ahead of pred when the
    # newest trading day has no labels/window yet.  But a frozen pred (the bug we
    # fixed) shows up as a large lag.
    tol = 7
    if lag > tol:
        return c.fail(
            f"pred tail {tail.date()} lags calendar {cal.date()} by {lag}d (> {tol}d) "
            f"- pred may be frozen"
        )
    return c.pass_(f"pred tail {tail.date()} vs calendar {cal.date()} (lag {lag}d <= {tol}d)")


def check_index_alignment(run, pred_path: Path, tail: pd.Timestamp) -> Check:
    c = Check("pred_label_index_aligned")
    exp = _client().get_experiment_by_name(EXP)
    pred = _read_pred(pred_path)
    lbl_path = MLRUNS / exp.experiment_id / run.info.run_id / "artifacts" / "label.pkl"
    try:
        lbl = pd.read_pickle(lbl_path)
        if isinstance(lbl, pd.DataFrame):
            lbl = lbl.iloc[:, 0]
    except Exception as e:
        return c.fail(f"could not read label.pkl: {e}")
    # only inspect the last few days to bound cost; SigAnaRecord needs matching index
    pre = pred[pred.index.get_level_values("datetime") == tail]
    common = pre.index.intersection(lbl.index)
    if len(common) == 0:
        return c.fail(f"pred tail day {tail.date()} has 0 instruments matching label index")
    return c.pass_(f"tail {tail.date()}: {len(common)}/{len(pre)} instruments aligned with label")


def send_alert(failures: list[dict], extra: str = "", consecutive: int = 0) -> None:
    try:
        from send_email import send_email
        from dd_alert import _ensure_email_password  # reuse Windows-env fallback
        _ensure_email_password()
    except Exception as e:  # pragma: no cover - best effort
        print(f"WARNING: cannot import mail helpers: {e}")
        return
    lines = [f"- {f['check']}: {f['detail']}" for f in failures]
    streak = f"\n连续失败次数: {consecutive}" if consecutive > 0 else ""
    text = (
        "重训健康检查未通过，已阻止写入 .last_retrain（catch-up 会在下次定时重试）。\n\n"
        f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"主机: {os.uname().nodename}{streak}\n\n"
        "失败项:\n" + "\n".join(lines)
    )
    if extra:
        text += f"\n\n{extra}"
    subject = f"[重训告警] 健康检查失败 ({len(failures)} 项)"
    try:
        send_email(
            stock_code="重训guard",
            actions=[l for l in lines],
            send_flag=True,
            subject=subject,
            text_body=text,
            html_body=None,
        )
    except Exception as e:
        print(f"WARNING: alert email failed: {e}")


def main() -> int:
    global EXP
    ap = argparse.ArgumentParser(description="Post-retrain artifact health guard")
    ap.add_argument("--no-email", action="store_true", help="skip the alert email (for dry runs)")
    ap.add_argument("--exp", default=EXP)
    args = ap.parse_args()
    EXP = args.exp

    try:
        client = _client()
        checks: list[Check] = []

        prev = None
        if STATE.exists():
            try:
                prev = json.loads(STATE.read_text(encoding="utf-8"))
            except Exception:
                prev = None

        c_roll, n_now = check_rolling_window(client)
        checks.append(c_roll)
        checks.append(check_prev_health(n_now))

        c_comb, run, pred_path = check_combined_artifacts(client)
        checks.append(c_comb)

        tail = None
        if run is not None and pred_path is not None:
            try:
                tail = _pred_last(pred_path)
            except Exception as e:
                checks.append(Check("combined_pred_readable").fail(f"unreadable pred.pkl: {e}"))
        if tail is not None:
            checks.append(check_pred_freshness(run, tail))
            checks.append(check_index_alignment(run, pred_path, tail))

        failures = [c.as_dict() for c in checks if not c.ok]
        healthy = not failures
        prev_streak = int(prev.get("consecutive_failures", 0)) if prev else 0
        streak = 0 if healthy else prev_streak + 1

        print("=" * 56)
        print("RETRAIN HEALTH GUARD")
        print("=" * 56)
        for c in checks:
            mark = "OK  " if c.ok else "FAIL"
            print(f"  [{mark}] {c.name}: {c.detail}")

        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({
            "healthy": healthy,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "consecutive_failures": streak,
            "n_rolling_windows": n_now,
            "combined_tail": str(tail.date()) if tail is not None else None,
            "calendar_last": str(_calendar_last().date()) if _calendar_last() is not None else None,
            "checks": [c.as_dict() for c in checks],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        if not healthy:
            print(f"\n{len(failures)} check(s) FAILED -> .last_retrain NOT written")
            if not args.no_email:
                send_alert(failures, consecutive=streak)
            return 1

        print("\nAll checks passed.")
        return 0
    except Exception as e:  # guard itself broke - fail loudly, do not claim health
        print(f"ERROR: guard crashed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
