# qlib CSI300 滚动策略研究索引

本文件汇总 `examples/rolling_csi300/` 的策略研究结论、配置、运行方式与关键操作要点。

## 策略概况

- **策略**: rolling 滚动重训 + LightGBM(Alpha158Industry) + TopkDropoutStrategy, CSI300 内选股
- **市场**: CSI300, 回测区间 2020-01~2026-07
- **基准配置**: `rolling_config.yaml` (已固化 **n_drop=5**, topk=30)
- **当前 baseline**: `rolling_csi300_lgbm_ndrop5` (mlruns 实验)
- **关键指标** (n_drop=5, 2020-2026): 年化超额 ~23.9%, IR 1.94, 最大回撤 -15%, 期末净值 4.40

## 核心研究结论

### 1. 分红融资比因子 (dvratio) → 已否决
- 详见 `dvratio_factor.md`
- 结论: 因子是"红利风格 beta"非 alpha (2022-2024 强 +38pp, 2025-2026 -13pp), 生产不采用

### 2. 单股权重上限 (8% cap) → 已回退
- SingleWeightCapStrategy 已从 custom_handler.py 移除
- 结论: 单股上限严重伤绩效 (IR 1.21→0.72, 回撤 -17.6%→-25.3%)。权重漂移是动量增强来源, 非 bug

### 3. n_drop 换手率敏感性
- n_drop=5 (换手76/yr) 是甜点, 收益倒 U 型 (见 style_profile.md)
- 结论: 机构暴力靠新信号源而非提高换手频率

### 4. 因子衰减分析
- 无结构性衰减 (2020-2022 IC 0.077 vs 2023-2026 IC 0.077)
- IC 呈周期性波动 (低谷 2021H2/2025H2, 高效 2020H2/2022Q2/2024H1), 低谷后可恢复

### 5. 风格画像
- 详见 `style_profile.md`
- 大盘蓝筹 + 红利价值打底 + 量价轮动增强
- 防御/红利权重逐年抬升 (2020 25% → 2024 53%), 大小盘 beta≈0 (纯 alpha)
- 超额与 515180 红利ETF 相关性 0.001 (不是红利 beta)

### 6. alpha 基本原理 (理论)
- 详见 `style_profile.md`
- alpha = 市场定价错误 (行为偏差+信息摩擦), 非风险补偿
- A股散户占比高 → 定价错误多 → alpha 比美股厚 (美股 Alpha158 IC 约 1/3-1/2)
- 错误越少市场越有效 alpha 越薄 → 因子周期衰减

### 7. 回测→实盘差距 (100w 资金)
- 详见 `style_profile.md`
- 结论: 100w 资金下回测≈实盘, 甚至实盘可能略好 (低频+大盘股+小资金=无冲击成本)
- 保守估计: 回测 23.9% → 实盘预期 20-25%
- 机构说"qlib 是玩具"主要针对百亿资金场景, 小资金研究 qlib 相当可靠

## 运行方式

```bash
cd examples/rolling_csi300

# 完整训练 + 回测 (约1小时, 依赖 23GB swap)
python3 run_rolling.py

# 跳过训练, 复用已有 rolling_models 实验, 只跑 ensemble + 回测
python3 run_rolling.py --skip-train --conf rolling_config_ndrop5.yaml --exp-name rolling_csi300_lgbm_ndrop5
```

**重要**: 必须在 `examples/rolling_csi300/` 目录下运行! qlib 默认 mlflow tracking_uri 相对 cwd (否则 RecorderCollector 找不到 pred, KeyError: 'pred')

## 关键文件

| 文件 | 说明 |
|---|---|
| `rolling_config.yaml` | 默认配置 (n_drop=5, topk=30) |
| `custom_handler.py` | 策略/处理器: VolatilityTimingStrategy, IndustryProcessor, Alpha158Industry, DvRatioProcessor, Alpha158DvRatio, IndustryCappedStrategy |
| `run_rolling.py` | 滚动训练+回测入口, 注册所有 custom handler |
| `dump_dv_ratio.py` | dvratio PIT 落库脚本 |
| `dump_dv_ratio_daily.py` | PIT→日频 bin 提速脚本 |
| `dvratio_factor.md` | dvratio 因子研究记录 (否决) |
| `style_profile.md` | 风格画像 + alpha 原理 + 实盘差距评估 |
| `mlruns/` | mlflow 实验存储 |
| `industry_map.pkl` | 行业映射 (股票->行业) |

## mlruns 关键实验

- `rolling_csi300_lgbm` — 技术面 baseline (n_drop=1)
- `rolling_csi300_lgbm_ndrop5` — **当前新 baseline** (n_drop=5)
- `rolling_models_*` — 各次滚动训练的 27 期模型

## 数据/环境备注

- qlib 数据: ~/.qlib/qlib_data/cn_data
- 机器: 15GB RAM + 23GB swap, 20 线程, 训练峰值依赖 swap 兜底
- PIT 记录格式: 20 字节 (date+period+value+_next), financial/<code>/<field>_q
- 日频 bin 与 PIT 逐日一致 (243/243 天验证)
- tushare 代理: API_URL 默认 http://jiaoch.site, token 从环境变量 `TUSHARE_TOKEN` 读取 (已从代码中移除, 勿硬编码)
- 无中文字体 (matplotlib 需用英文标签)

## 数据管道: bin 格式与 baostock 坐标系 (必读)

### bin 文件格式
- 每字段一个二进制文件: `features/<code>/close.day.bin` 等
- 结构: 第 0 位 float32 = 该股起始日历索引, 之后每交易日一个 float32, 与 `calendars/day.txt` 对齐
- 读某天价格: `bin[日历索引 - start_index + 1]`

### 价格字段口径 (qlib 存的是前复权价)
| 字段 | 含义 | 招行 2026-07-31 例 |
|---|---|---|
| `close` | 前复权, 以**上市首日=1** 归一 | 24.75 |
| `adjclose` | 后复权价 (累积分红调整) | 263.88 |
| `factor` | 复权系数, **真实价 = close/factor** | 0.608 |

- 恒等式: `close = adjclose / first_adjclose` (first_adjclose = 该股 bin 首日 adjclose, 恒定)
- 同一天真实价: qlib `close/factor` ≈ baostock 前复权价 (相差一个恒定比例)

### baostock 增量更新的坐标系转换
- baostock (adjustflag=2) 前复权到**最新交易日**; qlib close 前复权到**上市首日** → 两套基准差一个**恒定比例**
- `update_baostock.py` 的 `map_and_scale` 用重叠期行算经验比例再换算:
  - `adj_ratio = bin的last_adjclose / baostock重叠日前复权close`
  - `close = baostock价 × adj_ratio / first_adjclose`
  - **open/high/low 必须与 close 同口径** (同乘 `adj_ratio/first_adjclose`), 否则 O/C 偏离 ~first_adjclose 倍
- 更新本质: 先把 baostock 数据换算进 qlib 坐标系, 再 append 进 bin (UPDATE 模式只 append 新日期, 不覆盖旧数据)
- `build_existing_last_values` 用每只股票 bin 的**实际最后有效行** (不是 calendar[-1]); first_adjclose 遇 NaN 取首个有效值

## 数据 bugs 与修复 (2026-08-01)

### Bug 1: open/high/low 复权口径错误 (2026-04-20 起)
- **症状**: 4-20 后 open/high/low 比 close 大 ~10 倍 (O/C≈10.5~390), 波及 5186/6082 只股票
- **根因**: `update_baostock.py` 的 `map_and_scale` 中 open/high/low 只乘 `adj_ratio` 未除 `first_adjclose` (close 是 `adjclose/first_adjclose`), 导致口径不一致 (open 为后复权, close 为前复权)
- **修复**: 统一乘 `adj_ratio/first_adjclose`; 已修复 5186 只股票 bin (含每只最后一条的遗漏)
- **影响**: ≤2026-04-17 数据不受影响; 修复后 4-20 起 O/C≈1

### Bug 2: OHLCV 数据断档 (6-12 后)
- **症状**: 行情 bin 停 6-12, 但日历/基本面已到 7-31
- **根因**: 7-31 晚 update_baostock.py 运行中断 (仅 ~145/6082 只成功 append); 且脚本用 `calendar[-1]` (7-31) 判断"已最新"导致后续早退不再补
- **修复**: `backfill_missing.py` 断点续跑补齐 (分批 250-700 只/次, 因 baostock 长连接会被杀); 4604 只到 7-31
- **附带修复**: `build_existing_last_values` 用 bin 实际最后行而非 calendar[-1]; first_adjclose 遇 NaN 取首个有效值

### 运行注意
- **baostock 长任务易被杀**: 单进程 >1 分钟会被环境终止, 无 traceback. 大批量下载必须分批 (每批 timeout 窗口内完成) 或用 BACKFILL_START/END chunk
- **验证标准**: open/close 比值≈1; 除权日收益率对照; 与 baostock 前复权价连续
- **已知无害异常**: 39 只 ST 股/新股上市首日/北交所 O/C 偏离 (历史遗留, 与 CSI300 无关)

## 当前待办/可继续方向

- (可选) IC 低谷期防御 (动态降仓) — 理论可行, 未验证
- (可选) 更高频/另类信号源 (分钟级) — 机构暴力来源, 需新数据
- (可选) 减少训练窗口减轻 swap 依赖
