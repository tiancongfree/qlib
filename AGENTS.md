# qlib CSI300 滚动策略研究索引

本文件汇总 `examples/rolling_csi300/` 的策略研究结论、配置、运行方式与关键操作要点。

## 策略概况

- **策略**: rolling 滚动重训 + LightGBM(Alpha158Industry) + ICTimingStrategy, CSI300 内选股
- **市场**: CSI300, 回测区间 2020-01~2026-07
- **基准配置**: `rolling_config.yaml` (已固化 **n_drop=1**, topk=30, ICTiming 动态降仓)
- **当前 baseline**: `rolling_csi300_lgbm_ndrop1` (mlruns 实验)
- **关键指标** (n_drop=1 + ICTiming, 2020-2026, 净成本): 年化超额 ~17.9%, IR 1.44, 最大回撤 -18.4%, 换手 16/yr

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

#### 2026-08-04 生产事故: sort patch 未同步到 244 (重要!)
- **事故**: 244 实盘机 08-04 09:30 自动 flow 下单, 实际买入 12 只 (含宇通 500 股 ¥1.59w), 其中 9 只是非正确 target 的股票
- **根因**: 本机 position.py 有 sort patch (commit b5ec5b21), 但**之前部署 244 只用 tar 包同步 examples/ 文件夹, 漏掉 qlib 源码 patch** → 244 的 `get_stock_list()` 仍是非确定 set 顺序 → 每次回测随机持仓路径 (宇通 3285 vs 443 vs 406, pred 却 100% 一致)
- **诊断链**: 同一 rolling_models/配置/数据, 仅 exp_name 不同回测结果迥异 → pred 对比 diff=0 → 定位到 position.py 差异 (本机有 sort, 244 无)
- **修复**: 244 position.py 打上同款 sort patch; 验证 patch 后连续 2 次回测宇通=443 完全一致; 禁用 QlibDailyFull 防再错单
- **教训 (重要)**: **手动 patch/tar 同步不靠谱, 改用 git 管理** — 见"运行方式"里 git 部署部分; 本事故后 244 一律 `git pull` 获取代码

### 4. 因子衰减分析
- 无结构性衰减 (2020-2022 IC 0.077 vs 2023-2026 IC 0.077)
- IC 呈周期性波动 (低谷 2021H2/2025H2, 高效 2020H2/2022Q2/2024H1), 低谷后可恢复
- **2026-08 复核** (analyze_ic_decay.py, 数据到 2026-04-14):
  - 半年度 IC_IR: 2020H2 0.1265(峰值) / 2021H1 0.0328(谷) / 2022H1 0.0937 / 2024H1 0.1959(峰值) / **2025H2 -0.0796(谷)** / **2026H1 0.0948(已恢复)**
  - 前后半对比: 2020~2023 mean IC 0.0631 vs 2023~2026 0.0545 → 无结构性衰减
  - 滚动60日IC 最低 -0.239 (2025-09-08, 此时 ICTiming 已降仓), 最新 0.092 (>ic_low 0.04, 满仓)
  - **⚠️ 数据缺口**: pred/label 只到 2026-04-14 (Bug 3 残留), 2026Q2 -0.089 仅9天不可信; 需 baostock 恢复后重跑滚动回测补 4-7 月数据

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

### 7e. Volume 因子 (VOLUME0-4) → 已存档, 不作为正式配置
- **动机**: Alpha158 源码里 volume 块 (30 个 rolling 量因子: VMA/VSTD/WVMA/VSUMP/VSUMN/VSUMD) 默认未启用, 想试增量量能信息
- **实现**: `custom_handler.py` Alpha158Industry.get_feature_config 加 `"volume": {"windows":[0,1,2,3,4]}` → 172→177 因子; 配置文件 `rolling_config_volume.yaml`, 实验 `rolling_csi300_lgbm_volume` (27 期 full rolling 训练, 39 分钟)
- **结果** (2020-2026 全量回测, 对比 baseline n_drop=1 + ICTiming):

| 指标 | baseline | +volume | Δ |
|---|---|---|---|
| RankIC | 0.0751 | 0.0751 | 0 |
| 净超额年化 | 16.78% | 17.87% | **+1.09pp** |
| 净 IR | 1.29 | 1.44 | +0.15 |
| 最大回撤 | -17.6% | -18.4% | -0.8pp (略差) |

- **结论**: 净收益/IR 有提升, 但 IC 持平、回撤略深; **用户决定不作为正式配置, 结果仅存档** (不上生产, 244 无需重训/重算缓存)

### 8. IC 动态降仓 (ICTimingStrategy) → 已纳入生产
- **策略**: 基于滚动 RankIC (前瞻20天收益, 正确口径) 动态调仓; IC 低谷 (滚动60日 < ic_low=0.04) 时降 risk_degree 到 0.5, 恢复 (ic_high=0.06) 回满仓 0.95
- **无未来函数 (关键修复)**: 初版用 `pct_change(20)` (回顾收益) 算 IC, 方向错误导致降仓信号无意义; 修复为**前瞻20天收益** (`close[t+20]/close[t]-1`) + **20交易日因果滞后** (`shift(20)`, 决策时只用已完全实现的IC)
- **样本外验证 (2020-2024定阈值 → 2025-2026检验)**: 样本外年化 +0.8pp (29.17% vs 28.35%), 超额 +0.8pp, **回撤 -13.7%→-9.3% (改善4.4pp)**; 训练段也改善 → 非过拟合
- **全量回测 (生产配置)**: 净年化 17.87% vs 原始 17.12% (+0.8pp), 净IR 1.44 vs 1.29, 回撤 -18.4% vs -19.8%; 换手/成本不变 (纯降仓防守)
- **教训**: 任何动态策略必须检查①IC收益方向 (前瞻 vs 回顾) ②因果滞后 (用未来窗口数据必须 shift); 否则"看似有效"是伪信号
- **注意**: 样本外仅1.5年, 实盘需持续监控 IC; 若 IC 长期失效 (如连续1年 <0.03) 应暂停策略而非仅降仓

### 9. 滚动模型退化探索 (2026-08-03, 修复已回退)

#### 现象: 最近 4 期滚动模型只训练到 1 棵树
- 检查 `rolling_models_20260801180017` (ndrop1 生产基线) 各期模型 `params.pkl` 的 `num_trees()`: 最近 4 期 (2025-09/2025-12/2026-03/2026-06) 只有 **1-3 棵树** (文件 ~34KB vs 正常 1.5MB, 预测只有 ~54 个不同取值), 早期期次正常 (44-269 棵树)
- **根因**: 生产 pred 月度 RankIC 在 **2025-06~08 深度塌陷** (−0.15/-0.20/-0.25, 因子关系短暂反转, 即 4 节记录的 2025H2 IC 谷)。所有 4 个退化期次的 **valid 窗口都覆盖这个塌陷期** → 加树永远不改善 valid loss → early stopping 第 1 轮触发 → 1 棵树
- 验证: 末期限模型 (931caacf, test 2026-06-15→07-31) valid loss 从第 0 轮起就不下降 (0.9969→1.006), train loss 却降到 0.944 (能拟合但无法泛化)
- **影响**: 当前 pred (含 2026-07-31 迈瑞 262/300) 来自退化模型, 排名低置信度

#### 尝试的修复 (RobustLGBModel min-trees floor) → 已回退
- 实现: `custom_handler.py` 加 `RobustLGBModel` (自定义 early stopping, 当 valid 从未改善时强制保留 min_iterations=30 棵树), `rolling_config.yaml` 切换模型类
- 干净验证 (用原缓存对比, 各期树数与原基线 EXACTLY 一致, 仅 3 个退化期 1→30 棵): **净年化 17.87%→15.89%, IR 1.44→1.31, 回撤 -18.4%→-25.9% → 修复是负面的**
- **为什么负面**: 30 棵树的模型在 alpha 塌陷期过拟合训练数据的反转关系; 月度 IC 显示 2026-03/04/05 (回撤段) 明显变差 (−0.055/-0.020/-0.036), 仅 2026-07 (+0.18, n=999 不完整月) 假性改善
- **结论**: 退化 1 树模型是模型对低 alpha 期的**自然稳健响应** (第一棵树=最强单一分裂), 不是性能问题 → **不修, 维持原 LGBModel**
- 教训: "模型树数少=有问题" 的直觉不成立; 任何"修复"必须用干净对比 (同缓存) 验证, 且警惕不完整月份 (n<1000) 的假性改善

#### 过程中发现/修正 (重要)
- **缓存重建误诊**: 以为 Alpha158Industry cache 的 VMA20 爆到 4e18 是缓存过期, 实际是**真实停牌** (东方证券 2026-04-20→05-06、拓荆科技 2026-06-29→07-10, baostock 确认成交量空/价格冻结) → 缓存无需重建
- **缓存重建引入 label bug**: rebuild 时未带 label override, 缓存用了默认 **1 日 label** (应为 20 日 `Ref($close,-21)/Ref($close,-1)-1`), 导致 fixed/fixed2 两次重训全部失效 (5.3%/7.0%) → 已修正
- **原缓存已找回**: 本机 5bf590b2fe 被重建覆盖后, 从 244 取回原始副本 (md5 `7bfd40e6...`, 5407275632 bytes), 现本机已恢复
- **训练确定性**: 同缓存同代码训练结果完全确定 (155/155/155), 但原缓存 vs 重建缓存给不同树数 (142 vs 155) → 缓存内容确实不同, 对比必须用同一缓存

#### 状态
- 代码已回退: `rolling_config.yaml` (LGBModel)、`run_rolling.py`、`custom_handler.py` 恢复原样; `rebuild_handler_cache.py`/`test_last_period_fix.py` 已删除
- mlruns 已清理: `rolling_csi300_lgbm_ndrop1_fixed/fixed2/fixed3/fixed4` + 对应 4 个 `rolling_models_2026...` + 测试临时 `Experiment` 共 9 个实验已删除 (mlflow 移至 .trash, 磁盘未释放; 如需回收磁盘需手动删 mlruns/.trash)
- 生产管线回到原始 17.87% 状态, 无需重训

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
| `rolling_config.yaml` | 默认生产配置 (n_drop=1, topk=30, ICTiming 动态降仓) |
| `rolling_config_ictiming.yaml` | ICTiming 策略专用配置 (ic_low=0.04, low_risk=0.5) |
| `custom_handler.py` | 策略/处理器: VolatilityTimingStrategy, ICTimingStrategy, MinTradeValueStrategy, IndustryProcessor, Alpha158Industry, DvRatioProcessor, Alpha158DvRatio, IndustryCappedStrategy |
| `run_rolling.py` | 滚动训练+回测入口, 注册所有 custom handler |
| `dump_dv_ratio.py` | dvratio PIT 落库脚本 |
| `dump_dv_ratio_daily.py` | PIT→日频 bin 提速脚本 |
| `dvratio_factor.md` | dvratio 因子研究记录 (否决) |
| `style_profile.md` | 风格画像 + alpha 原理 + 实盘差距评估 |
| `mlruns/` | mlflow 实验存储 |
| `industry_map.pkl` | 行业映射 (股票->行业) |

## mlruns 关键实验

- `rolling_csi300_lgbm` — 技术面 baseline (n_drop=1)
- `rolling_csi300_lgbm_ndrop1` — **当前 baseline** (n_drop=1 + ICTiming, 净成本最优)
- `rolling_csi300_lgbm_ictiming` — ICTiming 策略专项实验
- `rolling_models_*` — 各次滚动训练的 27 期模型

## 数据/环境备注

- qlib 数据: ~/.qlib/qlib_data/cn_data
- **当前研究机** (DESKTOP-JTV5CLG, WSL IP 192.168.52.105): 15GB RAM + 23GB swap, 20 线程, 训练峰值依赖 swap 兜底; 回测/研究/模型训练
- PIT 记录格式: 20 字节 (date+period+value+_next), financial/<code>/<field>_q
- 日频 bin 与 PIT 逐日一致 (243/243 天验证)
- tushare 代理: API_URL 默认 http://jiaoch.site, token 从环境变量 `TUSHARE_TOKEN` 读取 (已从代码中移除, 勿硬编码)
- 无中文字体 (matplotlib 需用英文标签)
- **两机数据对齐 (重要, 2026-08-04 验证)**: 本机 vs 244 的持仓对比**必须先对齐数据端** (日历 + instruments + features bin), 否则数据端差 1-2 天持仓必然不同 (非 bug)。已验证: 本机对齐到 08-03 后, 与 244 回测持仓 **0/29 差异** (07-31 和 08-03 两天都完全一致) → position.py sort patch 生效, 两机同代码/同模型/同数据下回测确定且一致
- **baostock 更新不稳定 → 244 数据作本机数据源**: 2026-08-04 baostock 虽能 login 但批量下载/单只查询均卡死; 244 每天早上自动更新成功 (数据最新)。若本机需对齐数据, 可从 244 打包增量: 244 上 `find features -name "*.bin" -newermt "<上次同步日>" -print0 | tar --null -cf /tmp/incr.tar -T -` + `tar -rf` 追加 `calendars/day.txt instruments/all.txt instruments/csi300.txt`, scp 到 Windows 家目录再取回, 本机解压到 `features/` 覆盖 + 替换日历/instruments (bin 是完整文件可直接覆盖)

## 交易执行机 (192.168.11.244, 重要)

- **角色**: 实盘交易执行机 (跑 run_daily.ps1 → WSL 更新数据+回测 → easyths 下单), 与当前研究机分离
- **识别**: Windows 主机名 desktop-6muresa; WSL 发行版 Ubuntu (WSL2), **默认 root 登录**; WSL 里有 tc 用户 (HOME=/home/tc, qlib 在 /home/tc/qlib)
- **SSH**: 连的是 **244 的 Windows OpenSSH** (whoami 返回 `desktop-6muresa\tc`), 不是 WSL 直连; 密钥 `~/.ssh/id_ed25519_new` (注意默认 id_ed25519 不存在, 必须 `-i` 指定)
- **操作 WSL 的方式**: 通过 Windows ssh 执行 `wsl -u tc bash -lc '命令'`; 但复杂引号/管道/`&&` 会被 Windows cmd 破坏, **最稳妥 = 把 bash 脚本 scp 到 C:\Users\tc\Desktop 再 `wsl -u tc bash /mnt/c/Users/tc/Desktop/脚本.sh`**
- **Windows 桌面文件在 WSL 里的路径**: `/mnt/c/Users/tc/Desktop/`
- **easyths 服务**: 运行在 244 Windows 侧, 端口 7648, config 路径 `C:\ProgramData\miniforge3\Scripts\easyths.exe --config C:\Users\tc\easyths\config.toml`; 当前机器 (52.105) 的 sync_to_realtime.py 连它下单
- **数据**: 244 的 qlib 数据须定期 `update_baostock.py` 同步 (上次 2026-08-02 更新到 2026-07-31); 更新跑完 dump features 后需确认日历已到最新
- **Alpha158Industry 缓存** (5.4GB pkl) 已传至 244, 保证两台机器 pred 一致 (0.1pp 内浮点抖动可接受)
- **WSL 后台任务坑**: nohup 会在 ssh 会话断开时被杀; 必须 `setsid cmd > log 2>&1 < /dev/null &` 才能存活
- **WSL 默认 root 坑 (重要)**: 244 的 WSL 默认用户是 root, ps1 里 `wsl -e bash` 会以 root 跑 (HOME=/root), 导致 `Path.home()/qlib` = `/root/qlib` 不存在 → `No module named 'scripts'`. **ps1 必须用 `wsl -u tc bash` 指定 tc 用户**
- **定时任务**: 244 上已设 2 个 Windows 任务 — `QlibUpdateData` (每晚 21:00, update_data_only.ps1) + `QlibDailyFull` (每早 9:30, run_daily.ps1 完整回测+下单)。**2026-08-02 起 QlibDailyFull 已禁用** (观察期手动 sync); **08-04 事故后再次禁用** (QlibDailyFull 曾于 08-03 被重新启用, 08-04 09:30 因 position.py 非确定 bug 错单, 现已禁用防再错)
- **部署方式 (2026-08-04 起, 用 git 不用 patch/tar)**: 本仓库 fork 为 `congt` (github.com/tiancongfree/qlib), 分支 `snapshot/rolling_csi300`。**所有代码改动走本机 commit → push 到 congt → 244 `git pull`**。严禁手动 patch/tar 同步 (见 3b 事故教训)。244 pull 后需注意: 若 position.py 有 sort patch 但 git 显示 clean, 说明已含在历史 commit (b5ec5b21) 中; 部署 qlib 源码改动后 244 需重装 `.venv` 中对应包或确认 import 用本地源码
- **244 同步流程**: `git remote add congt https://github.com/tiancongfree/qlib.git` → `git fetch congt` → `git checkout snapshot/rolling_csi300` → `git pull congt snapshot/rolling_csi300`。数据/缓存 (18GB pkl、mlruns 实验) 仍单独同步, 不入 git
- **244 无法访问 GitHub 时 (被墙, 2026-08-04 已验证)**: 用 `git bundle` 离线传输增量 commit, 保留 git 历史与 commit:
  1. 本机打包增量: `git bundle create /tmp/rolling_csi300.bundle <244当前HEAD>..snapshot/rolling_csi300` (109KB/13 commits)
  2. scp 到 244 (上传到 Windows 家目录, 再 `cp /mnt/c/Users/tc/rolling_csi300.bundle /tmp/`)
  3. 244 上: `git fetch /tmp/rolling_csi300.bundle snapshot/rolling_csi300 && git merge --ff-only FETCH_HEAD`
  4. 若报 untracked 文件冲突 (tar 包遗留的 rolling_csi300 文件未 track): 先把冲突文件 `mv` 到备份, merge 后再核对 (已验证 18 个备份文件全在 git 中, 无丢失), 最后删除备份
  5. 验证: `git log --oneline -1` 应为最新 commit; 关键文件 md5 与本机一致 (`position.py` sort、`custom_handler.py` 无 volume、`run_workflow.py` exp_name=ndrop1)
  - **后续代码改动仍走 本机 commit → congt → (GitHub 通时 pull / 被墙时 bundle)**
- **run_workflow.py 默认 exp_name**: 已改为生产实验 `rolling_csi300_lgbm_ndrop1` (勿再改回 `rolling_csi300_lgbm` — 那是旧实验, 历史持仓轨迹不同)
- **部署包 (已废弃)**: 旧方式 `rolling_csi300_install.tar.gz` (代码+配置+3个核心mlruns实验, 不含18GB缓存pkl), 覆盖解压到 244 的 examples/rolling_csi300/ — **不再使用, 仅备份参考**
- **244 桌面残留**: rolling_csi300_install.tar.gz (备份, 可删)
- **完整 flow 已验证 (2026-08-02)**: run_daily.ps1 全链路跑通 (数据更新→回测 ICTiming 0.198→sync 下单), 买入/卖出挂单均成功提交
- **A股 T+1 资金约束 (重要)**: sync 一次性提交"卖+买", 但**当日卖出资金次日才到账** → 买单冻结当日可用现金, 若买入总额超过当日可用资金, 尾部买单会报 "可用余额不够" 失败 (2026-08-02: 300450/000425 差 ~4000元未买). **非脚本 bug, 属正常现象**: 次日卖出资金到账后再跑一次 flow 即可补齐
- **sync 挂单为限价单**: 基于**腾讯行情实时价** (qt.gtimg.cn, 批量 50 只/请求, 替代不可用的 baostock) ±0.2% (price_slippage); 买入总数受 max_total 限制但受 T+1 可用资金约束更强
- **baostock 服务端不可用 (2026-08-02 起)**: login 挂起 (Connection reset by peer), 两台机器都如此, 疑似服务端故障; 实时价改用腾讯行情 API (`_load_real_prices`), 数据更新用 `--skip-update` 跳过
- **max_total 固定现金垫方案 (2026-08-04 起, 重要)**: `sync_to_realtime.py:427` 用 `max_total = max(0.0, total_assets - 400000)` — **保留 40w 现金垫, 其余投入目标组合**。相比 `total_assets * invest_ratio`: 比例方案会随盈利把 scale 拉回固定比例, **强制兑现盈利 (稀释)**: 持仓涨到 22w 时 invest_ratio 会把 target 拉回 95%×总资产, 被迫砍仓。固定垫方案让超出现金垫的部分自然跟涨, 不稀释。现金垫调整: 40w→400000 | 20w→200000 | 投满→`total_assets * invest_ratio`。机制: max_total 是目标组合市值上限, `_parse_target_stocks` 按 `scale = max_total / 目标总市值` 等比缩放所有股票

### 分批建仓方案 (2026-08-02 起; 08-04 改固定现金垫 40w)

- **目标组合稳定性 (已验证)**: 回测 `account: 1000000` (100w) 是 config **固定常量**, lastday CSV 里各股 amount/weight 是**相对权重**, 与实盘账户资金**无关**。重跑回测只要数据/模型不变, 相对权重完全一致 → 目标名单稳定, 分批基准可靠
- **建仓进度由 max_total 控制**: `max_total = max(0, 总资产−400000)` (40w 现金垫)。总资产 60w→target 20w; 总资产 70w→30w; 总资产 100w→60w。**无需改代码**, 随账户增长自动加仓; 现金垫阈值可按需下调 (见上一条)
- **重合度验证**: qlib lastday 持仓 29 只, 实时价 29 只全获取 (无缺失); 60w 档 sync 目标 29 只 = qlib **100% 重合**; 20w 档 21 只 (8 只小仓股整手化归零); 5.7w 档 10 只
- **资金缩放只影响整手粒度**: scale 越小, 小仓位股 (<4.5% 权重) 越先被 `_to_lots` 归零。资金越大自动解锁越多小仓股, 3 笔组齐完整组合
- **前 10 只权重股占 55.7%** (60w 目标): 600346 9.85% / 600585 7.16% / 002920 5.63% / 600233 5.36% / 601899 5.36% / 601058 4.89% / 000858 4.75% / 600588 4.73% / 688472 4.05% / 002594 3.89%。最大单只 <10% 无超集中
- **sync 是调仓引擎, 非一次性建仓**: sync_positions 自动对比 target vs actual → BUY(新进)/SELL(剔除)/调整数量。每次跑 flow 自动完成"加仓+调仓", 中途 qlib 调仓天然被处理
- **观察期策略 (用户选定)**: 第一笔买完观察一周**不跑 sync** (忽略每日调仓信号), 只看前 10 只 (55.7% 仓位) 实际表现; 一周后跑 flow 自动调仓+加仓; 若前 10 只系统性下跌 (疑似 IC 失效) 暂停加仓

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
| `factor` | 复权系数 (⚠️ **不可靠, 见 Bug 4**, 勿用于价格换算) | 0.608 |

- 恒等式: `close = adjclose / first_adjclose` (first_adjclose = 该股 bin 首日 adjclose, 恒定)
- **关键**: `amount × close` (前复权市值) = **真实市值, 完全自洽正确**; 真实股数 = `市值 / 市场真实价 (baostock adjustflag=2)`
- ⚠️ **`close/factor` 不是可靠真实价** — factor 存在系统性复权错误 (见 Bug 4)

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

### Bug 4: factor 复权系数系统性错误 (2026-08-02 发现, 重要!)
- **症状**: 300 只 CSI300 中 163 只 (54%) 的 `close/factor` 与 baostock 真实价偏差>3%, 63 只>12% (新易盛 -98%, 天孚 -160%, 三环集团 -27%)
- **根因**: `update_baostock.py` map_and_scale 的 `factor_val = exist["last_factor"]` 增量更新时**沿用旧 factor 不重算** (line 533); 历史某次初始化复权基准错误后被永久固化
- **关键澄清**: ① **qlib 日收益率完全正确** (相关系数 1.0000, 平均绝对差 0) → **所有回测结论不受影响** (收益是相对量); ② **`amount × close` = 真实市值自洽正确**; ③ 只有 **factor 不可靠**
- **正确换算**: 真实市值 = `amount × close`; 真实股数 = `市值 / baostock真实价 (adjustflag=2)`
- **sync_to_realtime.py 已修复**: 原用 `amount × factor` 算股数 → 改为 `amount × close / 腾讯行情实时价`; 对 factor 错误的股票 (紫金/三环等) 挂单数量修正
- **未根治**: qlib factor 本身仍错, 但**不影响回测与 sync** (都已绕过 factor); 彻底修复需重建历史 bin 的 factor, 低优先级

### 运行注意
- **baostock 长任务易被杀**: 单进程 >1 分钟会被环境终止, 无 traceback. 大批量下载必须分批 (每批 timeout 窗口内完成) 或用 BACKFILL_START/END chunk
- **baostock 服务不可用 → 数据更新挂起**: 2026-08-02 起 login 挂起不退出, run_workflow.py 的 update 步骤已加 600s 超时保护 (超时自动跳过); 或直接 `--skip-update` 用已有数据跑回测+sync
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

- **(进行中) 分批建仓 (固定现金垫 40w)**: 当前 max_total = 总资产−400000 (总资产 60w→target 20w), 用户本周手动 sync 观察前 10 只 (55.7% 仓位); 账户增长自动加仓, 无需改代码 (见"分批建仓方案")
- (可选) 观察期后重新启用 QlibDailyFull 定时任务 (当前已禁用)
- (可选) IC 长期失效监控与自动暂停机制 — 已在 ICTiming 内降仓, 极端情况需暂停
- (可选) 更高频/另类信号源 (分钟级) — 机构暴力来源, 需新数据
- (可选) 减少训练窗口减轻 swap 依赖
