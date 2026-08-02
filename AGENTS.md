# qlib CSI300 滚动策略研究索引

本文件汇总 `examples/rolling_csi300/` 的策略研究结论、配置、运行方式与关键操作要点。

## 策略概况

- **策略**: rolling 滚动重训 + LightGBM(Alpha158Industry) + TopkDropoutStrategy, CSI300 内选股
- **市场**: CSI300, 回测区间 2020-01~2026-07
- **基准配置**: `rolling_config.yaml` (已固化 **n_drop=1**, topk=30)
- **当前 baseline**: `rolling_csi300_lgbm_ndrop1` (mlruns 实验)
- **关键指标** (n_drop=1, 2020-2026, 净成本): 年化超额 ~17.1%, IR 1.29, 最大回撤 -19.8%, 换手 16/yr

## 核心研究结论

### 1. 分红融资比因子 (dvratio) → 已否决
- 详见 `dvratio_factor.md`
- 结论: 因子是"红利风格 beta"非 alpha (2022-2024 强 +38pp, 2025-2026 -13pp), 生产不采用

### 2. 单股权重上限 (8% cap) → 已回退
- SingleWeightCapStrategy 已从 custom_handler.py 移除
- 结论: 单股上限严重伤绩效 (IR 1.21→0.72, 回撤 -17.6%→-25.3%)。权重漂移是动量增强来源, 非 bug

### 3. n_drop 换手率敏感性 (净成本口径, 已修正)
- **毛收益**: n_drop=5 (换手80/yr) 仍最优 (20.5%), 毛口径倒 U 型成立
- **净收益 (关键)**: n_drop=1 最优 (17.1%, 净IR 1.29, 回撤 -19.8%); n_drop=5 净仅 12.9%
  - n_drop=5 增量毛 alpha (+2.3pp) 远抵不上成本拖累 (+6.1pp), 高换手被成本吃掉
  - 敏感性表 (2020-2026, 净成本): n_drop=1: 17.1% | 2: 14.7% | 3: 14.1% | 5: 12.9%
- 结论: 机构暴力靠新信号源而非提高换手频率
- **注意**: 早期"n_drop=5 甜点 23.9%/IR 1.94"基于不含成本口径, 且受回测非确定性 bug 污染, 已作废

### 3b. 回测非确定性 bug (已修复, 重要!)
- **症状**: 同一命令多次回测结果不同 (ndrop=1 时 0.183~0.207 波动 ±2.4pp), 持仓在 2022-07-05 等日期分歧
- **根因**: `qlib/backtest/position.py` `get_stock_list` 用 `list(set(...))` 迭代, set 顺序依赖 PYTHONHASHSEED → 每次进程结果不同
- **影响**: 仅 n_drop=1 敏感 (每次只换1只, 边界选择对顺序敏感); n_drop>=2 稳定 (已验证 nd2/3/5 复现一致)
- **修复**: `get_stock_list` 返回前 `stock_list.sort()`, 已 patch 到 qlib 源码 (~/qlib/qlib/backtest/position.py:424)
- **验证**: patch 后 ndrop=1 连跑两次完全一致; ndrop=2/3/5 结果与 patch 前一致 (无偏差)
- **教训**: 任何回测对比前必须确认确定性; 早期 n_drop 敏感性结论受此污染

### 4. 因子衰减分析
- 无结构性衰减 (2020-2022 IC 0.077 vs 2023-2026 IC 0.077)
- IC 呈周期性波动 (低谷 2021H2/2025H2, 高效 2020H2/2022Q2/2024H1), 低谷后可恢复

### 4b. 尾部爬升机制 (仓位动力学, 量化验证)
- **现象**: 23/339 只持仓股出现"尾部爬升"——进入持仓时 rank≥24 (权重~1-2%), 随后 rank 逐步升到前 15 甚至前 5, 权重翻 3-8 倍
- **代表**: SZ300450 沃森 (rank 27→3, 权重1.7%→6.6%), SH600066 宇通 (rank 15→1, 5.1%→12%), SH600845 宝信 (rank 19→2, 2.2%→9.5%), SH603501 韦尔 (2026Q2 冲到 15.7%)
- **机制**: TopkDropout 新进股票从底部 rank 进入权重最小 → 若持续高 pred 排名会被反复加仓 → 有 alpha 的股票自动从尾部爬到头部
- **量化贡献** (2020-2026, 绝对贡献口径):
  - 平均贡献/只: 爬升股 +0.773pp vs 非爬升股 +0.604pp
  - 平均超额贡献/只: 爬升股 +0.430pp vs 非爬升股 +0.173pp
  - **贡献效率** (贡献/权重·天): 爬升股 23.2 vs 非爬升股 14.4 = 高 61%; 超额效率高 2.5x
  - 规模占比小: 23 只总 +17.8pp, 仅占全部贡献的 8.5% (爬升股是小仓位高质量)
- **爬升股内部也分化**: 赢家 SZ002920 +4.2pp / SZ300450 +4.0pp / SZ002812 +2.9pp; 输家 SZ300529 -2.4pp (爬升后 alpha 消退照样亏, 但单只损失小)
- **真正的亏损源是"高位重仓股阴跌"**: SZ002607 三只松鼠 -4.6pp (rank 14→11 却持仓268天), SZ603659 璞泰来 -2.5pp, SZ002129 -2.0pp —— 权重高+持仓久+一路走低
- **结论**: 尾部爬升 = 低风险试错 + 自动加仓赢家, 是策略核心机制; 这解释了 8% cap 为什么伤绩效 (打断自动加仓), 也印证"权重漂移是动量增强来源非 bug"

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
- 保守估计: 回测 17.1% (净) → 实盘预期 15-20%
- 机构说"qlib 是玩具"主要针对百亿资金场景, 小资金研究 qlib 相当可靠
- 小资金场景见 7c (40w 模拟, 无损)

### 7b. 小仓位交易费与 min_trade_value 过滤 (已验证, 不采用)
- **问题**: `min_cost=5` 使单笔成交额 <2k 的交易实际费率高达 ~9700%, 2-5k 档 0.32%
- **量化**: 单笔<2k 占 9% (105笔), 2-5k 占 5% (58笔), 其余 85% 正常费率 0.2%
- **MinTradeValueStrategy** (custom_handler.py, 可选): 过滤成交额低于阈值的下单
- **测试结果** (全量回测): mtv=5000 净 15.6% (IR 1.18, 回撤-23.6%), mtv=20000 净 3.6% (崩) vs 原始 17.1% (IR 1.29, 回撤-19.8%)
- **结论**: 过滤小单 = 掐断尾部爬升种子仓 (爬升股从 1-2% 权重起步), 成本节省抵不上 alpha 损失 → **生产不采用**, 保留类作可选工具
- **实盘启示**: 小单占比极小, 接受即可; 真正要控的是头部重仓股风险 (单票 10-12% 上限) 而非尾部小单

### 7c. 40w 小资金实盘模拟 (已验证, 无损)
- **测试**: 同一套模型/pred, 账户 100w vs 40w, 2020-2026 全量回测
- **结果**: 40w 期末 3.43x (年化 +21.5%, 超额 +19.4%) vs 100w 3.34x (+21.0%, +18.8%) → **40w 反而略优 +0.5pp**
- **原因**: ① min_cost 固定成本占比略升可忽略 (总成本 10.42% vs 10.25%); ② 100股取整在高价股产生天然过滤 (40w 下茅台等一手 17 万买不起被排除), 降低集中风险, 回撤 -31.3% vs -35.6%
- **边界**: 资金降到 ~10 万以下才会大量触发单笔<2k 的 min_cost 高费率; 40w 是安全区间, 实盘可直接跑
- **结论**: 小资金不影响策略有效性, 换手/决策与资金规模无关

### 7d. 资金规模敏感性 (50w~1000w, 已验证)
- **测试**: 同一套模型/pred, 账户 50w/100w/400w/1000w, 2020-2026 全量回测
- **结果表**:

| 账户 | 期末 | 倍数 | 年化 | 超额 | 回撤 | 换手 |
|---|---|---|---|---|---|---|
| **50w** | 182.5 万 | **3.65x** | **22.72%** | **20.54%** | **-31.6%** | 16.4 |
| 100w | 333.8 万 | 3.34x | 20.99% | 18.84% | -35.6% | 16.3 |
| 400w | 1356.9 万 | 3.39x | 21.30% | 19.15% | -34.6% | 16.8 |
| 1000w | 2909.3 万 | 2.91x | 18.39% | 16.29% | -36.5% | 16.4 |

- **结论 (反直觉)**: 资金越大收益反而越低 — 50w 最优 (3.65x, 年化22.7%, 回撤最小), 1000w 最差 (2.91x, 年化18.4%, 回撤最大)
- **机制**: 整手粒度效应 — 小资金下高价股 (茅台一手12.9万等) 买不起整手被自然过滤, 组合自动避开高价股降低集中风险; 1000w 什么都能买, 暴露在更多高波动标的, 回撤更大
- **成本不变**: 各档总成本均 ~10.3-10.6%, min_cost 影响在 50w 也可忽略 (单笔仍远高于 2k)
- **实盘启示**: 策略资金规模甜点在 **50-100w**; 1000w 收益反而下滑 ~4pp; 无需为"资金大"而加仓

## 运行方式

```bash
cd examples/rolling_csi300

# 完整训练 + 回测 (约1小时, 依赖 23GB swap)
python3 run_rolling.py

# 跳过训练, 复用已有 rolling_models 实验, 只跑 ensemble + 回测
python3 run_rolling.py --skip-train --conf rolling_config.yaml --exp-name rolling_csi300_lgbm_ndrop1
```

**重要**: 必须在 `examples/rolling_csi300/` 目录下运行! qlib 默认 mlflow tracking_uri 相对 cwd (否则 RecorderCollector 找不到 pred, KeyError: 'pred')

## 关键文件

| 文件 | 说明 |
|---|---|
| `rolling_config.yaml` | 默认生产配置 (n_drop=1, topk=30) |
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
- `rolling_csi300_lgbm_ndrop1` — **当前 baseline** (n_drop=1, 净成本最优)
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

### Bug 3: csi300.txt 陈旧导致回测实际只交易到 2026-04-17
- **症状**: baseline"回测到 2026-07"实际只有效交易到 04-17; pred.pkl 只到 04-17, 7-31 尾巴是静态持仓估值
- **根因**: csi300.txt 最后一段 2025-12-31→2026-04-18 (无 2026-06 段), 唯一更新机制是 CSI 官网 collector 且无自动化; 2026-04-20 起 `D.list_instruments('csi300')` 返回 0 只
- **修复**: 收口 2025-12-31 段 (end→2026-06-29) + 新建 2026-06-30→2026-07-31 段 (baostock 当前 300 成分); 见下方"csi300.txt 维护机制"

### 运行注意
- **baostock 长任务易被杀**: 单进程 >1 分钟会被环境终止, 无 traceback. 大批量下载必须分批 (每批 timeout 窗口内完成) 或用 BACKFILL_START/END chunk
- **验证标准**: open/close 比值≈1; 除权日收益率对照; 与 baostock 前复权价连续
- **已知无害异常**: 39 只 ST 股/新股上市首日/北交所 O/C 偏离 (历史遗留, 与 CSI300 无关)

## csi300.txt 维护机制 (2026-08-01 起)

### 半年调样规则
- CSI300 每年 6/12 月**最后交易日**调样; csi300.txt 按调样期存段, 每段 [start, end] 各 300 只
- 段边界惯例: 2024-06-28 / 2024-12-31 / 2025-06-30 / 2025-12-31 / **2026-06-30** ...

### sync_csi300_instruments(end_date) — update_baostock.py
- 每次运行 main 自动调用, 用 `bs.query_hs300_stocks()` 当前成分维护最后段落:
  - 若自当前段 start 起已跨过调样日: 收口旧段 (end = 调样日前一交易日) + 新建 [调样日, end_date] 段 (用 baostock 当前 300 成分)
  - 否则仅把当前段 end 延伸至 end_date
- **解决了 csi300.txt 陈旧 bug**: 旧 csi300.txt 只到 2026-04-18 (无 2026-06 段), 导致 qlib 在 2026-04-20 起返回 0 只 → 回测实际只交易到 04-17 (pred.pkl 只到 04-17, 7-31 尾巴是静态持仓估值非真实交易)
- 已修复: 收口 2025-12-31 段 (end→2026-06-29) + 新建 2026-06-30→2026-07-31 段; `D.list_instruments('csi300')` 2026-04~07 均返回 300 只

### 新股流程 (sync_new_stocks + download_new_stocks)
- `sync_new_stocks`: 纯发现 (query_all_stock - all.txt 差集), **不写 all.txt/csi300.txt**
- **新股进 csi300.txt 只按实际调样日**: 由 sync_csi300_instruments 用 baostock 官方成分维护, 非 IPO 即进 → 避免 look-ahead (长鑫 688825 7-27 IPO, 流通市值 2430 亿, 但当前不在 HS300 → 12 月调样才可能进)
- `download_new_stocks`: 下载新股 IPO→end 全历史, 直接换算进 qlib 坐标系 (新股无复权, `first_adjclose=首日close`, `factor=1/first_adjclose`, close 首日=1); 落 CSV 后由 DumpDataUpdate 的 **new-stock 分支** 全量建 bin + 自动注册 all.txt
- 已验证: 19 只新股 (长鑫等) 落 bin 且 O/C≈1; all.txt +19 条

## 当前待办/可继续方向

- (可选) IC 低谷期防御 (动态降仓) — 理论可行, 未验证
- (可选) 更高频/另类信号源 (分钟级) — 机构暴力来源, 需新数据
- (可选) 减少训练窗口减轻 swap 依赖
