"""
Drawdown alert: email when the strategy drawdown deepens past a new 5% tier.

Reads the latest backtest report (report_normal_1day.pkl) from an mlflow
experiment, computes the current drawdown against the all-time peak of the
strategy equity curve, and emails a short notification each time the drawdown
crosses a new 5% tier (5% -> 10% -> 15% -> ...).  Tiers are de-duplicated
across days via a small JSON state file (only the highest tier reached so far
triggers; recovery does not re-trigger until the drawdown deepens again).

Usage:
    python dd_alert.py                                          # defaults
    python dd_alert.py --exp_name rolling_csi300_lgbm --dry-run True
"""

import json
import os
import subprocess
import sys
from pathlib import Path

if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import pandas as pd
from qlib import auto_init
from qlib.workflow import R

from send_email import send_email

SCRIPT_DIR = Path(__file__).parent
DEFAULT_STATE_FILE = SCRIPT_DIR / "dd_alert_state.json"


def _ensure_email_password():
    """Make sure EMAIL_PASSWORD is available to send_email.

    On 244 the password lives as a Windows user environment variable, which
    does not always propagate into a WSL bash session (WSLENV bridging is
    unreliable).  Fall back to reading it via cmd.exe interop (WSL accesses
    Windows user env the same way the daily bat does).
    """
    if os.environ.get("EMAIL_PASSWORD"):
        return True
    try:
        raw = subprocess.run(
            ["cmd.exe", "/c", "echo %EMAIL_PASSWORD%"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        raw = ""
    if raw:
        os.environ["EMAIL_PASSWORD"] = raw
        return True
    print("WARNING: EMAIL_PASSWORD not found in WSL env or Windows user env.")
    return False


def _latest_report(exp_name: str) -> pd.DataFrame | None:
    """Return the report with the latest end date from the experiment."""
    exp = R.get_exp(experiment_name=exp_name)
    recorders = exp.list_recorders()
    if not recorders:
        print(f"[ERROR] No recorders found in experiment {exp_name!r}")
        return None

    best_rid = None
    best_end = None
    for rid in recorders:
        try:
            rpt = pd.read_pickle(
                str(SCRIPT_DIR / "mlruns" / exp.id / rid / "artifacts"
                    / "portfolio_analysis" / "report_normal_1day.pkl")
            )
            end = rpt.index[-1]
            if best_end is None or end > best_end:
                best_end = end
                best_rid = rid
        except Exception:
            continue
    if best_rid is None:
        print("[ERROR] No valid report found in any recorder")
        return None
    rpt = pd.read_pickle(
        str(SCRIPT_DIR / "mlruns" / exp.id / best_rid / "artifacts"
            / "portfolio_analysis" / "report_normal_1day.pkl")
    )
    print(f"Using recorder {best_rid[:8]}... report end: {rpt.index[-1]}")
    return rpt


def check_drawdown(exp_name: str = "rolling_csi300_lgbm",
                   state_file: str = None,
                   dry_run: bool = False):
    """Compute current drawdown and email when a new 5% tier is crossed."""
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    report = _latest_report(exp_name)
    if report is None:
        return

    cum_return = (1 + report["return"]).cumprod()
    dd = (cum_return / cum_return.cummax() - 1).iloc[-1]
    # tier = number of 5% steps the drawdown has reached: 6.3% -> 1 (5%),
    # 11.7% -> 2 (10%), 26% -> 5 (25%).  Drawn down by %drawdown means dd<0.
    tier = int(abs(dd) * 100 / 5)
    print(f"Current drawdown: {dd:.2%} (tier {tier}, i.e. {tier*5}%)")

    if state_file is None:
        state_file = str(DEFAULT_STATE_FILE)
    state_path = Path(state_file)
    state = {"last_tier": 0}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARNING: Failed to read state file {state_path}: {e}")

    last_tier = int(state.get("last_tier", 0))
    if tier <= last_tier or tier == 0:
        print(f"No new 5% tier crossed (last_tier={last_tier}). No email.")
        return

    pct = tier * 5
    subject = f"回撤提醒：策略回撤已达 {pct}%"
    text_body = (
        f"策略回撤已达 {pct}%（历史峰值算起）。\n"
        f"当前回撤：{dd:.2%}\n"
        f"档位：{pct}%（每 5% 提醒一次）\n"
        f"数据截至：{report.index[-1].strftime('%Y-%m-%d')}"
    )
    print(f"Drawdown crossed tier {pct}% -> sending email (dry_run={dry_run})")
    if dry_run:
        print("  [dry-run] would send:")
        print(f"  subject: {subject}")
        print(text_body)
        return

    _ensure_email_password()
    try:
        send_email(
            stock_code="回撤提醒",
            actions=[f"回撤已达 {pct}%"],
            send_flag=True,
            subject=subject,
            text_body=text_body,
            html_body=None,
        )
        state["last_tier"] = tier
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        print(f"Email sent. State updated: last_tier={tier} -> {state_path}")
    except Exception as e:
        print(f"ERROR: Failed to send email: {e}")


if __name__ == "__main__":
    import fire
    fire.Fire(check_drawdown)
