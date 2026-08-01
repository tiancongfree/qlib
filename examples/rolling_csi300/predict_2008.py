import warnings; warnings.filterwarnings('ignore')
import sys, os, pickle
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, '/home/tc/qlib')

from qlib import auto_init
from qlib.utils import init_instance_by_config
from qlib.utils.pickle_utils import add_safe_class
import custom_handler  # noqa
add_safe_class("custom_handler", "Alpha158Industry")
add_safe_class("custom_handler", "IndustryProcessor")

auto_init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")

from ruamel.yaml import YAML
y = YAML(typ="safe", pure=True)
with open('rolling_config.yaml') as f:
    conf = y.load(f)

# 重建 handler (Alpha158Industry)
handler_conf = dict(conf["task"]["dataset"]["kwargs"]["handler"])
# 覆盖区间到 2008
handler_conf["kwargs"]["start_time"] = "2007-06-01"
handler_conf["kwargs"]["end_time"] = "2009-03-31"
handler_conf["kwargs"]["fit_start_time"] = "2007-06-01"
handler_conf["kwargs"]["fit_end_time"] = "2008-12-31"
handler = init_instance_by_config(handler_conf)

# 构建 dataset, test=2008全年
ds_conf = {
    "class": "DatasetH",
    "module_path": "qlib.data.dataset",
    "kwargs": {
        "handler": handler,
        "segments": {
            "test": ("2008-01-02", "2008-12-31"),
        },
    },
}
dataset = init_instance_by_config(ds_conf)
print("Building dataset for 2008...")
# 用 model.predict 内部自动 prepare 特征 (不手动 prepare, 避免 label 拆分问题)
from qlib.utils.pickle_utils import add_safe_class
add_safe_class("custom_handler", "Alpha158Industry")
add_safe_class("custom_handler", "IndustryProcessor")

# 加载第 1 个滚动模型 (train 2008-2018)
from mlflow.tracking import MlflowClient
c = MlflowClient()
exp = c.get_experiment_by_name('rolling_models_20260801180017')
runs = c.search_runs([exp.experiment_id], order_by=['attributes.start_time'])
r = runs[0]
with open(f'mlruns/{exp.experiment_id}/{r.info.run_id}/artifacts/params.pkl', 'rb') as f:
    model = pickle.load(f)
print("model loaded:", type(model).__name__)

pred = model.predict(dataset, segment="test")
print("pred type:", type(pred), "shape:", pred.shape)
pred_df = pred if isinstance(pred, pd.DataFrame) else pred.to_frame(name="score")
pred_df = pred_df.sort_index()
print("pred range:", pred_df.index.get_level_values('datetime').min(), "->", pred_df.index.get_level_values('datetime').max(), "rows:", len(pred_df))
pred_df.to_pickle('/tmp/opencode/pred2008.pkl')
print("saved /tmp/opencode/pred2008.pkl")
print(pred_df.head())
