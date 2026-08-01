# 分红融资比因子研究记录（DV_RATIO）

> 结论：**生产不采用**。该因子在 2022–2024 红利风格占优期大幅增强，但 2025–2026 出现风格反转拖累组合，整体呈强风格 beta 而非稳定 alpha。

---

## 1. 因子定义

**分红融资比 = 累计现金分红 / 累计股权融资**

度量一家公司对股东的现金回报能力：分红代表股东收到的现金回报，融资（IPO / 增发 / 配股吸收投资）代表股东投入的资本。比率越高，说明公司越倾向于用真金白银回报股东而非不断向股东要钱。

- 现金分红：`c_pay_dist_dpcp_int_exp`（现金流量表"分配股利、利润或偿付利息支付的现金"）
- 股权融资：`c_recp_cap_contrib`（现金流量表"吸收投资收到的现金"）
- 滚动口径：近 10 年累计

两个变体：
| 字段 | 定义 |
|---|---|
| `dv_ratio_q` | 分红 / 股权融资 |
| `dv_ratio_all_q` | 分红 / (股权融资 + 借款) |

## 2. 数据获取

数据源：tushare 代理（`http://jiaoch.site`），接口 `cashflow`（现金流量表）。

```
pro.cashflow(ts_code=..., start_date=..., end_date=...,
             fields="ts_code,ann_date,end_date,c_pay_dist_dpcp_int_exp,c_recp_cap_contrib,c_recp_borrow")
```

- 覆盖率：939 只 csi300 中 917 只 (97.7%) 有现金流数据
- 茅台等无股权融资的股票：该字段=0，按"融资=0 时不定义"处理，保留 NaN
- 银行类可用：`c_pay_dist_dpcp_int_exp` 对银行也有效（不能用的字段是 `incl_dvd_profit_paid_sc_ms`，那是少数股东口径）

## 3. 无未来函数处理

PIT（Point-in-Time）落库，`ann_date` 对齐：
- `date` = 报告公告日 `ann_date`（真实披露时点）
- `period` = 报告期 `end_date`（YYYYQ）
- 滚动 10 年累计只使用"该 ann_date 之前已披露"的报告

同时提供日频版本（`dump_dv_ratio_daily.py`）：把 PIT 按"公告后第一个交易日"映射到日频 bin 并前向填充，与 PIT 逐日完全一致（243/243 天偏差为 0），构建速度提升约 5 倍。

## 4. 接入方式

`custom_handler.py` 新增两个类（保留技术面 baseline 不动）：
- `DvRatioProcessor`：NaN→0 填充、横截面 1%/99% winsorize、CSR 横截面 rank
- `Alpha158DvRatio`：Alpha158Industry + 4 个分红融资比特征（`DV_RATIO`、`DV_RATIO_ALL` + 各自 YoY）

配置：`rolling_config_dvratio.yaml`，运行 `run_rolling.py --conf=rolling_config_dvratio.yaml`

## 5. 特征重要性验证

训练区间 2015–2018，LightGBM feature importance：

- 8 个 dv_ratio 特征合计重要性 **879/9000 ≈ 9.8%**（输入仅占 8/364 ≈ 2.2%，即 4.5 倍超额贡献）
- `DV_RATIO_ALL` 排名 **#6**，`DV_RATIO_ALL_YOY` #10，`DV_RATIO` #12
- 原始值比 CSR rank 版更重要，说明存在单调非线性关系（低分红融资比 → 后续超额）

## 6. 回测对比（2020-01 ~ 2026-07，csi300，27 轮季度滚动）

| 指标 | 技术面 baseline | 技术面 + 分红融资比 |
|---|---|---|
| 年化超额（含费） | 15.19% | **16.06%** (+0.87pp) |
| IR（含费） | 1.209 | 1.330 |
| 最大超额回撤 | -17.59% | **-20.71%** |
| IC | 0.0698 | 0.0707 |
| Rank IC | 0.0770 | 0.0771 |
| 波动率 | 0.0081 | 0.0078 |

整体小增益：年化 +0.87pp、IR +0.12，但最大回撤从 -17.6% 扩大到 -20.7%。

## 7. 失效分析：强风格 beta 而非稳定 alpha

按年度拆分超额收益（dvratio − baseline）：

| 年份 | baseline | dvratio | diff |
|---|---|---|---|
| 2020 | +24.7% | +19.0% | −5.7pp |
| 2021 | +22.1% | +15.2% | −7.0pp |
| 2022 | +11.1% | +18.7% | **+7.6pp** |
| 2023 | +15.1% | +23.9% | **+8.8pp** |
| 2024 | +31.6% | +53.7% | **+22.1pp** |
| 2025 | +7.2% | −0.2% | **−7.4pp** |
| 2026(1–7) | +5.1% | −0.5% | −5.6pp |

**结论**：
- 2022–2024 dvratio 累计多赚 +38pp —— 这正是高股息/红利风格大幅占优的时期
- 2025–2026 反转，累计少赚 −13pp —— 红利风格退潮，高分红因子拖累组合
- 最深超额回撤（−18.6%）发生在 2025-09/10，恰为 2024 年高位回落、红利风格转弱之时

因子与红利风格高度相关：**红利占优时大幅增强，红利退潮时拖累**。整体拉平后的 +0.87pp 年化增益，本质是风格 beta 的贡献，不具备跨周期稳定性。

## 8. 生产决策

**不采用。** 理由：
1. 增益来源是红利风格 beta，不是稳定 alpha，2025–2026 已实证失效
2. 最大回撤扩大 3.1pp（-17.6% → -20.7%），风险收益不划算
3. 需要依赖"红利风格是否占优"的择时判断，增加复杂度

保留技术面 baseline（`Alpha158Industry`）作为生产配置。

## 9. 相关文件

| 文件 | 说明 |
|---|---|
| `dump_dv_ratio.py` | PIT 落库（现金流滚动累计，ann_date 对齐） |
| `dump_dv_ratio_daily.py` | PIT → 日频 bin（公告后首交易日对齐 + ffill，提速 5x） |
| `custom_handler.py` | `Alpha158DvRatio` + `DvRatioProcessor` |
| `rolling_config_dvratio.yaml` | dvratio 滚动配置 |
| `dvratio_feature_importance.csv` | 特征重要性结果 |
| `positions_rolling_csi300_lgbm_dvratio.csv` | dvratio 回测持仓 |
