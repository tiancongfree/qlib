"""
Analyze Rank IC decay over time for rolling retraining experiments.

Usage:
    python analyze_ic_decay.py --exp_name rolling_csi300_lgbm
    python analyze_ic_decay.py --exp_name rolling_csi300_lgbm --freq yearly
    python analyze_ic_decay.py --exp_name rolling_csi300_lgbm --freq quarterly
"""
import os
import sys
from pathlib import Path

# Avoid importing local qlib source
if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import fire
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.stats import spearmanr

from qlib import auto_init
from qlib.workflow import R


def compute_rank_ic(pred: pd.Series, label: pd.Series) -> float:
    """Compute Rank IC (Spearman correlation) between pred and label."""
    mask = pred.notna() & label.notna()
    if mask.sum() < 10:
        return np.nan
    ic, _ = spearmanr(pred[mask].rank(pct=True), label[mask].rank(pct=True))
    return ic


def compute_ic_ir(ic_series: pd.Series) -> float:
    """Compute Information Ratio of IC series."""
    return ic_series.mean() / ic_series.std() if ic_series.std() > 0 else 0


def analyze_ic_decay(
    exp_name: str = "rolling_csi300_lgbm",
    freq: str = "quarterly",
    output_dir: str = None,
):
    """
    Analyze IC decay across rolling windows.

    Parameters
    ----------
    exp_name : str
        Name of the mlflow experiment containing the rolling ensemble result.
    freq : str
        Frequency for IC aggregation: 'yearly' or 'quarterly'.
    output_dir : str
        Directory to save the plot. Defaults to the script's directory.
    """
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    if output_dir is None:
        output_dir = Path(__file__).parent
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Get the combined rolling result from the experiment
    exp = R.get_exp(experiment_name=exp_name)
    recorders = exp.list_recorders()
    if not recorders:
        print(f"[ERROR] No recorders found in experiment '{exp_name}'")
        return

    # Get the last recorder (the combined ensemble result)
    rid = list(recorders.keys())[-1]
    rec = R.get_recorder(experiment_name=exp_name, recorder_id=rid)

    # Step 2: Load pred and label
    try:
        pred = rec.load_object("pred.pkl")
        label = rec.load_object("label.pkl")
    except Exception as e:
        # Try alternative: load from artifacts path
        print(f"[WARN] Could not load pred/label from recorder: {e}")
        print("Trying to read from rolling model experiment...")
        return _analyze_from_rolling_models(exp_name, freq, output_dir)

    print(f"Pred shape: {pred.shape}")
    print(f"Label shape: {label.shape}")

    # pred and label are DataFrames with DatetimeIndex and stock columns
    if isinstance(pred, pd.DataFrame):
        # Flatten to series, aligning by (date, stock)
        pred_s = pred.stack().droplevel(-1)
        label_s = label.stack().droplevel(-1)
        combined = pd.DataFrame({"pred": pred_s, "label": label_s}).dropna()
    elif isinstance(pred, pd.Series):
        combined = pd.DataFrame({"pred": pred, "label": label}).dropna()
    else:
        print(f"[ERROR] Unexpected type for pred: {type(pred)}")
        return

    print(f"Combined shape after alignment: {combined.shape}")

    # Step 3: Compute IC per time period
    combined.index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(d), s) for d, s in combined.index],
        names=["date", "stock"],
    )

    dates = combined.index.get_level_values("date").unique().sort_values()
    print(f"Date range: {dates.min()} to {dates.max()}, {len(dates)} unique dates")

    if freq == "yearly":
        periods = pd.date_range(dates.min(), dates.max(), freq="YS")
        period_labels = [f"{p.year}" for p in periods]
    elif freq == "quarterly":
        periods = pd.date_range(dates.min(), dates.max(), freq="QS")
        period_labels = [f"{p.year}Q{p.quarter}" for p in periods]
    else:
        raise ValueError(f"Unknown freq: {freq}")

    ic_results = []
    for i, p_start in enumerate(periods):
        if i < len(periods) - 1:
            p_end = periods[i + 1]
        else:
            p_end = dates.max() + pd.Timedelta(days=1)
        mask = (dates >= p_start) & (dates < p_end)
        period_dates = dates[mask]
        if len(period_dates) == 0:
            continue

        # Compute IC for each date in this period
        date_ics = []
        for d in period_dates:
            subset = combined.loc[d] if d in combined.index.get_level_values("date") else None
            if subset is None or len(subset) < 10:
                continue
            if isinstance(subset, pd.DataFrame):
                ic = compute_rank_ic(subset["pred"], subset["label"])
            else:
                ic = compute_rank_ic(subset["pred"], subset["label"])
            date_ics.append(ic)

        if len(date_ics) == 0:
            continue

        mean_ic = np.nanmean(date_ics)
        std_ic = np.nanstd(date_ics)
        ic_ir = mean_ic / std_ic if std_ic > 0 else 0

        ic_results.append(
            {
                "period": period_labels[i],
                "start_date": p_start,
                "mean_IC": mean_ic,
                "std_IC": std_ic,
                "IC_IR": ic_ir,
                "n_dates": len(date_ics),
            }
        )

    if not ic_results:
        print("[ERROR] No IC results computed")
        return

    ic_df = pd.DataFrame(ic_results)
    print("\n" + "=" * 60)
    print(f"  Rank IC Decay Analysis - {freq.upper()}")
    print("=" * 60)
    print(ic_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # Overall stats
    all_ics = ic_df["mean_IC"].dropna()
    print(f"\nMean IC: {all_ics.mean():.4f}")
    print(f"Std IC:  {all_ics.std():.4f}")
    if len(all_ics) >= 2:
        decay = all_ics.iloc[0] - all_ics.iloc[-1]
        print(f"IC Decay (first - last): {decay:.4f}")
        if all_ics.iloc[0] != 0:
            print(f"IC Decay %: {decay / abs(all_ics.iloc[0]) * 100:.1f}%")

    # Step 4: Plot
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    # Plot mean IC
    ax1 = axes[0]
    x = range(len(ic_df))
    ax1.bar(x, ic_df["mean_IC"], color="steelblue", alpha=0.7, label="Mean Rank IC")
    ax1.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax1.set_ylabel("Mean Rank IC")
    ax1.set_title(f"Rank IC Decay Over Time ({freq.capitalize()}) - {exp_name}")
    ax1.legend()
    ax1.grid(axis="y", alpha=0.3)

    # Annotate values
    for i, row in ic_df.iterrows():
        ax1.text(i, row["mean_IC"] + 0.002, f"{row['mean_IC']:.3f}", ha="center", fontsize=8)

    # Plot IC IR
    ax2 = axes[1]
    ax2.bar(x, ic_df["IC_IR"], color="coral", alpha=0.7, label="IC IR (mean/std)")
    ax2.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax2.set_xlabel("Period")
    ax2.set_ylabel("IC IR")
    ax2.legend()
    ax2.grid(axis="y", alpha=0.3)

    # Annotate values
    for i, row in ic_df.iterrows():
        ax2.text(i, row["IC_IR"] + 0.02, f"{row['IC_IR']:.2f}", ha="center", fontsize=8)

    # Set x-tick labels
    ax2.set_xticks(x)
    ax2.set_xticklabels(ic_df["period"], rotation=45)

    plt.tight_layout()
    save_path = output_dir / f"ic_decay_{freq}_{exp_name}.png"
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"\nPlot saved to: {save_path}")
    plt.close()


def _analyze_from_rolling_models(exp_name: str, freq: str, output_dir: Path):
    """
    Fallback: try to read from individual rolling model experiments.
    Walk through mlruns directory and find all rolling tasks.
    """
    mlruns_dir = Path(__file__).parent / "mlruns"
    if not mlruns_dir.exists():
        print(f"[ERROR] No mlruns directory found at {mlruns_dir}")
        return

    print(f"Scanning {mlruns_dir} for rolling experiment data...")

    # Find all experiments and their rolling tasks
    all_preds = []
    all_labels = []
    all_test_segs = []

    for exp_dir in mlruns_dir.iterdir():
        if not exp_dir.is_dir() or not (exp_dir / "meta.yaml").exists():
            continue
        for run_dir in exp_dir.iterdir():
            if not run_dir.is_dir() or not (run_dir / "meta.yaml").exists():
                continue
            artifacts = run_dir / "artifacts"
            if not artifacts.exists():
                continue

            # Check if it's a rolling task by looking at task artifact
            task_file = artifacts / "task"
            if not task_file.exists():
                continue

            # Try to load pred/label from artifacts
            pred_file = artifacts / "pred.pkl"
            label_file = artifacts / "label.pkl"
            if not pred_file.exists() or not label_file.exists():
                continue

            test_seg = _read_test_segment(artifacts)
            if test_seg is None:
                continue

            try:
                pred = pd.read_pickle(str(pred_file))
                label = pd.read_pickle(str(label_file))
                all_preds.append(pred)
                all_labels.append(label)
                all_test_segs.append(test_seg)
                print(f"  Loaded: test {test_seg[0].date()} -> {test_seg[1].date()}")
            except Exception as e:
                print(f"  [WARN] Failed to load {run_dir.name}: {e}")
                continue

    if not all_preds:
        print("[ERROR] No rolling task data found in mlruns")
        return

    # Concatenate all predictions and labels
    pred_all = pd.concat(all_preds, axis=0).sort_index()
    label_all = pd.concat(all_labels, axis=0).sort_index()

    # Remove duplicate indices (overlapping rolling windows — use first occurrence)
    pred_all = pred_all[~pred_all.index.duplicated(keep="first")]
    label_all = label_all[~label_all.index.duplicated(keep="first")]

    # Align
    common_idx = pred_all.index.intersection(label_all.index)
    pred_all = pred_all.loc[common_idx]
    label_all = label_all.loc[common_idx]

    print(f"Total combined: {len(pred_all)} rows, {len(pred_all.columns)} stocks")

    # Flatten
    if isinstance(pred_all, pd.DataFrame):
        pred_s = pred_all.stack()
        label_s = label_all.stack()
        combined = pd.DataFrame({"pred": pred_s, "label": label_s}).dropna()
    else:
        combined = pd.DataFrame({"pred": pred_all, "label": label_all}).dropna()

    # Build MultiIndex
    combined.index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(d), s) for d, s in combined.index],
        names=["date", "stock"],
    )

    dates = combined.index.get_level_values("date").unique().sort_values()
    print(f"Date range: {dates.min()} to {dates.max()}, {len(dates)} unique dates")

    # Compute IC per period
    if freq == "yearly":
        period_grouper = dates.year
    elif freq == "quarterly":
        period_grouper = dates.to_period("Q")
    else:
        raise ValueError(f"Unknown freq: {freq}")

    ic_results = []
    for period, period_dates in dates.groupby(period_grouper):
        date_ics = []
        for d in period_dates:
            try:
                subset = combined.loc[d]
            except KeyError:
                continue
            if isinstance(subset, pd.DataFrame):
                ic = compute_rank_ic(subset["pred"], subset["label"])
            else:
                ic = compute_rank_ic(subset["pred"], subset["label"])
            date_ics.append(ic)

        if len(date_ics) == 0:
            continue

        mean_ic = np.nanmean(date_ics)
        std_ic = np.nanstd(date_ics)
        ic_ir = mean_ic / std_ic if std_ic > 0 else 0

        ic_results.append(
            {
                "period": str(period),
                "mean_IC": mean_ic,
                "std_IC": std_ic,
                "IC_IR": ic_ir,
                "n_dates": len(date_ics),
            }
        )

    if not ic_results:
        print("[ERROR] No IC results computed")
        return

    ic_df = pd.DataFrame(ic_results)
    print("\n" + "=" * 60)
    print(f"  Rank IC Decay Analysis - {freq.upper()}")
    print("=" * 60)
    print(ic_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # Plot
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    x = range(len(ic_df))
    axes[0].bar(x, ic_df["mean_IC"], color="steelblue", alpha=0.7, label="Mean Rank IC")
    axes[0].axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    axes[0].set_ylabel("Mean Rank IC")
    axes[0].set_title(f"Rank IC Decay Over Time ({freq.capitalize()}) - {exp_name}")
    axes[0].legend()
    axes[0].grid(axis="y", alpha=0.3)

    axes[1].bar(x, ic_df["IC_IR"], color="coral", alpha=0.7, label="IC IR")
    axes[1].axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    axes[1].set_ylabel("IC IR")
    axes[1].legend()
    axes[1].grid(axis="y", alpha=0.3)

    axes[1].set_xticks(x)
    axes[1].set_xticklabels(ic_df["period"], rotation=45)

    plt.tight_layout()
    save_path = output_dir / f"ic_decay_{freq}_{exp_name}.png"
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"\nPlot saved to: {save_path}")
    plt.close()


def _read_test_segment(artifacts_dir: Path):
    """Read test segment from params directory."""
    params_dir = artifacts_dir.parent / "params"
    test_file = params_dir / "dataset.kwargs.segments.test"
    if not test_file.exists():
        return None
    try:
        content = test_file.read_text().strip()
        # Format: (Timestamp('2020-07-06 00:00:00'), Timestamp('2020-09-25 00:00:00'))
        import ast
        from datetime import datetime
        # Simple parsing: extract the two dates
        dates = content.strip("()").split(",")
        d1 = pd.Timestamp(dates[0].strip().split("(")[1].split(")")[0].strip("'"))
        d2 = pd.Timestamp(dates[1].strip().split("(")[1].split(")")[0].strip("'"))
        return (d1, d2)
    except Exception:
        return None


if __name__ == "__main__":
    fire.Fire(analyze_ic_decay)