"""
Compare Alpha158 vs Alpha360 rolling retraining results.

Usage:
    python3 compare_alpha158_vs_360.py
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
    rid = recorders[-1][0]
    rec = R.get_recorder(experiment_name=exp_name, recorder_id=rid)

    pred = rec.load_object("pred.pkl")
    label = rec.load_object("label.pkl")

    print(f"  Loaded {exp_name}: pred {pred.shape}, label {label.shape}")

    # pred is MultiIndex DataFrame with columns=['score'], label is MultiIndex DataFrame with columns=['Ref(...)']
    # stack to Series for easier manipulation
    if isinstance(pred, pd.DataFrame):
        pred_s = pred.iloc[:, 0]  # first column as Series
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

    # pandas DatetimeIndex.groupby with PeriodIndex needs .to_series()
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


def main(exp_a158: str = "rolling_csi300_lgbm", exp_a360: str = "rolling_csi300_lgbm_alpha360"):
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    print("=" * 70)
    print("  Alpha158 vs Alpha360 — Rolling Retraining Comparison")
    print("=" * 70)

    # Load both
    print("\n[1] Loading results...")
    combined_a158 = load_combined_result(exp_a158)
    combined_a360 = load_combined_result(exp_a360)

    # Overall stats
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

    ic_a158 = overall_stats(combined_a158, "Alpha158")
    ic_a360 = overall_stats(combined_a360, "Alpha360")

    # Period-by-period comparison
    print("\n[3] Quarterly IC Decay Comparison")
    print("-" * 70)

    q_a158 = compute_period_ic(combined_a158, "quarterly")
    q_a360 = compute_period_ic(combined_a360, "quarterly")

    # Align periods
    periods = sorted(set(q_a158["period"].tolist()) & set(q_a360["period"].tolist()))
    comparison = []
    for p in periods:
        row_a158 = q_a158[q_a158["period"] == p]
        row_a360 = q_a360[q_a360["period"] == p]
        if len(row_a158) == 0 or len(row_a360) == 0:
            continue
        ic_a158_val = row_a158["mean_IC"].values[0]
        ic_a360_val = row_a360["mean_IC"].values[0]
        diff = ic_a360_val - ic_a158_val
        comparison.append({
            "period": p,
            "IC_Alpha158": ic_a158_val,
            "IC_Alpha360": ic_a360_val,
            "Δ": diff,
            "Δ%": (diff / abs(ic_a158_val) * 100) if abs(ic_a158_val) > 0 else np.nan,
        })

    comp_df = pd.DataFrame(comparison)
    print(comp_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # Trend analysis
    print("\n[4] IC Trend Summary")
    print("-" * 70)
    for name, df in [("Alpha158", q_a158), ("Alpha360", q_a360)]:
        ics = df["mean_IC"].dropna().values
        if len(ics) >= 2:
            decay = ics[0] - ics[-1]
            trend = "↓ DECAY" if decay > 0 else "↑ IMPROVING" if decay < 0 else "→ FLAT"
            print(f"  {name:>20s}: first={ics[0]:.4f}  last={ics[-1]:.4f}  delta={decay:.4f}  {trend}")

    # Plot
    print("\n[5] Generating comparison plot...")
    fig, axes = plt.subplots(3, 1, figsize=(14, 12), sharex=True)

    x = np.arange(len(comparison))
    width = 0.35

    # Panel 1: Mean IC
    ax = axes[0]
    ax.bar(x - width / 2, comp_df["IC_Alpha158"], width, label="Alpha158", color="steelblue", alpha=0.8)
    ax.bar(x + width / 2, comp_df["IC_Alpha360"], width, label="Alpha360", color="coral", alpha=0.8)
    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_ylabel("Mean Rank IC")
    ax.set_title("Alpha158 vs Alpha360 — Quarterly Rank IC Comparison")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    # Panel 2: IC delta
    ax = axes[1]
    colors = ["green" if v > 0 else "red" for v in comp_df["Δ"]]
    ax.bar(x, comp_df["Δ"], color=colors, alpha=0.7)
    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_ylabel("Δ IC (Alpha360 - Alpha158)")
    ax.grid(axis="y", alpha=0.3)

    # Panel 3: IC IR
    ax = axes[2]
    ax.bar(x - width / 2,
           q_a158[q_a158["period"].isin(periods)]["IC_IR_kappa"], width,
           label="Alpha158", color="steelblue", alpha=0.8)
    ax.bar(x + width / 2,
           q_a360[q_a360["period"].isin(periods)]["IC_IR_kappa"], width,
           label="Alpha360", color="coral", alpha=0.8)
    ax.set_xlabel("Quarter")
    ax.set_ylabel("IC IR (Information Ratio)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    ax.set_xticks(x)
    ax.set_xticklabels(comp_df["period"], rotation=45)

    plt.tight_layout()
    save_path = Path(__file__).parent / "comparison_alpha158_vs_alpha360.png"
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"  Plot saved to: {save_path}")
    plt.close()

    # Overall conclusion
    print("\n" + "=" * 70)
    print("  CONCLUSION")
    print("=" * 70)
    a158_mean = np.nanmean(ic_a158)
    a360_mean = np.nanmean(ic_a360)
    a158_std = np.nanstd(ic_a158)
    a360_std = np.nanstd(ic_a360)
    a158_icir = a158_mean / a158_std if a158_std and a158_std > 0 else 0
    a360_icir = a360_mean / a360_std if a360_std and a360_std > 0 else 0

    print(f"  Alpha158:  Mean IC = {a158_mean:.4f}  |  Std IC = {a158_std:.4f}  |  IC IR = {a158_icir:.2f}")
    print(f"  Alpha360:  Mean IC = {a360_mean:.4f}  |  Std IC = {a360_std:.4f}  |  IC IR = {a360_icir:.2f}")
    diff = a360_mean - a158_mean
    if diff > 0.002:
        print(f"  → Alpha360 IMPROVES by {diff:.4f} ({diff/abs(a158_mean)*100:.1f}%)")
    elif diff < -0.002:
        print(f"  → Alpha360 DEGRADES by {abs(diff):.4f} ({abs(diff)/abs(a158_mean)*100:.1f}%)")
    else:
        print(f"  → Alpha360 ≈ Alpha158 (difference {diff:.4f} is negligible)")


if __name__ == "__main__":
    fire.Fire(main)