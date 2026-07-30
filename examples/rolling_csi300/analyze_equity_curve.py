import sys
from pathlib import Path

if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import fire
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from qlib import auto_init
from qlib.workflow import R


def analyze_equity_curve(exp_name="rolling_csi300_lgbm", output_dir=None):
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

    if output_dir is None:
        output_dir = Path(__file__).parent
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Read last update timestamp
    script_dir = Path(__file__).parent
    last_update_file = script_dir / ".last_update.txt"
    last_update_str = None
    if last_update_file.exists():
        last_update_str = last_update_file.read_text().strip()
        print(f"Last data update: {last_update_str}")

    exp = R.get_exp(experiment_name=exp_name)
    recorders = exp.list_recorders()
    if not recorders:
        print(f"[ERROR] No recorders found in experiment {exp_name!r}")
        return

    # Find the recorder with the latest report end date
    best_rid = None
    best_end = None
    for rid in recorders:
        try:
            rpt = pd.read_pickle(str(script_dir / "mlruns" / exp.id / rid / "artifacts" / "portfolio_analysis" / "report_normal_1day.pkl"))
            end = rpt.index[-1]
            if best_end is None or end > best_end:
                best_end = end
                best_rid = rid
        except Exception:
            continue
    if best_rid is None:
        print("[ERROR] No valid report found in any recorder")
        return
    rec = R.get_recorder(experiment_name=exp_name, recorder_id=best_rid)
    print(f"Using recorder {best_rid[:8]}... report end: {best_end}")

    artifacts_dir = script_dir / "mlruns" / exp.id / best_rid / "artifacts"
    report_file = artifacts_dir / "portfolio_analysis" / "report_normal_1day.pkl"
    if not report_file.exists():
        print(f"[ERROR] Report not found at {report_file}")
        return

    report = pd.read_pickle(str(report_file))
    print(f"Report shape: {report.shape}")
    print(f"Date range: {report.index[0]} to {report.index[-1]}")

    report["cum_return"] = (1 + report["return"]).cumprod()
    report["cum_bench"] = (1 + report["bench"]).cumprod()

    final_return = report["cum_return"].iloc[-1] - 1
    final_bench = report["cum_bench"].iloc[-1] - 1
    n_years = (report.index[-1] - report.index[0]).days / 365.25
    ann_return = (1 + final_return) ** (1 / n_years) - 1 if n_years > 0 else 0
    ann_bench = (1 + final_bench) ** (1 / n_years) - 1 if n_years > 0 else 0
    max_drawdown = (report["cum_return"] / report["cum_return"].cummax() - 1).min()
    dd = report["cum_return"] / report["cum_return"].cummax() - 1

    print(f"Cumulative return:      {final_return:+.2%}")
    print(f"Cumulative benchmark:   {final_bench:+.2%}")
    print(f"Annualized return:      {ann_return:+.2%}")
    print(f"Annualized benchmark:   {ann_bench:+.2%}")
    print(f"Max drawdown:           {max_drawdown:.2%}")
    print(f"Span:                   {n_years:.1f} years")

    dates = report.index

    # Build title with last update info
    title_line1 = f"Equity Curve - {exp_name}"
    title_line2 = f"Ann: {ann_return:+.2%}  Bench: {ann_bench:+.2%}  MaxDD: {max_drawdown:.2%}"
    if last_update_str:
        report_end = report.index[-1].strftime("%Y-%m-%d")
        title_line2 += f"  |  Data: {report_end}  Updated: {last_update_str}"

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.5, 0.25, 0.25],
        subplot_titles=(
            f"{title_line1}<br><sup>{title_line2}</sup>",
            "Drawdown",
            "Daily Return",
        ),
    )

    # Equity curve
    fig.add_trace(
        go.Scatter(
            x=dates, y=report["cum_return"],
            mode="lines", name="Strategy",
            line=dict(color="steelblue", width=1.5),
            hovertemplate="Date: %{x|%Y-%m-%d}<br>Strategy: %{y:.4f}<br>Cum Ret: %{customdata:.2%}<extra></extra>",
            customdata=report["cum_return"] - 1,
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=dates, y=report["cum_bench"],
            mode="lines", name="Benchmark",
            line=dict(color="gray", width=1.5, dash="dash"),
            hovertemplate="Date: %{x|%Y-%m-%d}<br>Benchmark: %{y:.4f}<br>Cum Ret: %{customdata:.2%}<extra></extra>",
            customdata=report["cum_bench"] - 1,
        ),
        row=1, col=1,
    )
    fig.add_hline(y=1, line_width=0.5, line_color="black", row=1, col=1)

    # Drawdown
    fig.add_trace(
        go.Scatter(
            x=dates, y=dd,
            fill="tozeroy", mode="lines", name="Drawdown",
            line=dict(color="coral", width=1),
            hovertemplate="Date: %{x|%Y-%m-%d}<br>Drawdown: %{y:.2%}<extra></extra>",
        ),
        row=2, col=1,
    )
    fig.add_hline(y=0, line_width=0.5, line_color="black", row=2, col=1)

    # Daily return bar chart
    colors = np.where(report["return"] >= 0, "green", "red")
    fig.add_trace(
        go.Bar(
            x=dates, y=report["return"],
            name="Daily Return",
            marker_color=colors,
            marker_line_width=0,
            hovertemplate="Date: %{x|%Y-%m-%d}<br>Return: %{y:.4%}<extra></extra>",
        ),
        row=3, col=1,
    )
    fig.add_hline(y=0, line_width=0.5, line_color="black", row=3, col=1)

    fig.update_layout(
        height=800,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=60, r=30, t=80, b=40),
    )

    # Range selector for zooming
    fig.update_xaxes(
        rangeslider_visible=False,
        rangeselector=dict(
            buttons=list([
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(count=1, label="1Y", step="year", stepmode="backward"),
                dict(count=3, label="3Y", step="year", stepmode="backward"),
                dict(step="all"),
            ]),
            bgcolor="lightgray",
            activecolor="steelblue",
        ),
        row=3, col=1,
    )

    fig.update_yaxes(title_text="Cumulative Return", row=1, col=1)
    fig.update_yaxes(title_text="Drawdown", tickformat=".1%", row=2, col=1)
    fig.update_yaxes(title_text="Daily Return", tickformat=".1%", row=3, col=1)
    fig.update_xaxes(title_text="Date", row=3, col=1)

    save_path = output_dir / f"equity_curve_{exp_name}.html"
    fig.write_html(str(save_path), include_plotlyjs=True)
    print(f"Plot saved to: {save_path}")

    # Also save a static PNG as preview
    try:
        import plotly.io as pio
        png_path = output_dir / f"equity_curve_{exp_name}.png"
        pio.write_image(fig, str(png_path), width=1400, height=800, scale=2)
        print(f"Preview PNG saved to: {png_path}")
    except Exception:
        pass


if __name__ == "__main__":
    fire.Fire(analyze_equity_curve)
