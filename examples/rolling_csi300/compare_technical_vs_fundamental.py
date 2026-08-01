"""
Compare pure-technical (Alpha158Industry) vs technical+fundamental
(Alpha158Fundamental) rolling retraining results.

Usage:
    python3 compare_technical_vs_fundamental.py \
        --exp-tech rolling_csi300_lgbm \
        --exp-fund rolling_csi300_lgbm_fund
"""
import sys
from pathlib import Path

# Avoid importing local qlib source
if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import fire
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import spearmanr

from qlib import auto_init
from qlib.workflow import R


def compute_rank_ic(pred: pd.Series, label: pd.Series) -> float:
    mask = pred.notna() & label.notna()
    if mask.sum() < 10:
        return np.nan
    ic, _ = spearmanr(pred[mask].rank(pct=True), label[mask].rank(pct=True))
    return ic


def load_combined_result(exp_name: str):
    exp = R.get_exp(experiment_name=exp_name)
    recorders = list(exp.list_recorders().items())
    if not recorders:
        raise ValueError(f"No recorders found in '{exp_name}'")
    # Pick the recorder created last (dict order is not guaranteed by time).
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=str(Path(__file__).parent / "mlruns"))
    mf_exp = client.get_experiment_by_name(exp_name)
    runs = {r.info.run_id: r.info.start_time for r in client.search_runs([mf_exp.experiment_id])}
    recorders.sort(key=lambda kv: runs.get(kv[0], 0))
    rid = recorders[-1][0]
    rec = R.get_recorder(experiment_name=exp_name, recorder_id=rid)

    pred = rec.load_object("pred.pkl")
    label = rec.load_object("label.pkl")

    print(f"  Loaded {exp_name}: pred {pred.shape}, label {label.shape}")

    if isinstance(pred, pd.DataFrame):
        pred_s = pred.iloc[:, 0]
        label_s = label.iloc[:, 0]
    elif isinstance(pred, pd.Series):
        pred_s = pred
        label_s = label
    else:
        raise TypeError(f"Unexpected type: {type(pred)}")

    combined = pd.DataFrame({"pred": pred_s, "label": label_s}).dropna()
    return combined


def compute_period_ic(combined: pd.DataFrame, freq: str = "quarterly"):
    dates = combined.index.get_level_values(0).unique().sort_values()

    if freq == "quarterly":
        period_grouper = pd.DatetimeIndex(dates).to_period("Q")
    elif freq == "yearly":
        period_grouper = dates.year
    else:
        raise ValueError(f"Unknown freq: {freq}")

    date_series = pd.Series(dates, index=dates)
    results = []
    for period, period_dates in date_series.groupby(period_grouper):
        date_ics = []
        for d in period_dates.values:
            try:
                subset = combined.xs(d, level=0, drop_level=False)
            except KeyError:
                continue
            ic = compute_rank_ic(subset["pred"], subset["label"])
            date_ics.append(ic)

        if len(date_ics) == 0:
            continue

        mean_ic = np.nanmean(date_ics)
        std_ic = np.nanstd(date_ics)
        ic_ir = mean_ic / std_ic if std_ic and std_ic > 0 else 0

        results.append({
            "period": str(period),
            "mean_IC": mean_ic,
            "std_IC": std_ic,
            "IC_IR_kappa": ic_ir,
            "n_dates": len(date_ics),
        })
    return pd.DataFrame(results)


def main(exp_tech: str = "rolling_csi300_lgbm", exp_fund: str = "rolling_csi300_lgbm_fund"):
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    print("=" * 70)
    print("  Technical vs Technical+Fundamental — Rolling Retraining Comparison")
    print("=" * 70)

    print("\n[1] Loading results...")
    combined_tech = load_combined_result(exp_tech)
    combined_fund = load_combined_result(exp_fund)

    print("\n[2] Overall Metrics")
    print("-" * 70)

    def overall_stats(combined, name):
        daily_ics = []
        for d in combined.index.get_level_values(0).unique():
            try:
                subset = combined.xs(d, level=0, drop_level=False)
                ic = compute_rank_ic(subset["pred"], subset["label"])
                daily_ics.append(ic)
            except (KeyError, IndexError):
                continue
        daily_ics = np.array(daily_ics)
        mean_ic = np.nanmean(daily_ics)
        std_ic = np.nanstd(daily_ics)
        icir = mean_ic / std_ic if std_ic and std_ic > 0 else 0
        print(f"  {name:>20s}:  Mean IC={mean_ic:.4f}  Std IC={std_ic:.4f}  ICIR={icir:.2f}  N_dates={len(daily_ics)}")
        return daily_ics

    ic_tech = overall_stats(combined_tech, "Technical")
    ic_fund = overall_stats(combined_fund, "Tech+Fund")

    print("\n[3] Quarterly IC Comparison")
    print("-" * 70)

    q_tech = compute_period_ic(combined_tech, "quarterly")
    q_fund = compute_period_ic(combined_fund, "quarterly")

    periods = sorted(set(q_tech["period"].tolist()) & set(q_fund["period"].tolist()))
    comparison = []
    for p in periods:
        row_t = q_tech[q_tech["period"] == p]
        row_f = q_fund[q_fund["period"] == p]
        if len(row_t) == 0 or len(row_f) == 0:
            continue
        ic_t = row_t["mean_IC"].values[0]
        ic_f = row_f["mean_IC"].values[0]
        diff = ic_f - ic_t
        comparison.append({
            "period": p,
            "IC_Tech": ic_t,
            "IC_Fund": ic_f,
            "delta": diff,
            "delta_pct": (diff / abs(ic_t) * 100) if abs(ic_t) > 0 else np.nan,
        })

    comp_df = pd.DataFrame(comparison)
    print(comp_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n[4] IC Trend Summary")
    print("-" * 70)
    for name, df in [("Technical", q_tech), ("Tech+Fund", q_fund)]:
        ics = df["mean_IC"].dropna().values
        if len(ics) >= 2:
            decay = ics[0] - ics[-1]
            trend = "↓ DECAY" if decay > 0 else "↑ IMPROVING" if decay < 0 else "→ FLAT"
            print(f"  {name:>20s}: first={ics[0]:.4f}  last={ics[-1]:.4f}  delta={decay:.4f}  {trend}")

    print("\n[5] Generating comparison plot...")
    fig, axes = plt.subplots(3, 1, figsize=(14, 12), sharex=True)

    x = np.arange(len(comparison))
    width = 0.35

    ax = axes[0]
    ax.bar(x - width / 2, comp_df["IC_Tech"], width, label="Technical", color="steelblue", alpha=0.8)
    ax.bar(x + width / 2, comp_df["IC_Fund"], width, label="Tech+Fund", color="coral", alpha=0.8)
    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_ylabel("Mean Rank IC")
    ax.set_title("Technical vs Tech+Fundamental — Quarterly Rank IC")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    ax = axes[1]
    colors = ["green" if v > 0 else "red" for v in comp_df["delta"]]
    ax.bar(x, comp_df["delta"], color=colors, alpha=0.7)
    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_ylabel("Δ IC (Fund - Tech)")
    ax.grid(axis="y", alpha=0.3)

    ax = axes[2]
    ax.bar(x - width / 2,
           q_tech[q_tech["period"].isin(periods)]["IC_IR_kappa"], width,
           label="Technical", color="steelblue", alpha=0.8)
    ax.bar(x + width / 2,
           q_fund[q_fund["period"].isin(periods)]["IC_IR_kappa"], width,
           label="Tech+Fund", color="coral", alpha=0.8)
    ax.set_xlabel("Quarter")
    ax.set_ylabel("IC IR (Information Ratio)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    ax.set_xticks(x)
    ax.set_xticklabels(comp_df["period"], rotation=45)

    plt.tight_layout()
    save_path = Path(__file__).parent / "comparison_tech_vs_fund.png"
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"  Plot saved to: {save_path}")
    plt.close()

    print("\n" + "=" * 70)
    print("  CONCLUSION")
    print("=" * 70)
    t_mean = np.nanmean(ic_tech)
    f_mean = np.nanmean(ic_fund)
    t_std = np.nanstd(ic_tech)
    f_std = np.nanstd(ic_fund)
    t_icir = t_mean / t_std if t_std and t_std > 0 else 0
    f_icir = f_mean / f_std if f_std and f_std > 0 else 0

    print(f"  Technical:   Mean IC = {t_mean:.4f}  |  Std IC = {t_std:.4f}  |  IC IR = {t_icir:.2f}")
    print(f"  Tech+Fund:   Mean IC = {f_mean:.4f}  |  Std IC = {f_std:.4f}  |  IC IR = {f_icir:.2f}")
    diff = f_mean - t_mean
    if diff > 0.002:
        print(f"  → Fundamental IMPROVES by {diff:.4f} ({diff/abs(t_mean)*100:.1f}%)")
    elif diff < -0.002:
        print(f"  → Fundamental DEGRADES by {abs(diff):.4f} ({abs(diff)/abs(t_mean)*100:.1f}%)")
    else:
        print(f"  → Fundamental ≈ Technical (difference {diff:.4f} is negligible)")


if __name__ == "__main__":
    fire.Fire(main)
