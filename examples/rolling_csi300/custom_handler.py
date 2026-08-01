import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from qlib.data.dataset.processor import Processor
from qlib.contrib.data.handler import Alpha158
from qlib.contrib.data.loader import Alpha158DL
from qlib.contrib.strategy import TopkDropoutStrategy
from qlib.backtest.decision import Order, OrderDir, TradeDecisionWO
from qlib.data import D
import copy


class VolatilityTimingStrategy(TopkDropoutStrategy):
    """Dynamic topk based on CSI 300 volatility + trend regime.

    牛市高波（acceleration）→  stay invested
    熊市高波（panic）    →  cut hard
    牛市低波（calm uptrend）→ full attack
    熊市低波（quiet downtrend）→ light position
    """

    def __init__(
        self,
        *,
        vol_window=20,
        trend_window=120,
        vol_pctile_high=0.75,
        vol_pctile_low=0.30,
        high_topk=12,
        mid_topk=20,
        low_topk=30,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.vol_window = vol_window
        self.trend_window = trend_window
        self.vol_pctile_high = vol_pctile_high
        self.vol_pctile_low = vol_pctile_low
        self.high_topk = high_topk
        self.mid_topk = mid_topk
        self.low_topk = low_topk
        self._market_data = None

    def _precompute_market(self):
        cal = D.calendar()
        start = str(cal[0].date())
        end = str(cal[-1].date())

        close = D.features(
            D.instruments("csi300"), ["$close"], start_time=start, end_time=end
        )
        market = close.groupby(level="datetime").mean().squeeze()
        ret = market.pct_change(fill_method=None).dropna()

        vol = ret.rolling(self.vol_window).std() * np.sqrt(252)
        ma = market.rolling(self.trend_window).mean()

        df = pd.DataFrame({"close": market, "ma": ma, "vol": vol}).dropna()
        # Rolling vol percentile over trailing 2yr (~500 trading days)
        df["vol_pctile"] = (
            df["vol"]
            .rolling(500)
            .apply(lambda x: (x.iloc[-1] >= x).mean(), raw=False)
        )
        # Trend: close / MA - 1
        df["trend"] = df["close"] / df["ma"] - 1

        self._market_data = df

    def _regime_topk(self, cur_ts):
        df = self._market_data
        past = df[df.index <= cur_ts]
        if len(past) == 0:
            return self.mid_topk

        row = past.iloc[-1]
        is_bull = row["trend"] > -0.02  # slightly forgiving
        is_high_vol = row["vol_pctile"] >= self.vol_pctile_high
        is_low_vol = row["vol_pctile"] <= self.vol_pctile_low

        if is_bull:
            if is_low_vol:
                return self.low_topk     # 牛市低波 → 满仓进攻
            elif is_high_vol:
                return self.mid_topk     # 牛市高波 → 正常持有（加速阶段）
            else:
                return self.mid_topk     # 中性
        else:
            if is_high_vol:
                return self.high_topk    # 熊市高波 → 轻仓防守
            elif is_low_vol:
                return self.mid_topk     # 熊市低波 → 轻微防御
            else:
                return self.mid_topk

    def generate_trade_decision(self, execute_result=None):
        if self._market_data is None:
            self._precompute_market()

        trade_step = self.trade_calendar.get_trade_step()
        _, trade_end_time = self.trade_calendar.get_step_time(trade_step)

        self.topk = self._regime_topk(pd.Timestamp(trade_end_time))
        return super().generate_trade_decision(execute_result)


class IndustryProcessor(Processor):
    """Add industry-relative features (industry mean, excess, rank)."""

    # Fundamental feature names handled by FundamentalProcessor; the IndustryProcessor
    # must NOT touch them (its substring matching would wrongly pick up e.g. "MA" in
    # "GROSSPROFIT_MARGIN").
    FUNDAMENTAL_FEATURES = set(
        [
            "PE_TTM", "PB", "PS_TTM", "DV_TTM", "LOG_MV", "CIRC_MV", "TURNOVER",
            "ROE", "ROE_WAA", "ROA", "GROSSPROFIT_MARGIN", "NETPROFIT_MARGIN",
            "DEBT_TO_ASSETS", "CURRENT_RATIO", "QUICK_RATIO", "OR_YOY",
            "NETPROFIT_YOY", "Q_GR_YOY", "Q_PROFIT_YOY", "OCF_TO_OR", "EPS",
            "ROE_YOY", "ROE_WAA_YOY", "ROA_YOY", "GM_YOY", "NM_YOY",
            "LEVERAGE_YOY", "EPS_YOY",
        ]
    )

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
            if col_name in self.FUNDAMENTAL_FEATURES:
                continue  # handled by FundamentalProcessor
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


class DvRatioProcessor(Processor):
    """Robust preprocessing for 分红融资比 (dividend/financing ratio) features.

    The raw ratio is heavily right-skewed and undefined (NaN) for stocks with no
    equity financing. We:
      1) fill NaN with 0 (separates "no financing" from "low ratio");
      2) winsorize extreme values cross-sectionally (per-date 1%/99% clip);
      3) add a cross-sectional rank version CSR_ (scale-free, robust to skew).
    """

    DV_FEATURES = ("DV_RATIO", "DV_RATIO_ALL", "DV_RATIO_YOY", "DV_RATIO_ALL_YOY")

    def __init__(self, winsor_quantile: float = 0.01):
        self.winsor_quantile = winsor_quantile
        self._fit_done = False

    def fit(self, df=None):
        self._fit_done = True

    def __call__(self, df):
        if not self._fit_done:
            self.fit(df)

        is_multi = isinstance(df.columns, pd.MultiIndex)
        dts = df.index.get_level_values(0)

        def winsor(s):
            lo, hi = s.quantile(self.winsor_quantile), s.quantile(1 - self.winsor_quantile)
            return s.clip(lo, hi)

        new_cols = {}
        for col in df.columns:
            col_name = col[1] if is_multi else col
            if not any(col_name.startswith(k) for k in self.DV_FEATURES):
                continue
            s = pd.Series(df[col].values, index=dts)
            # 1) NaN -> 0 (no financing = 0 financing, keep as a distinct value)
            s = s.fillna(0.0)
            # 2) cross-sectional winsorize (per date)
            wins = s.groupby(level=0).transform(winsor)
            if is_multi:
                df[col] = wins.values
            else:
                df[col] = wins.values
            # 3) cross-sectional rank version (approx unit-normalized)
            csr = wins.groupby(level=0).rank(pct=True)
            csr = (csr - 0.5) * 3.46
            new_cols[(f"CSR_{col_name}")] = csr.values

        if new_cols:
            extra = pd.DataFrame(new_cols, index=df.index)
            if is_multi:
                extra.columns = pd.MultiIndex.from_tuples([("feature", c) for c in extra.columns])
            df = pd.concat([df, extra], axis=1)

        return df

    def readonly(self):
        return False


class Alpha158DvRatio(Alpha158Industry):
    """Alpha158Industry + 分红融资比 (dividend/financing ratio) features.

    DvRatio features (no lookahead):
        use_daily=True:   $dv_ratio, $dv_ratio_all (precomputed daily bins, ffill
                          from announcement dates -> ~4x faster rolling build)
        use_daily=False:  P($$dv_ratio_q), P($$dv_ratio_all_q) (PIT quarterly)
        + YoY reference 4 quarters ago in both cases.
    """

    def __init__(self, use_daily: bool = True, *args, **kwargs):
        self.use_daily = use_daily
        processor = {"class": "DvRatioProcessor", "module_path": "custom_handler"}
        shared = list(kwargs.pop("shared_processors", []))
        shared.append(processor)
        kwargs["shared_processors"] = shared
        super().__init__(*args, **kwargs)

    def get_feature_config(self):
        fields, names = super().get_feature_config()

        if self.use_daily:
            dv_fields = [
                ("$dv_ratio", "DV_RATIO"),
                ("$dv_ratio_all", "DV_RATIO_ALL"),
                ("Ref($dv_ratio, 60)", "DV_RATIO_YOY"),
                ("Ref($dv_ratio_all, 60)", "DV_RATIO_ALL_YOY"),
            ]
        else:
            dv_fields = [
                ("P($$dv_ratio_q)", "DV_RATIO"),
                ("P($$dv_ratio_all_q)", "DV_RATIO_ALL"),
                ("P(Ref($$dv_ratio_q, 4))", "DV_RATIO_YOY"),
                ("P(Ref($$dv_ratio_all_q, 4))", "DV_RATIO_ALL_YOY"),
            ]
        for expr, name in dv_fields:
            fields += [expr]
            names += [name]

        return fields, names


class IndustryCappedStrategy(TopkDropoutStrategy):
    """TopK with gradual industry cap — phases out excess gradually to limit turnover."""

    def __init__(self, *, max_per_industry=4, industry_map_path=None, **kwargs):
        super().__init__(**kwargs)
        self.max_per_industry = max_per_industry
        if industry_map_path is None:
            industry_map_path = Path(__file__).parent / "industry_map.pkl"
        with open(industry_map_path, "rb") as f:
            self.industry_map = pickle.load(f)

    def _industry_code(self, stock):
        return self.industry_map.get(stock.lower(), "UNKNOWN")

    def generate_trade_decision(self, execute_result=None):
        trade_step = self.trade_calendar.get_trade_step()
        trade_start_time, trade_end_time = self.trade_calendar.get_step_time(trade_step)
        pred_start_time, pred_end_time = self.trade_calendar.get_step_time(trade_step, shift=1)
        pred_score = self.signal.get_signal(start_time=pred_start_time, end_time=pred_end_time)
        if isinstance(pred_score, pd.DataFrame):
            pred_score = pred_score.iloc[:, 0]
        if pred_score is None:
            return TradeDecisionWO([], self)

        def get_first_n(li, n):
            return list(li)[:n]
        def get_last_n(li, n):
            return list(li)[-n:]

        current_temp = copy.deepcopy(self.trade_position)
        current_stock_list = current_temp.get_stock_list()

        # --- Step 1: normal TopkDropout candidate selection ---
        last = pred_score.reindex(current_stock_list).sort_values(ascending=False).index
        today = get_first_n(
            pred_score[~pred_score.index.isin(last)].sort_values(ascending=False).index,
            self.n_drop + self.topk - len(last),
        )
        comb = pred_score.reindex(last.union(pd.Index(today))).sort_values(ascending=False).index

        # --- Step 2: apply industry cap to the candidate list ---
        # Count current industry exposure
        cur_ind_count = {}
        for s in current_stock_list:
            ind = self._industry_code(s)
            cur_ind_count[ind] = cur_ind_count.get(ind, 0) + 1

        capped = []
        for stock in comb:
            ind = self._industry_code(stock)
            cnt = sum(1 for s in capped if self._industry_code(s) == ind)
            if cnt < self.max_per_industry:
                capped.append(stock)
        comb = pd.Index(capped)

        # --- Step 3: normal sell/buy with the capped candidate list ---
        sell = last[last.isin(get_last_n(comb, self.n_drop))]
        buy = today[: len(sell) + self.topk - len(last)]

        sell_order_list = []
        buy_order_list = []
        cash = current_temp.get_cash()

        for code in current_stock_list:
            if not self.trade_exchange.is_stock_tradable(
                stock_id=code, start_time=trade_start_time, end_time=trade_end_time, direction=OrderDir.SELL
            ):
                continue
            if code in sell:
                time_per_step = self.trade_calendar.get_freq()
                if current_temp.get_stock_count(code, bar=time_per_step) < self.hold_thresh:
                    continue
                sell_amount = current_temp.get_stock_amount(code=code)
                sell_order = Order(
                    stock_id=code, amount=sell_amount,
                    start_time=trade_start_time, end_time=trade_end_time, direction=Order.SELL,
                )
                if self.trade_exchange.check_order(sell_order):
                    sell_order_list.append(sell_order)
                    trade_val, trade_cost, _ = self.trade_exchange.deal_order(sell_order, position=current_temp)
                    cash += trade_val - trade_cost

        value = cash * self.risk_degree / len(buy) if len(buy) > 0 else 0
        for code in buy:
            if not self.trade_exchange.is_stock_tradable(
                stock_id=code, start_time=trade_start_time, end_time=trade_end_time, direction=OrderDir.BUY
            ):
                continue
            buy_price = self.trade_exchange.get_deal_price(
                stock_id=code, start_time=trade_start_time, end_time=trade_end_time, direction=OrderDir.BUY
            )
            buy_amount = value / buy_price
            factor = self.trade_exchange.get_factor(stock_id=code, start_time=trade_start_time, end_time=trade_end_time)
            buy_amount = self.trade_exchange.round_amount_by_trade_unit(buy_amount, factor)
            buy_order = Order(
                stock_id=code, amount=buy_amount,
                start_time=trade_start_time, end_time=trade_end_time, direction=Order.BUY,
            )
            buy_order_list.append(buy_order)
        return TradeDecisionWO(sell_order_list + buy_order_list, self)


