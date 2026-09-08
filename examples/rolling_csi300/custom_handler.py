import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from qlib.data.dataset.processor import Processor
from qlib.contrib.data.handler import Alpha158
from qlib.contrib.data.loader import Alpha158DL
from qlib.contrib.strategy import TopkDropoutStrategy
from qlib.backtest.decision import Order, OrderDir, TradeDecisionWO
from qlib.backtest.position import Position
from qlib.data import D
import copy


class SortedPosition(Position):
    """Deterministic-Position subclass of qlib's `Position`.

    qlib's base `Position.get_stock_list()` (position.py) iterates over a python set,
    whose order depends on PYTHONHASHSEED -> backtest outcomes vary run-to-run for
    n_drop=1 (see AGENTS.md 3b). We sort the list to make results byte-identical.
    Selecting this class via `pos_type` in the backtest config lets us avoid patching
    qlib source.
    """

    def get_stock_list(self):
        return sorted(super().get_stock_list())


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


class ICTimingStrategy(TopkDropoutStrategy):
    """TopkDropoutStrategy with dynamic position sizing based on trailing RankIC.

    Uses the realized RankIC over the trailing ``ic_window`` trading days to
    detect factor decay / crowded-alpha periods.  When the trailing IC is weak
    (below ``ic_low``), the strategy reduces ``risk_degree`` (de-risk).  When IC
    recovers above ``ic_high``, it returns to the full ``risk_degree``.

    Parameters
    ----------
    ic_window : int
        Trailing window (trading days) used to compute realized RankIC.
    ic_low : float
        RankIC below which the strategy de-risks (lower risk_degree).
    ic_high : float
        RankIC above which the strategy restores full risk_degree.
    low_risk : float
        risk_degree used when de-risking.
    """

    def __init__(
        self,
        *,
        ic_window=60,
        ic_low=0.03,
        ic_high=0.05,
        low_risk=0.4,
        **kwargs,
    ):
        self.risk_degree = kwargs.pop("risk_degree", 0.95)
        super().__init__(**kwargs)
        self.ic_window = ic_window
        self.ic_low = ic_low
        self.ic_high = ic_high
        self.low_risk = low_risk
        self._ic_series = None

    def _precompute_ic(self):
        # Pred score (already available through self.signal)
        cal = D.calendar()
        start = str(cal[0].date())
        end = str(cal[-1].date())
        close = D.features(D.instruments("csi300"), ["$close"], start_time=start, end_time=end)
        # FORWARD 20-day return matching label Ref($close,-21)/Ref($close,-1)-1:
        #   fwd[t] = close[t+20] / close[t] - 1
        # pct_change(-20) = close[t]/close[t+20] - 1, so forward = -pct/(-pct+1)... derive:
        #   close[t+20]/close[t]-1 = -(close[t]/close[t+20]-1) / (close[t]/close[t+20])
        #   = -pct_change(-20) / (1 + pct_change(-20))
        pct_back = close["$close"].groupby(level="instrument").pct_change(-20)
        fwd = -pct_back / (1 + pct_back)
        df = close["$close"].to_frame("close")
        df["fwd"] = fwd
        df = df.dropna(subset=["fwd"])

        pred = self.signal.get_signal(start_time=pd.Timestamp(start), end_time=pd.Timestamp(end))
        if isinstance(pred, pd.DataFrame):
            pred = pred.iloc[:, 0]
        p = pred.to_frame("score")
        m = p.join(df["fwd"], how="inner").dropna()

        def _daily_ic(g):
            if len(g) < 5:
                return np.nan
            return g["score"].corr(g["fwd"], method="spearman")

        daily = m.groupby(level="datetime").apply(_daily_ic).dropna()
        self._ic_series = daily.sort_index()

    def _current_ic(self, cur_ts):
        ic = self._ic_series
        # CAUSALITY: the IC computed at date t uses close[t+20] (forward 20d).
        # At decision time cur_ts, the most recent usable IC is the one whose
        # forward window is fully realized: it must have been computed at least
        # 20 trading days before cur_ts.  Shift the series back by 20 trading
        # days so that at decision time cur_ts we only see fully-realized ICs.
        usable_ic = ic[ic.index <= cur_ts].shift(20)
        usable_ic = usable_ic.dropna()
        if len(usable_ic) < 20:
            return 1.0
        return usable_ic.iloc[-self.ic_window:].mean()

    def generate_trade_decision(self, execute_result=None):
        if self._ic_series is None:
            self._precompute_ic()

        trade_step = self.trade_calendar.get_trade_step()
        _, trade_end_time = self.trade_calendar.get_step_time(trade_step)
        cur_ts = pd.Timestamp(trade_end_time)

        trailing_ic = self._current_ic(cur_ts)
        if trailing_ic < self.ic_low:
            self.risk_degree = self.low_risk
        elif trailing_ic > self.ic_high:
            self.risk_degree = 0.95
        # else: keep previous risk_degree (hysteresis)
        return super().generate_trade_decision(execute_result)


GLOBAL_TECH_INDUSTRIES = {"C39", "I63", "I64", "I65", "R86", "R87"}


class _AdjustSignal:
    """Wrap a raw signal and adjust the score of per-instrument codes by a weight.

    ``weights`` maps a lowercase instrument code to an adjustment that is added to the
    score as ``weight * <daily cross-sectional std of score>``.  Positive weights boost
    selection (e.g. tilt toward tech), negative weights de-weight selection (e.g. avoid
    baijiu/white-liquor).  Storing weights in units of one daily cross-sectional std
    keeps the adjustment effective across regimes where the raw score scale changes
    drastically (e.g. the post-2025-09 clustered-score regime).

    The wrapper preserves the raw DataFrame/Series shape so downstream code
    (TopkDropout selection, ICTiming) handles it identically.
    """

    def __init__(self, base_signal, weight_map, scale_name=None):
        self._base = base_signal
        self._w = weight_map  # Dict[str, float], keys lowercase instrument
        self._scale_name = scale_name

    def get_signal(self, start_time, end_time):
        raw = self._base.get_signal(start_time=start_time, end_time=end_time)
        if raw is None:
            return raw
        if isinstance(raw, pd.DataFrame):
            score = raw.iloc[:, 0]
        else:
            score = raw
        # pairing factor per instrument (weight in units of daily cross-sectional std)
        factor = score.index.map(lambda c: self._w.get(str(c).lower(), 0.0))
        inst_level = score.index.nlevels - 1 if score.index.nlevels else 0
        if score.index.nlevels >= 2:
            # multi-day: daily cross-sectional std by day (level 0 = datetime)
            grp = score.groupby(level=0).transform("std")
        else:
            # single-day snapshot: whole cross-section
            grp = pd.Series(score.std(), index=score.index)
        adj = score + factor * grp
        if isinstance(raw, pd.DataFrame):
            out = raw.copy()
            out.iloc[:, 0] = adj.values
        else:
            out = adj
        return out


class _TiltedSignal(_AdjustSignal):
    """Back-compat wrapper; single tech-code set + single tilt (see _AdjustSignal)."""

    def __init__(self, base_signal, tech_codes, tilt):
        super().__init__(base_signal, {c: float(tilt) for c in tech_codes})


class TechTiltICTimingStrategy(ICTimingStrategy):
    """ICTimingStrategy (IC-based de-risking) + tech-industry tilt in stock selection.

    De-risking uses the RAW (untilted) IC so the tilt never biases the timing signal.
    Only the stock selection ranking is tilted toward tech names.
    """

    def __init__(
        self,
        *,
        tilt=1.0,
        tech_industries=None,
        industry_map_path=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        base_signal = self.signal
        if tech_industries is None:
            tech_industries = GLOBAL_TECH_INDUSTRIES
        tech_prefixes = tuple(sorted(set(tech_industries)))
        if industry_map_path is None:
            industry_map_path = Path(__file__).parent / "industry_map.pkl"
        with open(industry_map_path, "rb") as f:
            industry_map = pickle.load(f)
        self._tech_codes = {
            code.lower() for code, ind in industry_map.items() if str(ind).startswith(tech_prefixes)
        }
        self._raw_signal = base_signal
        self._tilted_signal = _TiltedSignal(base_signal, self._tech_codes, float(tilt))
        # Route stock-selection reads through the tilted signal.  We keep the raw
        # signal reference for IC computation (see _precompute_ic override).
        self._base_meta = type(self._tilted_signal)
        self.signal = self._tilted_signal

    def _precompute_ic(self):
        # Compute IC from the UNTILTED signal so tilt does not pollute the timing.
        saved = self.signal
        self.signal = self._raw_signal
        try:
            super()._precompute_ic()
        finally:
            self.signal = saved


class IndustryAdjustStrategy(ICTimingStrategy):
    """ICTimingStrategy + arbitrary per-industry score adjustment in stock selection.

    Unlike TechTiltICTimingStrategy (single tilt for one code set), this builds a
    code->weight map from a list of (industry-prefix or explicit code, weight) rules:

      - weight > 0 : boost selection (score += weight * daily_std)
      - weight < 0 : de-weight selection (score -= |weight| * daily_std)

    De-risking still uses the RAW (unadjusted) IC so the adjustment never biases the
    timing signal.  ``baijiu`` de-weight is the motivating use case: baijiu stocks sit
    inside C15 (酒、饮料和精制茶) together with beer/soft-drink/tea, so they need an
    explicit whitelist (see ``BAIJIU_CODES``) rather than an industry prefix.

    Parameters
    ----------
    weight_rules : list of tuples
        Each item is ``(key, weight)`` where ``key`` is either an industry prefix
        (matched against the industry_map value, e.g. ``"C39"``) or an exact lowercase
        instrument code (e.g. ``"sh600519"``).  ``weight`` in units of daily std.
    baijiu_codes : iterable of str
        Explicit whitelist of baijiu instrument codes (lowercase).  Convenience alias
        that appends each code with its weight to the rules.  If given, ``baijiu``
        de-weighting is applied regardless of C15 composition.
    baijiu_weight : float
        Weight applied to each ``baijiu_codes`` entry (default -0.5 => de-weight).
    """

    def __init__(
        self,
        *,
        weight_rules=None,
        baijiu_codes=None,
        baijiu_weight=-0.5,
        industry_map_path=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        base_signal = self.signal
        if baijiu_codes is None:
            baijiu_codes = BAIJIU_CODES  # default: de-weight the whole baijiu whitelist
        if industry_map_path is None:
            industry_map_path = Path(__file__).parent / "industry_map.pkl"
        with open(industry_map_path, "rb") as f:
            industry_map = pickle.load(f)
        weight_map = {}
        for key, weight in (weight_rules or []):
            if key.lower().startswith(("sh", "sz")):
                weight_map[key.lower()] = float(weight)
            else:
                prefix = str(key)
                for code, ind in industry_map.items():
                    if str(ind).startswith(prefix):
                        weight_map[code.lower()] = float(weight)
        if baijiu_codes is not None:
            for code in baijiu_codes:
                weight_map[str(code).lower()] = float(baijiu_weight)
        self._raw_signal = base_signal
        self._adjust_signal = _AdjustSignal(base_signal, weight_map)
        self._base_meta = type(self._adjust_signal)
        self.signal = self._adjust_signal

    def _precompute_ic(self):
        saved = self.signal
        self.signal = self._raw_signal
        try:
            super()._precompute_ic()
        finally:
            self.signal = saved


# 白酒 (white-liquor) 证券代码白名单, 用于单独降权 (区别于 C15 中的啤酒/饮料/茶)
BAIJIU_CODES = [
    "sh600519",  # 贵州茅台
    "sz000858",  # 五粮液
    "sz000568",  # 泸州老窖
    "sz002304",  # 洋河股份
    "sh600809",  # 山西汾酒
    "sz000596",  # 古井贡酒
    "sh603369",  # 今世缘
    "sh600779",  # 水井坊
    "sh603589",  # 口子窖
    "sz000799",  # 酒鬼酒
    "sh600702",  # 舍得酒业
    "sh600559",  # 老白干酒
    "sz000860",  # 顺鑫农业
    "sh600197",  # 伊力特
    "sh603198",  # 迎驾贡酒
    "sz000995",  # 皇台酒业
    "sh600616",  # 金枫酒业
    "sz002646",  # 天佑德酒
    "sh600238",  # 海南椰岛
    "sh600199",  # 金种子酒
    "sz603919",  # 金徽酒
]


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


class Alpha158Earnings(Alpha158Industry):
    """Alpha158Industry + earnings-momentum / surprise factors.

    Adds cross-period-stable earnings factors (validated by IC analysis:
    positive IC in EVERY year 2020-2026, no style-beta like dvratio):
      NP_ACCEL    = netprofit_yoy - lag1(netprofit_yoy)   (earnings acceleration)
      QPROF_ACCEL = q_profit_yoy - lag1(q_profit_yoy)     (single-quarter profit momentum)

    Both are precomputed daily bins (ffill from announcement dates, no lookahead)
    -> fast rolling builds.  Rejected factors (roe_chg / margin_chg / eps_yoy had
    negative IC in 2022-2023, i.e. style-cyclical) are intentionally NOT included.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get_feature_config(self):
        fields, names = super().get_feature_config()

        earnings_fields = [
            ("$np_accel", "NP_ACCEL"),
            ("$qprof_accel", "QPROF_ACCEL"),
        ]
        for expr, name in earnings_fields:
            fields += [expr]
            names += [name]

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


class MinTradeValueStrategy(TopkDropoutStrategy):
    """TopkDropoutStrategy with a minimum trade value filter.

    Skips buy/sell orders whose notional value is below ``min_trade_value``,
    avoiding the situation where a tiny position pays a large share of the
    per-order minimum commission (``min_cost``).

    Parameters
    ----------
    min_trade_value : float
        Minimum notional trade value (in account currency, e.g. RMB). Orders
        with ``value < min_trade_value`` are not placed. If None, the filter
        is disabled (identical to plain TopkDropoutStrategy).
    """

    def __init__(self, *, min_trade_value: float = None, **kwargs):
        super().__init__(**kwargs)
        self.min_trade_value = min_trade_value

    def generate_trade_decision(self, execute_result=None):
        trade_step = self.trade_calendar.get_trade_step()
        trade_start_time, trade_end_time = self.trade_calendar.get_step_time(trade_step)
        pred_start_time, pred_end_time = self.trade_calendar.get_step_time(trade_step, shift=1)
        pred_score = self.signal.get_signal(start_time=pred_start_time, end_time=pred_end_time)
        if isinstance(pred_score, pd.DataFrame):
            pred_score = pred_score.iloc[:, 0]
        if pred_score is None:
            return TradeDecisionWO([], self)

        if self.only_tradable:
            def get_first_n(li, n, reverse=False):
                cur_n = 0
                res = []
                for si in reversed(li) if reverse else li:
                    if self.trade_exchange.is_stock_tradable(
                        stock_id=si, start_time=trade_start_time, end_time=trade_end_time
                    ):
                        res.append(si)
                        cur_n += 1
                        if cur_n >= n:
                            break
                return res[::-1] if reverse else res

            def get_last_n(li, n):
                return get_first_n(li, n, reverse=True)

            def filter_stock(li):
                return [
                    si
                    for si in li
                    if self.trade_exchange.is_stock_tradable(
                        stock_id=si, start_time=trade_start_time, end_time=trade_end_time
                    )
                ]
        else:
            def get_first_n(li, n):
                return list(li)[:n]

            def get_last_n(li, n):
                return list(li)[-n:]

            def filter_stock(li):
                return li

        current_temp = copy.deepcopy(self.trade_position)
        sell_order_list = []
        buy_order_list = []
        cash = current_temp.get_cash()
        current_stock_list = current_temp.get_stock_list()
        last = pred_score.reindex(current_stock_list).sort_values(ascending=False).index

        if self.method_buy == "top":
            today = get_first_n(
                pred_score[~pred_score.index.isin(last)].sort_values(ascending=False).index,
                self.n_drop + self.topk - len(last),
            )
        elif self.method_buy == "random":
            topk_candi = get_first_n(pred_score.sort_values(ascending=False).index, self.topk)
            candi = list(filter(lambda x: x not in last, topk_candi))
            n = self.n_drop + self.topk - len(last)
            try:
                today = np.random.choice(candi, n, replace=False)
            except ValueError:
                today = candi
        else:
            raise NotImplementedError(f"This type of input is not supported")

        comb = pred_score.reindex(last.union(pd.Index(today))).sort_values(ascending=False).index

        if self.method_sell == "bottom":
            sell = last[last.isin(get_last_n(comb, self.n_drop))]
        elif self.method_sell == "random":
            candi = filter_stock(last)
            try:
                sell = pd.Index(np.random.choice(candi, self.n_drop, replace=False) if len(last) else [])
            except ValueError:
                sell = candi
        else:
            raise NotImplementedError(f"This type of input is not supported")

        buy = today[: len(sell) + self.topk - len(last)]

        # sell loop with min_trade_value filter
        for code in current_stock_list:
            if not self.trade_exchange.is_stock_tradable(
                stock_id=code,
                start_time=trade_start_time,
                end_time=trade_end_time,
                direction=None if self.forbid_all_trade_at_limit else OrderDir.SELL,
            ):
                continue
            if code in sell:
                time_per_step = self.trade_calendar.get_freq()
                if current_temp.get_stock_count(code, bar=time_per_step) < self.hold_thresh:
                    continue
                sell_amount = current_temp.get_stock_amount(code=code)
                sell_price = self.trade_exchange.get_deal_price(
                    stock_id=code, start_time=trade_start_time, end_time=trade_end_time, direction=OrderDir.SELL
                )
                sell_value = sell_amount * sell_price
                if self.min_trade_value is not None and sell_value < self.min_trade_value:
                    continue
                sell_order = Order(
                    stock_id=code,
                    amount=sell_amount,
                    start_time=trade_start_time,
                    end_time=trade_end_time,
                    direction=Order.SELL,
                )
                if self.trade_exchange.check_order(sell_order):
                    sell_order_list.append(sell_order)
                    trade_val, trade_cost, _ = self.trade_exchange.deal_order(sell_order, position=current_temp)
                    cash += trade_val - trade_cost

        value = cash * self.risk_degree / len(buy) if len(buy) > 0 else 0

        # buy loop with min_trade_value filter
        for code in buy:
            if not self.trade_exchange.is_stock_tradable(
                stock_id=code,
                start_time=trade_start_time,
                end_time=trade_end_time,
                direction=None if self.forbid_all_trade_at_limit else OrderDir.BUY,
            ):
                continue
            buy_price = self.trade_exchange.get_deal_price(
                stock_id=code, start_time=trade_start_time, end_time=trade_end_time, direction=OrderDir.BUY
            )
            buy_amount = value / buy_price
            factor = self.trade_exchange.get_factor(stock_id=code, start_time=trade_start_time, end_time=trade_end_time)
            buy_amount = self.trade_exchange.round_amount_by_trade_unit(buy_amount, factor)
            if self.min_trade_value is not None and buy_amount * buy_price < self.min_trade_value:
                continue
            buy_order = Order(
                stock_id=code,
                amount=buy_amount,
                start_time=trade_start_time,
                end_time=trade_end_time,
                direction=Order.BUY,
            )
            buy_order_list.append(buy_order)
        return TradeDecisionWO(sell_order_list + buy_order_list, self)


