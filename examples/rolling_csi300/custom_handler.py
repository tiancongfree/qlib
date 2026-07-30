import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from qlib.data.dataset.processor import Processor
from qlib.contrib.data.handler import Alpha158
from qlib.contrib.data.loader import Alpha158DL


class IndustryProcessor(Processor):
    """Add industry-relative features (industry mean, excess, rank)."""

    def __init__(self, industry_map_path=None):
        if industry_map_path is None:
            industry_map_path = Path(__file__).parent / "industry_map.pkl"
        with open(industry_map_path, "rb") as f:
            self.industry_map = pickle.load(f)
        all_industries = sorted(set(self.industry_map.values()))
        self.industry_to_code = {ind: i for i, ind in enumerate(all_industries)}
        self._fit_done = False

    def fit(self, df=None):
        self._fit_done = True

    def __call__(self, df):
        if not self._fit_done:
            self.fit(df)

        instruments = df.index.get_level_values("instrument")
        industries = instruments.map(self.industry_map).map(self.industry_to_code).fillna(-1)

        # Add industry code column
        is_multi = isinstance(df.columns, pd.MultiIndex)
        if is_multi:
            df[("feature", "industry")] = industries.values
        else:
            df["industry"] = industries.values

        # Only compute industry-relative for a few key feature types
        key_feats = ["ROC", "MA", "MOM", "KMID", "RSV", "RANK", "SUMP", "CNTP"]
        group_key = [df.index.get_level_values(0), industries.values]

        new_cols = {}
        for col in df.columns:
            col_name = col[1] if is_multi else col
            if not any(k in str(col_name) for k in key_feats):
                continue
            values = df[col].values
            ind_mean = pd.Series(values).groupby(group_key).transform("mean").values
            new_cols[(f"ind_mean_{col_name}")] = ind_mean
            new_cols[(f"ind_excess_{col_name}")] = values - ind_mean
            new_cols[(f"ind_rank_{col_name}")] = pd.Series(values).groupby(group_key).rank(pct=True).values

        if new_cols:
            extra = pd.DataFrame(new_cols, index=df.index)
            if is_multi:
                extra.columns = pd.MultiIndex.from_tuples([("feature", c) for c in extra.columns])
            df = pd.concat([df, extra], axis=1)

        return df

    def readonly(self):
        return False


class Alpha158Industry(Alpha158):
    """Alpha158 + momentum + industry features."""

    def __init__(self, *args, **kwargs):
        processor = {"class": "IndustryProcessor", "module_path": "custom_handler"}
        shared = list(kwargs.pop("shared_processors", []))
        shared.append(processor)
        kwargs["shared_processors"] = shared
        super().__init__(*args, **kwargs)

    def get_feature_config(self):
        fields, names = super().get_feature_config()

        extra_windows = [20, 40, 60, 120]
        fields += [
            "($close - Ref($close, %d)) / (Ref($close, %d) + 1e-12)" % (d, d)
            for d in extra_windows
        ]
        names += ["MOM%d" % d for d in extra_windows]

        fields += ["$close / (Max($high, %d) + 1e-12)" % d for d in extra_windows]
        names += ["HIGHPCT%d" % d for d in extra_windows]

        fields += [
            "($close - Min($low, %d)) / (Max($high, %d) - Min($low, %d) + 1e-12)" % (d, d, d)
            for d in extra_windows
        ]
        names += ["POSITION%d" % d for d in extra_windows]

        fields += [
            "(Ref($close, %d) / $close) / (Ref($close, %d) / Ref($close, %d) + 1e-12)" % (d, 2*d, d)
            for d in [20, 40]
        ]
        names += ["ACCL%d" % d for d in [20, 40]]

        return fields, names
