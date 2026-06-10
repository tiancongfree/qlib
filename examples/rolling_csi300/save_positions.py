"""
Save daily positions from rolling backtest results to CSV.

This module is a shared utility called by run_rolling.py and
run_rolling_alpha360.py after rolling training completes.

Usage (as a library):
    from save_positions import save_positions_to_csv
    save_positions_to_csv("rolling_csi300_lgbm")

Usage (standalone):
    python save_positions.py --exp_name rolling_csi300_lgbm
    python save_positions.py --exp_name rolling_csi300_lgbm --output /path/to/output.csv
"""

import sys
from pathlib import Path

# Avoid importing local qlib source (which has uncompiled C extensions)
if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import pandas as pd
from qlib import auto_init
from qlib.workflow import R
from qlib.contrib.report.analysis_position.parse_position import parse_position


def save_positions_to_csv(
    exp_name: str = "rolling_csi300_lgbm",
    output_path: str = None,
    freq: str = "1day",
) -> pd.DataFrame:
    """
    Load positions from a rolling experiment recorder and save to CSV.

    Parameters
    ----------
    exp_name : str
        Name of the mlflow experiment containing the combined rolling result.
    output_path : str, optional
        Path to save the CSV file. If None, defaults to
        ``<script_dir>/positions_<exp_name>.csv``.
    freq : str
        Position frequency key, e.g. "1day". Defaults to "1day".

    Returns
    -------
    pd.DataFrame
        The parsed position DataFrame with MultiIndex (instrument, datetime).
        Columns: amount, cash, count, price, status, weight.
    """
    # Find the recorder with the most recent portfolio_analysis positions
    exp = R.get_exp(experiment_name=exp_name)
    recorders_dict = exp.list_recorders()
    if not recorders_dict:
        raise ValueError(f"No recorders found in experiment '{exp_name}'")

    positions_key = f"portfolio_analysis/positions_normal_{freq}.pkl"
    best_positions = None
    best_max_date = None

    for rid in recorders_dict:
        rec = R.get_recorder(experiment_name=exp_name, recorder_id=rid)
        try:
            pos = rec.load_object(positions_key)
            if pos:
                dates = sorted(pos.keys())
                if best_max_date is None or dates[-1] > best_max_date:
                    best_max_date = dates[-1]
                    best_positions = pos
        except Exception:
            continue

    if best_positions is None:
        raise FileNotFoundError(
            f"Could not load positions from '{positions_key}' in experiment "
            f"'{exp_name}'. Has the rolling training completed?"
        )
    positions = best_positions

    # Parse positions dict into DataFrame
    position_df = parse_position(positions)

    # Determine output path
    if output_path is None:
        output_dir = Path(__file__).parent
        output_path = output_dir / f"positions_{exp_name}.csv"
    else:
        output_path = Path(output_path)

    # Save to CSV
    position_df.to_csv(
        output_path,
        index=True,            # Keep MultiIndex (instrument, datetime)
        encoding="utf-8-sig",  # Avoid garbled characters for Chinese stock names
    )

    print(f"\n{'=' * 60}")
    print(f"  Daily positions saved to: {output_path}")
    print(f"  Experiment: {exp_name}")
    print(f"  Shape: {position_df.shape}")
    print(f"  Date range: {position_df.index.get_level_values('datetime').min()} -> "
          f"{position_df.index.get_level_values('datetime').max()}")
    print(f"  Unique stocks: {position_df.index.get_level_values('instrument').nunique()}")
    print(f"  Columns: {list(position_df.columns)}")
    print(f"{'=' * 60}")

    return position_df


def save_last_day_positions(
    exp_name: str = "rolling_csi300_lgbm",
    output_path: str = None,
    freq: str = "1day",
) -> pd.DataFrame:
    """
    Load positions from a rolling experiment recorder and save only the last
    trading day's positions (i.e. the final portfolio) to a CSV file.

    This is used as the target portfolio for real-time trading sync.

    Parameters
    ----------
    exp_name : str
        Name of the mlflow experiment containing the combined rolling result.
    output_path : str, optional
        Path to save the CSV file. If None, defaults to
        ``<script_dir>/last_day_positions_<exp_name>.csv``.
    freq : str
        Position frequency key, e.g. "1day". Defaults to "1day".

    Returns
    -------
    pd.DataFrame
        The last-day position DataFrame.
        Columns: instrument, datetime, amount, cash, count, price, status, weight.
    """
    position_df = save_positions_to_csv(exp_name=exp_name, output_path=None, freq=freq)

    last_date = position_df.index.get_level_values("datetime").max()
    last_day = position_df.xs(last_date, level="datetime")

    # Filter to held positions only (count_day > 0 and not sold)
    held = last_day[last_day["count_day"].notna() & (last_day["count_day"] > 0)].copy()
    held["datetime"] = last_date
    held = held.reset_index().set_index(["instrument", "datetime"])

    if output_path is None:
        output_dir = Path(__file__).parent
        output_path = output_dir / f"last_day_positions_{exp_name}.csv"
    else:
        output_path = Path(output_path)

    held.to_csv(
        output_path,
        index=True,
        encoding="utf-8-sig",
    )

    print(f"\n{'=' * 60}")
    print(f"  Last-day positions saved to: {output_path}")
    print(f"  Date: {last_date}")
    print(f"  Unique stocks: {len(held)}")
    print(f"  Total value: {held['amount'].sum():.2f}")
    print(f"{'=' * 60}")

    return held


if __name__ == "__main__":
    import fire

    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")
    fire.Fire({"save": save_positions_to_csv, "save_last": save_last_day_positions})