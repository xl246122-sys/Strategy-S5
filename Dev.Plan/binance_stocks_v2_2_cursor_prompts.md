# Binance Stocks V2.2 量化机器人 — Cursor + Grok 4.7 High 分阶段开发 Prompt 手册

> **当前真实环境绑定（已从你的 Cursor 会话自动对齐）**
>
> * **本地 Windows 工作区**：`D:\Strategy`（账号凭证参考：`D:\Strategy\Dev.Plan\ServerStation.txt`，注意严禁将明文密码/API Key 提交进 Git 或留在临时脚本中）
>
> * **日本生产/研发服务器**：腾讯云日本东京 `43.163.203.127` | 主机名 `VM-0-13-ubuntu` | 系统 `Ubuntu 26.04.1 LTS (Resolute Raccoon)` | 登录用户 `ubuntu`（`sudo` 免密 root 已就绪）
>
> * **回测行情库（投资人 2026-11-02 授权，只读）**：479 支美股与 ETF 的两年大盘历史在 `D:\Alpaca_Research_DB\FRESH_2Y_5TF_20261101`。需要回测时直接读这个目录，代码里的默认路径是 `bstocks_v2.research_data.paths.FRESH_DEFAULT`。阶段 0 已核对：`feed=sip`，`adjustment=raw`，周期 `1Min` / `5Min` / `1Hour` / `1Day` / `1Week`，纽约日期 `2024-11-01` 至 `2026-10-30`；Parquet 在 `parquet\` 下按周期和代码分片。目录内实际是 481 个代码，即股票池 479 只再加上 `SGOV` 与 `BIL`。候选名单在 `D:\Alpaca_Research_DB\股池数据\GrokBot_美股ETF股票池479_五周期_全新采集_20261101.csv`。禁止写入、覆盖或把派生结果写回该目录；研究层仍落在 `D:\Strategy\data_v2_2\`。价格是未复权 raw，拆股日必须先复权再当收益。`2026-05-01` 至 `2026-10-30` 仍是 Lockbox：训练和验证扫描停在 `2026-04-30`，这段日期只留给参数冻结后的一次全样本检验。本授权只开放行情读取，不把未经时点市值核验的个股写成已经通过研究资格。
>
> * **执行铁律**：每次**只复制并发送一个阶段的 Prompt** 给 Cursor（Grok 4.7 High）。等它执行完毕、输出《阶段验收报告》后，你检查无误（或发给我帮你把关），再发下一阶段的 Prompt。**严禁一次性合并发送多个阶段**。

## 为什么这套 V2.2 方案能直接进实盘级开发？（核心架构亮点与 4 个实操注意点）

### 1. V2.2 方案的三大核心突破

1. **彻底封死币安美股 API 的隐蔽陷阱（V2.2-U1 \~ U6）**：

   * 修复了 WebSocket `{listenKey}@orderReport` 在部分成交（`partially_filled`）时把**整单冻结总金额 `tc` 减去已成交金额 `FN` 误算为巨额手续费**、进而瞬间误触发 8% HWM 永久回撤锁的致命 Bug；

   * 针对币安 `/sapi/v1/equity/market/` **没有历史 K 线 REST 接口**的缺陷，设计了本地 SQLite WAL 日内 5m K 线实时落盘 + 盘中重启 100ms 热恢复 + Alpaca SIP 只读缺口回补；

   * 解决了 `orderReport` 标的带 `EQ_` 前缀、枚举全小写、无 `clientOrderId`，以及卖单强制回流 `CARD`（Funding 资金钱包）导致的资金割裂问题；

   * 针对撤止盈单遇 `486446`（撤单排队份额冻结）与 `486444`（止盈单已成交）、市价止损遇 `-1112`（无深度）或跨过 16:00 ET 遇 `486441`（盘后禁市价单），设计了 **15:55:00 ET 前置强平 + `CANCEL_PENDING_FOR_SL` 状态机 + 阶梯降级限价退出器**。

2. **小资金（1,600 USDC）摩擦控制与合规护栏（V2.2-U7 & P0-2/P1-1）**：

   * 强制校验分数股限价单冲突（`486442`），支持自动向下取整数股，并设置 `min_viable_notional_usdc = 120 USDC` 与双边摩擦占毛利上限（`≤ 20%`），防止百元碎单被最低佣金和点差吞噬；

   * 内置滚动 5 交易日 PDT（`486440`）计数门禁与 8% HWM 持久化回撤锁。

3. **机构级防过拟合与因果数学体系（V2.2-U8 & P1-4）**：

   * 四大策略（S1 因果 Kalman/RLS 特异动量、S2 多分类单纯形校准趋势回调、S3 1m/截面 CUSUM+SPRT 开盘突破、S4 无量纲 OU 超跌实验）均强制采用 **`Low <= L - delta` 保守穿透撮合**、**四阶消融实验**、**Deflated Sharpe Ratio (DSR)** 与 **±15% 参数邻域平稳性检验**。

### 2. 给 Grok 4.7 High 执行时的 4 个关键工程护栏（已全部注入下方 Prompt）

1. **SSH 凭据零残留**：你刚才在本地生成过 `_ssh_check.py`，已在下方「前置步」加入强制清理与 `.gitignore` 保护，改用系统级 SSH 免密密钥或环境变量连接 `43.163.203.127`，严禁在代码中硬编码密码。

2. **Ubuntu 26.04 LTS 的 Python 环境兼容**：Ubuntu 26.04 默认系统 Python 为 3.13/3.14，而部分量化底层库在 **Python 3.11 / 3.12** 下最稳定。已在阶段 1 指示 Grok 使用 `uv` 或 `venv` 创建锁定版本的独立虚拟环境。

3. **日本服务器（研发/实盘）与本地数据职责切分**：回测用的全量历史是上面的只读库 `D:\Alpaca_Research_DB\FRESH_2Y_5TF_20261101`（479 支美股与 ETF，另加 `SGOV`、`BIL`；`2024-11-01` 至 `2026-10-30`；`1Min/5Min/1Hour/1Day/1Week`）。派生研究层落在本地 `D:\Strategy\data_v2_2\`，不覆盖原始库。腾讯云日本服务器 `/home/ubuntu/binance_stocks_v2_2/` 只部署核心代码、SQLite WAL 实时库与精简研究集，不存放这套全量 Parquet。

4. **常驻上下文锚点（`.cursor/rules`）**：长周期开发中 AI 容易在阶段 6 以后遗忘阶段 0 的宪法。**请先执行下方的「前置步（Step 00-Init）」**，让 Grok 把《V2.2 项目宪法》写入 `.cursor/rules/v2_2_constitution.mdc`，这样后续每一轮对话它都会自动遵守全部硬性红线！

## 前置步（Step 00-Init）— 注入全局项目宪法与 SSH 安全通道加固

> **操作方式**：直接复制下方代码块内容，粘贴到你当前已经打开的 Cursor 对话框中发送给 Grok 4.7 High。

```
我们现在正式启动《Binance Stocks 美股/ETF 自动短线交易机器人 V2.2（终极审计完善版）》的分阶段工程执行。
当前环境信息：
- 本地项目根目录：`D:\Strategy`
- 腾讯云日本服务器：`43.163.203.127`（Ubuntu 26.04.1 LTS，用户 `ubuntu`，具备 `sudo` 免密 root 权限）
- 严禁一次性编写所有阶段代码！我们将严格按照 Stage 0 -> Stage 16 逐阶段推进，每个阶段结束你必须输出《阶段验收报告》并立即停下等待我审批。

在进入【阶段 0：只读审计】之前，请先执行以下 3 项环境安全与规则锚定动作（严禁触碰或修改日本服务器上任何已有运行中的交易服务或业务文件）：

1. 【清理临时脚本与凭据防泄漏】
   - 检查本地 `D:\Strategy` 下的 `_ssh_check.py` 或任何包含服务器密码/API Key 的临时文件，确保彻底删除 `_ssh_check.py`；
   - 在 `D:\Strategy\.gitignore` 中加入 `Dev.Plan/ServerStation.txt`、`*.env`、`*secret*`、`_ssh_*.py`、`*.pem`、`*.key`，确保敏感凭据绝不被 Git 追踪；
   - 配置本地到 `ubuntu@43.163.203.127` 的免密 SSH Key 认证（若尚未配置），后续你通过 SSH 在日本服务器执行命令时，一律使用免密 SSH，不再读取或生成含明文密码的临时脚本。

2. 【归档 V2.2 蓝图与写入 Cursor 全局强制宪法】
   - 创建目录 `D:\Strategy\docs\` 与 `D:\Strategy\.cursor\rules\`；
   - 创建 `D:\Strategy\.cursor\rules\v2_2_constitution.mdc`（设置 `alwaysApply: true`），将以下 10 条不可妥协的《V2.2 项目宪法与 U1-U8 核心修正铁律》完整写入其中，作为后续所有阶段的代码审查最高准则：
     (1) 唯一市场与接口边界：仅交易 Binance Stocks 美国上市个股与 ETF（`api.binance.com/sapi/v1/equity/*`）；仅允许两项只读白名单：SAPI Funding 钱包只读查询（`/sapi/v1/asset/get-funding-asset`）与 Alpaca Market Data 只读历史/断流回补；严禁加密货币、合约、杠杆、做空及 `/tokenized/mint`、`/tokenized/redeem`。
     (2) 订单与钱包铁律：`quoteAsset=USDC`；所有 `BUY` 强制显式传 `walletType="CARD"` 与 `tokenize=false`；首次交易前必查 `/sapi/v1/equity/account/disclaimer`（防 `486410`）；严格遵守 `BUY LIMIT`/`SELL LIMIT`/`SELL MARKET` 字段矩阵，开仓禁用 `BUY MARKET`；`price` 最多 2 位小数；`clientOrderId` 严格符合 `^[a-zA-Z0-9-_]{32,36}$`。
     (3) V2.2-U1 防 `tc - FN` 佣金陷阱：WebSocket `{listenKey}@orderReport` 的 `b` 带 `EQ_` 前缀、`S/o/s` 全小写、无 `clientOrderId`；且当 `s != "filled"`（或 `fq < Q`）时，`tc` 为含未成交本金的整单冻结总成本，严禁在未完全成交时用 `tc - FN` 计算已实现佣金或扣减账户 Equity（防瞬间误触发 8% HWM 锁）！终态佣金一律以 `GET /order/detail` 的 `fee` 字段为准。
     (4) V2.2-U2 无历史 K 线 REST 接口防御：`/sapi/v1/equity/market/` 无历史 K 线接口；`market_ingestor` 必须将每根实时 `@kline_5m` 同步写入本地 SQLite WAL 表 `intraday_bars_wal` 支持 100ms 热恢复；>5m 断流缺口需用只读 Alpaca SIP 分钟线回补或标记 `vwap_intact=False` 禁入日内累积指标策略。
     (5) V2.2-U3 三维持仓对账：结合本地 SQLite 订单/成交账本 + `/order/open-orders` 与 `/trade/history` 聚合重建 + SAPI `CARD` 钱包 `USDC` 与 `EQ_*` 资产快照进行三维交叉对账。
     (6) V2.2-U4 日终前置强平与阶梯降级退出：仅在美股 RTH（09:30–16:00 ET，半日市 09:30–13:00 ET）开仓；15:45 ET（半日市 12:45 ET）停止新开仓，15:55:00 ET（半日市 12:55:00 ET）强制启动撤单与平仓（防跨过 16:00 ET 触发 `486441` 盘后禁市价单）；Guardian 遇 `-1112 NO_DEPTH` 或 `486413` 时自动在价格笼子内降级发送可成交 Marketable `SELL LIMIT`（跨入盘后时整数股改用 `EXTENDED SELL LIMIT`）。
     (7) V2.2-U5 流健康度与 `listenKey` 续期：区分周期流（`price`/`@kline_5m`/`@quote`）与无心跳的状态跳变流（`calendar`/`@tradability`/`@tradingStatus`），严禁因全天无停牌而误判数据陈旧；`listenKey` 创建与每 25 分钟续期统一调用签名接口 `POST /sapi/v1/equity/listenKey`。
     (8) V2.2-U6 下单超时与撤单竞态闭环：遇 `-1006`/`-1007` 严禁换新 `clientOrderId` 盲目重发，必先查 `/order/detail`；Guardian 撤 TP 单进入 `CANCEL_PENDING_FOR_SL`，若报 `486444` 立即查 `/order/detail` 确认原 TP 单是否已 `FILLED`（已成交则直接转入 `FLAT_RECONCILED`，严禁重复卖），若报 `status="S"` 或 `486446` 必须等终态确认后再对剩余未成份额平仓。
     (9) V2.2-U7 & P0-2/P1-1 资金、小额摩擦与风控铁律：初始规划 `1,600 USDC`，最多同时 5 仓（含在途买单占槽位），单笔风险预算默认 `0.25%`（约 `4 USDC`），单仓上限 `20%`（约 `320 USDC`），最小经济可行名义金额 `min_viable_notional_usdc = 120 USDC`，双边总摩擦占目标止盈毛利 `<= 20%`；支持分数股 `486442` 拒单时向下取整数股；内置 8% HWM 永久回撤锁（跨重启不重置，仅人工可解锁）与 PDT `486440` 滚动 5 交易日计数器。
     (10) V2.2-U8 纯本地可复现数学决策与防伪发现：回测强制 `Low <= L - delta` 穿透成交，同柱 TP/SL 冲突下钻 1m 复核或按悲观止损计；必须通过四阶消融、Deflated Sharpe Ratio (DSR) 与核心超参 ±15% 邻域平稳性检验；阶段 0–14 严禁干扰现有生产系统，严禁真实下单。

3. 完成上述配置后，向我汇报本地 `D:\Strategy` 与 SSH 免密通道准备状态，并停下等待我发送【阶段 0 Prompt】。

```

## 阶段 0 — 只读审计、API 契约核验与最终架构评审（禁止任何修改与部署）

> **何时发送**：前置步完成后发送。 **核心目标**：让 Grok 4.7 High 登录 `43.163.203.127` 和扫描本地 `D:\Strategy`，把服务器现有进程、历史代码、Alpaca 数据状况、币安接口只读契约全部摸清，输出 G0 审计报告。

```
你是日本服务器（`43.163.203.127`）与本地工作区（`D:\Strategy`）美股量化机器人项目的首席架构工程师。请严格遵守 `.cursor/rules/v2_2_constitution.mdc` 中的《V2.2 项目宪法》。

【本阶段铁律】
本阶段仅做 100% 只读审计：通过 SSH 检查日本服务器（`43.163.203.127`）并检查本地 `D:\Strategy` 目录，盘点已有代码仓库、Python版本/虚拟环境、systemd服务、crontab、进程状态、服务器CPU/内存/磁盘空间与IO、东京到 `api.binance.com` 及 `nbstream.binance.com` 的网络 RTT 延迟、历史数据目录（服务器与本地 `D:\Strategy`）、密钥加载方式与风控实现。
**严禁修改任何业务文件、严禁重启或停止现有服务、严禁调用任何写操作或下单/撤单接口，看到现有 bug 也只记录不顺手修改。**

你必须重点核查并回答以下 7 项关键工程事实（提供代码文件行号定位、服务器命令输出或只读接口实测证据）：
1. 【钱包与持仓读取路径及三维对账】若已有只读 API Key 配置，核对 `/sapi/v1/asset/get-funding-asset` 等 SAPI 钱包只读接口：当股票以 `tokenize=false` 买入后，SAPI Funding 钱包（`CARD`）是否直接返回 `EQ_*` 持仓份额，还是必须结合 `/sapi/v1/equity/trade/history` 与 `/order/open-orders` 聚合重建持仓？现有日志或配置中是否已签署美股免责声明（防 `486410`）？（注：只允许只读 GET 查询或检查已有日志，绝不发起 POST 写请求，除非是纯只读查询接口）。
2. 【分数股与限价单冲突】调用无需签名的公开只读接口 `GET https://api.binance.com/sapi/v1/equity/market/exchangeInfo`（或核查现有缓存/日志），检查目标股票池的 `tradability`、`fractionable`、`fractionableEh`、`stepSize`、`minQty`、`minNotional`、`multiplierUp`/`multiplierDown` 字段，并结合现有历史订单日志核实：在 RTH 时段下，分数股（quantity含小数）提交 `BUY LIMIT` 或 `SELL LIMIT` 是否会被上游以 `486442` 或 `486443` 拒绝？
3. 【PDT与交割规则】核查历史日志或账户属性：在 1,600 USDC 规模下高频日内回转（每日3-5笔当日买卖）是否曾触发或面临 `486440`（Pattern Day Trading protection）或 T+1 资金交割冻结限制？
4. 【WS与REST契约差异及 tc 字段陷阱】核查现有代码（如有）是否正确处理了 `wss://nbstream.binance.com/equity` 中 `{listenKey}@orderReport` 的 `b` 字段带 `EQ_` 前缀、`S`/`o`/`s` 枚举全小写、无 `clientOrderId`、以及 `partially_filled` 时 `tc` 为整单含未成交部分的累计总成本（不可用 `tc - FN` 算实时佣金）的特性？`listenKey` 续期是否使用了正确的 `POST /sapi/v1/equity/listenKey`？
5. 【币安无历史K线REST接口的恢复机制】鉴于 `/sapi/v1/equity/market/` 仅有 `/exchangeInfo`、`/tokenized-assets`、`/quote` 而无历史 K 线接口，现有系统在盘中重启或断流重连时如何恢复当日 09:30 ET 以来的 5m K 线、RTH VWAP 与 ORB 区间？
6. 【历史S3 ORB断链原因】检查现有代码仓库（若存在旧版代码），定位为何早先 ORB 策略有信号却未能进入正式下单链路的具体代码断点；若当前是全新空仓环境，也请如实说明目录现状。
7. 【数据源盘点】盘点日本服务器与本地 `D:\Strategy` 中已有的候选股票池 CSV（约480只）及已下载 Alpaca 历史数据的周期、起止时间、标的数量，并核实其采集时使用的是 `feed=sip` 还是 `feed=iex`。

请将完整审计报告保存至 `D:\Strategy\docs\stage_00_audit_report.md` 并在对话框输出：
- A. 日本服务器与本地 `D:\Strategy` 现状架构图与资源盘点
- B. 七大关键核查实证结论（附真实接口返回样例或日志片段）
- C. 风险清单（P0/P1/P2）
- D. 现有系统（如有）与 V2.2 差异对照表
- E. 建议复用/重构模块
- F. 独立研发目录规划（建议本地 `D:\Strategy\binance_stocks_v2_2` + 日本服务器 `/opt/binance_stocks_v2_2` 或 `/home/ubuntu/binance_stocks_v2_2`）与分阶段资源预算
- G. 需投资人拍板的事项
输出报告后立即停止，等待我的明确批准，绝不进入阶段 1。

```

**验收门槛 G0**：生产环境零变更、零交易；七大关键接口与环境现状均有真实命令/接口实证。

## 阶段 1 — V2.2 隔离工程骨架、契约归一化层（防 `tc-FN` 陷阱）与风险宪法固化

> **何时发送**：确认阶段 0 报告无误后发送。

```
阶段 0 已由投资人验收通过。现在进入【阶段 1：V2.2 隔离工程骨架、契约归一化层与风险宪法固化】。
请在完全独立的研发目录（本地 `D:\Strategy\binance_stocks_v2_2` 及日本服务器 `/home/ubuntu/binance_stocks_v2_2` 同步镜像，搭配独立 Python 3.11/3.12 虚拟环境）中搭建 V2.2 工程骨架与核心契约层，严禁干扰现有生产服务，严禁连接真实下单接口。

本阶段实现要求：
1. 模块化目录搭建（按 SOLID 与依赖倒置原则）：
   - 创建 `pyproject.toml`，配置 `pytest`, `ruff`, `mypy`；
   - 创建标准包结构：`src/bstocks_v2/` 下设 `config`, `contracts`, `normalizer`, `market_ingestor`, `feature_store`, `regime`, `strategies/{s1,s2,s3,s4}`, `signal_arbitrator`, `limit_optimizer`, `portfolio_risk`, `execution_gateway`, `guardian`, `reconciler`, `research_backtest`, `reporter` 以及 `tests/`。
2. 核心契约与 `EquityPayloadNormalizer`（强制合入 V2.2-U1）：
   - 使用 Pydantic v2 / Dataclass + `decimal.Decimal`（严禁用 float 计算金额、价格与份额）定义强类型契约：
     * `TradeIntent`（必须完整包含：`intent_id, created_at_utc, data_asof_utc, symbol, strategy_id, model_version, regime, features_hash, vwap_intact, signal_valid_until, direction, p_win_calibrated, p_sl_calibrated, p_timeout_calibrated, expected_return_bps, expected_net_bps, prediction_interval, candidate_limit_prices, p_fill_by_price_horizon, entry_price_ceiling, stop_price, take_profit_price, max_holding_seconds, risk_budget_usdc, min_viable_notional_usdc, expected_cost_usdc, requires_fractional, whole_share_fallback_qty, rejection_reasons, data_feed, data_freshness_ms`）
     * `CanonicalOrder`、`CanonicalPosition`、`AccountSnapshot`。
   - 专门实现 `EquityPayloadNormalizer`：
     * 将 WebSocket `{listenKey}@orderReport` 的 `b` 字段强制 `removeprefix("EQ_")` 还原为标准美股代码（如 `"EQ_AAPL" -> "AAPL"`）；
     * 将 `S`（`"buy"`/`"sell"`）、`o`（`"limit"`/`"market"`）、`s`（`"accepted"`/`"partially_filled"`/`"filled"`/`"canceled"` 等）统一转换为大写枚举，利用 `x == "ORDER_TERMINAL"` 判定终态；
     * 将 `n`（如 `"Regular"`、`"24 Hours Trading"`）双向映射为 `RTH`/`EXTENDED`/`24H`；
     * 处理 WS 不推送 `clientOrderId` 的问题：实现基于本地 WAL 预备记录的 `clientOrderId <-> orderId (UUID)` 绑定映射器；
     * **【关键修正 V2.2-U1：防 `tc - FN` 虚假巨额佣金】**：在 `orderReport` 归一化器中，当 `s != "filled"`（或 `fq < Q`）时，**严禁将 `tc - FN` 算作已实现佣金**（因为 `tc` 包含未成交份额的冻结本金）！部分成交期间仅按预估费率计提浮动费用或用 `tc - Q * p` 估算整单预留费；仅当 `s == "filled"` 且 `fq == Q` 时可用 `tc - FN` 作为买单总佣金初值，且最终一律以 REST `GET /order/detail` 的 `fee` 字段为准。
3. 固化不可越权宪法规则（代码级硬编码校验器 `ConstitutionGuard`，外部配置文件无法绕过）：
   - 仅允许 `/sapi/v1/equity/*`、只读 SAPI Funding 钱包接口及只读历史研究数据源；硬编码拦截一切 crypto/合约/杠杆/做空（`side="SELL"` 时若无已确认多头持仓直接抛出 `ShortSellingForbiddenError`）及 `/tokenized/mint`、`/tokenized/redeem`；
   - 强制 `quoteAsset="USDC"`, `walletType="CARD"`, `tokenize=False`；
   - 严格校验字段矩阵：`BUY LIMIT` 与 `SELL LIMIT` 必填 `price, quantity, tradingSession`（禁传 `notional`）；`SELL MARKET` 必填 `quantity`（禁传 `price, notional, tradingSession`）；开仓禁用 `BUY MARKET`；
   - `price` 最多 2 位小数；`clientOrderId` 严格匹配正则 `^[a-zA-Z0-9-_]{32,36}$`；
   - 最大同时 5 个持仓（含在途未终态买单占槽位）、8% HWM 持久化回撤锁、RTH 交易时段（15:45 ET 禁开新仓、15:55:00 ET 启动强平，半日市对应 12:45 ET / 12:55:00 ET）、最小经济可行仓位 `min_viable_notional_usdc = Decimal("120")`、双边摩擦占毛利上限 `<= 20%`、全局默认 `live_trade_enabled = False`。
4. 编写完整单元测试（`pytest` + `ruff` + `mypy` 全绿）：
   - 必须包含官方 `orderReport` 部分成交样例（`Q=10, p=185.5, fq=5, FN=927.5, tc=1856.2`）的专门测试，断言计算出的已实现佣金绝不等于 `928.7`，且账户净值不会因此暴跌误触发 8% HWM 锁；
   - 包含越权配置拦截、错误参数组合、非法 `clientOrderId`、超 2 位小数价格、非 RTH 开仓拦截、做空拦截、小于 120 USDC 仓位拦截的全部反例测试。

完成后将代码同步至本地与日本服务器独立研发目录，运行测试并按《V2.2 统一报告格式（第8节）》提交阶段 1 验收报告（保存至 `D:\Strategy\docs\stage_01_report.md`），不启动任何真实网络交易，等待我审批。

```

**验收门槛 G1**：`pytest` / `ruff` / `mypy` 100% 通过；`orderReport` 部分成交 `tc - FN` 陷阱及所有宪法禁令均由单元测试 100% 覆盖。

## 阶段 2 — 三级 WebSocket 分片池、本地日内 K 线 WAL 热恢复与三维只读对账适配

> **何时发送**：阶段 1 验收通过后发送。

```
阶段 1 已由投资人验收通过。现在进入【阶段 2：三级 WebSocket 分片池、本地日内 K 线 WAL 热恢复与三维只读对账适配】。
本阶段仅使用无副作用的只读接口与 WebSocket 流，绝不调用下单（`/order/place`）或撤单（`/order/cancel`）接口。

本阶段实现要求：
1. REST 只读与三维持仓/资金对账适配器（合入 V2.2-U3, U5）：
   - 实现 `/sapi/v1/equity/market/exchangeInfo`、`/market/tokenized-assets`、`/market/quote`（专门处理未知或停牌标的返回 HTTP 200 空响应体 `b""` 的边界情况，严禁因 `json.loads("")` 崩溃）；
   - 实现 `/sapi/v1/equity/order/open-orders`、`/order/detail`、`/order/history`（硬编码校验：`orderStatus` 过滤参数仅允许传终态枚举 `FILLED,PARTIALLY_FILLED,CANCELED,EXPIRED,REJECTED`，绝不传 `NEW` 或 `ACCEPTED`）、`/trade/history` 只读查询；
   - 实现 SAPI Funding 钱包（`CARD`）只读查询 + 基于 `/trade/history`、`/order/open-orders` 与本地 SQLite 订单账本的**三维持仓对账器（`PositionLedgerReconciler`）**；
   - 实现签名接口 `POST /sapi/v1/equity/listenKey`（带 `timestamp` 与 HMAC-SHA256 签名）用于创建及每 25 分钟自动续期 `listenKey`，捕获 `-1125`/`-3038` 错误码自动重建 `listenKey` 并重连用户流。
2. 三级漏斗 WebSocket 采集与分片管理（基址 `wss://nbstream.binance.com/equity`）：
   - 统一封装 `/ws/<streamName>` 裸流与 `/stream?streams=<A>/<B>` 组合流信封解析器；处理 `@kline_5m.vw` 为 `null`（回退使用 `(h+l+c)/3` 或成交额/成交量）与 `@quote.T` 为 `null`（回退使用外层事件时间 `E`）的空值保护；
   - L1 全局流：单连接订阅 `price`（3秒全市场快照，解析市场阶段 `mp`）与 `calendar`；
   - L2 重点扫描池（≤300标的）：多连接分片池管理器（每条 WS 连接严格 ≤120 streams），订阅 `{SYMBOL}@kline_5m`、`{SYMBOL}@tradingStatus`、`{SYMBOL}@tradability`；
   - L3 焦点报价池（≤20标的）：通过 WebSocket RPC `SUBSCRIBE`/`UNSUBSCRIBE` 动态订阅候选或持仓标的的 `{SYMBOL}@quote`（200ms 盘口）。
3. 本地日内 K 线 WAL 热恢复与区分流类型的 Staleness Gate（合入 V2.2-U2, U5）：
   - **【解决币安无历史 K 线 REST 接口盲点】**：每根收到的 `@kline_5m` 实时写入本地 SQLite WAL 表 `intraday_bars_wal`；实现盘中重启热恢复器，在进程启动后 100ms 内从本地 WAL 重建当日自 09:30 ET 起的 5m K 线序列、ORB 高低点、RTH 累积 VWAP 与 Wilder RSI(6) 状态；若检测到 >5m 断流缺口，将该标的标记为 `vwap_intact=False`（并提供可选的只读 Alpaca SIP 分钟线回补接口）；
   - **【区分周期流与状态跳变流】**：仅对周期流（`price`/`@kline_5m`/`@quote`）施加消息时间戳陈旧度门禁（Staleness Gate）；对仅在状态跳变时才推送、无周期心跳包的事件流（`calendar`/`@tradability`/`@tradingStatus`），采用启动时 `/market/exchangeInfo` 初始状态 + WS 底层 PING/PONG 存活监控，**严禁因盘中无停牌事件而误判为断流陈旧**！

请在日本服务器上进行只读实测与模拟注入测试，提交：
1) 各 REST 只读接口延迟分布与 HTTP 200 空包处理测试；
2) L1/L2/L3 WS 分片连接稳定性与空值字段回退验证；
3) 模拟盘中 `kill -9` 进程崩溃重启后，当日 5m K 线与 RTH VWAP 从 SQLite WAL 恢复的耗时与 100% 数值一致性证明；
4) 按《V2.2 统一报告格式》生成 `D:\Strategy\docs\stage_02_report.md` 并停下等待我审批。

```

**验收门槛 G2**：300 标的 WS 分片稳定运行，盘中重启后 K 线与 VWAP 100% 从本地 WAL 恢复（<100ms），状态跳变流零误报。

## 阶段 3 — 历史数据库统一（强制 SIP 一致性）、时点股票池与防泄漏对齐

> **何时发送**：阶段 2 验收通过后发送。

```
阶段 2 已由投资人验收通过。现在进入【阶段 3：历史数据库统一（强制 SIP 一致性）、时点股票池与防泄漏对齐】。
请盘点现有 Alpaca 历史数据与候选 CSV，构建全新且绝不覆盖旧库的 V2.2 标准研究数据层（研究区间：`2024-11-01` 至 `2026-10-30`，周期：`1Min, 5Min, 1Hour, 1Day, 1Week`），全量库落盘于本地 `D:\Strategy\data_v2_2\`，并在日本服务器保留研究同步集与元数据校验清单。

本阶段实现要求：
1. 时点有效（Point-in-Time）股票池构建：
   - 读取约 480 只美股及 ETF 候选 CSV，与 Binance Stocks `GET /sapi/v1/equity/market/exchangeInfo` 真实可交易清单取交集；
   - 保留 `TSLA`，硬性排除 `SpaceX`/`SPCX` 及在美上市中国企业（中概股）；
   - 个股按历史时点市值 ≥ 50 亿美元过滤，ETF 按 ETF 自身流动性/规模标准独立过滤（不套用公司市值过滤）；
   - 结合 `listingTime`、`delistingTime` 及 Alpaca `asof` 历史符号改名映射，确保存储的每一天样本只包含当时已上市且符合条件的标的，彻底杜绝幸存者偏差与未来信息泄漏。
2. 强制 `feed=sip` 一致性与质量清洗：
   - 核验所有历史 Bar 的数据源标签：RVOL 与 VWAP 特征必须建立在统一的 `feed=sip` 全市场合并数据之上；若发现任何旧数据为 `IEX`，必须使用 `feed=sip` 重新拉取或严格隔离，严禁混用 `IEX` 与 `SIP` 计算相对成交量；
   - 结合 `exchange_calendars` / NYSE 官方日历（含美东 DST 夏令时/冬令时切换、提前收盘半日市 13:00 ET、节假日休市）精确标记每根 K 线的 `session`（`RTH` / `EXTENDED` / `OVERNIGHT`）；
   - 扫描并修复时间戳缺失、重复 Bar、零成交量、异常价格毛刺及拆股/分红复权调整状态，为每条记录写入明确的质量标志（`quality_flag`）。
3. 预计算无泄漏基准特征与跨域偏移报告（Domain Shift Report）：
   - 预计算严格在每个 RTH 交易日 09:30 ET 重置的累积 VWAP，以及基于“过去 N 个交易日同一分钟时间窗（Time-of-Day bucket）”的历史成交量均值与中位数基准（供 RVOL 与波动率归一化使用）；
   - 对比同一交易时段内 Binance Stocks 录制的实时 `@kline_5m` / `@quote` 与 Alpaca `SIP` 历史 Bar 在价格、成交量、VWAP 上的分布差异，输出《跨数据源偏差预算报告（Domain Shift Report）》。

生成不可变数据版本清单 `data_manifest_v2_2.json`（含各表行数、起止时间、SHA256 校验值）与阶段 3 验收报告 `D:\Strategy\docs\stage_03_report.md`，不做任何策略调参，停下等待我审批。

```

**验收门槛 G3**：历史库 100% 时点一致、无未来信息泄漏、`feed=sip` 口径统一，且完成 Binance 实时流与 Alpaca 历史流的误差对标。

## 阶段 4 — 保守穿透撮合 Backtrader 引擎与事件驱动离线重放器

> **何时发送**：阶段 3 验收通过后发送。

```
阶段 3 已由投资人验收通过。现在进入【阶段 4：保守穿透撮合 Backtrader 引擎与事件驱动离线重放器】。
请构建独立的 Backtrader 研究回测引擎与事件驱动离线订单重放器（Order Replay Engine），专门解决默认回测过度乐观、同根 K 线偷看未来与限价单逆向选择（Adverse Selection）问题。

本阶段实现要求：
1. 杜绝同根偷看与建立双模式对照：
   - 信号只能在 K 线 $t$ 完全闭合（`bar_close` 确认）后生成，委托最早在 $t+1$ 周期进入撮合；
   - 同时实现两套撮合模式作为对照：(a) Backtrader 默认宽松撮合（触价即成，仅作乐观理论上限参考）；(b) **V2.2 保守穿透撮合模式（作为唯一实际上线判据）**。
2. V2.2 保守穿透撮合与微观结构约束：
   - 针对 `BUY LIMIT` 限价单 $L$：若下根 K 线仅触及 `Low == L`，严禁按 100% 成交计！必须满足穿透条件 `Low <= L - delta`（支持配置固定穿透如 `$0.02` 或动态穿透 `0.05 * ATR` / 点差倍数）才视为确定成交；在临界区间 `[L - delta, L]` 内按保守队列模型模拟未成交或部分成交；
   - **同柱 TP/SL 冲突消解**：当同一根 5m K 线的 `[Low, High]` 同时触及止盈价（TP）与止损价（SL）时，引擎必须自动下钻读取同期的 `1Min` K 线序列判定真实触发先后顺序；若在同一根 `1Min` K 线内仍同时触及，一律按**悲观原则（SL 止损优先触发）**结算，并打上 `ambiguous_bar_sl_assumed=True` 审计标记；
   - 完整模拟 `exchangeInfo` 的 `stepSize`、`minQty`、`minNotional`、2位小数限价、分数股限价拒单回退整数股（`486442` 规则）、最小经济可行名义仓位（`min_viable_notional_usdc = 120 USDC`）、双边佣金、买卖点差、网络延迟、TTL 超时撤单，以及 **15:45 ET 停止新开仓、15:55:00 ET（半日市 12:55:00 ET）日终强制平仓**。
3. 全样本候选日志库与反例测试套件：
   - 将所有生成的 `TradeIntent`（含风控拒绝、未成交踏空、部分成交、已成交单）完整落盘，计算每笔已成交与未成交踏空信号的 MFE（最大有利偏移）/ MAE（最大不利偏移）与踏空机会成本；
   - 编写专门的反例单元测试证明：未闭合 K 线不可访问、未来 MFE 不可进入策略决策、休市与 15:45 ET 后禁止新开仓、同柱 TP/SL 冲突严格走 1m 复核或悲观止损。

提交回测/重放引擎代码、反例测试结果及 5 笔人工逐字段复核样例，生成 `D:\Strategy\docs\stage_04_report.md` 并停下等待我审批。

```

**验收门槛 G4**：反例测试 100% 通过，保守穿透撮合与宽松撮合差异量化清晰，每笔交易损益、佣金与滑点可逐笔精确复核。

## 阶段 5 — 严格因果的市场状态分类器与共同特征层

> **何时发送**：阶段 4 验收通过后发送。

```
阶段 4 已由投资人验收通过。现在进入【阶段 5：严格因果的市场状态分类器与共同特征层】。
请基于 `SPY`、`QQQ` 及 11 大行业板块 ETF（`XLK, XLF, XLV, XLY, XLC, XLI, XLP, XLE, XLU, XLRE, XLB`）的价格、成交量、ATR、日内同时间窗相对波动率、VWAP 斜率与市场广度，开发严格因果的市场状态分类器（`MarketRegimeClassifier`）与共享特征快照层（`FeatureSnapshot`）。

本阶段实现要求：
1. 五状态因果分类与防抖机制：
   - 定义标准状态枚举：`TREND_UP`, `SIDEWAYS`, `TREND_DOWN`, `HIGH_VOL_PANIC`, `UNKNOWN`；
   - 仅使用截至决策时点 $t$ 已闭合的 K 线与实时状态，引入日内时间槽归一化（Time-of-Day Normalization）消除开盘/尾盘天然高波动对状态分类的误导，并加入状态切换滞后防抖保护（Hysteresis，防止在临界值附近每 5 分钟频繁跳变）；
   - **安全铁律**：任何基准 ETF 数据缺失、陈旧过期、`vwap_intact == False` 或分类置信度低于阈值时，强制归入 `UNKNOWN`；在 `UNKNOWN` 与 `HIGH_VOL_PANIC` 状态下，100% 严禁任何做多新开仓。
2. 策略路由矩阵与流批一致性契约（Stream-Batch Parity）：
   - 固化各状态下的策略启用门禁矩阵（如 `TREND_UP` 允许 S1/S2，开盘窗口结合 `TREND_UP`/`SIDEWAYS` 允许 S3，`SIDEWAYS` 供 S4 离线研究）；
   - 实现带 `data_asof_utc`、`vwap_intact` 与 `features_hash` 的不可变 `FeatureSnapshot`；
   - 编写专门的【流批一致性测试】：拿同一段历史数据，分别用“离线全量批处理”与“逐根 K 线流式递推（含盘中模拟崩溃从 SQLite WAL 热恢复）”计算全部特征（VWAP、Wilder RSI(6)、ATR(14)、同时间窗 RVOL、日内波动率归一化收益），断言两者输出误差 `< 1e-9`。

仅交付市场状态分类器、共享特征层及 2024-11 至 2026-10 各市场状态时间占比与转移矩阵诊断报告（`D:\Strategy\docs\stage_05_report.md`），不提前优化策略收益，停下等待我审批。

```

**验收门槛 G5**：流批特征计算（含 WAL 热恢复场景）误差 `< 1e-9`，严格因果无泄漏，`UNKNOWN` 与 `HIGH_VOL_PANIC` 100% 阻断新开仓。

## 阶段 6 — S1 日内动量 + 相对强弱 + 日内波动率归一化因果 Kalman/RLS

> **何时发送**：阶段 5 验收通过后发送。

```
阶段 5 已由投资人验收通过。现在进入【阶段 6：S1 日内动量 + 相对强弱 + 日内波动率归一化因果 Kalman/RLS】。
请独立实现策略 S1，仅输出结构化 `TradeIntent` 并接入阶段 4 的保守穿透撮合回测引擎评估，严禁连接真实下单网关。

本阶段实现要求：
1. 解决多重共线性与日内异方差的数学实现：
   - **日内波动率 U 型归一化**：先用历史同时间槽（Time-of-Day bucket，仅由历史窗口统计，严禁跨未来）标准差 $\sigma_{\text{tod}, t}$ 对 5m 收益率做标准化：$\tilde{r}_{i,t} = r_{i,t} / \sigma_{\text{tod}, t}$；
   - **正交化板块因子**：将对应行业板块 ETF 归一化收益对 `SPY` 回归取残差 $\tilde{r}_{\text{sector}, t}^{\perp} = \tilde{r}_{\text{sector}, t} - \hat{\gamma}_t \tilde{r}_{\text{SPY}, t}$，彻底消除大盘与板块因子的共线性；
   - **三种滤波器实现与对照**：在状态空间 $\tilde{r}_{\text{stock}, t} = \alpha_t + \beta_{m,t} \tilde{r}_{\text{SPY}, t} + \beta_{\text{sec}, t} \tilde{r}_{\text{sector}, t}^{\perp} + \epsilon_t$ 上，同时实现：
     (a) 纯因果 Kalman Filter（仅使用前向 Filtering 步 `predict/update`，严禁使用包含未来信息的 RTS Smoother）；
     (b) 带指数遗忘因子 $\lambda \in [0.96, 0.99]$ 的递推最小二乘（RLS）；
     (c) 固定窗口滚动正交 OLS 基线。
2. 信号触发、结构化退出与样本外消融对比：
   - 当特异动量截距 $\alpha_t$ 及其滤波后验信噪比 $z_{\alpha} = \alpha_t / \sqrt{P_{\alpha, t}}$ 超过训练阈值、板块相对强弱为正、处于允许的市场状态、且价格未过度偏离 RTH VWAP（防追高衰竭）时，生成做多 `TradeIntent`；
   - 严格在训练集（`2024-11-01` ~ `2025-10-31`）与滚动验证集（`2025-11-01` ~ `2026-04-30`）上对比：固定 $\beta$ OLS 基线 vs. RLS vs. 因果 Kalman 的信号数量、保守穿透撮合净收益、单笔净期望（bps）、Profit Factor、最大回撤与单步计算耗时（注意：**严禁在此阶段偷看 `2026-05-01` 之后的 Lockbox 测试集**！）；
   - 若因果 Kalman 在验证集上相比 RLS 或滚动 OLS 无显著统计增益，必须如实报告并推荐更稳健简洁的参数化方案。

提交 S1 代码、因果性反例测试（证明修改 $t+1$ 数据绝不影响 $t$ 时刻 $\alpha_t$）及消融对比报告 `D:\Strategy\docs\stage_06_report.md`，停下等待我审批。

```

**验收门槛 G6**：S1 严格前向因果无泄漏，正交化与同时间窗波动率归一化有效，完成验证集三模型消融对比且未触碰 Lockbox 测试集。

## 阶段 7 — S2 VWAP/RSI(6) 趋势回调 + 层级 Bayesian 与多分类单纯形校准

> **何时发送**：阶段 6 验收通过后发送。

```
阶段 6 已由投资人验收通过。现在进入【阶段 7：S2 VWAP/RSI(6) 趋势回调 + 层级 Bayesian 与多分类单纯形校准】。
请独立实现策略 S2：5 分钟 RTH VWAP + Wilder RSI(6) 趋势回调策略，结合分层广义线性/Bayesian 概率模型与多分类联合校准。

本阶段实现要求：
1. 精确指标口径与基础触发条件：
   - RTH VWAP 每日 09:30 ET 归零重置；Wilder RSI(6) 仅在 5m K 线闭合（`k.x == True`）时更新，固化跨日预热（Warmup）规则并由测试锁定；
   - 基础回调买点条件：处于上升趋势状态、收盘价 `> VWAP`、VWAP 斜率 `> 0`、Wilder RSI(6) 在近期回踩窗口内曾下探 `< 40`（洗盘释放短线抛压）随后重新上穿 `50` 确认止跌回升、回调期间缩量且未跌破关键结构支撑位。
2. 完备三分类路径标签与单纯形联合概率校准（解决独立二分类概率和不为 1 的数学缺陷）：
   - 针对每个候选触发点，以未来最大允许持仓窗口 $H$（如 60–90 分钟，且受 15:55 ET 日终强平截断）内**先触及止盈（TP）、先触及止损（SL）、或超时未触及（Timeout）**构造完备互斥的三分类标签 $(y_{\text{TP}}, y_{\text{SL}}, y_{\text{TO}})$（同柱冲突一律下钻 1m 或按悲观 SL 计）；
   - 建立按板块/波动率分层的正则化多分类 Logistic / 层级 Bayesian 模型，并使用 **Multinomial Logistic Calibration 或 Temperature Scaling** 在验证集上进行样本外联合校准，严格满足单纯形约束 $p_{\text{TP}} + p_{\text{SL}} + p_{\text{TO}} = 1$；
   - 基于校准后的三分类概率与对应的期望盈亏幅度，直接计算先验风险调整净期望：
     $$\mathbb{E}[\text{net\_bps}] = p_{\text{TP}} \cdot R_{\text{TP}} - p_{\text{SL}} \cdot R_{\text{SL}} + p_{\text{TO}} \cdot \mathbb{E}[R_{\text{TO}}] - \text{Cost}_{\text{roundtrip}}$$
   - 对同一标的短时间内连续触发的重叠信号实施去重冷却或样本降权，输出多分类 Brier Score、ECE（Expected Calibration Error）、可靠性校准曲线（Reliability Diagram）及 Bootstrap 置信区间。

在训练集与验证集（严禁触碰 2026.05-2026.10 Lockbox 测试集）上对比“纯规则 VWAP+RSI(6) 基线”与“+多分类单纯形校准门禁”的保守撮合净表现，提交 `D:\Strategy\docs\stage_07_report.md` 并停下等待我审批。

```

**验收门槛 G7**：三分类校准概率严格满足 $\sum p_i = 1$（误差 `< 1e-9`），校准曲线 ECE 显著改善，无未来路径泄漏。

## 阶段 8 — S3 ORB (5/15/30m) + 同时间窗 RVOL + 低延迟 CUSUM/SPRT

> **何时发送**：阶段 7 验收通过后发送。

```
阶段 7 已由投资人验收通过。现在进入【阶段 8：S3 ORB (5/15/30m) + 同时间窗 RVOL + 低延迟 CUSUM/SPRT】。
请独立实现策略 S3：开盘区间突破（ORB 5/15/30 分钟）+ 日内同时间窗 RVOL + VWAP + 序贯突破检验，并构建端到端全链路打通测试。

本阶段实现要求：
1. 严格因果的 ORB 与同时间窗 RVOL：
   - 5m（09:30–09:35 ET）、15m（09:30–09:45 ET）、30m（09:30–10:00 ET）的开盘区间最高价/最低价（ORH/ORL）必须在该时间窗完全闭合后才可锁定，窗口闭合前严禁提前生成突破信号；
   - `RVOL` 严格定义为：**当日自 09:30 ET 截至当前分钟的累积成交量 ÷ 过去 $N$ 个交易日（如 20 日）同一分钟时间窗的均值/中位数累积成交量**，彻底消除开盘天然放量偏差。
2. 消除 5m 多步等待滞后的 CUSUM / SPRT 结构设计：
   - 若在突破 ORH 后还等待 3–4 根 5m K 线（15–20 分钟）才完成序贯检验，将彻底错过突破点并买在日内顶部；因此，CUSUM（参数 $k, h$ 由训练集固定）与 Wald SPRT / 单步贝叶斯对数似然比（$\text{LLR}_t$）必须直接作用于 **1 分钟闭合 K 线（离线回测与 1m 聚合）或突破当根/次根的截面标准化价量创新（价格突破幅度/ATR、同时间窗 RVOL、收盘在 Bar 内的分位强度、大盘同步强度）**；
   - 设定严格的最大观察步数（如突破后最多观察 3 分钟或 1 根 5m 确认）与超时未决拒绝状态（`UNDECIDED -> REJECT`）；
   - 入场限价优先设定在突破后微幅回踩 ORH 上沿的可控区间 `[ORH, ORH + c * ATR]`，TTL 设为 1–2 根 5m K 线，超时未成交立即撤销，严禁反复抬价追高。
3. 端到端全链路打通验证：
   - 针对阶段 0 核查的下单链路，编写从 `S3 Signal -> SignalArbitrator -> LimitOptimizer -> PortfolioRisk -> MockExecutionGateway -> Guardian Exit` 的端到端集成测试，断言每一个环节的状态流转、槽位占用与审计日志落盘。

在训练集与验证集（不触碰 Lockbox 测试集）上分别评估 5m/15m/30m ORB 以及“纯 ORB+RVOL 基线 vs. +低延迟 CUSUM/SPRT”的样本外保守撮合净表现与未成交踏空率，提交 `D:\Strategy\docs\stage_08_report.md` 并停下等待我审批。

```

**验收门槛 G8**：ORB 全链路端到端集成测试 100% 畅通，无多步等待滞后追高，完成 5m/15m/30m 三种周期与基线的验证集消融对比。

## 阶段 9 — S4 VWAP/RSI(6) 超跌反弹 + 无量纲化 OU 均值回归（实验隔离）

> **何时发送**：阶段 8 验收通过后发送。

```
阶段 8 已由投资人验收通过。现在进入【阶段 9：S4 VWAP/RSI(6) 超跌反弹 + 无量纲化 OU 均值回归（实验隔离）】。
请独立实现实验策略 S4：VWAP + Wilder RSI(6) 超跌反弹 + 无量纲化 Ornstein-Uhlenbeck (OU) 均值回归模型。**本策略在代码与配置中双重强制锁定 `live_enabled = False`，仅用于离线科学对比研究。**

本阶段实现要求：
1. 解决日内 VWAP 方差发散的无量纲残差构造：
   - 由于 RTH 累积 VWAP 是时间加权积分，原始价差 $P_t - \text{VWAP}_t$ 的方差随日内时间推移而改变，直接拟合 OU 会失真；因此必须先用日内滚动 ATR(14) 进行无量纲化变换：
     $$z_t = \frac{P_t - \text{VWAP}_t}{\text{ATR}_t}$$
   - 在滚动窗口上对无量纲偏离度 $z_t$ 拟合离散 AR(1) / OU 过程：$z_t = a + \phi z_{t-1} + \eta_t$；
   - 仅当滚动窗口满足平稳性诊断、自回归系数 $0 < \phi < 1$ 统计显著、且隐含均值回归半衰期 $\tau_{1/2} = -\frac{\ln 2}{\ln \phi}\Delta t \le 25$ 分钟（小于最大持仓时间的一半）时，才允许进入超跌反弹候选。
2. 暴跌保护与“反接飞刀”硬性门禁：
   - 当市场状态处于 `TREND_DOWN` 或 `HIGH_VOL_PANIC`、大盘快速杀跌、个股伴随极端高 RVOL（如 `RVOL > 2.5`）放量长阴破位、或 $\phi \ge 1$（发散趋势）时，100% 强制拒绝开仓并记录 `FALLING_KNIFE_GUARD_TRIGGERED`。

在训练集与验证集上对比“纯 VWAP+RSI(6) 超跌基线”与“+无量纲 OU 半衰期与暴跌保护门禁”的净表现，总结 S4 的适用与失效边界，生成 `D:\Strategy\docs\stage_09_report.md` 并停下等待我审批。

```

**验收门槛 G9**：无量纲 OU 数学诊断严谨，暴跌反接飞刀门禁有效，`live_enabled = False` 硬编码隔离验证通过。

## 阶段 10 — 统一信号仲裁器与交易资格数学门禁

> **何时发送**：阶段 9 验收通过后发送。

```
阶段 9 已由投资人验收通过。现在进入【阶段 10：统一信号仲裁器与交易资格数学门禁】。
请开发 S1–S4 共享的统一信号仲裁器（`SignalArbitrator`）与净期望资格门禁。

本阶段实现要求：
1. 同标的冲突消解与冷却去重：
   - 以 `(symbol, bar_close_time)` 及可配置的同标的冷却时间窗（Cooldown Window）作为去重键；
   - 当同一标的在同一时刻被多个策略（如 S1 与 S3）同时触发时，严禁重复发单！必须按照各策略样本外校准后的**风险调整净期望（$\mathbb{E}[\text{net\_bps}] / \text{RiskUnit}$）**及预测置信区间下界择优合成或保留唯一最优 `TradeIntent`，并在审计日志中记录被合并/淘汰的次优信号。
2. 跨标的组合排序与槽位分配：
   - 当同一截面产生的合格候选信号数量超过当前可用仓位槽位（`available_slots = 5 - open_positions_count - pending_buy_orders_count`）时，综合风险调整净期望、所属行业板块分散度（防止 5 仓全部集中于同一高相关性细分板块）、盘口点差流动性与所需资金占用进行全局优先级排序。
3. 严格拒绝码（`rejection_reasons`）体系：
   - 任何信号若存在以下任一情形，一律拦截并写入标准化结构化 `reason_code`：
     * `DATA_STALE`（行情陈旧超阈值）
     * `VWAP_GAP_UNRECOVERED`（`vwap_intact == False`）
     * `SYMBOL_NOT_TRADABLE`（`tradability != "BUY_SELL"` 或处于 `TRADING_HALTED`/`TRADING_PAUSED`）
     * `MARKET_REGIME_BLOCKED`（处于 `UNKNOWN` 或 `HIGH_VOL_PANIC`）
     * `PAST_ENTRY_CUTOFF_TIME`（处于美东 15:45 ET 之后，半日市 12:45 ET 之后）
     * `STRATEGY_LIVE_DISABLED`（如 S4 在生产模式下触发）
     * `NEGATIVE_OR_INSUFFICIENT_NET_EDGE`（扣除双边佣金与穿透滑点后 $\mathbb{E}[\text{net\_bps}] \le 0$，或双边总摩擦成本占目标止盈毛利 `> 20%`）。

提交仲裁器代码、多策略同标的并发冲突测试、跨标的满槽位择优测试及仲裁审计日志样例，生成 `D:\Strategy\docs\stage_10_report.md` 并停下等待我审批。

```

**验收门槛 G10**：同标的冲突与跨标的槽位竞争 100% 确定性消解，在途买单预占槽位严密，所有拒单原因 100% 结构化落盘。

## 阶段 11 — 限价优化器、分数股/整数股自适应与最小经济仓位门禁

> **何时发送**：阶段 10 验收通过后发送。

```
阶段 10 已由投资人验收通过。现在进入【阶段 11：限价优化器、分数股/整数股自适应与最小经济仓位门禁】。
请实现可插拔的限价执行数学优化模块（`LimitOptimizer`）与执行网关前置幂等协议，将通过仲裁的 `TradeIntent` 转化为精确符合 Binance Stocks 规则的委托请求。

本阶段实现要求：
1. 三档候选限价与风险调整净效用最大化：
   - 针对每个合格 `TradeIntent` 构建三档候选买价 $L \in \{L_{\text{passive}}, L_{\text{retest}}, L_{\text{aggressive}}\}$：
     * `passive`：当前买一价 `bidPrice` 或微幅回踩价；
     * `retest`：关键结构位 / VWAP / ORH 回踩价；
     * `aggressive`：当前卖一价 `askPrice` 或受控可成交限价（但严禁超过 `entry_price_ceiling`）；
   - 在候选等待时间窗 $T$（TTL，如 1–3 根 5m K 线）上评估：
     $$U(L, T) = P(\text{fill} \le T \mid X, L) \cdot \mathbb{E}[\text{netPnL} \mid \text{fill}, L, X] - C_{\text{missed}}(L, T) - \lambda \cdot \text{CVaR}_{\alpha}(\text{Loss} \mid X, L, T)$$
   - 其中 $P(\text{fill} \le T \mid X, L)$ 基于阶段 4 保守穿透撮合准则在历史样本上训练并校准，严禁使用未来成交结果。
2. 交易所精度、分数股规则（`486442`）与最小经济可行仓位（强制合入 V2.2-U6, U7）：
   - 所有价格严格使用 `Decimal(..., ROUND_DOWN)` 截断至最多 2 位小数（`$0.01` tick），严格满足 `stop_price < L <= entry_price_ceiling < take_profit_price`，并校验在 `/exchangeInfo` 的 `[referencePrice * multiplierDown, referencePrice * multiplierUp]` 价格笼子内；
   - **分数股与整数股双模式自适应（应对 `486442`）**：计算初始目标份额 $Q = \min\left(\frac{\text{RiskBudgetUSDC}}{L - \text{StopPrice}}, \frac{\text{MaxPositionUSDC}}{L}\right)$；若目标标的不支持 RTH 分数股 `LIMIT` 委托（或配置为整数股安全模式），强制将 $Q$ 向下取整为整数股 $\lfloor Q \rfloor$（按 `stepSize` 对齐）；若取整后 $Q_{\text{final}} < \max(1, \text{minQty})$，立即拒绝下单并记录 `WHOLE_SHARE_EXCEEDS_RISK_OR_CAP`；
   - **最小经济可行仓位门禁（V2.2-U7）**：若最终委托名义金额 $Q_{\text{final}} \times L < \text{min\_viable\_notional\_usdc}$（默认 `120 USDC`），或预计双边总摩擦成本（买佣+卖佣+点差）占目标止盈毛利的比例 `> 20%`，立即拒绝下单并记录 `NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY`；
   - **下单超时幂等恢复协议（应对 `-1006`/`-1007`）**：在调用 `POST /sapi/v1/equity/order/place` 之前，必须先将生成的 32–36 位 `clientOrderId` 与订单参数写入本地 SQLite WAL 表（状态 `WAL_PREPARED`）；若请求发生网络超时或收到 `-1006 UNEXPECTED_RESP` / `-1007 TIMEOUT`，**严禁更换新的 `clientOrderId` 盲目重发**！必须先调用 `GET /sapi/v1/equity/order/detail?clientOrderId=...` 核实上游是否已落单。

在离线重放器与 Mock Gateway 中全面测试三档限价效用选择、高价股整数股取整拦截、低于 120 USDC 碎单拦截及 `-1007` 超时幂等恢复，生成 `D:\Strategy\docs\stage_11_report.md` 并停下等待我审批。

```

**验收门槛 G11**：限价精度、价格笼子、最小经济仓位（`120 USDC`）、分数股 `486442` 边界及 `-1006`/`-1007` 超时幂等恢复 100% 合规通过。

## 阶段 12 — 独立 24/7 Guardian 守护进程、撤单竞态（`486446`/`486444`）与阶梯降级退出

> **何时发送**：阶段 11 验收通过后发送。

```
阶段 11 已由投资人验收通过。现在进入【阶段 12：独立 24/7 Guardian 守护进程、撤单竞态（`486446`/`486444`）与阶梯降级退出】。
请实现完全独立于策略引擎的 24/7 止盈止损守护服务（`guardian`）与三维对账服务（`reconciler`），并在 Mock Broker 与故障注入环境中完成 15 项极限验证。

本阶段实现要求：
1. 完整持久化状态机与基于真实成交份额的动态保护：
   - 严格实现第 5.1 节状态机：`NO_POSITION -> BUY_PENDING -> PARTIAL_POSITION / PROTECTED_POSITION -> TP_PENDING -> CANCEL_PENDING_FOR_SL -> EXIT_EXECUTION_LADDER -> FLAT_RECONCILED`（以及异常态 `UNCERTAIN / EXIT_REQUIRED / FROZEN_ESCALATED`）；
   - 只要买单发生任何部分成交（`PARTIALLY_FILLED` 或 `fq > 0`），Guardian 立即以真实已成交净份额建立 SL、TP 与最大持仓时间（Max Holding Seconds）保护记录；买单超时撤单后若存在部分成交，仅对实际已成份额挂止盈/止损；
   - **15:55:00 ET 日终前置强平（V2.2-U4）**：每日 **15:55:00 ET（半日市 12:55:00 ET）** 自动触发所有在途买单撤销与日内持仓强平流程，严禁拖延至 15:59:55 ET 导致撤单排队跨过 16:00:00 ET 边界。
2. 彻底解决“TP 挂单中触发 SL”的撤单竞态与错误码 `486446`/`486437`/`486444`（合入 V2.2-U6）：
   - 当持仓存在在途 `SELL LIMIT` 止盈单而触发止损（SL）、超时或 15:55 ET 强平时，Guardian 必须进入 `CANCEL_PENDING_FOR_SL` 状态：
     (a) 立即加锁禁止对该持仓产生任何并发新卖单，调用 `POST /sapi/v1/equity/order/cancel` 撤销在途 TP 单；
     (b) **防已止盈重复卖（`486444`）**：若撤单返回 `486444`（Order not found or already filled），立即调用 `GET /sapi/v1/equity/order/detail` 核查原 TP 单状态——若确认原 TP 单已 `FILLED`，直接更新账本转入 `FLAT_RECONCILED`，**严禁再发止损卖单**！
     (c) **防撤单排队中份额冻结报错（`486446` -> `486437`）**：若撤单返回 `status="S"` 或 `486446`（Cancel pending, shares remain frozen），严禁立即盲目发 `SELL MARKET`！必须等待 WebSocket `orderReport` 推送 `x="ORDER_TERMINAL"`（`s="canceled"`）或轮询 `/order/detail` 确认撤单终态（设超时重试与对账升级），然后精确计算剩余未成交可卖份额 `remaining_sellable_qty = total_filled_buy_qty - tp_order_filled_qty`，仅当 `remaining_sellable_qty > 0` 时进入下一步退出执行。
3. 阶梯降级退出器（`EXIT_EXECUTION_LADDER`，合入 V2.2-U4）：
   - Step 1（RTH 首选）：在 RTH 时段且市场正常交易时，发送 `SELL MARKET`（仅传 `quantity=remaining_sellable_qty`，禁传 `price/notional/tradingSession`）；
   - Step 2（无深度或市价受限自动降级）：若 `SELL MARKET` 遇 `-1112 NO_DEPTH` 或 `486413` 拒绝，立即在价格笼子内按 `max(multiplierDown_floor, current_bid * Decimal("0.997"))`（截断至 2 位小数）降级发送可立即成交的 **Marketable `SELL LIMIT`（`tradingSession="RTH"`）**；
   - Step 3（极端跨过 16:00 ET 盘后兜底）：若因极端网络故障或撤单排队跨入 16:00 ET 盘后触发 `486441`（Extended hours market orders not allowed），立即将整数股部分 `floor(remaining_sellable_qty)` 改发 `EXTENDED SELL LIMIT` 平仓，零头分数股标记为 `EXIT_REQUIRED` 持久化保留至下一 RTH 开盘第一秒平仓并持续向投资人发出最高级告警。

编写并执行 **15 项自动化故障注入测试（Chaos & Fault Injection Suite）**：
1) 买单部分成交后 TTL 到期撤单，Guardian 准确接管部分成交份额；
2) TP 挂单期间触发 SL，撤单返回 `status="S"` 但 800ms 后才收到 `ORDER_TERMINAL`；
3) 撤 TP 单时收到 `486446`（撤单进行中份额冻结），等待解冻后成功止损且不触发 `486437`；
4) 撤 TP 单时收到 `486444`（原 TP 单刚在 10ms 前完全成交），Guardian 查 `/order/detail` 确认已止盈并直接 `FLAT_RECONCILED`，零重复卖单；
5) 撤 TP 单期间原 TP 单发生部分成交（如 10 股成交了 4 股后被成功撤销），Guardian 准确只对剩余 6 股发止损单；
6) `SELL MARKET` 遇 `-1112 NO_DEPTH`，自动降级为 Marketable `SELL LIMIT` 成功平仓；
7) `SELL MARKET` 遇 `486413`，自动降级为 Marketable `SELL LIMIT` 成功平仓；
8) 模拟极端延迟跨过 16:00 ET 遇 `486441`，整数股自动改发 `EXTENDED SELL LIMIT`；
9) 15:55:00 ET（及半日市 12:55:00 ET）日终前置强平准时触发并清空在途单与持仓；
10) 持仓期间标的突发停牌（`TRADING_HALTED`），Guardian 保持 `EXIT_REQUIRED` 状态并在复牌后自动完成退出；
11) WebSocket `{listenKey}@orderReport` 断线丢包，Guardian 通过 `/order/detail` 轮询与三维对账自动恢复真实状态；
12) `signal_engine` 策略进程被 `kill -9` 强杀崩溃，独立 `guardian` 进程不受影响并正常完成持仓止损平仓；
13) `guardian` 进程自身被 `kill -9` 重启，启动后 100ms 内从本地 SQLite WAL 与三维对账恢复所有受保护持仓状态；
14) 下单遇 `-1007 TIMEOUT`，通过相同 `clientOrderId` 查 `/order/detail` 幂等恢复；
15) 三维对账发现本地持仓与远端不一致时，立即锁定 `UNCERTAIN / HALT_NEW_ENTRIES` 阻断新买单并告警。

提交全部代码与 15 项故障注入测试 100% 通过的日志证据，生成 `D:\Strategy\docs\stage_12_report.md` 并停下等待我审批。

```

**验收门槛 G12**：15 项故障注入测试 100% 通过，策略崩溃绝不影响 Guardian 独立平仓，`-1112`/`486441`/`486444`/`486446` 全部闭环。

## 阶段 13 — 账户级总风控 `PortfolioRisk`（8% HWM 锁、防 `tc-FN` 误锁、PDT 防护与三维对账）

> **何时发送**：阶段 12 验收通过后发送。

```
阶段 12 已由投资人验收通过。现在进入【阶段 13：账户级总风控 `PortfolioRisk`（8% HWM 锁、防 `tc-FN` 误锁、PDT 防护与三维对账）】。
请实现账户级唯一资金与风险审批权威模块 `PortfolioRisk`，并在 Mock Broker 下执行多进程并发与极端行情压力测试。

本阶段实现要求：
1. 真实账户净值（Equity）核算与防 `tc - FN` 误锁保护（强制合入 V2.2-U1, U3）：
   - 实时计算真实总权益：
     $$\text{Equity} = \text{Available\_USDC}_{\text{CARD}} + \text{Locked\_Buy\_Principal\_USDC} + \sum_{i \in \text{Positions}} \left(Q_i \times \text{BidPrice}_i - \text{EstSellFee}_i\right)$$
   - **【核心防误锁校验 V2.2-U1】**：当买单处于 `NEW`、`ACCEPTED` 或 `PARTIALLY_FILLED` 状态时，未成交部分的本金仍在冻结中，**严禁把 WebSocket `orderReport` 的 `tc - FN` 当作费用或亏损从 Equity 中扣除**！编写专门的不变量断言：一笔 `320 USDC` 的买单在成交 0 股、成交 1 股、成交一半及全部成交的整个生命周期内，账户计算的 Equity 波动仅等于真实的已成交份额买卖点差与真实已实现佣金，绝不会出现因冻结本金导致的虚假净值跳水；
   - 严格执行槽位与集中度限制：最多同时 **5 个独立标的槽位**（`open_positions + pending_buy_orders <= 5`）、单笔风险预算默认 `0.25%` Equity（`1,600 USDC` 时为 `4 USDC`）、单标的名义仓位上限默认 `20%` Equity（`320 USDC`）、单笔最小经济可行名义金额 `120 USDC`。
2. 持久化 8% 历史高水位（HWM）永久回撤锁：
   - 账户历史最高净值（`HWM_Equity`，初始值为 `max(1600.00, 首次启动核实的真实Equity)`）与锁定状态（`HALT_NEW_ENTRIES`）必须通过 **SQLite WAL 事务 + 独立带 HMAC/SHA256 签名校验的本地状态文件** 双重持久化落盘；
   - 一旦检测到 `Current_Equity <= HWM_Equity * Decimal("0.92")`（回撤达 8%），立即原子性触发 `HALT_NEW_ENTRIES`：
     (a) 拒绝一切新开仓请求；
     (b) 立即向 `execution_gateway` 发出指令撤销所有在途未成交 `BUY` 单；
     (c) 仅保留 `guardian` 的风险降低操作（止损/止盈/日终平仓卖出）；
     (d) 发出最高级别持久化告警；
   - **不可绕过铁律**：进程重启、服务器重启、跨交易日日切、策略版本升级，均**绝不重置 HWM 或自动解除 `HALT_NEW_ENTRIES`**；解除锁定的唯一途径是投资人执行带显式确认签名的管理员解锁命令。
3. PDT（`486440`）与交割保护门禁：
   - 内置滚动 5 个交易日内的日内回转交易（Day Trade）计数器（支持按账户规则配置上限开关），并实时捕获上游错误码 `486440`（PDT 限制）与 `486439`（资产不足），一旦触发立即进入当日禁买保护状态并告警。

在 Mock 环境下完成极限并发与压力测试：
1) 10 个并发信号同时冲击 `PortfolioRisk`，验证严格只放行前 5 个槽位，第 6 个 100% 拦截；
2) 模拟 5 笔各 `320 USDC` 买单同时处于 `PARTIALLY_FILLED`（如各成交 10%），证明账户 Equity 计算精准、绝不因 `tc - FN` 误触发 8% HWM 锁；
3) 模拟真实持仓市值下跌导致总净值从 `1,600 USDC` 跌至 `1,472 USDC`（-8.0%），验证毫秒级触发 `HALT_NEW_ENTRIES`、自动撤回所有在途买单、允许 Guardian 继续平仓卖出，且模拟 `kill -9` 重启后锁定状态依然 100% 保持生效。

提交形式化风控不变量验证报告 `D:\Strategy\docs\stage_13_report.md` 并停下等待我审批。

```

**验收门槛 G13**：形式化风控不变量 100% 满足，部分成交绝不误触发 HWM 锁，真实 8% 回撤 100% 永久锁定且跨重启不可绕过。

## 阶段 14 — 全样本科学回测、四阶消融、DSR/邻域稳健性检验与 Go/No-Go 决策包

> **何时发送**：阶段 13 验收通过后发送。

```
阶段 13 已由投资人验收通过。现在进入【阶段 14：全样本科学回测、四阶消融、DSR/邻域稳健性检验与 Go/No-Go 决策包】。
请冻结所有策略代码与验证集选出的超参数，在 `2024-11-01` 至 `2026-10-30` 标准历史库上执行完整的：
- **训练集（Train）**：`2024-11-01` 至 `2025-10-31`（12个月）
- **滚动验证集（Walk-Forward Validation）**：`2025-11-01` 至 `2026-04-30`（6个月）
- **最终锁定测试集（Holdout Lockbox Test）**：`2026-05-01` 至 `2026-10-30`（6个月，边界两侧强制施加至少 2 个交易日 Purge + Embargo 隔离带，**参数冻结后仅允许运行一次**作为最终 Go/No-Go 准据）

本阶段实现要求：
1. 四阶消融实验、DSR 与参数邻域稳健性检验（强制合入 V2.2-U8）：
   - 针对 S1、S2、S3、S4 分别输出：
     * **Stage A**：纯规则简单基线
     * **Stage B**：+ 数学滤波器 / 多分类校准概率模型
     * **Stage C**：+ `LimitOptimizer` 限价优化与保守穿透撮合（`Low <= L - delta`）
     * **Stage D**：+ 动态结构/MFE 止盈止损与完整 `PortfolioRisk`（1,600 USDC，最多 5 仓，120 USDC 最小经济仓位，15:55 ET 强平）
   - 在验证集与最终 Lockbox 测试集上分别统计并汇报：总候选信号数、风控拦截数（按 `reason_code` 分类）、限价单成交率、未成交踏空笔数及踏空机会成本、扣除真实双边佣金与穿透滑点后的净收益（USDC 与 %）、单笔净期望（bps 与 USDC）、Profit Factor、胜率、平均盈亏比、最大回撤、CVaR(95%)、Bootstrap 95% 置信区间；
   - **防伪发现检验（V2.2-U8）**：计算多重假设检验校正后的 **Deflated Sharpe Ratio (DSR)**，并输出 **核心超参数上下浮动 ±15% 的参数邻域净期望矩阵**；
   - **铁律裁定**：任何在 Lockbox 测试集（`2026-05-01` ~ `2026-10-30`）上净期望 $\le 0$、DSR 不显著、或参数浮动 ±15% 即由盈转亏的策略，必须如实标记为 `NO_GO / RESEARCH_ONLY`，严禁为了好看而回调参数重新跑 Lockbox！
2. 极端摩擦压力测试与上线前全面安全审计：
   - 压力测试：测试 (a) 双边佣金与点差翻倍、(b) 限价穿透门槛 `delta` 加倍、(c) 委托延迟增加 1–5 秒、(d) 开盘剧烈跳空 场景下的净值表现与最大回撤是否仍受控于 8% HWM 锁内；
   - 安全与合规逐条核查清单（提供代码级证明）：零明文密钥、仅允许接口白名单、`walletType="CARD"`、`tokenize=False`、分数股 `486442` 保护、最小经济仓位 `120 USDC`、WS `EQ_` 前缀与小写归一化、防 `tc-FN` 误算、本地日内 K 线 WAL 热恢复、状态跳变流防误判、Guardian `486446`/`486444`/`-1112`/`486441` 阶梯退出、15:55:00 ET 前置强平、8% HWM 永久回撤锁。

提交完整可复现审计包、P0/P1/P2 问题清零证明、分策略上线推荐清单（明确哪些策略 Go、哪些 No-Go）及最终 Go/No-Go 决策报告 `D:\Strategy\docs\stage_14_go_nogo_report.md`。**严禁触碰实盘，输出报告后立即停下等待投资人最终裁定。**

```

**验收门槛 G14**：P0/P1 缺陷 100% 清零，Lockbox 测试集（仅跑一次）通过 DSR 与 ±15% 参数邻域稳健性检验；未达标策略严格剔除。

## 阶段 15 — 日本云端受控上线（必须获得投资人针对本阶段的专门书面授权）

> **何时发送**：仅当你审阅了阶段 14 的 Go/No-Go 报告并明确决定启动实盘后发送。

```
阶段 14 已由投资人审阅通过，我现在正式书面授权启动【阶段 15：日本云端（`43.163.203.127`）受控实盘切换】。

请严格按照以下标准操作程序（SOP）在日本服务器 `43.163.203.127` 上执行：
1. 上线前完整备份与一键回滚点制作：
   - 若服务器上存在任何旧版生产代码、配置、数据库、systemd 单元或历史日志，先完整打包备份至独立归档目录，记录 Git Commit SHA 与全部文件的 SHA256 校验值；
   - 编写并验证一键回滚脚本 `rollback_to_pre_v2_2.sh`；**严禁新旧两套交易执行进程同时运行**。
2. 生产前置核验清单（Pre-flight Check）：
   - 核验美股交易免责声明签署状态（`POST /sapi/v1/equity/account/disclaimer`，防 `486410`）；
   - 核验 SAPI `CARD`（Funding 钱包）实际可用 `USDC` 余额与三维持仓对账器初始状态，确认无遗留未知在途单或未受保护持仓；
   - 核验日本服务器 `chrony`/`systemd-timesyncd` NTP 时间同步（与 UTC 时间偏差 `< 100ms`）、NYSE 交易日历（含 15:45 ET 禁开新仓与 15:55:00 ET 日终强平定时）、8% HWM 初始基准值落盘、告警通知通道畅通。
3. systemd 独立守护服务部署与严格按序灰度启动：
   - 配置 systemd 独立服务单元（确保 `guardian` 与 `reconciler` 独立于 `signal_engine` 运行，配置 `Restart=always` 与日志落盘）：
     * 第一步：启动 `bstocks-market-ingestor.service`（含本地 SQLite WAL 日内 K 线落盘）与 `bstocks-reconciler.service`，观察并确认 L1/L2/L3 WebSocket 流、`POST /sapi/v1/equity/listenKey` 创建与续期、以及三维对账 100% 正常；
     * 第二步：启动独立 `bstocks-guardian.service` 并验证心跳与应急撤单/平仓链路就绪；
     * 第三步：启动 `bstocks-execution-gateway.service` 与 `bstocks-signal-engine.service`，**仅启用在 G14 Lockbox 测试中正式批准 Go 的策略（S4 及任何未通过 G14 的策略强制保持 `live_enabled=False`）**；
   - 首周采用保守灰度风险参数（如单笔风险预算 `0.15%–0.20%`、最多同时 `2–3` 个持仓槽位、最小经济仓位 `120 USDC`）观察真实限价单成交率、穿透滑点与接口稳定性；任何 P0 异常立即自动触发 `HALT_NEW_ENTRIES` 并告警。

完成部署后，提交上线检查表、各 systemd 服务运行状态证据、实时健康度监控输出及首日對账报告 `D:\Strategy\docs\stage_15_deployment_report.md`。

```

**验收门槛 G15**：日本服务器 systemd 独立服务稳定运行，三维持仓与 `CARD` 钱包账实 100% 相符，Guardian 独立存活，零越权调用。

## 阶段 16 — 周期复盘与受控数学模型迭代

> **何时发送**：阶段 15 实盘稳定运行后发送，用于建立常态化自动日报/周报与漂移监控。

```
阶段 15 已在日本服务器稳定运行。现在进入【阶段 16：周期复盘与受控数学模型迭代】。

本阶段实现要求：
1. 自动化复盘与归因报告服务（`bstocks-reporter`）：
   - 每个美股交易日 RTH 收盘并完成 16:05 ET 最终三维对账后，自动生成《每日实盘交易审计日报》，每 5 个交易日自动生成《周度量化归因周报》（同步保存至服务器与本地 `D:\Strategy\reports\`）：
     * 统计分策略真实信号数、风控拒绝分布、限价单成交率、部分成交比例、平均价格改善（Price Improvement）vs. 逆向选择损耗（Adverse Selection Cost）；
     * 统计真实实现滑点、双边佣金占毛利比例、止盈/止损/15:55 ET 日终强平分布；
     * 对比实盘真实胜率/净期望与阶段 14 回测及模型预测概率的校准偏差（ECE / Brier Score），计算特征分布漂移指标（PSI — Population Stability Index）。
2. 不可变审计轨迹与严控迭代纪律：
   - 确保每笔真实委托在 SQLite 与审计日志中 100% 完整关联：`intent_id <-> clientOrderId <-> orderId <-> features_hash <-> model_version <-> 当时盘口快照`；
   - 固化《模型迭代变更 SOP》：任何模型超参数、特征或限价优化器的修改建议，只能在隔离研究分支中走“离线训练 → 验证集消融 → G14 锁定测试（含 DSR 与 ±15% 邻域稳健性） → 投资人书面批准”完整流程；**严禁线上进程自学习、自动改参或放宽 8% HWM 回撤锁与 5 仓上限**。

提交自动化日报/周报生成模块、PSI 模型漂移监控脚本、版本回滚演练记录及 `D:\Strategy\docs\stage_16_operations_report.md`。

```

**验收门槛 G16**：每笔实盘交易可 100% 追溯还原，实盘与回测偏差量化清晰，迭代审批闭环稳固。