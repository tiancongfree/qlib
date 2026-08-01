"""Resume rolling training for remaining tasks (after OOM crash).

Trains tasks [start_idx:] from the same task list into the existing rolling
experiment, so previously-finished tasks are not retrained.
"""
import sys
from pathlib import Path

if str(Path(__file__).parent.parent.parent) in sys.path:
    sys.path.remove(str(Path(__file__).parent.parent.parent))

import mlflow
from ruamel.yaml import YAML
from qlib import auto_init
from qlib.data import D
from qlib.contrib.rolling.base import Rolling
from qlib.utils.pickle_utils import add_safe_class
import custom_handler  # noqa: F401
from save_positions import save_positions_to_csv

add_safe_class("custom_handler", "Alpha158Momentum")
add_safe_class("custom_handler", "Alpha158Industry")
add_safe_class("custom_handler", "Alpha158Fundamental")
add_safe_class("custom_handler", "IndustryProcessor")
add_safe_class("custom_handler", "FundamentalProcessor")
add_safe_class("custom_handler", "VolatilityTimingStrategy")
add_safe_class("custom_handler", "IndustryCappedStrategy")

CONF_PATH = Path(__file__).parent / "rolling_config_fund.yaml"
EXP_NAME = "rolling_csi300_lgbm_fund"
ROLLING_EXP = "rolling_models_20260731221442"


def main(start_idx: int = 22, exp_name: str = EXP_NAME, rolling_exp: str = ROLLING_EXP):
    auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")
    cal = D.calendar()
    test_end = str(cal[-1].date()) if hasattr(cal[-1], "date") else str(cal[-1])[:10]
    print(f"  test_end: {test_end}")

    rolling = Rolling(
        conf_path=CONF_PATH,
        step=60,
        horizon=20,
        exp_name=exp_name,
        rolling_exp=rolling_exp,
        test_end=test_end,
    )
    task_list = rolling.get_task_list()
    print(f"  total tasks: {len(task_list)}")
    remaining = task_list[start_idx:]
    print(f"  training tasks [{start_idx}:] = {len(remaining)}")

    from qlib.model.trainer import TrainerR

    trainer = TrainerR(experiment_name=rolling_exp, call_in_subproc=True)
    trainer(remaining)
    print("  remaining tasks done")

    # Ensemble + backtest using all finished tasks in rolling_exp
    rolling._ens_rolling()
    rolling._update_rolling_rec()
    print("  ensemble + backtest done")

    save_positions_to_csv(exp_name=exp_name)


if __name__ == "__main__":
    import fire

    fire.Fire(main)
