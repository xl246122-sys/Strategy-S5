# Binance Stocks V2.2 — S5 尾盘极端下跌 / 盘后反弹策略增量开发手册

> **文档性质**：独立增量开发方案 + 可逐阶段复制给 Cursor / Grok 4.7 High 的执行 Prompt  
> **方案版本**：`V2.2-S5 / Plan 1.0`  
> **编制日期**：2026-11-03  
> **现有项目**：`D:\Strategy` 中已推进到 Phase 14 的 Binance Stocks V2.2；原 Stage 0–14 开发成果视为**待验收的既有工程基线**，不等于 Phase 14 终验已经通过。  
> **研究状态**：`S5 = RESEARCH_ONLY`, `live_enabled = false`；不授权真实下单、撤单或更改实盘进程。  
> **对接方式**：每次只发 **一个** S5 阶段 Prompt；每阶段形成报告、验收后再进入下一阶段。

## 0. 执行摘要与决策

**目标**：在不推倒既有 V2.2 的前提下，新增 **S5 — End-of-Day Selloff Reversal / After-Hours Recovery**。该策略识别临近美股常规收盘时出现的异常抛售、卖压衰竭和有限的短期修复机会，研究入场后在盘后或符合条件的夜盘退出、获取 **净收益 +1.0% / +1.5% / +2.0%** 的可能性。目标收益是**实验参数**，不是盈利承诺。

**特别强调**：S5 与原 S4（RTH VWAP / RSI(6) / OU 日内均值回归）是两项不同研究。S4 的 OU 半衰期 ≤25 分钟、`live_enabled=False` 等规则保持不变；S5 不继承 S4 的回测结论，也不把“暴跌”当作“应该买入”的充分条件。

**本轮决定**：

1. **冻结基线**：保存 V2.2 当前 Git SHA、配置、依赖锁文件、数据库 schema、既有测试与阶段验收资料。S5 在独立分支和独立数据库中开发。
2. **先验证数据与交易资格，再写数学策略**。如果无法得到盘后和夜间可复现成交证据，S5 只能做实时影子模拟，不能以常规交易时段回测冒充夜盘可交易回测。
3. **现有 S1–S4 完全不变**。原先 `15:45 ET` 停止日内开仓、`15:55 ET` 前置强平，仍对 S1–S4 生效。新 S5 的跨收盘权限必须通过策略标签和独立状态机受控实现，不能在全局关闭强平。
4. **单一风险权威**：S5 复用 V2.2 的 SignalArbitrator、LimitOptimizer、PortfolioRisk、ExecutionGateway、Guardian 和账户对账，资金与仓位不能按策略分别重复计算。
5. **先形成 S5-G/NO-GO 报告，再决定是否申请上线**。本手册不包含任何自动启用实盘交易的授权。

### 0.1 现有设计继承与冲突点

| V2.2 已有内容 | S5 增量处理 | 不得改变的约束 |
|---|---|---|
| S1 Kalman/RLS、S2 VWAP/RSI、S3 ORB、S4 OU | 新增独立 `s5` 包；不覆盖 `s4` | 4 个既有策略的参数、信号逻辑、代码与报告不可被隐式修改 |
| Stage 2 实时 WS + SQLite WAL | 增加 `RTH/POST/OVERNIGHT/PRE` 分会话采集和重放 | 原 WAL 恢复和数据陈旧度检测不回归 |
| Stage 3 历史 Alpaca SIP | 单独的 S5 时段覆盖率审计和市场分布偏移报告 | 源数据只读、时点正确、不得填造缺失夜盘 K 线 |
| Stage 4 保守撮合 | 补充盘后 bid/ask、盘口深度、限价排队与跳空测试 | 触价不等于成交，同柱 TP/SL 止损优先/1m 验证 |
| Stage 5 市场状态 | S5 用独立个股抛售状态；全局 `UNKNOWN/HIGH_VOL_PANIC` 依然禁止开多 | 不为增加交易次数放宽全局风控 |
| Stage 10 仲裁器 | 新增 S5 `strategy_id` 和同标的冲突仲裁 | 全局唯一资金/仓位槽位；不重复买入 |
| Stage 11 限价优化器 | 增加 `EXTENDED`、符合资格时 `24H`、`GTC/DAY` 研究与模拟 | 开仓禁 BUY MARKET、金额用 Decimal、手续费/价差门禁 |
| Stage 12 Guardian | 按 `position_policy` 区分 RTH-only 与允许跨收盘的 S5 | 原 15:55 规则不得被整体关掉，绝不能无保护持仓 |
| Stage 13 账户总风控 | 新增尾盘集中持仓、流动性消失及跨收盘价格跳空风险 | 原 0.25% 单笔风险预算、20% 单仓上限、5 仓上限、8% HWM 锁不得放宽 |
| Stage 14 Lockbox | S5 实验重新建立独立的测试治理记录 | 不假装既有 Lockbox 对新策略仍是未接触的测试集 |

> **状态澄清**：用户最新截图表示 Cursor 正在排查 Phase 14 中 Binance 5 分钟线与 SIP 重叠样本的覆盖问题；“推进到 Phase 14”不等于已提供 G14 通过证明。S5 的工作流必须先核实，而不是声称 V2.2 已投产。

## 1. S5 产品边界和时段

### 1.1 官方文档支持的时段（美东 ET，按 NYSE 日历 / DST 处理）

- `RTH`：09:30–16:00，正常交易时段。
- `POST`：16:00–20:00，属于 `tradingSession=EXTENDED` 能覆盖的部分。
- `OVERNIGHT`：20:00–次日 04:00，**只允许标的支持 `overnightSupported=true` 且 API 实测认可 `tradingSession=24H` 时参与**。
- `PRE`：04:00–09:30，盘前；第一版仅用于必要的超时/事故处置或研究，不主动扩展为新的盘前策略。
- 周五、周末、节假日、半日交易日另按官方日历和产品规则计算；不得简单按固定 UTC 小时滚动。

**准入条件**：启动或每日刷新 `/sapi/v1/equity/market/exchangeInfo` 的 `symbol`, `tradability`, `overnightSupported`, `extendedSession`, `fractionableEh`, `stepSize`, `minQty`, `minNotional`, `multiplierUp/Down`。禁止把静态“所有股票都能 24H”当作事实。限价单才有对应延长时段执行能力；Market 单在盘后/夜盘可能排队到下一个 RTH，不能当作立即止损手段。

### 1.2 分成 3 条**互不混淆**的实验臂

| 实验臂 | 扫描 / 计划入场 | 计划退出 | 目的 |
|---|---|---|---|
| `A_RTH_TO_POST` | RTH 15:30–15:54 ET 观察并选择可行入场（仅模拟，未来须专用豁免） | 优先 16:00–19:30 POST | 最贴近“尾盘买、盘后反弹卖” |
| `B_POST_TO_POST` | 15:30–16:00 观察，16:05–17:00 POST 才考虑入场 | 19:30 前 POST 完成 | 避开最后几分钟撮合拥堵 |
| `C_POST_TO_OVERNIGHT` | POST 开仓且 `overnightSupported=true` | 20:00–03:30 仅 `24H LIMIT` | 检验真正夜盘的额外收益与流动性成本 |

上述时间窗都是**初始研究窗口，不是实盘授权**；模型应根据交易日历、产品交易状态自动裁剪。必须给每笔样本标记实验臂及实际可交易时段；不得把 A/B/C 合并成一个胜率数字掩盖差异。原则上以 A、B 优先，C 数据充足、滑点可信后再评估。

### 1.3 产品和合规约束

- 原项目定位为 `/sapi/v1/equity/*` 的 **Binance Stocks equity** 接口。`tokenize=false` 与截图中 `INTCB` 的 **bStocks 代币化证券** 不是同一层概念；不可把 bStocks 价格截图直接转换为 equity API 的可成交收益。
- 不改变原 `walletType=CARD`, `quoteAsset=USDC`, `tokenize=false`, 不做加密货币/合约/杠杆/空头/补仓。
- 接口存在不等于用户必定有资格访问交易；上线前须单独验证账户身份、地域限制及产品可用性。bStocks 的地域限制尤其不能以服务器所在国家替代使用者资格判断。
- 任何资格缺失、API 行为不明或标的无法交易 → `S5_HALT_NEW_ENTRY`；持仓风险处理走 Guardian 的人工升级/授权规则。

## 2. 数学研究规范（不是交易信号的既定真理）

### 2.1 候选筛选：极端下跌必须**相对**其历史波动来定义

对于标的 `i`，在决策时点 `t`：

```text
r_i,t = log(P_i,t / P_i,prev_close)
residual_i,t = r_i,t - beta_m,i,t * r_SPY,t - beta_sector,i,t * r_sector,t
z_selloff_i,t = residual_i,t / sigma_residual_i,t  # 所有 beta/sigma 均仅用 t 时刻之前可得数据
```

候选只在经过研究选择的极端负残差、价格相对日内低点/波动率、流动性及**可信新闻可用性**同时满足时入选。不要预设“跌幅超过 8% 一定反弹”。不允许仅按 RSI(6) 超卖触发开仓。

### 2.2 抛售衰竭与买方恢复

分离 `SELL_PRESSURE`, `SELLING_EXHAUSTION`, `RECOVERY_CONFIRMATION`, `FALLING_KNIFE`, `UNKNOWN`。可以比较：

- 1m/5m 连续创新低频率下降、价格低点抬升、向上收盘分位；
- 收盘窗口相对过去 N 日同时间槽的 RVOL 与成交量尖峰消退；
- 盘口 `bid/ask spread`、买卖深度、有效价差是否改善；
- `VWAP` 偏离的衰减，而不是机械假定其会均值回归；
- 大盘与行业同步下跌程度，重大公司事件和披露时点是否可核实。

公司基本面利空、财报、停牌、稀释等如无法可靠识别，应将新闻状态设为 `UNKNOWN` 并阻断；不能写死“无新闻”或将后来发生的新闻回填至决策时点。

### 2.3 净结果路径预测

对每个**模拟真实成交后**的候选 `TradeIntent`，在允许持仓时段内构造互斥标签：`TP_FIRST`, `SL_FIRST`, `TIMEOUT`, `UNFILLABLE/UNTRADEABLE`；额外跟踪部分成交、停牌、价差断层、被动限价排队、退市/拆股事件。

用于首轮研究的止盈目标：`+1.0% / +1.5% / +2.0% 净收益`，必须扣掉买卖佣金、点差、执行滑点、汇兑/其他适用交易成本。止损不能直接规定“亏 0.8% 一定成交”，因为夜盘没有保证成交的市价退出；使用**计划止损触发 + 保守的实际执行价格 / 未成交尾部风险**分开记账。

```text
E_net_bps = p_tp * R_tp + p_sl * R_sl + p_timeout * E[R_timeout]
            + p_unfillable * E[R_unfillable] - all_in_cost_bps
```

`R_sl`, `R_unfillable` 等收益以有符号值记录，不能把机会成本漏掉。概率需按市场状态/时段/波动率条件校准，输出 Brier、ECE、区间覆盖率和不确定性。若极端时段样本不足，优先简单、可解释的正则化逻辑回归或基线，不因复杂度而默认 Kalman/OU/深度学习更好。

### 2.4 宏观与保护门禁

- 原 V2.2 全局 `UNKNOWN` / `HIGH_VOL_PANIC` 继续禁止任何做多新开仓；S5 不因“恐慌能反弹”而例外。
- `FALLING_KNIFE`, `EVENT_RISK_UNKNOWN`, `NEWS_HARD_NEGATIVE`, `DATA_STALE`, `SPREAD_TOO_WIDE`, `DEPTH_TOO_THIN`, `AFTER_HOURS_UNSUPPORTED`, `OVERNIGHT_UNSUPPORTED`, `NON_POSITIVE_EXPECTED_EDGE` → 拒绝。
- 价格持续单边下跌、被动挂单可能产生逆向选择，不因为“限价成交率低”就取消价格上限或反复追高。

## 3. 数据最小合格标准

1. **历史基线**：原项目只读数据 `D:\Alpaca_Research_DB\FRESH_2Y_5TF_20261101`，起止约 2024-11-01 至 2026-10-30，供 RTH 候选信号研究；派生数据只能进入 `D:\Strategy\data_v2_2\s5\`。首先验证实际 session、bar 起止、缺失和价格复权，不得先声称历史库包含全夜盘。
2. **回测执行域**：应有对应时点的可交易报价（至少可核实 bid/ask/成交时刻），最好有盘口深度、订单状态、实际成交/撤单样本，特别是 16:00 之后。仅有 RTH OHLCV 不能证明盘后成交与止盈。
3. **源间差异**：在双方均有数据时比较 Alpaca SIP vs. Binance Stocks 同时间标的/分钟；20:00 ET 后如果 SIP 不覆盖，不得虚构所谓“同步对照”。切换成 Binance 独立采集数据质量评价。
4. **实时采集**：在现有 L1/L2/L3 之上按焦点池小规模记录 `price`, `@kline_5m`, `@quote` 与交易/停牌状态；所有记录写 UTC 事件时间、接收时间、ET 会话标签、来源、数据新鲜度、质量状态和去重键。保存 parquet/sqlite 时用独立路径，不抢占原有 SQLite WAL 和服务器内存。
5. **切勿后验挑选**：每天必须记录**所有**符合预扫描条件的股票与被拒绝的原因，而不是只保存最终上涨的赢家。上市/退市/停牌时点正确，样本包含未成功退出和流动性中断的尾部损失。
6. **保密**：原凭证只保留于现有凭证体系，不读出到报告，不复制密钥、不修改生产 .env。采集须经接口权限允许；影子模拟不调用真实订单写接口。

## 4. 强平规则的**最小改动**方案

### 4.1 策略级生命周期，而不是修改全局时钟

```python
# 结构示例，最终以项目当前契约和风控实现为准
PositionPolicy(
    strategy_id="S5_EOD_REVERSAL",
    session_profile="RESEARCH_POST_OR_24H",
    allow_cross_rth_close=True,       # 仅策略级，并受实盘开关和授权门禁约束
    live_enabled=False,
    max_holding_seconds=...,
    exit_deadline_utc=...,
    allowed_order_types=["LIMIT"],
    session_eligibility_verified=False,
)
```

`15:55 ET` 的原守护器仍须按原规定对 S1–S4 平仓。S5 的持仓只能由 `S5GuardianPolicy` 接管，并且必须经过：

1. 非线上模式 / 已授权策略标记；
2. `exchangeInfo` 实时支持对应时段；
3. 完整的时段切换、撤单竞态和部分成交保护测试；
4. 独立 max holding / deadlines；
5. 无成交/停牌/锁仓时 `EXIT_REQUIRED` 持久化 + 告警 + 新开仓冻结；
6. 单一 PortfolioRisk 统一审批、全局槽位/敞口计数。

### 4.2 必测边界

- 15:54:59 提交、15:55:00 RTH 基线强平：S1–S4 应强平；S5 **模拟仓位**应切换自身策略保护，不能因 S5 改动让 S1–S4 残留。
- 15:59:59 / 16:00:01 的订单 TIF 与 session 行为，不能预设 DAY/GTC 自动如何跨阶段；依据官方文档及只读历史证据和 Mock 验证。
- 19:59:59 → 20:00:01：`EXTENDED` 不能被错误当作 `24H`，不支持 overnight 的股票必须不再新开仓；退出受限时走异常升级，不宣称必然可以卖掉。
- 卖出止盈在途又触发止损：沿用原 `CANCEL_PENDING_FOR_SL` / `486446` / `486444` 精准对账，绝不卖空或双卖。
- 过夜停牌/新闻导致无法成交、0 bid/ask、交易所拒单、WS 断流、周末/假日切换、半日收盘均必须触发可审计的安全状态。

## 5. 回测统计与 Go/No-Go

### 5.1 数据分区治理

原 V2.2 手册的 Train/Validation/Lockbox：

- Train：2024-11-01 至 2025-10-31；
- Walk-forward Validation：2025-11-01 至 2026-04-30；
- 原 Lockbox：2026-05-01 至 2026-10-30。

**新增 S5 是 2026-11 才明确提出的策略，且当前已经在 V2.2 Phase 14 进行数据/模型调试。不能无证据地把先前使用过的 2026-05~10 区间重新命名为 S5“完全未触碰 Lockbox”。** 建议：旧数据按用途标注 `exploratory/train/validation`；真正独立的 S5 前瞻测试数据从冻结 S5 代码/参数之后开始按时间采集，形成不可回写的 `prospective_lockbox`。若有独立、未接触且可审计的数据源可以补充，但绝不能假造数据隔离证据。

### 5.2 实验对照组

- `B0`：固定异常跌幅尾盘反弹的规则基线（在不泄漏未来的条件下）。
- `B1`：加入市场/板块残差 z-score + 卖压衰竭特征。
- `B2`：加入独立校准的 TP/SL/Timeout/Unfillable 路径概率与新闻风控门禁。
- `B3`：加入受限价价差、成交概率和真实成本影响的 LimitOptimizer。
- `B4`：加入 Guardian / 组合风险 / 跨会话约束 / 故障注入。
- `B5`：对照 `A_RTH_TO_POST`, `B_POST_TO_POST`, `C_POST_TO_OVERNIGHT`，**分臂**报告，不混合交易结果。

报告最少包括：每日候选数、有效信号数、`NO_TRADE` 数、假阳性、成交率/部分成交率、未成交机会成本、TP 先达率、SL 先达率、Timeout、无法成交退出率、净期望 bps、平均亏损、Profit Factor、净收益、MDD、CVaR(95)、置信区间、手续费/点差占毛利比、模型校准 ECE/Brier、数据源差异、分行业/事件/宏观状态表现及运行性能。

### 5.3 不能上线的情形

- 无足够可核验的盘后/隔夜数据，只有日内 K 线想象的夜盘收益；
- 样本数量过少且盈利置信区间跨零，或多重检验后不显著；
- 仅某一只股票/单次新闻事件决定总盈亏；
- 收益对参数 ±15% 或 2 倍摩擦极度敏感；
- Exit 无法覆盖无盘口、停牌及跨时段 TIF 事件；
- 任何生产 S1–S4 回归测试失败、锁箱数据被调参污染、权限/合规未知；
- 连续 Shadow/ Paper 阶段不能复核成交率及净成本。

## 6. 文件组织和改动白名单

**以下是目标目录示例，先由 Cursor 的 S5-P0 审计真实仓库结构后映射，不能假设文件一定存在：**

```text
D:\Strategy\
  docs\s5\
    s5_00_baseline_audit.md
    s5_01_market_data_contract.md
    s5_02_data_quality_report.md
    s5_03_model_research_report.md
    s5_04_execution_guardian_report.md
    s5_05_backtest_report.md
    s5_06_shadow_report.md
    s5_07_go_nogo.md
  binance_stocks_v2_2\src\bstocks_v2\strategies\s5\
    __init__.py
    features.py
    universe.py
    event_gate.py
    model.py
    policy.py
    signals.py
  data_v2_2\s5\       # 独立数据；永不回写 Alpaca 只读源库
  reports\s5\
  tests\s5\
```

仅允许因 S5 而需要的**可审计、可回滚、向后兼容**的公共接口扩展（策略注册、数据会话标签、交易资格、`PositionPolicy`、Guardian 调度、统一报表）。严禁未经批准批量重构公共引擎、重写原 S1–S4、改动账户风险硬边界。

---

# 7. Cursor / Grok 4.7 High 逐阶段开发 Prompts

> 使用方法：**从 S5-P0 开始，只复制该阶段的完整代码块，发送给 Cursor。** 每阶段完成后要求返回指定验收文件、修改清单、证据和 Git SHA，然后停止。任何阶段未通过不得自动继续。以下 Prompt 不授权任何实盘写操作。

## S5-P0 — 冻结现有 V2.2、审计 Phase 14 和接口边界（只读）

```text
你是 Binance Stocks V2.2 的资深量化架构审计工程师。投资人已把工程推进到 Phase 14，正在处理 SIP 与 Binance 5m 行情重叠样本问题。现在仅为新增独立 S5 研究策略做准备，不许重构原工程。

任务：在 D:\Strategy 仓库执行 100% 只读的源码/依赖/测试/阶段报告/配置结构审计。仅盘点，不修改、不运行下单/撤单/钱包转账接口，不重启、不部署生产服务，不读取或打印密钥明文。识别真实 Repo 根目录与 Git SHA，检查 Stage 14 Go/No-Go 是已通过、进行中还是有未解决缺陷；不能把进入 Phase14 说成 G14 PASS。

逐一定位（真实路径+行号）：S1~S4 注册、SignalArbitrator、TradeIntent、LimitOptimizer、ConstitutionGuard、Guardian、PortfolioRisk、market_ingestor/SQLite WAL、回测入口、时区和交易日历、原 15:45 停开仓与 15:55 强平、8% HWM/PDT/分数股/钱包规则。列出现有测试基线（命令、通过/失败/跳过数量）；不修测试。

只读核查：现有 Binance 同时段采集数据、SIP 1m/5m 实际历史覆盖（尤其 16:00~20:00, 20:00~04:00）、存储容量及服务器资源；把缺失列清楚，不能填造。任何 API 可用性判断应标注“官方文档/实际只读实测/未验证”。核实 equity API 与 bStocks 显示价格是否被错误混用。

输出：docs/s5/s5_00_baseline_audit.md（若本阶段严格只读无法落盘，则先在对话框输出可复制报告，待授权阶段保存）；列出 P0/P1/P2 风险、最小触及公共模块的差异清单、完整回滚计划、准入证据，并停在 G-S5-0 等待人工审批。不可自行启动下一阶段。
```

**G-S5-0 验收**：原有代码/生产零修改、G14 真实进度清楚、最小改动点和历史数据缺口完整。

## S5-P1 — 独立分支、规则锚点与只读会话资格适配

```text
前提：投资人已审核 S5-P0。现在仅允许在新 Git 分支 feature/s5-eod-reversal 建立隔离的研究基础代码，不得自动进实盘或合并主分支。

1) 固化 baseline SHA 与 S5 变更白名单，创建 docs/s5/ 与 tests/s5/；将 S5 永久默认设置 RESEARCH_ONLY/live_enabled=False，并增加代码级安全校验，不能只靠 .env 开关。
2) 新增可插拔 SessionEligibility/TradingCalendar 适配器：按 America/New_York 日历标记 RTH/POST/OVERNIGHT/PRE/半日交易；从已存在的 Binance equity exchangeInfo 只读适配器取得每个标的 tradability/extendedSession/overnightSupported/fractionableEh/精度和名义金额约束。将 RTH/EXTENDED/24H 映射成严格可验证的订单时段资格，未证实则 fail closed。
3) 追加结构化事件字段：session, capture_ts, exchange_ts, feed, quote_freshness_ms, session_eligibility_snapshot_hash, strategy_id, research_arm, news_event_state。采用新增字段默认值/可选属性，确保 S1-S4 反序列化向后兼容。
4) 不改变原全局 15:45/15:55 的任何行为；仅设计 PositionPolicy 契约与 S5GuardianPolicy 接口，暂不激活。
5) 先通过现有完整单元测试和新测试；验证未知资格、休市、半日、市价盘后订单、24H 不支持标的全部被拦截。

提交：docs/s5/s5_01_market_data_contract.md，附 Git diff/SHA、测试记录、真实源代码路径、回退操作。停在 G-S5-1；严禁下单/部署。
```

**G-S5-1 验收**：旧测试无回归、资格门禁关闭失败（fail closed）、S5 无实盘入口。

## S5-P2 — 时段数据审计、独立数据集与只读实时采集

```text
前提：G-S5-1 PASS。当前目标只建设研究数据，不写交易订单。

1) 读取既有 Alpaca 只读目录，逐股票/天/分钟统计 RTH、POST、OVERNIGHT、PRE 的实际存在率、空 Bar、市场状态、SIP feed、复权与缺失。输出真实覆盖报告，不假定“有两年 1Min 就有两年夜盘”。
2) 在有重叠的时段、同一时点，比较 Alpaca SIP 与 Binance Stocks 的 close/volume/VWAP/bid/ask（对齐前提必须成立），以分时段结果和数据偏差为依据。20:00 后若 SIP 没有数据，则标记 NO_SIP_REFERENCE，不得插值或虚构同步。
3) 若已有经过授权的实时 market_ingestor，可新增容量受限的 S5 shadow recorder，保存候选全集、焦点池 bid/ask、行情、会话事件和数据质量。复用既有 WS，不复制 300 标的额外全量订阅；先测 2vCPU/2GB 负载与存储。不得从生产系统读取或输出密钥，不能改现有进程配置。
4) 隔离数据路径 data_v2_2/s5，提供 manifest+SHA256+源/时区/采集时间/行情许可说明；事件时点不能晚于信号决策时点。无法验证历史夜盘数据时，明确切换为 prospective forward-collection 模式。
5) 对低流动性无报价、休市、短周期 0 成交、断流重连、重复事件、时区 DST 做反例测试。

提交：docs/s5/s5_02_data_quality_report.md、覆盖率矩阵、数据 manifest、测试通过证据、服务器资源基线。停在 G-S5-2；没有可交易夜盘数据不进入收益宣传。
```

**G-S5-2 验收**：明确哪部分是可回测历史、哪部分必须前瞻收集；数据不泄漏、不写回源库、不影响旧流。

## S5-P3 — S5 信号、新闻过滤和校准概率研究

```text
前提：G-S5-2 PASS。新增 src/bstocks_v2/strategies/s5/ 独立实现，只输出 TradeIntent / research signal，不接真实 ExecutionGateway。

1) 设计 B0 纯跌幅基线；B1 基于 market/sector beta 的极端下跌残差 z-score；B2 抛售衰竭+流动性恢复+事件风险筛选；B3 TP/SL/Timeout/Unfillable 路径联合概率校准。参数只能在训练集和经审计的验证集上拟合，严禁根据 INTC 单一样例或近期赢家过拟合。
2) 使用严格因果 1m/5m 已闭合 K 线和当时盘口/新闻快照。没有可验证的新闻时点数据时将 NEWS_UNKNOWN 标记拒绝而不是假定无利空；不得隐式拉取未经授权第三方数据。
3) 实验臂分别为 A_RTH_TO_POST、B_POST_TO_POST、C_POST_TO_OVERNIGHT，各自样本与统计独立。初始研究净止盈参数为 1.0/1.5/2.0%，止损与退出实际执行成本由数据决定，不做“自动保证成交”。
4) 仅在原全局市场状态允许的情况下产生候选；UNKNOWN/HIGH_VOL_PANIC 禁开新仓；特别增加 FALLING_KNIFE/EVENT_RISK/SPREAD/DEPTH 过滤。
5) 输出每个候选和每个被拒绝样本的 features_hash/decision_ts/数据可见性证明；离线批处理与流式逐步结果一致。优先以简单可解释模型为基线，复杂数学模型只在样本外显著改进时保留。

提交：代码、单元测试、无未来信息泄漏反例、Brier/ECE/样本数量及消融诊断至 docs/s5/s5_03_model_research_report.md。停在 G-S5-3。
```

**G-S5-3 验收**：严格因果、未成交/新闻未知也进入研究样本、概率经过样本外校准或如实声明不足。

## S5-P4 — 执行模拟、Guardian 跨收盘状态机与账户总风控

```text
前提：G-S5-3 PASS。所有执行走 Mock Broker / Replay Engine。不得连接真实下单/撤单接口。

1) 扩展 LimitOptimizer 的研究适配：RTH/EXTENDED/24H 的 LIMIT 会话资格、两位价格、精度/整数股回退、TIF(DAY/GTC)实测/文档契约、买卖价差和穿透队列撮合、部分成交、价格上限；无法确定真实成交的事件一律按保守/不可成交处理。
2) 新增 S5GuardianPolicy：策略级允许跨 16:00 的有保护持仓，但不关闭其他策略的 15:55 强平。S5 通过实际 session 可用性切换限价退出；在 POST/OVERNIGHT 禁止把 SELL MARKET 当成即时止损。无法成交时持久化 EXIT_REQUIRED / HALT_NEW_ENTRIES + 高优先级告警。
3) 防订单竞态：沿用原 clientOrderId 幂等、tc-FN 部分成交误算保护、486444/486446 撤单终态验证、份额冻结时不得双卖或做空；不允许 S5 绕过账户级单笔风险/全局 5 仓/8% HWM 锁/PDT 等原边界。
4) 设计跨收盘 20 项以上的故障注入（15:55 基线强平、15:59->16:00、19:59->20:00、半日收盘、DAY/GTC、限制分数股、停牌、新闻、报价断流、交易所拒单、长时间无 bid、部分成交/撤单竞态、Guardian 重启恢复、实际损失越过计划止损等）。
5) 出具公共代码每一项 diff；已完成 Stage 12/13 的守护和风控测试必须全部重跑并通过。

提交：docs/s5/s5_04_execution_guardian_report.md、可复现故障测试记录、回滚计划。停在 G-S5-4。严禁把测试结果当真实可成交证据。
```

**G-S5-4 验收**：S1–S4 日终保护零回归，S5 不会凭借跨时段豁免获得无限风险或无保护持仓。

## S5-P5 — 历史/前瞻回测与独立 Go/No-Go 预评估

```text
前提：G-S5-4 PASS。现在只运行离线/影子模拟，不进行真实交易。

1) 先核查数据：若缺乏历史可执行报价，明确列为 RTH 候选筛选研究，禁止输出“夜盘可交易盈利”结论；若有满足可信度的数据，则分别运行实验臂 A/B/C，使用 next-event 因果撮合、盘口价差、排队成交概率、部分成交、跳空和退出失败规则。
2) 按 B0/B1/B2/B3/B4/B5 输出消融表；统计 TP first/SL first/Timeout/Unfillable 以及全部信号（包括未成交）；分别计算净期望、Profit Factor、MDD、CVaR、bootstrap 置信区间、样本充足度、交易次数、事件日/非事件日和分行业结果。
3) 执行费用与点差翻倍、delta 穿透加倍、延迟 +1s/+5s、夜盘 0 bid、价格跳空/停牌、参数 ±15%、样本分段/事件簇留出、去掉 INTC 单一样例的敏感性检验。
4) 严格记载原 2026-05~10 既有 V2.2 Lockbox 使用史；若已被研究者查看或用于选择 S5，不得再宣布它对 S5 是“未触碰测试集”。形成新的样本外前瞻封存计划，预注册筛选、训练/验证/冻结参数和允许的最终评估次数。
5) 任何净期望<=0、样本不足、滑点不可靠、出场失败风险不可接受都输出 NO_GO / RESEARCH_ONLY，禁止为了满足 1%-2% 预期改变数据或选择性剔除亏损。

提交：docs/s5/s5_05_backtest_report.md + 可复现 manifest、实验配置、Git SHA、下游可解释审计账本。停在 G-S5-5。
```

**G-S5-5 验收**：收益只来源于有证据的成交模拟，披露全部不确定性；未过门槛明确 NO_GO。

## S5-P6 — 只读实时影子交易与延迟/成交能力跟踪

```text
前提：G-S5-5 允许 SHADOW ONLY。此阶段依然不得调用 POST /order/place 或 cancel。

1) 在现有服务器资源允许且不影响任何运行中的系统时，启动隔离的 shadow worker；若环境不满足条件，仅提供部署方案及可本地回放的数据，不擅自开进程。
2) 预先冻结 S5 参数与测试口径后，连续采集至少 20 个真实美股交易日的实时数据（这是首轮工程观察下限，不代表统计显著）；每个信号保存当时 bid/ask、盘口、队列估计、拟挂限价、TTL、假设成交、部分成交/未成交、真实后续市场路径与假设退出失败。
3) 按 A/B/C 各自报告可交易样本量及置信区间；当样本少于显著性分析要求时继续观测，不通过补造 K 线或扩大收益假设凑数据。
4) 只读 Shadow 与生产程序进程、数据库、日志、健康度指标隔离；验证峰值 CPU、RAM、磁盘写量、WS 延迟、断流率、数据对齐。每个 RTH 收盘和最终夜盘结束后形成差异报告。
5) Guardian 故障模拟和原 Stage 12/13 P0 不变量再跑一次；按指标出具影子与离线偏差归因。

提交：docs/s5/s5_06_shadow_report.md，包含启动/停止/回滚步骤、观察数据数量和实际平台约束。停在 G-S5-6。
```

**G-S5-6 验收**：可追溯报价与实测质量；没有真实成交证据的成交率必须标注为估计值。

## S5-P7 — S5 独立 Go/No-Go、冻结交付和是否申请 V2.3

```text
前提：G-S5-6 研究数据已完成当前样本外评估，且 V2.2 原版 G14/系统安全状态真实可核验。当前阶段仍不允许真实下单或部署。

1) 汇总全部 S5 证据，包括独立数据覆盖、资格、风控不变量、公共模块回归、历史/前瞻样本外净期望、DSR/多重假设、参数邻域、市场极端情景、成交/退出困难和最大尾部风险。
2) 分实验臂独立裁定：GO_FOR_REVIEW / SHADOW_MORE / NO_GO。GO_FOR_REVIEW 不等于允许实盘；只有投资人后续单独书面授权，以及既有 Phase 15 的前置检查都通过，才能提出真实交易部署变更单。
3) 生成 S5 版本锁定清单、Git SHA、变更白名单、旧版 V2.2 基线差异、数据库 schema 迁移和回滚 SOP；清楚说明 S1–S4 没有被覆盖、改参或禁用。
4) 若通过，给出未来独立的 S5 Live Enablement RFC，仅列拟变更内容、上线窗口、灰度与 kill switch；本阶段不得设置 live_enabled=true，不得解锁 HWM，也不得越过原 Phase 15 人工审批流程。
5) 若未通过，将 S5 保留为 RESEARCH_ONLY，不影响旧项目继续进入其原有 Phase15/16 路线。

提交：docs/s5/s5_07_go_nogo.md、最终独立补丁清单、可复现验收材料和下一步投资人决策选项。停下。
```

**G-S5-7 验收**：可清晰区分“策略研发完成”、“统计通过”、“影子通过”和“实盘授权”；任何一个都不自动推导下一个。

---

## 8. 统一验收报告格式（每阶段必报）

每份报告至少包含：

```text
S5 Phase / Gate:
基线 Git SHA / 本阶段 Git SHA:
实际修改的文件及行号:
实际新增的文件:
原 S1-S4 回归测试（总数、通过、失败、跳过）:
S5 新测试（总数、通过、失败、跳过）:
是否发生生产文件、进程、订单、资金变更（必须为否）:
API 官方契约核验与只读实测差异:
研究数据版本、数据范围、源和 session 覆盖率:
P0/P1/P2 余项:
本阶段 GATE: PASS / FAIL / BLOCKED（附证据）:
回滚命令与恢复验证:
下阶段的前置条件:
投资人需要批准的具体变更:
```

任何“已经完成”都必须能对应 Git commit、测试日志、数据 manifest 或可复现的只读证据。不得以模型自述替代工程验收。

## 9. 不建议做的事

1. 不把 S5 混进 S4 的 OU 代码后反复改参；两者交易窗口、分布和风险不同。
2. 不复制现有全套 Phase 0–14 从头做，不重写基础风控和执行器。
3. 不绕过原 15:55 日终强平来“试一下”尾盘买入；必须先完成策略级 Guardian 保护验证。
4. 不使用常规交易时段 SIP K 线预测结果冒充 20:00 后的 Binance 24H 实际成交收益。
5. 不以一例 INTC 反弹 +2% 推断长期统计胜率，更不预设每天稳定赚 1%。
6. 不因 S5 的收益目标而提高仓位、追加保证金、补仓或关闭原 PDT/HWM 风险锁。
7. 不让 Cursor 在尚未人工验收前自动进入下一阶段或触发真实委托。

## 10. 官方接口与风险参考（2026-11-03 访问核对）

- Binance Stock Trading 交易时段、24/5 支持范围、延长时段限价单规则：<https://www.binance.com/en/support/faq/detail/a7469c7703524024b5bc2d492b03639d>
- Binance Stocks API `exchangeInfo` 资格字段：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data>
- Binance Stocks API `order/place` 字段矩阵、`tradingSession`, `DAY/GTC`, `tokenize`, `walletType`：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/trade>
- Binance Stocks WS 用户订单生命周期：<https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/ws-streams/user-streams>
- Binance bStocks 代币化证券与地域限制（与 `tokenize=false` 美股交易接口需区分）：<https://www.binance.com/en-NG/support/faq/detail/f0c03cd6509a4085b4cce1636f16be38>

**本方案的结论**：值得把 S5 纳入既有 V2.2 做低成本、可关闭、可回滚的独立科研扩展；能否依靠夜盘反弹稳定获得净 1%–2%，目前并未验证，应交由有真实盘后成交数据的严谨样本外测试裁定。
