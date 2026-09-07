# qlib CSI300 滚动策略研究索引

本文件汇总 `examples/rolling_csi300/` 的策略研究结论、配置、运行方式与关键操作要点。

## 四个环境对齐 (重要, 2026-08-29)

| 编号 | 环境 | 角色 | 位置 |
|---|---|---|---|
| **1** | **训练 WSL** | 训练/重训、cron、数据落库、daily_predict | `DESKTOP-JTV5CLG` (192.168.52.105) 内 WSL, 当前仓库即在此 |
| **2** | **训练宿主 Windows** | 承载训练 WSL (环境1) | `DESKTOP-JTV5CLG` 宿主 Windows |
| **3** | **244 宿主 Windows** | 跑 easyths 下单 | `192.168.11.244` |
| **4** | **244 WSL** | 跑 sync_to_realtime、数据更新、回测 | 244 内 Ubuntu WSL (默认 root, 操作须 `wsl -u tc`) |

- **重训全在环境1** (monthly_retrain.sh + cron): 训练 WSL 内部的 cron 只在 **WSL 已启动** 时才生效, 不随宿主自动启动
- **训练 WSL (环境1) 依赖宿主 Windows (环境2) 开机才活**: 环境2 关机 → 环境1 的 cron 全废 → 需环境2 开机后拉起 WSL 跑 catchup 补漏
- **244 侧 (环境3/4) 是消费端**: 依赖环境1 重训后 push 过去, 各自管各自的 Windows 定时任务, 与本机训练 cron 无直接依赖

### 重训错过补跑机制 (2026-08-29)

WSL cron 只在 WSL 存活时跑 → 宿主关机 = cron 全错过。现已加 4 层兜底:

| 触发 | 时机 | 载体 | 作用 |
|---|---|---|---|
| **定时期** | 每月 1/15 01:00 | 环境1 cron | 正常重训 `monthly_retrain.sh` |
| **每日 catchup** | 每天 09:00 | 环境1 cron | `monthly_retrain_catchup.sh` 检查超期则补跑 |
| **登录 catchup** | 每次登录 tc 会话 | 环境1 `~/.profile` (nohup 后台) | 登录即查, 平时秒退(~7ms) |
| **宿主轮询** | 环境2 每 30 分钟 | 训练宿主 Windows 计划任务 `Wake_TrainWL_Catchup` | **拉起训练 WSL 跑 catchup** (覆盖"白天关机晚上到家登录") |

- **关键文件**:
  - `monthly_retrain.sh` — 成功后写时间戳 `logs/.last_retrain`
  - `monthly_retrain_catchup.sh` — 读 `logs/.last_retrain`, 距上次成功 ≥16 天(默认 `MAX_AGE_DAYS`)则调用 `monthly_retrain.sh`; **flock 锁** `logs/.retrain.lock` 防多触发并发重训
  - `wake_train_wsl_catchup.bat` (`环境2 Desktop`) — 宿主任务调 `wsl.exe -d Ubuntu -u tc bash -lc "...catchup.sh >> logs/cron_catchup.log"`
- **宿主任务注册** (`Wake_TrainWL_Catchup`): SCHTASKS `/sc minute /mo 30`, 只在中、非管理员 token 可创建 (onlogon/onstart 需管理员, 会被拒)。含之前一次事故: 测试触发时 state 过期导致 2 个并发重训抢数据 → **已加 flock** + 重置 state=0815
- **当前 state**: `logs/.last_retrain=20260815` (age≈14d, 正常不补跑)


## 策略概况

- **策略**: rolling 滚动重训 + LightGBM(Alpha158Industry) + ICTimingStrategy, CSI300 内选股
- **市场**: CSI300, 回测区间 2020-01~2026-08 (数据已到 08-21)
- **基准配置**: `rolling_config.yaml` (已固化 **n_drop=1**, topk=30, ICTiming 动态降仓)
- **当前 baseline**: `rolling_csi300_lgbm_ndrop1` (mlruns 实验)
- **关键指标** (n_drop=1 + ICTiming, 2020-2026, 净成本): 年化超额 ~17.9%, IR 1.44, 最大回撤 -18.4%, 换手 16/yr

## ⚠️ 每日 pred 冻结 bug (2026-08-22 已修复, 重要!)

### 症状
- 244 每日 `--skip-train` 回测/实盘 **持仓长期冻结**: 2026-08 三周内只换仓 1 次, 五粮液/长安/赛力斯等低分股一直挂着
- 即使数据更新到 08-21, 目标名单仍停留在 07-31 的决策

### 根因 (架构缺陷, 非 qlib bug)
- qlib rolling 的 pred **只在训练时生成** (每期模型对固定 test 窗口 predict 一次), 两次重训 (3个月) 之间新日期**没有新 pred**
- `run_rolling.py --skip-train` 只是**重复拼接已有 pred + 回测**, 不产生新预测 → 回测永远用旧 pred → 持仓冻结
- 244 的 `run_daily.ps1` 用 `run_workflow.py --skip-train`, 每日"看似在跑"实际只是重放 07-31 预测

### 修复方案 (daily_predict.py + 每月重训)
- **`daily_predict.py`**: 用最新一期已训模型对「上一 pred 末+1 → 今天」推理新 pred, append 进 ensemble pred (dedup 取最新)
  - 特征用 QlibDataLoader + IndustryProcessor **即时计算** (已验证与 5.4GB 缓存数值完全一致, diff=0), 不依赖缓存更新
  - 已验证: standalone 预测与训练时 ensemble pred 完全一致
- **`run_workflow.py`**: 数据更新后插入 daily_predict (step 2), 再回测
- **每月 1/15 号重训** (本机 cron): `monthly_retrain.sh` 完整重训 → 推新 rolling 实验 + 缓存 + pred 到 244
- **`monitor_ic.py`**: 每日 IC 健康监控 (前瞻20天+因果shift20, 与 ICTiming 同口径), 连续 <0.03 报警
- **验证**: 修复后 8 月换仓 9 次 (修复前 2 次), 三只目标股全部换出; 本机与 244 pred/指标一致
- **⚠️ 重训语义已改 (2026-09-07 起, Plan B)**: "每月 1/15 重训" **不再默认跑完整 27 窗**。`monthly_retrain.sh` 默认 `MODE=append` —— 只重训**最新 1 个窗口** (rolling 2008→now, expand-only, `task_l[-1]`) 并 **APPEND 进既存 rolling_models_*** 实验 (不 delete, 不动历史窗 pred)。原理:
  - 实盘 target 只消费**最新窗** pred (qlib RollingEnsemble concat+dedup 无跨窗平均);LightGBM 无"真在线行更新",每月"权重刷新" ≡ 用 2008→now **全量重拟最新窗一次**。那 ~26 个 2020 起的历史窗每月重跑是纯报告重复功 (pred 冻结即够)。
  - append 只跑一次最大窗 (~10GB 峰值, 几分钟), 旧 OOM/几十分钟全 27 窗的算力/swap 开销基本去掉。
- **全量重刷报告 = 手动**: 脚本 **不设自动 full 计时**, 也不依赖 daily catchup 触发 full。需要整段 2020→今 equity/IC/回测重刷时, 手动 `MODE=full bash monthly_retrain.sh` (retrain 全 27 窗)。`monthly_retrain_catchup.sh` (每日 09:00) 仍只判 `logs/.last_retrain` ≥16 天没跑到 → 补跑, 但默认也是 **append**(轻)。
- **实现 (零改 qlib 源码, 升级安全)**: `examples/rolling_csi300/rolling_append.py` 定义 `class AppendRolling(Rolling)`, 新增方法 `train_append()`: `TrainerR(experiment_name=self.rolling_exp, call_in_subproc=True)([task_l[-1]])` 且 **不调用 `R.delete_exp`** (不像 qlib 原生 `_train_rolling_tasks`)。其余继承父类 `get_task_list/_ens_rolling/_update_rolling_rec`。`run_rolling.py` `--mode {full,append}` (默认 append): append 分支 = resolve 既存 rolling exp → `train_append()` → `_ens_rolling(extra_pred=_latest_combined_pred())` (保留日推 pred 尾, 冻结修复) → `_update_rolling_rec()`; full 分支 = 旧 `rolling.run()` 全量。append/skip 都**必须存在** rolling_models_* (无则报错提示先跑一次 full)。
- **append 的窗口推进**: 随数据端 60 交易日滑动 → 通常每次 (≥16 天间隔) 只需把最近窗 re-fit 覆盖到当前 calendar 末即可更新模型/pred 尾。若数据连续多次没跨过新 step 边界, 末窗 test 区间不变, 重复二次同一窗 = 无害重复 (dedup keep-latest)。
- **部署 (append)**: 照旧 push 该 rolling_models_* 实验 (复用, 只是多一个 run) + 新 combined pred 到 244; **缓存 pkl 不动** (append 不重建缓存, 跳过 5.4GB scp; 仅 `MODE=full` 时一并 scp 缓存)。

### 关键教训
- **任何"每日流程"必须确认 pred 真的覆盖到最新日期**, 不能只看日志"跑成功"
- `--skip-train` = 复用旧结果, 不是"用新数据预测"
- 重训换模型会改变评分 → 持仓可能大幅变化 (n_drop=1 天然限速 1 只/天, 无需担心)

### 部署要点
- 代码走 git (本机 commit → congt, GitHub 被墙用 git bundle)
- 数据/缓存/mlruns 单独 scp (5.4GB 缓存, md5 校验 a5e51d81)
- 244 桌面 run_daily.ps1 需手动 scp 同步 (它不在 git 里, 是实际调度用的)

### sync 双模式 + 方案D (2026-08-22, sync_to_realtime.py)
- **双模式判定** (`detect_mode`): setup (建仓) = 空仓 或 target∩actual 交集占比 <50%; 否则 daily (日常)
  - setup: 一次性全量同步到 qlib target (首次建仓/重训后大换血自动触发, 如 2026-08-22 交集仅 9.7%)
  - daily: 严格按 target 增删 (换入换出)
  - `--allow-empty-holdings` 参数已移除, 空仓在 daily 模式仍拦截, setup 模式放行
- **方案 D** (持仓期间股数锁定): 移除 stocks_in_both 的每日 BUY+/SELL- 微调
  - 依据: qlib TopkDropout 持仓段内 amount 恒定 (验证: 397股1560次变化全在 count_day=1 重新买入, 零例外)
  - 微调单 = 纯整手对齐噪音 (观察期 17笔 vs 换股 6笔), 年化成本 ~0.25%, 非 alpha 来源
- **15% 极端权重保险**: 实盘单票市值占比 >15% 时卖出超额部分 (整手: 主板100/科创200), 基于实盘实际权重非 qlib 目标权重 (qlib topk=30 天然 <12%)

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

### 7d-bis. 小资金整手化死角 (20w 模拟盘缩放验证, 2026-08-26, 重要)

- **背景**: 用户 20w 模拟盘观察, 发现"只买到 20 多只且剩余大量资金"。用 `_parse_target_stocks` + 腾讯实时价缩放 2026-08-21 目标 (31只) 实测复现。
- **根因**: `_to_lots` 整手化**向下取整** (sync_to_realtime.py:163-170) + 等比缩放 (scale = max_total/总市值)。20w 下 scale=0.0642, 大量权重 2-4% 的中价股 (赣锋/亿纬/华工/阳光/指南针/江波龙) 缩放后落在 **0.85~0.99 手**, 差一点不到一手被归零 (赣锋 002460 缩放后 93 股 < 一手 100, 被砍) —— **不是买不起, 是整手化死角**
- **另一类**是真买不起: 中际旭创一手 ~¥85k (实价850元)、芯原一手¥36k (科创200股) 等贵标的, 20w 额度下连一手都配不起
- **实测结果** (20w, 缩放 2026-08-21 目标):

| 方案 | 买入 | 总市值 | 闲置资金 | 赣锋002460 | 中际300308 |
|---|---|---|---|---|---|
| **A. floor(现状)** | 19只 | ¥103,679 | **48%** | ❌ 丢(93股) | ❌ 丢 |
| **B. 纯ceil** | 3只 | ¥164,218 | 18% | ❌ | ✅ 但只剩3只贵股, 分散崩坏 |
| **C. floor保底+剩余补贵** | **25只** | **¥196,289** | **2%** | ✅ | ❌ (一手¥85k不够) |

- **关键结论**:
  - **纯 ceil 是陷阱**: 每只都向上凑整瞬间耗尽预算, 20w 只够买 3 只最贵股整手, 组合畸形
  - **方案C最优**: 先用 floor 保底保住低价分散仓 (19只), 再用剩余资金按权重优先级给被砍的贵标的补一手 → **25 只、资金利用率 52%→98%、闲置 ¥96k→¥3.7k**; 赣锋补买进; 额外补进兆易(39k)/芯原(18k)/阳光/华工/指南针; 唯一中际旭创装不下 (一手¥85k, 20w实不够)
  - 机制: 低价股 floor 保底、贵的按权重用剩余资金择优补购, 不同于纯ceil的"先保贵的拖垮分散"
- **状态**: **未改码** (用户决定暂不改 `_parse_target_stocks`), 仅记录发现; 若日后启用方案C = 在 `_parse_target_stocks` 加一层按 weight 排序的补购循环 (_to_lots 只对保底层用)
- **真实价口径**: qlib 存前复权价 (如赣锋13.22), 腾讯实时价才是真实价 (赣锋实时53.5); 真实市值=amount×qlib价=¥77.5k 自洽正确, 真实股数=市值/实时价

#### 60w 验证 + 方案C权重畸变 + 改account无效 (2026-08-26 续)

- **60w floor(现状)**: 买入25只, ¥430,934, **闲置 ¥169k (28%)**; 赣锋上了(002460=200股, scale 变大跨过一手), 仍丢 6 只 → 全是"一手 >¥34k 的贵标的": 中际(85k)/兆易/新易盛/深南/江波龙/东山
- **60w 方案C (floor保底+补贵)**: 28只, 闲置 28%→1%; 但**补购造成权重畸变**:
  - **SZ300308 中际 目标2.9% → 实际14.4% (+11.5pp 超标5倍)** ⚠️ 一手¥85k 占60w的14.4%, 严重超配(>§7d 有害的 8% cap)
  - SH603986 兆易 +3.1pp / SZ301308 江波龙 +2.8pp (可接受); 其余低价仓贴合目标(±1.1pp)
  - 结论: **方案C 的"按权重补贵标的"在"一手>目标仓位"时会把单票权重瞬间撑爆** → 若启用需加单票上限(如 ≤5%)或只补"一手价 < 其目标仓位金额"者, 否则不补贵股
- **改 account 无效 (重要, 用户曾想"pred 设 20w 不设 100w"):**
  - pred 是纯分数**(无资金/无股数维度)**, 与训练阶段(2008~ test 段, 也无资金)完全解耦; 资金只存在于**回测** `port_analysis_config.backtest.account` (rolling_config.yaml:11, 100w)
  - 实测: `_parse_target_stocks` 的 scale=max_total/总真实市值 **天然抵消 account** (account 只线性放大 amount, 不改变相对权重)。account=100w vs account=20w(amount×0.2) 缩放到20w → **持仓完全一致(19只), 赣锋两种都买不进** → **改回测 account 无法规避整手化死角**
  - 根因: qlib 回测输出的是**相对权重**而非"某资金下整手可执行解"; 整手化必然发生在外部 `_to_lots`, 与 account 解耦
- **偏贵价股跑不赢理论 (系统性偏差, 量化)**: 小资金配不了一手的贵价高分股被丢 → 市场偏贵价股时 20w 实盘**系统性低配成长龙头**、跑输理论回测:
  - **权重漏损** (sum of 权重 of 目标市值配不起一手者): **20w 漏 34.6%** (中际缺1388%/新易盛缺893%/兆易缺450%/深南缺727%/江波龙缺437%, 连芯原5.8%重仓/东山/华工也配不起一手), **60w 漏 14.9%** (中际缺396%/新易盛/兆易/深南), 全覆盖需资金 ≥ ~**300w** (中际 ¥85k一手 ÷ 2.9%权重)
  - **与 §7d 矛盾不冲突**: §7d"小资金避贵股反赚更多"是**统计性巧合**(50w恰好避开高波动拖累股), 非策略性胜利; 一旦市场风格切到"贵价=高质量成长" (贵价高分), 20w 就是系统性丢 alpha
  - **结论**: 20w/60w 实盘模拟盘只能反映"该资金下真实可获收益", **不能直接对标模型理论能力**; 评估模型真实水平须用多账户回测 (§7c: 100w/400w/1000w), 小资金实盘仅作"信号健康监控"
  - **贵价股历史贡献归因实证 (2026-08-26 补, 可靠口径)**: 修正了先前"20w 系统性丢 alpha"的推测。方法: qlib 原生 `positions_normal_1day.pkl` 解析持仓 + **期初权重(前一日 weight)× 当日收益** (qlib `$close` pct_chg, 非 positions CSV 的 price——后者含前复权断点污染, pct_change 会爆出 +1208% 假极值), 重构复利 +23.04% vs report +21.37%, corr 0.9975 → 可信。近1年(2025-08~2026-08) 31只目标中:
    - **60w 档被丢 6 只贵价股: 净贡献 ≈0** (新易盛+0.39/兆易+0.13/中际+0.13/江波龙+0.02 pp 正贡献被深南-0.26/东山-0.42 抵消) → 被丢弃**没损失 alpha**
    - **20w 档多丢 6 只中价股: 净贡献 -2.41pp (负)** → 20w 丢它们**反而躲过芯原-1.42/东山-0.42/阳光-0.40/华工-0.38 等拖累**, 唯一真实损失是新易盛(+0.39pp, 占比<2%)
    - **修正先前担忧**: 近1年样本里"丢贵价股"不构成实质亏本, 20w 反而微赚; 但局限是**仅近1年 + 贵价集用当下持仓回看**, 若市场切换"贵价=强势成长"结论可能翻转
  - **0.8手向上取整实验 (2026-08-26, 未改码, 仅记录)**: 在 `_to_lots` 加规则"缩放手数 ≥ 阈值 (0.9/0.8…) 向上取整到一手"以救回"差一点不到手"被 floor 归零的中价股, 量化对资金利用率 (花呗/投入额 max_total) 的影响 (2026-08-21 target + 腾讯实时价):

    | 资金 | floor | 0.9手 | **0.8手** | 0.7手 | 0.5手 |
    |---|---|---|---|---|---|
    | 20w | 49.9% | 55.8% | **62.9%** | 62.9% | 73.1% |
    | 60w | 68.4% | 74.9% | **74.9%** | 74.9% | 88.2% |
    | 100w | 78.8% | 78.8% | **86.8%** | 86.8% | 90.4% |

    - 0.8手新增买进: 20w→亿纬300014(+¥5.5k)/赣锋002460(+¥5.4k)/指南针300803(+¥8.7k)/300442; 60w→芯原688521(+¥38.7k)。**60w 档 0.9 与 0.8 相同** (只有芯原); 100w 在 0.8 才补进江波龙301308/兆易603986 (各+¥39k)
    - **0.7 与 0.8 完全一样** (三档均无新触发, 0.7~0.8 间无待补股) → 0.8 是甜点
    - **结论**: 0.8 在三档资金都有提升 (20w→63%, 60w→75%, 100w→87%), 是"利用率 vs 权重畸变"的平衡点; 0.5 激进 (20w→73%/60w→88%/100w→90%) 但把半只手都补成整手, 每只超配目标 50-100%+, 畸变更深、破坏 scale 守恒。**技术成本低但收益轻微**: 只救回 1-4 只中价股, 大头闲置仍在"一手太贵配不起"的贵价股。未改码
  - **回测层剔除贵价股思路 (2026-08-26 用户提出, 未实现, 规划)**: 既然 `sync_to_realtime` 层的 floor 只能救回中价股、解不了贵价股, 更彻底的做法是**在回测源头就限定额子资金 + 剔配不起的股**, 让模拟盘口径 = 理论回测口径:
    - **定义**: 限定账户资金 (如 account=20w); **贵价股 = 一整手市值相对该股目标权重偏离 ≥5%** (一手目标市值 = max_total×权重, 一手真实价×整手 ≥5% 偏离即判贵价 → 配不起一手)
    - **实现设想**: 在 `custom_handler.py` 的选股/回测层加过滤器, 或 rolling 训练后 target 里剔除贵价股再回测; 这样 pred 排名/权重在源头就不含配不起的股, 而非外部 `_to_lots` 事后砍
    - **用意**: 修正"实盘模拟盘跑输理论是整手/贵价丢弃造成"的系统偏差, 使小资金回测能真正代表该定子资金下的可得收益

### 7d-ter. 回测层剔除贵价股 `ExpensiveFilterICTimingStrategy` → 已验证, 不建议纳入 (2026-08-27)

- **实现** (`custom_handler.py`, 纯新增, 继承 `ICTimingStrategy`): 覆写 `generate_trade_decision` 复刻 qlib TopkDropout, 在**新买入候选**中剔除"贵价股"、不卖已持有、名额顺延 (与 §7d-bis 规划一致)
  - **贵价判定**: `_is_expensive` = `get_deal_price(close) × trade_unit(100) >= account/topk × (1+expense_deviation)`; 配置 `account=200000`, `expense_deviation=0.05`, `trade_unit=100`
  - 配置文件 `rolling_config_20w.yaml` (baseline 20w, ICTiming) + `rolling_config_expfilter20w.yaml` (expfilter, ExpensiveFilterICTimingStrategy); 两者都设 `backtest.account=200000`; 复用 `rolling_models_20260822155347` ensemble pred, `--skip-train`
  - 实验: `rolling_csi300_lgbm_20w` (823403817055318923)、`rolling_csi300_lgbm_expfilter20w` (702341441852413333)
- **⚠️ 价格坐标认知修正 (重要)**: 回测 `get_deal_price`/close 是**前复权价** (上市首日=1)。大多股票前复权一手只有几百元 (p50≈573)，但**绝对高价股在真实市场也是贵价** (茅台前复权一手 34075 / 中际旭创 39594 / 新易盛 33752，仍远超等权目标 6667) → 前复权价**能识别并剔除茅台/中际旭创/新易盛/兆易等真·高价股**, 但无法涵盖所有"真实配不起一手的股票" (上市晚/低价股的 real 价 vs 前复权差异被压缩)。语义是"剔除回测内绝对一手高价股", 与真实价口径部分一致。
- **过滤器生效验证**: baseline 累计持有贵价股 1579 day-rows (3.3%)/16只 → expfilter 162 day-rows (0.3%)/11只, **-90%** → 判定真实起作用
- **结果** (account=20w, 2020-2026, 净成本):

| 指标 | baseline-20w | expfilter-20w | Δ |
|---|---|---|---|
| 年化 | 20.82% | 21.03% | +0.21pp |
| 超额年化 | 17.86% | 18.19% | +0.33pp |
| 净IR | 1.405 | 1.414 | +0.01 |
| 最大回撤 | **-24.37%** | **-26.07%** | **-1.7pp (变差)** |
| 换手 | 15.4 | 15.6 | 持平 |

- **年度分解 (极不稳定, 关键)**:

| 年 | baseline超额 | expfilter超额 | Δ |
|---|---|---|---|
| 2020 | 2.76% | 8.91% | +6.2pp |
| 2021 | 24.81% | 28.16% | +3.4pp |
| 2022 | 34.98% | 31.22% | **-3.8pp** |
| 2023 | 17.71% | 11.89% | **-5.8pp** |
| 2024 | 30.49% | 35.64% | +5.2pp |
| 2025 | 2.81% | 5.18% | +2.4pp |
| 2026 | 14.41% | 5.94% | **-8.5pp** |

- **结论**: 方向在年份间剧烈翻转 (+6.2pp → -8.5pp) = 高方差非稳定信号; 收益几乎无净改善 (+0.3pp) 却**回撤每年几乎都略差** (2021/2023/2024/2026)。与 §7d-bis 归因完全呼应: 贵价股净贡献≈0、不构成实质亏本 → 剔除无得、反添波动。**不建议纳入生产**；代码/config/实验保留供参考, 未 commit 策略启用 (min 结论同 7e volume: 视觉上"更合理"的改动实测中性偏负)
- **当前实盘判定 (致函数)**: 20w 模拟盘的整手/贵价闲置是**结构性问题**, 但它**不构成 alpha 损失** (实测贵价股近1年贡献≈0); 模拟盘跑输理论更多来自整手离散噪声+单票集中度, 而非"贵价被丢"。评估模型真实水平仍用多账户回测 (§7c/§7d), 小资金实盘仅作信号健康监控

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

### 10. 科技行业倾斜选股 (TechTiltICTimingStrategy) → 已验证样本外稳健, 暂存不上生产

#### 动机
- 之前分析出"中际旭创/兆易创新多次进整体 top30 却因 n_drop=1 换股僵化买不进池子"
- 尝试**选股层面给科技类加权**的倾斜, 而非改模型/改换股名额

#### 实现 (custom_handler.py, 纯新增 88 行, 不触碰生产 ICTiming)
- `_TiltedSignal`: 包装原始 pred signal, 在 `get_signal` 输出时给科技类加分
  - 加分方式 = `tilt × 当日横截面 std` (**相对缩放**, 非绝对加分)
  - **必须相对缩放**: score 尺度在 2025-09 有断崖 (std 0.13→0.004, 退化 1-tree 残留), 固定绝对加分近期会失效
  - 兼容单层 index (回测 get_signal 返回单日 instrument 横截面) 与多层 (跨日)
- `TechTiltICTimingStrategy(ICTimingStrategy)`: 覆写 `_precompute_ic` 用 **UNTILTED raw signal 算 IC, 倾斜**不污染择时信号; 仅选股排序用倾斜后的 score
- **科技类定义** (证监会行业):** `C39`(661)+`I65`软件(334)+`I64`互联网(60)+`R86`新闻出版(29)+`R87`广电(20)+`I63`电信(21) = 1125 只; 中际旭创 C39、兆易创新 C39
- 用法: 配置文件 `rolling_config_techtilt.yaml` (strategy class=TechTiltICTimingStrategy, tilt=NN); `oos_techtilt.py` 支持分段回测 (绕开 run_rolling 强制 end_time=latest)
- `run_rolling.py` 的 add_safe_class 注册两行**已移除** (未纳入生产入口)

#### 全量回测结果 (2020-2026, 复用 rolling_csi300_lgbm_ndrop1 的 ensemble pred)

| tilt | 净年化 | 净IR | 回撤 | 中际旭创持仓天 | 兆易持仓天 |
|---|---|---|---|---|---|
| 0 (baseline) | 17.87% | 1.44 | -18.4% | 78 | 181 |
| 0.5 | 18.27% | 1.43 | -16.8% | 149 | 284 |
| **0.75** | **18.60%** | **1.45** | **-16.7%** | 266 | 375 |
| 1.5 | 17.02% | 1.16 | -16.8% | 302 | 697 |
| 2.0 | 13.90% | 0.88 | -20.4% | 336 | 728 |

- 全量段 tilt=0.75 表面最佳, 但**这是数据窥探陷阱** (见下)

#### 样本外验证 (关键方法学胜利)
- **选参段 2020-2024** 扫 tilt 定甜点 → 结果 tilt=0.5 (非 0.75!):

| tilt | 2020-24 净年化 | 净IR |
|---|---|---|
| 0 | 19.41% | 1.66 |
| **0.5 (选定)** | **20.25%** | 1.63 |
| 0.75 | 17.01% | 1.35 (明显变差!) |
| 1.5 | 15.48% | 1.09 |

- **检验段 2025-2026** (tilt 已固定, 从未见过):

| tilt | 净年化 | 净IR | 回撤 |
|---|---|---|---|
| 0 | 14.85% | 1.01 | -19.3% |
| **0.5 (OOS 选定)** | **23.19%** | **1.50** | **-17.0%** |
| 0.75 | 27.56% | 1.83 | -17.3% (更好但靠窥探) |

- **2022 科技熊市年** (抗风险校验): tilt=0.5 净18.79%→19.56%, 回撤改善(−10.17%→−9.41%), IR 1.50→1.44 (轻微)

#### 结论
- **tilt=0.5 是干净的样本外赢家**: 选参段年化且IR持平, 检验段 +8.3pp、净IR 1.01→1.50、回撤改善 2.3%; 连 2022 科技熊市都不亏 → **非过拟合, 方向(科技倾斜)三段一致为正**
- **tilt=0.75 是过拟合陷阱**: 全量段"最佳"纯因 2025-2026 半导体β; 选参段(2020-24)它反而差 (IR 1.66→1.35)。**不能按全量直接上 0.75**
- 适度倾斜(0.5) 既不伤风格又抓科技超额; 强倾斜(≥1.5) 科技过度集中, IR<1.2 且回撤加深
- **状态: 代码保留可启用(暂存), 未 commit/未 push/未部署 244, 生产 config 仍用 ICTimingStrategy 不启用倾斜**; 若日后启用, 推荐 tilt=0.5, 并监测 2026 下半年半导体是否退潮 (tilt>0.5 的超额集中近 1.5 年)

### 11. 白酒单独降权 (IndustryAdjustStrategy + BAIJIU_CODES) → 已验证, 不建议纳入

#### 动机
- 白酒在证监会行业分类归入 **C15 酒、饮料和精制茶制造业** (与啤酒/饮料/茶混同), 需**单独降权**而非整类
- 用白酒证券代码白名单 (`BAIJIU_CODES`, 21 只), 避免误伤啤酒/饮料/茶

#### 实现 (custom_handler.py, 纯新增, 不触碰生产 ICTiming)
- `_AdjustSignal`: 通用加权包装器 (泛化自 `_TiltedSignal`), 接受 `code -> weight` 映射, `score += weight x 当日横截面 std`
  - weight>0 = 提升选股 (科技倾斜), weight<0 = 降权; 保留单层/多层 index 兼容
  - `_TiltedSignal` 保留为 back-compat 别名 (科技倾斜不受影响)
- `IndustryAdjustStrategy(ICTimingStrategy)`: 通用多行业加权, `weight_rules=[(行业前缀|代码, w)]`; 内置 `baijiu_codes` 默认 = `BAIJIU_CODES`, `baijiu_weight` 默认 -0.5
- 用 `_AdjustSignal` 同样覆写 `_precompute_ic` 用 raw signal 算 IC (加权不污染择时)
- `oos_techtilt.py` 加 `--class IndustryAdjustStrategy` 支持白酒降权回测; **run_rolling.py 未注册该类** (不纳入生产入口)

#### 数据观察 (baseline ICTiming, 2020-2026)
- 白酒 8 只 (茅台/五粮液/泸州老窖/洋河/汾酒/古井/今世缘/顺鑫) 常年进持仓: 每年 30~177 持仓天 (2020 占 73%)
- 单日白酒总权重最高 14.5%, 有白酒日均值 5.25%; 汾酒最大单股权重 7.6%

#### 回测结果 (净成本, 复用 rolling_csi300_lgbm_ndrop1 ensemble pred)

| 降权档 | 全量 净年化 | 全量 净IR | 全量 回撤 | 2025-26 净年化 | 2025-26 净IR |
|---|---|---|---|---|---|
| **0 (真基线)** | **18.28%** | **1.47** | -18.4% | **14.85%** | **1.01** |
| -0.25 | 16.60% | 1.33 | -17.9% | 14.48% | 0.97 |
| -0.5 | 17.74% | 1.45 | -16.7% | 26.00% (噪声) | 1.75 (噪声) |
| -1.0 | 17.73% | 1.42 | -16.9% | 17.62% | 1.21 |

- **⚠️ 基线坑**: 初版 `IndustryAdjustStrategy` 默认 `baijiu_codes=BAIJIU_CODES`+`baijiu_weight=-0.5`, 导致 tilt=0 档也偷偷降权 (-0.5 档与 0 档结果相同 = 基线错误)。已在 `oos_techtilt.py` 修正: tilt==0 时传 `baijiu_codes=[]` 禁用默认降权

#### 结论
- **白酒降权无正收益**: 全量段任何降权档 ≤ 基线 (18.28%), 基线最优; -0.5/-1.0 回撤略降 (18.4%→16.7%) 被收益下降抵消更多 (净IR 1.47→1.42)
- **检验段非单调** (-0.5 跳升 26%、-0.25 反而最差) 是整手离散噪声, 不可信
- **与科技倾斜鲜明对比**: 白酒降权腾出的仓位被相似消费股承接, 净效应中性偏负; 白酒并非组合拖累 → **不建议纳入生产**
- **状态: 代码保留可复用 (`IndustryAdjustStrategy`+`_AdjustSignal`+`BAIJIU_CODES`), 未 commit/未 push/未部署 244, 生产 config 仍用 ICTimingStrategy**

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
- **max_total 全资产投入方案 (2026-08-13 起, 研究机)**: 实盘已**清仓**, 转入模拟盘观察, **不再保留现金垫**。`sync_to_realtime.py`: `max_total = total_assets` — 全部资产投入目标组合。历史参考 (2026-08-04~08-12 分批建仓固定现金垫 40w): `max_total = max(0.0, total_assets - 400000)`, 相比 `invest_ratio` 避免盈利被强制兑现稀释。max_total 是目标组合市值上限, `_parse_target_stocks` 按 `scale = max_total / 目标总市值` 等比缩放所有股票

### 集合竞价短线增强 (auction_factors.py, 2026-08-17)

- **来源**: 知乎 38812088 回答, 两个因子基于 A股"隔夜弱、日内强"结构性规律, 用 9:20-9:25 不可撤单时段的逐笔委托构造
- **严格因子 (需 L2 逐笔委托)**: `compute_auction_factors(orders, avg_5d_amount)` 
  - `auction_buy_strength` = (Σ下单价>卖一价的买单金额 − Σ下单价<买一价的卖单金额) / 过去5日平均日成交额
  - `retail_sell_strength` = (Σ散户卖单 − Σ散户买单) / 5日均额 (散户反向指标)
  - 主动方向判定优先价格比较 (ask1/bid1 原文口径), 无价退回 side 字段
  - 当前无 L2 数据源, 这部分是纯计算函数待接入
- **腾讯快照近似 (生产可用)**: `fetch_tencent_snapshot` + `compute_tencent_approx_factors`
  - 批量抓取 (50只/请求), 返回现价/今开/昨收/外盘/内盘/买卖一挂单
  - `auction_buy_strength ≈ gap*1e4*(1+0.5*(2*bid_ratio-1))`, gap=(今开/昨收-1) 是主信号, 挂单失衡只作同向调幅**不翻转符号** (低开始终弱)
  - `retail_sell_strength ≈ inner/(outer+inner)-0.5` (内盘占比, 主动卖盘强弱)
  - ⚠️ 时间口径: gap 用今开/昨收整天不变 (任意时点都是竞价定调); inner/outer 反映抓取时刻累计, 盘中/盘后含义不同
- **sync_to_realtime 集成 (短线过滤器, 2026-08-17 重构)**: 从"只影响排序顺序的增强"改为**真正切割目标名单的过滤器**
  - 候选池 = qlib 全部可买 target 股票; 按 auction_buy_strength 升序取末 N (auction_top_cut 默认5)
  - ⚠️ **小池保护 (重要)**: 候选池 ≤ 裁剪数时只裁末 `len-1`, **始终保留因子最强 1 只**, 避免组合被裁光 → 当天 0 买入/全卖
  - ⚠️ **SAFETY GUARD (重要)**: 腾讯行情全挂时 (snapshot 为空) **中止 sync**, 不清仓 — 否则 target_stocks 为空会被当"qlib调出"把全部持仓卖光
  - 末N处理: **已持仓→全卖清仓**; **无仓位→不下单(跳过)**; 其余正常买卖/调仓
  - **散户豁免** (retail_exempt_threshold): 末N中 retail_sell_strength 超过阈值的豁免裁剪 (散户反向看多, 原文语义)
  - **qlib 无条件卖出不受影响**: 实际持有但不在 target 的照常全卖
  - 一次批量快照同时供 实时价+限价单参考价+近似因子
  - 参数: `--auction-filter False` (**默认关, 只观察, 暂不实际裁剪** — 用户在积累胜率数据后决定是否开启) / `--auction-top-cut 5` / `--retail-exempt-threshold` (默认 None 不豁免)
  - **观察模式 (auction_observe, 默认 True)**: 算因子 + 完整打印"若开启会裁剪谁"的决策 + 把每日决策 append 到 `auction_observe_log.csv`, 但**不修改目标名单** (正常按 qlib 下单)。唯一目的 = 积累胜率统计样本。CSV 列: date/code/auction_buy_strength/retail_sell_strength/would_cut/held; 日后用下日收益对比 would_cut=True vs False 两组算胜率
  - **接口防护 (重要)**: 全流程腾讯行情只调**一次**批量快照 (目标+实际持仓合集, ≤50只/请求), 严禁逐股调用 → 防封号。原 `_load_real_prices` 二次调用已合并
  - **debug 打印 (2026-08-17 加, 因操作者隔日不在场)**: 完整输出因子横截面排序表 (*标末N)、每只裁剪原因 (全卖/跳过/豁免)、卖出原因标注 (因子裁剪 vs qlib调出)、过滤器决策摘要 (候选数/裁剪数/全卖/跳过/豁免/保留数 + [实际执行/仅观察] 标注)
  - **集合竞价抓取定时 (2026-08-17 加, 08-18 改为 09:30)**: 定时任务 09:19 启动。2026-08-18 起 `--auction-wait-until` 默认改 **09:30:00**(开盘后抓):竞价段(9:15-9:25)内外盘/均价因无真实成交恒为0/无效,开盘后才有真实累积值;gap(今开)9:25 定格全天不变仍有效。仅在 [09:00, wait_until) 时段等待, 过点/盘后/手动补跑不等待 (不会跨午夜)。`--auction-wait False` 可关
- **科创板委托手数规则 (2026-08-18 加, 重要)**: 科创板 (688/689) 与主板不同 — **买入单笔须 ≥200股** (超出可按1股递增), 卖出若持仓余额 <200 须一次性全卖 (零头只能整体清仓)。`sync_to_realtime.py` 已按 `_lot_size(code)` 区分 (688/689=200, 其余=100):
  - `_to_lots` 按对应 hand 数向下取整, 科创板 <200 股直接归 0 (不买, 避免违规 x100 买单)
  - BUY+/SELL- 调仓差 <200 时: BUY+ 跳过加仓; SELL- 改为全卖剩余 (而非卖零头)
  - 背景: 08-18 全天出现 `BUY+ 688472 x 100 -> FAIL` (科创板买100股被交易所/券商拒); 修复后此类下单合规

### 分批建仓方案 (关键变更: 2026-08-13 已清仓转模拟盘, 此方案归档)

- **状态**: 分批建仓阶段已结束。用户 2026-08-13 **清仓实盘, 转入模拟盘观察**, 不再保留现金垫 (去 40w 垫, max_total=total_assets)
- **目标组合稳定性 (已验证)**: 回测 `account: 1000000` (100w) 是 config **固定常量**, lastday CSV 里各股 amount/weight 是**相对权重**, 与实盘账户资金**无关**。重跑回测只要数据/模型不变, 相对权重完全一致 → 目标名单稳定
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

- (已结束) 分批建仓 (固定现金垫 40w) → **2026-08-13 已清仓转模拟盘**; 当前 `max_total = total_assets` 全资产投入; 模拟盘观察期看策略偏差/IC 有效性
- (可选) 观察期后重新启用 QlibDailyFull 定时任务 (当前已禁用)
- (可选) IC 长期失效监控与自动暂停机制 — 已在 ICTiming 内降仓, 极端情况需暂停
- (可选) 更高频/另类信号源 (分钟级) — 机构暴力来源, 需新数据
- (可选) 减少训练窗口减轻 swap 依赖

## 08-22 冻结 bug 的第二处根因: skip_train 把 daily_predict 延展 pred 覆盖掉 (2026-09-07 修复, 重要)

- **症状**: 244 实盘 2026-09-02/03/04 连续三天 sync 日志 `To sell: 0 / To buy: 0` (target==actual==27), 持仓冻结不出九月决策
- **真根因 (与 08-22 "pred 只到重训日" 同根, 但这次是重训间隔内新日期再被覆盖)**:
  - 每日流程 run_workflow.py: **STEP2 daily_predict** 把新交易日 pred append 到 combined 实验最新 run 的 pred.pkl (延展到今日), **STEP3 run_rolling --skip_train** 重新 ensemble。
  - 旧的 --skip_train 代码开头 `R.delete_exp(exp_name)` 删掉 combined 实验, 再从**静态 rolling (训练) 模型** 重新 ensemble → daily_predict append 的新日期被丢弃, pred 被重置回「最后滚动测试窗口 (重训日, 如 08-21)」→ 回测/实盘拿不到 08-22 之后决策 → 目标冻结。
  - 所以 08-22 修到的 daily_predict 在每次 --skip_train 后都被 erase, 只到下一次重训才靠新 rolling 窗口补回 → 重训间隔越长冻结越久 (本次 ~2 个月只有一个 rolling exp 迭代到 08-21)。
- **修复 (commit d3cf4f2a / 244 d3cf4f2ae, 改 STEP3 接线 + 不改训练, 无重训)**:
  1. `qlib/contrib/rolling/base.py` `_ens_rolling(extra_pred=None)`: 有 extra_pred 时先把既有 combined pred (已被 daily_predict 延展) 与 rolling ensemble **concat + dedup keep latest + sort**, 再存进新 run → combined pred 持续前进而非被重置。
  2. `examples/rolling_csi300/run_rolling.py` --skip_train 分支: 去掉 `R.delete_exp`; 新增 `_latest_combined_pred(exp_name)` 读最新 combined run pred.pkl (与 daily_predict 相同 runs[-1] 语义) 作为 `extra_pred` 传入。
  3. 硬化 `_find_latest_rolling_exp`: 只选「pred.pkl 齐全 run 数达候选最大值」的完整 rolling 实验 (按 creation_time 新的完整者), 避免被**重训中断/半成品** (无 pred 窗口) 抢先, 否则 ensemble 只剩前几期只盖 2020。
- **验证**:
  - merge 数学单测: 相同 (datetime,instrument) 保最新、新日期追加、sort — 通过
  - 冒烟 ens 非破坏: 往已存在 combined 实验新增 run (不再 delete 重建) — 通过
  - exp-picker: 本机现在返回 `rolling_models_20260831183707` (完整 27窗口), 不再误选半成品 — 通过
  - 244 已 deploy (bundle fast-forward), base.py/run_rolling.py sha256 与本机一字不差
  - **真·end-to-end 延展到 9 月待 244 重训模型后首次新数据日回归** (本机无能预测 9 月的完整模型: 最近完整 retrain 为 08-22/08-31, 窗口只到 08 月底; 09-07 自动重训 OOM 半途, 见下)
- **部署注意**: research 与 244 commit 因 committer 身份不同 SHA 不同 (d3cf4f2a vs d3cf4f2ae), 内容经 sha256 一致; 后续 bundle 对比别再依赖 SHA 相等。
- **相关半成品运维**: 09-07 07:33 自动 monthly retrain `rolling_models_20260907073326` 因内存 OOM (`BrokenProcessPool`) 只训成 3-4 个 2020 窗口即中断 (无 ens)。已把该实验目录移出 mlruns 至 `examples/rolling_csi300/mlruns_cache_aborted_20260907/` 隔离 (<b>勿把任何目录直接放 `mlruns/` 下非数值名, mlflow 会当实验目录扫)</b>. 生产仍用 `rolling_models_20260822155347` (244) / 本机最新完整 `rolling_models_20260831183707`.
- **健康度提醒**: 09-07 起研究机 auto-retrain cron 已因 OOM 中招 (AGENTS 记载训练峰值需 ~23GB swap, 现仅 4GiB)。**补跑前先扩 swap** 再跑 monthly_retrain.sh, 否则每次重训都失败 → combined pred 永远停在老模型末端 → 本修复无真 new-data 可用。

## 回撤邮件提醒 (dd_alert.py, 2026-08-13)

### 功能
- **语义**: 策略回撤 (按**全历史峰值** `cum_return/cummax-1` 算, 取末值) 每新突破一个 5% 档位 (5%→10%→15%…) 发一封简单中文邮件; 同一档位不重复 (跨天持久化)
- **数据源**: 回测权益曲线 `report_normal_1day.pkl` (实验 `rolling_csi300_lgbm`, 复用 analyze_equity_curve.py 找最新 recorder 逻辑)
- **状态文件**: `examples/rolling_csi300/dd_alert_state.json`, 存 `{"last_tier": n}`; 回撤恢复后再次加深到更高档才发
- **邮件**: `send_email.py` (QQ SMTP-LL 465), 纯文本 body (当前回撤/档位/数据截至日), 无 HTML/附件
- **集成**: `run_daily.ps1` 在 equity curve 分析后调用 `dd_alert.py --exp_name rolling_csi300_lgbm` (成功分支+重启后分支都加了)

### EMAIL_PASSWORD 获取 (关键)
- 密码存在 **244 Windows 用户环境变量 `EMAIL_PASSWORD`** (backtrader 项目 bat 也是靠它, 故邮件可正常发)
- **WSL 读不到** Windows 用户环境变量 (WSLENV 桥接不可靠); `dd_alert.py` 用 `_ensure_email_password()` 兜底 — 优先取 WSL env, 否则 `cmd.exe /c echo %EMAIL_PASSWORD%` 经由 Windows interop 读 Windows 用户环境变量 (已测 244 上 len=16 解析成功)
- 已设 Windows 用户 `WSLENV=EMAIL_PASSWORD/U` (计划内桥接, 但不依赖它); 本机研究机跑 dd_alert 时需自行设 `EMAIL_PASSWORD` env

### 已测
- 档位逻辑: run1 触发→state=5; run2 去重不重复发; 复原不改档不发; 加深触发 (本地 monkeypatch 验证)
- 生产 dry-run: report end 2026-07-31, 当前回撤 0%, 正确不发
- 244 端到端: 强制 -14% 回撤 → "邮件发送成功！" + state 写入 last_tier=2 → **实际邮件已发送成功**
