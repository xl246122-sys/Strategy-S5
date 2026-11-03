# Binance Stocks 美股/ETF 自动短线交易机器人 V2.2（终极审计完善版）

**PLAN MODE｜完整开发蓝图与逐阶段开发 Prompt｜2026-11-02**

> 本文在 V2.1（Argon / Astra 基准版）基础上，结合 [Binance Stocks 官方 API 文档](https://developers.binance.com/en/docs/products/stocks/introduction) 逐字段契约核验与实盘微观结构压力推演，额外修复了 **8 项隐蔽但致命的工程与数学漏洞**（编号 **V2.2-U1 至 V2.2-U8**）。每个阶段执行完毕必须提交证据和验收报告，等待投资人明确确认才进入下一阶段。禁止一次性执行全部 Prompt。

## V2.2 新增核心审计升级摘要（相对 V2.1 的 8 大补丁）

| 升级编号 | 级别 | V2.1 残留隐患与官方文档实证冲突 | V2.2 已合入的强制修正规则 |
| :--- | :--- | :--- | :--- |
| **V2.2-U1** | **P0 致命** | V2.1 第 1.2 节规定“由 `tc - FN` 推算 WS 实时买单佣金”。但官方 [`{listenKey}@orderReport`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/ws-streams/user-streams) 示例证实：当买单部分成交（`Q=10, p=185.5, fq=5, FN=927.5`）时，`tc=1856.2` 是**整单冻结总成本（含未成交份额本金）**！若按 `tc - FN` 算佣金会得出 `$928.7` 的虚假费用，瞬间误触发 8% HWM 回撤永锁（`128 USDC`） | 严禁在 `s != "filled"`（或 `fq < Q`）时用 `tc - FN` 计算已实现佣金！未完全成交时按 `预估费率 × FN` 计提浮动费用，或用 `tc - Q * p` 估算整单预留费；订单终态后一律以 [`GET /order/detail`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/trade) 的 `fee` 字段为准 |
| **V2.2-U2** | **P0 致命** | [Market Data REST API](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data) 仅提供 `/exchangeInfo`、`/tokenized-assets`、`/quote`，**无任何历史 K 线 REST 接口**！若进程在盘中 10:30 ET 重启，当日 09:30–10:30 的 5m K 线无法从币安回补，导致当日 RTH VWAP、ORB 区间、RSI(6)、ATR(14) 全部失明 | `market_ingestor` 必须将每根实时 `@kline_5m` 同步写入本地 SQLite WAL 时序表（`intraday_bars_wal`）支持毫秒级热恢复；若发生 >5m 断流缺口，支持只读回补 Alpaca SIP 分钟线或将缺口标的标记 `vwap_intact=False` 禁入依赖日内累积指标的策略 |
| **V2.2-U3** | **P0 致命** | V2.1 仅依赖 SAPI 钱包读取 `EQ_*` 持仓，但官方文档指出 `EQ_{symbol}` 为内部引用代码且 `tokenize=false` 结算为传统底层股票；若 SAPI `/get-funding-asset` 不返回非代币化股票份额，持仓对账将失效 | 建立**三维持仓对账引擎（Position Ledger Reconciler）**：本地 SQLite 事务持仓账本 + `/sapi/v1/equity/order/open-orders` 与 `/trade/history` 成交聚合重建 + SAPI `CARD` 钱包 `USDC` 资金变动闭环核验 |
| **V2.2-U4** | **P0 致命** | V2.1 规定 Guardian 止损只发 `SELL MARKET`。但若 15:59:55 ET 撤 TP 单耗时跨过 16:00 ET 进入盘后，市价单会被 [`486441`](https://developers.binance.com/en/docs/products/stocks/error-code)（盘后仅限限价单）拒绝；盘中遇 `-1112 NO_DEPTH` 或 `486413` 也会卡死 | 1) 将 RTH 日终强平窗口提前至 **15:55:00 ET（半日市 12:55:00 ET）**；2) Guardian 内置**阶梯降级退出器**：`SELL MARKET` 遇 `-1112`/`486413`/`486441` 时，自动在价格笼子内降级发送可成交 `SELL LIMIT`（跨入盘后时整数股改用 `EXTENDED SELL LIMIT`） |
| **V2.2-U5** | **P1 高危** | [`calendar`、`@tradability`、`@tradingStatus`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/ws-streams/market-streams) 均为**状态跳变触发流（无周期心跳包）**；若套用行情流的超时阈值会因全天无停牌而误判“数据陈旧”阻断全部交易；且 `listenKey` 续期接口与现货不同 | 明确区分周期流（`price`/`@kline_5m`/`@quote`）与事件触发流的健康度检测；明确 `listenKey` 创建与每 25 分钟续期统一调用签名接口 [`POST /sapi/v1/equity/listenKey`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/user-data-streams)，监控 `-1125`/`-3038` 错误码 |
| **V2.2-U6** | **P1 高危** | 下单遇 [`-1006 UNEXPECTED_RESP` / `-1007 TIMEOUT`](https://developers.binance.com/en/docs/products/stocks/error-code) 时状态未知；撤 TP 单遇 `486444`（订单已成交无法撤销）时若盲目报错会漏检已止盈状态 | 遇 `-1006`/`-1007` 严禁换新 `clientOrderId` 盲目重发，必须先调 `/order/detail` 核查；`CANCEL_PENDING_FOR_SL` 收到 `486444` 时立即查 `/order/detail`，若原 TP 单已 `FILLED` 则直接转入 `FLAT_RECONCILED`，严禁再发止损卖单 |
| **V2.2-U7** | **P2 中危** | `1,600 USDC` 小资金下，若止损较宽（如 4%），按 `4 USDC` 风险预算算出的名义仓位仅 `100 USDC`，双边固定最低佣金与点差占比过高，侵蚀净期望 | 在 `LimitOptimizer` 与 `PortfolioRisk` 新增**最小经济可行名义金额 `min_viable_notional_usdc`（默认 `120 USDC`）**与**双边总摩擦占目标毛利上限（默认 `≤ 20%`）**门禁 |
| **V2.2-U8** | **P2 中危** | 4 策略 × 多组超参在 6 个月验证集上反复筛选存在多重假设检验伪发现风险（Selection Bias） | 阶段 14 引入 **Deflated Sharpe Ratio (DSR)** 与**参数邻域平稳性检验（核心参数上下浮动 ±15% 样本外净期望仍须为正）**作为硬性 Go/No-Go 准入判据 |

## V2.1 既有核心审计基线（全部保留并强化）

| 审计编号 | 级别 | 核心发现与冲突点 | 强制修正规则 |
| :--- | :--- | :--- | :--- |
| **P0-1** | **致命** | `/sapi/v1/equity/` 下仅有签署免责声明的 [`POST /account/disclaimer`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/account)，无读取 `USDC` 余额接口；且卖单强制结算至 `CARD`（Funding 钱包） | 宪法第 1、2 条开辟 **SAPI 钱包只读白名单**；强制所有 `BUY` 显式传 `walletType=CARD`；上线前必查免责声明签署状态（防 `486410`） |
| **P0-2** | **致命** | [`Error Code`](https://developers.binance.com/en/docs/products/stocks/error-code) 存在 `486442`（分数股仅支持市价单）与 `486443`；而 `1,600 USDC` 下单仓上限 `320 USDC` 买高价股必为分数股 | 阶段 0/2 强制核验分数股 RTH `LIMIT` 可用性；在 `limit_optimizer` 与 `portfolio_risk` 中支持“整数股向下取整”或“支持分数股限价的白名单过滤”双模式 |
| **P0-3** | **致命** | WebSocket [`{listenKey}@orderReport`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/ws-streams/user-streams) 标的 `b` 带 `EQ_` 前缀、枚举 `S`/`o`/`s` 全小写、无 `clientOrderId`，与 REST 全大写规范分裂 | 阶段 1/2 强制实现 `EquityPayloadNormalizer`；下单响应立即持久化绑定 `clientOrderId <-> orderId (UUID)`，以 `orderId` 为唯一对账主键 |
| **P1-1** | **高危** | 错误码 `486440`（Pattern Day Trading 保护触发）可能拦截低于 $25,000 账户的日内高频回转交易 | 阶段 0/2 核实底层清算通道是否豁免 PDT 或受 T+1 现金交割约束；在 `PortfolioRisk` 内置滚动 5 交易日 Day-Trade 计数器门禁（可配置开关） |
| **P1-2** | **高危** | `/order/cancel` 返回 `status="S"` 仅代表上游接收请求，且 `486446` 下撤单排队期间份额仍冻结，立即市价止损会报 `486437`（份额不足） | 阶段 5/12 Guardian 状态机设 `CANCEL_PENDING_FOR_SL` 阶段，必须收到 `ORDER_TERMINAL` 或 `/order/detail` 终态确认后，再按剩余未成交可卖份额发止损单 |
| **P1-3** | **高危** | 300 标的若全量订阅 4 类单标的流将达 1,200 streams；且 `@kline_5m.vw` 与 `@quote.T` 存在 `null` 值 | 阶段 2 实施 **三级漏斗 + 多连接分片池**：全池用单条 `price` 流初筛，重点池分片订阅 `@kline_5m`，仅候选/持仓标的动态订阅 `@quote`；内置 `null` 字段回退逻辑 |
| **P1-4** | **高危** | 2 年 5m 样本下四策略数学模型参数过多、日内异方差、三分类独立校准和不为 1、限价触价即成交存在逆向选择偏差 | 阶段 3–11 升级：S1 引入日内同时间窗波动率归一化与 RLS 对照；S2 改用 Multinomial/Temperature 联合校准；S3 支持 1m/实时截面创新以消除 5m 序贯滞后；S4 改对无量纲 $(P-	ext{VWAP})/	ext{ATR}$ 拟合 OU；回测强制 `Low <= L - delta` 穿透成交 |

## 0. 项目宪法（所有阶段不可放宽）

1. **唯一交易市场与接口边界**：仅交易 [Binance Stocks](https://developers.binance.com/en/docs/products/stocks/introduction) 的**美国上市个股和 ETF**；交易与实时行情操作仅使用 `api.binance.com/sapi/v1/equity/` 系列接口。**仅有的两项只读跨模块白名单**：
   - 因 `/sapi/v1/equity/` 不提供余额查询接口，允许**只读**调用币安 SAPI 钱包资产查询接口（如 Funding Wallet `/sapi/v1/asset/get-funding-asset`）核对真实 `USDC` 可用余额、冻结资金与持仓；
   - 因 `/sapi/v1/equity/market/` **不提供历史 K 线 REST 接口**，允许在本地 WAL 时序库基础上，把 [Alpaca Market Data](https://docs.alpaca.markets/docs/market-data-faq) 严格作为**只读**历史行情与断流缺口修复数据源（绝不用于下单）。
   - 严禁加密货币现货、期货、期权、杠杆、外汇、做空、股票代币铸造/赎回（禁止调用 `/sapi/v1/equity/tokenized/mint` 或 `/tokenized/redeem`）及其他投资品种。
2. **订单参数、钱包一致性与分数股规则**：
   - `quoteAsset=USDC`。所有 `BUY` 订单必须显式指定 **`walletType=CARD`**（Funding 资金钱包），因为官方 [Common Definition](https://developers.binance.com/en/docs/products/stocks/common-definition) 规定所有 `SELL` 订单无论传参为何均强制结算回 `CARD`，统一使用 `CARD` 可彻底杜绝资金在 `MAIN`（现货钱包）与 `CARD` 之间割裂。
   - 股票下单必须显式设置 `tokenize=false`（官方 [Trade REST API](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/trade) 默认为 `true`）；账户首次交易前必须通过 [`POST /sapi/v1/equity/account/disclaimer`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/account) 签署美股免责声明（防错误码 `486410`）。
   - 限价单 `price` 最多两位小数；按 [`GET /sapi/v1/equity/market/exchangeInfo`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data) 逐标的检查 `tradability`、`fractionable`、`fractionableEh`、`stepSize`、`minQty`、`minNotional`（最低 $1.00）及价格笼子 `multiplierUp`/`multiplierDown`。
   - 严格遵守字段矩阵：`BUY LIMIT` 与 `SELL LIMIT` 必填 `price`、`quantity`、`tradingSession`（禁传 `notional`）；`SELL MARKET` 必填 `quantity`（禁传 `price`、`notional`、`tradingSession`）；`BUY MARKET` 必填 `notional`（本策略开仓禁用 `BUY MARKET`）。
   - **分数股与限价单冲突防御**：针对错误码 `486442`（分数股仅限市价单）与 `486443`，必须在只读阶段核实目标标的是否允许分数股 `LIMIT` 委托；若上游拒绝分数股 `LIMIT`，则在 `1,600 USDC` 规模下对单笔计算份额向下取整数股（`stepSize=1`），若 1 股名义金额超出单仓上限（20%）或单笔风险预算（0.25%），或总名义金额低于最小经济可行门槛（`min_viable_notional_usdc = 120 USDC`），则直接拒绝该信号。
3. **运行环境、交易时段（RTH）与收盘前置缓冲**：日本云服务器 24/7 运行。交易策略默认仅在 **America/New_York** 的美股正常交易日常规交易时段（RTH，09:30–16:00 ET，半日市为 09:30–13:00 ET）开仓、日内退出；**在 15:45 ET（半日市 12:45 ET）后停止新开仓，并在 15:55:00 ET（半日市 12:55:00 ET）启动日终强制撤单与平仓流程**，确保平仓动作不会因撤单排队跨过 16:00:00 ET 而触发 `486441`（盘后禁用市价单）。夜间/周末仍监控状态、持仓和报警，但夜盘（OVERNIGHT）、盘前（PRE_MARKET）、盘后（POST_MARKET）不自动开新仓。服务器端止损属于**软件托管合成止损**，若休市、停牌（`TRADING_HALTED`/`TRADING_PAUSED`）、断线、延迟或流动性不足则不保证按设定价成交，必须保留 `EXIT_REQUIRED` 风险状态并持续通知投资人。
4. **纯本地可复现数学决策**：实时交易决策仅由可复现的本地 Python 策略 + 数学模型完成；AI 可以用于离线研发、代码审计、生成复盘报告，但不得实时裁定下单、改动风险限额或自我部署未经审批的新模型。
5. **资金、仓位与 8% HWM 永久回撤锁**：
   - 资金初始规划 `1,600 USDC`，以 `CARD` 钱包真实 `USDC` 可用余额 + 冻结金额 + 持仓按实时 Bid 计价市值（扣除预计卖出费用，严禁在买单未完全成交时误用 `tc - FN` 扣减未成交本金）动态计算真实账户净值（Equity）。
   - 最多同时 **5 个独立标的持仓**；买入在途挂单（`NEW`/`ACCEPTED`/`PARTIALLY_FILLED`）预占仓位槽位（Slot）和可用资金。
   - 初始单笔规划风险 `0.25%` 权益（`1,600 USDC` 时约 `4 USDC`），单标的资金上限初始 `20%` 权益（约 `320 USDC`），单笔最小经济可行名义金额 `120 USDC`，均属可研究参数，任何模块不能自行提高硬性风险上限。
   - **8% 历史高水位（HWM）回撤锁**：账户净值相对持久化历史阶段最高点回撤达到 `8%` 时，立即进入持久化 `HALT_NEW_ENTRIES` 状态——禁止新开仓、取消所有待入场买单、发出最高级告警；只允许风险降低动作（平仓/止损卖出）；解除锁定必须经投资人明确人工批准；进程重启、日切、模型升级绝不重置高点或锁定状态。
   - **PDT 与交割合规保护**：内置对错误码 `486440`（Pattern Day Trading 保护）与资金未交割限制的检测与计数门禁，触发时立即停止新开仓并告警。
6. **交易频率与不留隔夜原则**：日内目标 3–5 笔、单笔希望获得 1–2% 收益，均为研究期望目标，**绝不是必须完成的 KPI**。允许零交易日，严禁为了凑单而放宽净期望或风险门槛。默认不得因为亏损而自动延长持仓至次日；任何跨日交易属未来独立策略，未经授权不实现。
7. **现有生产系统零干扰原则**：现有实盘程序只读审计优先。不得未经批准修改、重启、停用、替换已有执行程序；不得在研发阶段连接真实下单函数；禁止将影子账户/模拟盘作为强制前置流程，采用历史回测、离线重放、Mock 接口与故障注入自动化测试完成验证。只有全部阶段关卡（G0–G14）通过，且投资人明确批准，才在阶段 15 执行受控实盘切换。
8. **股票池与防未来信息泄漏**：前置股票池为约 480 只美股及 ETF CSV 候选，取 `Binance Stocks` [`/market/exchangeInfo`](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data) 真实可交易清单与用户规则的交集；保留 `TSLA`，排除 `SpaceX`/`SPCX` 及在美上市中国企业；个股市值至少 50 亿美元，ETF 采用 ETF 自身流动性/规模标准（不套用公司市值过滤）；公司属性、市值、上市时间（`listingTime`）、退市时间（`delistingTime`）必须按历史可得时点（Point-in-Time）存储，严防幸存者偏差与未来信息泄漏。动态候选池 ≤ 1,000，重点扫描池 ≤ 300，高频报价池 ≤ 20，实际监控数量以吞吐性能与 WebSocket 分片限制为准。
9. **双源历史与实时研究数据库**：研究区间 `2024-11-01` 至 `2026-10-30`，周期涵盖 `1Min, 5Min, 1Hour, 1Day, 1Week`，仅采集上市后的真实可得数据；最终存储在 Windows D 盘新独立目录，云端保留可审计副本与实盘研究特征，绝不覆盖旧库。所有数据表必须显式记录 `feed=IEX/SIP`（严禁混用）、币安实时 WS、交易时段、公司行为调整（拆股/分红/改名 `asof`）、时区、时间戳及质量标志。
10. **单一实盘网关与 `FAIL_CLOSED`**：单一实盘下单入口（`execution_gateway`）、单一订单状态机、独立 `guardian` 风控守护进程；策略层绝不持有交易 API Key，也不得直接调用下单接口；任何资金、持仓、行情新鲜度或订单状态不确定时一律执行 `FAIL_CLOSED`：立即停止新买入，仅允许经核对的风险降低操作。

## 1. 权威接口依据与契约差异对照表

### 1.1 官方文档基准
- **股票交易总览与通用约定**：[Introduction](https://developers.binance.com/en/docs/products/stocks/introduction) 与 [General Info](https://developers.binance.com/en/docs/products/stocks/general-info)。所有接口位于 `/sapi/v1/equity/*`；`POST /order/place` 限频为 200 次/分钟（UID）；买单限价委托时服务端自动计算并冻结佣金（无需传 `fee` 字段），实际成交费用体现在订单详情与历史记录的 `fee` 字段中。
- **枚举定义与状态机**：[Common Definition](https://developers.binance.com/en/docs/products/stocks/common-definition)。明确区分两套状态码：下单/撤单接口同步返回的 `status` 仅为请求受理确认码（`S` = 接受，`F` = 失败），**绝不代表订单成交或撤单完成**；真实订单生命周期状态为 `NEW`、`ACCEPTED`、`PARTIALLY_FILLED`、`FILLED`、`CANCELED`、`EXPIRED`、`REJECTED`。注意 `/order/history` 的 `orderStatus` 过滤参数仅接受终态值（`FILLED,PARTIALLY_FILLED,CANCELED,EXPIRED,REJECTED`），不接受 `NEW` 或 `ACCEPTED`。
- **交易接口与字段矩阵**：[Trade REST API](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/trade)。`tokenize` 默认值为 `true`（代码中必须对所有买单显式传 `tokenize=false`）；`clientOrderId` 必须符合正则 `^[a-zA-Z0-9-_]{32,36}$`。
- **用户流 `listenKey` 专属接口**：[User Data Streams REST API](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/user-data-streams)。创建与续期均调用签名接口 **`POST /sapi/v1/equity/listenKey`**（带 `timestamp` 与签名），有效期 60 分钟，必须在到期前（如每 25 分钟）调用同一接口续期。
- **错误码体系**：[Error Code](https://developers.binance.com/en/docs/products/stocks/error-code)。前置 SAPI 校验错误为负数（如 `-1006`/`-1007` 超时状态未知、`-1102` 参数错误、`-1112` 无对手盘深度、`-1125`/`-3038` `listenKey` 失效、`-26004` 标的不存在），业务层错误为 `486xxx` 正数（如 `486410` 未签免责声明、`486413` 新股暂禁市价单、`486437` 可卖份额不足、`486440` PDT 限制、`486441` 盘后仅限限价单、`486442` 分数股仅限市价、`486444` 订单不存在或已成交无法取消、`486446` 撤单进行中份额保持冻结、`486449` `clientOrderId` 重复）。
- **行情 REST 接口（无历史 K 线接口）**：[Market Data — General Info](https://developers.binance.com/en/docs/products/stocks/market-data-general-info) 与 [Market Data REST API](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api/market-data)。仅含 `/market/exchangeInfo`、`/market/tokenized-assets` 与 `/market/quote`（最高约 5 秒延迟，未知或停牌标的返回 HTTP 200 空 body）。**注意：币安股票 REST 不提供历史 K 线查询**，日内 K 线连续性必须由本地 WAL 持久化保障。
- **WebSocket 专用流**：[WebSocket Streams — General Info](https://developers.binance.com/en/docs/products/stocks/websocket-streams-general-info)、[Market Streams](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/ws-streams/market-streams) 与 [User Streams](https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/ws-streams/user-streams)。基址为 `wss://nbstream.binance.com/equity`。使用 `/ws/<streamName>` 或在 `/ws` 上发送 `SUBSCRIBE` RPC 时推送裸事件 JSON；使用 `/stream?streams=<A>/<B>` 时外层包裹 `{"stream": "...", "data": {...}}`。

### 1.2 必须强制实现的 REST vs. WebSocket `orderReport` 归一化对照表（V2.2 修正版）

| 业务语义 | REST (`/order/detail`, `/order/open-orders`) | WebSocket (`{listenKey}@orderReport`) | 归一化转换规则（`EquityPayloadNormalizer`） |
| :--- | :--- | :--- | :--- |
| **系统订单号** | `orderId`: `"c3c58f49-..."` (UUID) | `i`: `"b0b6dd9d-..."` (UUID) | 统一映射为内部主键 `order_id` |
| **客户端幂等号** | `clientOrderId`（注：仅 `/order/detail` 与 `/order/place` 返回，`/order/history` 不返回） | **不推送此字段** | 下单前先写本地 WAL 预备记录，同步响应立即持久化绑定 `clientOrderId <-> orderId` |
| **股票代码** | `symbol`: `"AAPL"`（大写无前缀） | `b`: `"EQ_AAPL"`（**带 `EQ_` 前缀**） | 强制执行 `b.removeprefix("EQ_")` 还原为 `"AAPL"` |
| **报价币种** | `quote`: `"USDC"` | `q`: `"USD"` | 内部统一标识报价币种并记录原始值 |
| **买卖方向** | `side`: `"BUY"` / `"SELL"`（大写） | `S`: `"buy"` / `"sell"`（**小写**） | 强制 `.upper()` 转换为 `BUY` / `SELL` |
| **订单类型** | `orderType`: `"LIMIT"` / `"MARKET"`（大写） | `o`: `"limit"` / `"market"`（**小写**） | 强制 `.upper()` 转换为标准枚举 |
| **生命周期状态** | `status`: `"NEW"`, `"ACCEPTED"`, `"PARTIALLY_FILLED"`, `"FILLED"`, `"CANCELED"`, `"EXPIRED"`, `"REJECTED"` | `s`: `"accepted"`, `"partially_filled"`, `"filled"`, `"canceled"` 等（**小写**），且带 `x`: `"ORDER_UPDATE"` / `"ORDER_TERMINAL"` | 强制 `s.upper()`，并利用 `x == "ORDER_TERMINAL"` 判定终态 |
| **交易时段** | `session`: `"RTH"` / `"EXTENDED"` / `"24H"` | `n`: `"Regular"` / `"24 Hours Trading"` 等描述文本 | 建立双向字典映射（如 `"Regular" -> "RTH"`） |
| **累计成本与佣金（V2.2 关键修正）** | `filledQty`, `avgFilledPrice`, `fee` (准确已实现佣金) | `fq` (已成数量), `FN` (已成金额), `tc` (**含未成交部分的整单累计买进总成本**) | **严禁在 `s != "filled"` 时用 `tc - FN` 算佣金**（否则会把未成交本金 `(Q - fq) * p` 误算为巨额手续费）！仅当 `s == "filled"` 且 `fq == Q` 时可用 `tc - FN`；部分成交期间按预估费率计提，终态后调用 `/order/detail` 读取精确 `fee` 字段 |

## 2. 总体模块架构

```text
Alpaca历史研究数据库(D盘, 强制SIP一致) ---> 数据质量/特征仓 ---> Backtrader+保守穿透回放引擎
Binance Stocks WS分片池/REST -----------> 本地SQLite WAL日内K线库+Normalizer --> 市场状态分类 --> 4个独立数学策略
  |- L1: 全局 price (3s) + calendar (全池<=1000)                                                    |
  |- L2: 分片 @kline_5m + @tradingStatus + @tradability (重点池<=300)                                |
  |- L3: 动态 @quote (200ms, 候选/持仓<=20)                                                          |
                                                                                                    |
   S1 动量/相对强弱 + 日内波动率归一化Kalman/RLS ---------------------------------------------------|
   S2 VWAP/RSI6趋势回调 + 层级Bayesian + 多分类联合校准 --------------------------------------------|---> 信号仲裁/净期望门禁
   S3 ORB/RVOL + 1m/实时截面CUSUM/SPRT ------------------------------------------------------------|                 |
   S4 VWAP/RSI6超跌 + ATR无量纲化OU/AR(1) (实验默认关闭) -------------------------------------------|                 v
                                                                                                    限价优化器(含分数股/最小经济仓位)
                                                                                                                     |
                                                                                               PortfolioRisk(8%HWM锁/PDT计数/三维对账)
                                                                                                                     |
                                                                                                            单一订单状态机/执行网关
                                                                                                                     |
                                                                              独立Guardian <--- 归一化orderReport与三维持仓/钱包对账
                                                                                   |
                                                            合成止盈/止损(含CANCEL_PENDING_FOR_SL + 阶梯降级退出 + 15:55 ET强平)
```

### 2.1 信号契约 `TradeIntent`（必须结构化且可序列化）

`intent_id, created_at_utc, data_asof_utc, symbol, strategy_id, model_version, regime, features_hash, vwap_intact, signal_valid_until, direction=LONG, p_win_calibrated, p_sl_calibrated, p_timeout_calibrated, expected_return_bps, expected_net_bps, prediction_interval, candidate_limit_prices, p_fill_by_price_horizon, entry_price_ceiling, stop_price, take_profit_price, max_holding_seconds, risk_budget_usdc, min_viable_notional_usdc, expected_cost_usdc, requires_fractional, whole_share_fallback_qty, rejection_reasons, data_feed, data_freshness_ms`。

仅当 `tradeable=true`、`vwap_intact=true`、`expected_net_bps > 0`、校准概率之和 $\sum p_i = 1$、单笔名义金额 $\ge 	ext{min\_viable\_notional\_usdc}$、双边摩擦占比 $\le 20\%$ 且通过 `PortfolioRisk` 账户校验时才允许转化为委托。

### 2.2 默认统一技术规范

- **语言与工具链**：Python 3.11+ / `pyproject.toml` / `pytest` / `ruff` / `mypy`；依赖 `numpy`, `pandas`, `scipy`, `statsmodels`, `scikit-learn`, 自研可测试 Kalman/RLS 模块, `backtrader`；SQLite（开启 WAL 模式与事务锁）或 PostgreSQL 持久化。
- **独立服务进程**：`market_ingestor`, `signal_engine`, `execution_gateway`, `guardian`, `reconciler`, `reporter`；由 `systemd` 托管，保证 `guardian` 在 `signal_engine` 异常崩溃时依然独立存活。
- **三级 WebSocket 订阅、本地 WAL K 线热恢复与空值防御**：
  - L1 全池（≤ 1,000 标的）：单连接订阅 `price`（3 秒全量快照，含市场阶段 `mp`）与 `calendar`；
  - L2 重点池（≤ 300 标的）：采用多连接分片池（每条 WS 连接 ≤ 120 streams，显式区分 `/ws` 裸消息与 `/stream` 组合信封）订阅 `{SYMBOL}@kline_5m`、`{SYMBOL}@tradingStatus`、`{SYMBOL}@tradability`；
  - L3 焦点池（≤ 20 标的）：仅对产生候选信号或已有在途单/持仓的标的动态 `SUBSCRIBE`/`UNSUBSCRIBE` `{SYMBOL}@quote`；
  - **日内 K 线 WAL 持久化（应对币安无历史 K 线 REST 接口）**：每根收到的 `@kline_5m` 实时落盘至本地 SQLite `intraday_bars_wal` 表；进程盘中重启时立即从本地 WAL 重建当日 09:30 ET 以来的 K 线序列、ORB 高低点、RTH 累积 VWAP 与 Wilder RSI(6) 预热状态；
  - **区分周期流与状态跳变流的 Staleness 判定**：`price`/`@kline_5m`/`@quote` 按时间戳陈旧度判定；而 `calendar`/`@tradability`/`@tradingStatus` 属于仅在状态改变时推送的事件流（无周期数据包），以启动时 `/market/exchangeInfo` 基准值 + WS 连接 PING/PONG 心跳存活作为有效依据，绝不因盘中无停牌事件而误判为断流。

## 3. 模型及数学定义（所有参数均为研究初值，需经样本外消融验证）

### 3.1 S1 — 日内动量 + Relative Strength + 日内波动率归一化动态因子 (Kalman / RLS)
- **目标**：识别大盘（`SPY`/`QQQ`）与对应行业板块 ETF 无法解释的正向持续特异动量（Idiosyncratic Momentum）。
- **数学结构**：
  - 为消除美股日内 5 分钟波动率 U 型曲线对滤波器的污染，先用历史同时间槽（Time-of-Day bucket）标准差 $\sigma_{	ext{tod}, t}$ 对收益率做归一化：$	ilde{r}_{i,t} = r_{i,t} / \sigma_{	ext{tod}, t}$。
  - 状态空间方程：$	ilde{r}_{	ext{stock}, t} = lpha_t + eta_{m,t} 	ilde{r}_{	ext{SPY}, t} + eta_{	ext{sec}, t} 	ilde{r}_{	ext{sector}, t}^{\perp} + \epsilon_t$，其中 $	ilde{r}_{	ext{sector}, t}^{\perp} = 	ilde{r}_{	ext{sector}, t} - \hat{\gamma} 	ilde{r}_{	ext{SPY}, t}$ 为正交化板块收益，彻底消除多重共线性。
  - 仅使用**因果 Kalman Filter**（严禁使用包含未来信息的 RTS Smoother）或带遗忘因子 $\lambda \in [0.96, 0.99]$ 的递推最小二乘（RLS）更新实时 $lpha_t, eta_t$。
- **消融基线**：固定窗口滚动正交 OLS 回归（无 Kalman 状态空间），若因果 Kalman 在样本外净收益或稳定性上无显著统计增益，则直接采用 RLS/滚动 OLS。

### 3.2 S2 — VWAP + Wilder RSI(6) 趋势回调 + 层级 Bayesian 与多分类联合校准
- **指标口径固化**：RTH 内 VWAP 每日 09:30 ET 重置；Wilder RSI(6) 仅基于已闭合（`k.x == true`）的 5m K 线计算，明确设定跨日预热（Warmup）规则，保证离线回测与实盘完全一致。
- **三分类联合概率模型**：
  - 以未来 $H$ 根 K 线路径（如 60–90 分钟）内**先触及止盈（TP）、先触及止损（SL）、或超时未触及（Timeout）**构造完备互斥的三分类标签 $(y_{	ext{TP}}, y_{	ext{SL}}, y_{	ext{TO}})$。
  - 采用按板块/波动率分层的 Logistic / Bayesian 广义线性模型估计原始得分，并采用 **Multinomial Logistic Calibration 或 Temperature Scaling** 进行样本外联合校准，严格保证单纯形约束 $p_{	ext{TP}} + p_{	ext{SL}} + p_{	ext{TO}} = 1$。

### 3.3 S3 — ORB (5/15/30分钟) + 同时间窗 RVOL + 1m/实时截面 CUSUM & SPRT
- **指标与无偏基准**：每个 RTH 交易日开盘区间最高/最低价（ORH/ORL）必须在对应开盘窗口（09:35 / 09:45 / 10:00 ET）完全闭合后锁定；`RVOL` 严格定义为**当日截至当前分钟的累积成交量 ÷ 过去 $N$ 个交易日同一分钟时间窗的均值/中位数成交量**。
- **解决 5m 序贯检验滞后的结构设计**：CUSUM（参数 $k, h$ 由历史训练集锁定）与 Wald SPRT（或单步贝叶斯对数似然比 $	ext{LLR}_t$）直接作用于 **1 分钟闭合 K 线（离线研究）或每次报价/5m 截面标准化价量创新**，设定严格的最大观察步数与超时未决（`UNDECIDED -> REJECT`）状态。

### 3.4 S4 — VWAP + RSI(6) 超跌反弹 + 无量纲化 Ornstein-Uhlenbeck（实验策略，默认禁用）
- **解决日内 VWAP 方差发散的无量纲变换**：定义经过日内滚动 ATR(14) 归一化的**无量纲偏离度** $x_t = rac{P_t - 	ext{VWAP}_t}{	ext{ATR}_t}$，对 $x_t$ 拟合离散 OU / AR(1) 方程：$x_t = a + \phi x_{t-1} + \eta_t$。仅当滚动窗口内满足平稳性检验、$0 < \phi < 1$ 统计显著、且隐含半衰期 $	au_{1/2} = -rac{\ln 2}{\ln \phi} 	imes \Delta t$ 小于最大允许持仓时间的一半时才允许进入候选。在 `TREND_DOWN` 或高 RVOL 放量破位时强制拒绝抄底。S4 强制设为 `live_enabled = false`。

## 4. 限价优化器、穿透撮合与小资金经济可行性设计

对每个通过仲裁的 `TradeIntent`，在候选买入限价集合 $L \in \{L_{	ext{passive}}, L_{	ext{retest}}, L_{	ext{aggressive}}\}$ 与有效等待时间窗 $T$（TTL）上最大化风险调整后净效用：

$$U(L, T) = P(	ext{fill} \le T \mid X, L) \cdot \mathbb{E}[	ext{netPnL} \mid 	ext{fill}, L, X] - C_{	ext{missed}}(L, T) - \lambda \cdot 	ext{CVaR}_{lpha}(	ext{Loss} \mid X, L, T)$$

约束条件：
1. `stop_price < L <= entry_price_ceiling < take_profit_price`，且 `L` 截断至最多 2 位小数（`$0.01` tick）。
2. **防逆向选择（Adverse Selection）保守穿透撮合准则**：离线回测与训练 `P(fill)` 时，**严禁假设 `Low <= L` 即 100% 成交**：必须要求下根 K 线满足穿透条件 `Low <= L - delta`（如穿透至少 `$0.02` 或 `0.05 * ATR`）才计为确定成交，并联合考核已成交单净收益与未成交踏空成本。
3. **分数股自适应与最小经济可行名义仓位（应对 `486442` 与小额佣金磨损）**：
   - 若当前标的不支持分数股 `LIMIT` 委托（`486442`），优化器自动将目标份额 $Q = \min\left(rac{	ext{RiskBudget}}{L - 	ext{Stop}}, rac{	ext{MaxPositionUSDC}}{L}ight)$ 向下取整为整数股 $\lfloor Q floor$；若 $\lfloor Q floor < 1$，立即拒绝并记录 `WHOLE_SHARE_EXCEEDS_RISK_OR_CAP`；
   - **最小经济可行门槛（V2.2-U7）**：若最终委托名义金额 $Q_{	ext{final}} 	imes L < 	ext{min\_viable\_notional\_usdc}$（默认 `120 USDC`），或预计双边佣金+点差占目标止盈毛利的比例 $> 20\%$，立即拒绝并记录 `NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY`。

## 5. 独立 Guardian 服务端止盈止损与交易状态机（V2.2 增强版）

### 5.1 完整持久化状态转换图

```text
NO_POSITION
  └─> BUY_PENDING (先写WAL预备记录 -> 发 BUY LIMIT -> 绑定 clientOrderId <-> orderId, 预占 1 个仓位 Slot)
        ├─> FLAT_RECONCILED (未成交且撤单成功 CANCELED / EXPIRED / REJECTED)
        ├─> PARTIAL_POSITION (收到 partially_filled, 按实际已成份额 fq 启动 Guardian 止损监控; 严禁用 tc-FN 算佣金!)
        └─> PROTECTED_POSITION (买单终态且持仓 > 0, 查 /order/detail 落账真实 fee, 挂 SELL LIMIT 止盈或进入监控态)
              ├─> TP_PENDING (在途 SELL LIMIT 止盈单正常挂单中)
              │     ├─> FLAT_RECONCILED (止盈单全部成交 FILLED, 三维对账平仓完成)
              │     └─> CANCEL_PENDING_FOR_SL (触发止损或 15:55 ET 日终强平! 冻结新卖意图 -> 发起撤回 TP 单)
              │           ├─> FLAT_RECONCILED (若撤单报 486444 且查 /order/detail 确认原 TP 单已 FILLED, 直接完成平仓!)
              │           └─> EXIT_EXECUTION_LADDER (确认 TP 单已 CANCELED -> 算剩余可卖份额 remaining_qty > 0 -> 启动阶梯降级退出)
              │                 └─> FLAT_RECONCILED (退出单成交确认并完成三维对账)
              └─> EXIT_EXECUTION_LADDER (无在途 TP 挂单时直接触发 SL / 超时 / 15:55 ET 日终强平)
异常分支：任何对账不平、撤单超时未知、停牌无法退出 -> 进入 UNCERTAIN / EXIT_REQUIRED / FROZEN_ESCALATED（禁止新开仓并持续告警）
```

### 5.2 关键竞态与异常防御机制（合入 V2.2-U3, U4, U6）
- **消除“一仓双卖”与 `486446`/`486437`/`486444` 闭环处理**：
  - Guardian 发起撤回在途 TP 单后必须等待 `orderReport` 的 `x="ORDER_TERMINAL"` 或 `/order/detail` 终态确认；
  - 若 `/order/cancel` 返回 `486444`（Order not found or already filled），立即查询 `/order/detail`：若原 TP 单已 `FILLED`，直接转入 `FLAT_RECONCILED`，绝不重发卖单；仅对 `remaining_qty = total_filled_buy_qty - tp_order_filled_qty > 0` 执行平仓。
- **15:55:00 ET 日终前置强平与阶梯降级退出器（`EXIT_EXECUTION_LADDER`）**：
  - 每日 **15:45:00 ET**（半日市 12:45:00 ET）停止新买单；**15:55:00 ET**（半日市 12:55:00 ET）强制启动在途单撤销与日内持仓平仓，预留充足的 5 分钟窗口防止跨过 16:00:00 ET 触发 `486441`；
  - 阶梯退出逻辑：RTH 内首选 `SELL MARKET`；若遇 `-1112 NO_DEPTH` 或 `486413` 拒绝，自动降级为以 `max(multiplierDown_floor, bidPrice * 0.997)` 发送可立即成交的 **Marketable `SELL LIMIT`（`tradingSession="RTH"`）**；若极端情况下跨入 16:00 ET 盘后，整数股部分立即改发 `EXTENDED SELL LIMIT`，零头分数股进入 `EXIT_REQUIRED` 并持续告警。
- **三维持仓与资金对账引擎（`Position Ledger Reconciler`）**：
  - 结合 (1) 本地 SQLite 订单/成交账本、(2) `/sapi/v1/equity/order/open-orders` 与 `/trade/history` 历史成交聚合、(3) SAPI `CARD` 钱包 `USDC` 余额与 `EQ_*` 资产快照进行交叉验证，确保即使单一接口字段缺失也能 100% 锁定真实持仓。

## 6. 数据研究与科学验收协议

- **严格时间切分与防泄漏**：
  - 研究区间：`2024-11-01` 至 `2026-10-30`。
  - **训练集（Train）**：`2024-11-01` 至 `2025-10-31`（12个月）；
  - **滚动验证集（Walk-Forward Validation）**：`2025-11-01` 至 `2026-04-30`（6个月，用于超参数选择、概率校准与消融筛选）；
  - **最终锁定测试集（Holdout Lockbox Test）**：`2026-05-01` 至 `2026-10-30`（6个月，参数冻结后**仅允许运行一次**作为最终 Go/No-Go 判据；切分边界两侧强制施加至少 2 个交易日的 Purge + Embargo 隔离带）。
- **四阶消融与防伪发现（DSR + 参数邻域平稳性）**：
  - 每个策略按 **Stage A（纯规则基线） → Stage B（+数学滤波器/概率模型） → Stage C（+限价优化器与穿透撮合） → Stage D（+动态 MFE/结构止盈止损）** 逐步对比；
  - 必须通过 **Deflated Sharpe Ratio (DSR)** 多重检验校正与 **参数邻域平稳性测试（核心超参上下浮动 $\pm 15\%$ 时样本外净期望仍 $>0$）**，防止挑出孤立过拟合尖峰。

## 7. 阶段式开发路线与每阶段独立 Prompt

> **执行纪律**：每次只向开发机器人发送**项目宪法 + 当前阶段的单个 Prompt**。每阶段完成后必须按第 8 节模板提交完整报告与测试证据，经投资人明确回复批准后，方可发送下一阶段 Prompt。所有研发在独立目录/分支进行，严禁触碰现有生产进程。

### 阶段 0 — 只读审计、API 契约核验与最终架构评审（禁止任何修改与部署）

**开发 Prompt 00：**

```text
你是日本服务器美股量化机器人项目的首席架构工程师。首先完整阅读《Binance Stocks 美股/ETF 自动短线交易机器人 V2.2 项目宪法》与第1节接口规范，所有限制必须严格遵守。
本阶段仅做100%只读审计：盘点已有代码仓库、Python版本/虚拟环境、systemd服务、服务器CPU/内存/磁盘空间与IO、当前实盘运行状态、历史数据目录（服务器与Windows D盘规划）、密钥加载方式与风控实现。严禁修改任何文件、严禁重启服务、严禁调用任何写操作或下单/撤单接口，看到现有bug也只记录不顺手修改。

你必须重点核查并回答以下7项关键工程事实（提供代码定位或只读接口实测证据）：
1. 【钱包与持仓读取路径及三维对账】核对 `/sapi/v1/asset/get-funding-asset` 等 SAPI 钱包只读接口：当股票以 `tokenize=false` 买入后，SAPI Funding 钱包是否直接返回 `EQ_*` 持仓份额，还是必须结合 `/sapi/v1/equity/trade/history` 与 `/order/open-orders` 聚合重建持仓？当前账号是否已签署美股免责声明（防 `486410`）？
2. 【分数股与限价单冲突】核查 `/sapi/v1/equity/market/exchangeInfo` 中目标股票池的 `fractionable`、`fractionableEh`、`stepSize`、`minQty`、`minNotional` 字段，结合现有历史订单日志核实：在 RTH 时段下，分数股（quantity含小数）提交 `BUY LIMIT` 或 `SELL LIMIT` 是否会被上游以 `486442` 或 `486443` 拒绝？
3. 【PDT与交割规则】核查历史日志或账户属性：在 1,600 USDC 规模下高频日内回转（每日3-5笔当日买卖）是否曾触发或面临 `486440`（Pattern Day Trading protection）或 T+1 资金交割冻结限制？
4. 【WS与REST契约差异及 tc 字段陷阱】核查现有代码是否正确处理了 `wss://nbstream.binance.com/equity` 中 `{listenKey}@orderReport` 的 `b` 字段带 `EQ_` 前缀、`S`/`o`/`s` 枚举全小写、无 `clientOrderId`、**以及 `partially_filled` 时 `tc` 为整单含未成交部分的累计总成本（不可用 `tc - FN` 算实时佣金）**的特性？`listenKey` 续期是否使用了正确的 `POST /sapi/v1/equity/listenKey`？
5. 【币安无历史K线REST接口的恢复机制】鉴于 `/sapi/v1/equity/market/` 仅有 `/exchangeInfo`、`/tokenized-assets`、`/quote` 而无历史 K 线接口，现有系统在盘中重启或断流重连时如何恢复当日 09:30 ET 以来的 5m K 线、RTH VWAP 与 ORB 区间？
6. 【历史S3 ORB断链原因】定位现有仓库中为何早先 ORB 策略有信号却未能进入正式下单链路的具体代码断点。
7. 【数据源盘点】盘点现有已下载 Alpaca 历史数据的周期、起止时间、标的数量，并核实其采集时使用的是 `feed=sip` 还是 `feed=iex`。

请按统一格式提交：A.现状架构图 B.七大关键核查实证结论 C.风险清单(P0/P1/P2) D.V1与V2.2差异对照表 E.建议复用/重构模块 F.独立研发目录与分阶段资源预算 G.需投资人拍板的事项。
输出报告后立即停止，等待我的明确批准，不得进入阶段1。
```
**验收门槛 G0：** 生产环境零变更、零交易；七大关键接口与业务问题均有确凿代码/日志/只读实测证据。

### 阶段 1 — V2.2 隔离工程骨架、契约归一化层（防 `tc-FN` 陷阱）与风险宪法固化

**开发 Prompt 01：**

```text
阶段0已由投资人验收。现在在完全独立的研发目录/分支中搭建 V2.2 工程骨架与核心契约层，严禁干扰现有生产服务，严禁连接真实下单接口。

本阶段实现要求：
1. 模块化目录搭建：按 SOLID 与依赖倒置原则创建 `market_ingestor`, `normalizer`, `feature_store`, `regime`, `strategies/s1..s4`, `signal_arbitrator`, `limit_optimizer`, `portfolio_risk`, `execution_gateway`, `guardian`, `reconciler`, `research_backtest`, `reporter` 及 `tests/`。
2. 核心契约与 `EquityPayloadNormalizer`（合入 V2.2-U1）：
   - 定义强类型（Pydantic / Dataclass + `decimal.Decimal`）的 `TradeIntent`、`CanonicalOrder`、`CanonicalPosition`、`AccountSnapshot`。
   - 专门实现 `EquityPayloadNormalizer`：将 WebSocket `{listenKey}@orderReport` 的 `b` 字段剥离 `EQ_` 前缀、`S`/`o`/`s` 小写转大写、`n`（如 `"Regular"`）映射为标准 `RTH`/`EXTENDED`/`24H`、处理缺失 `clientOrderId` 的 UUID `orderId` 绑定逻辑；
   - **关键修正（防 `tc - FN` 虚假巨额佣金）**：在 `orderReport` 归一化器中，当 `s != "filled"`（或 `fq < Q`）时，严禁将 `tc - FN` 算作已实现佣金；仅当 `s == "filled"` 且 `fq == Q` 时才可用 `tc - FN` 作为买单总佣金初值，且最终一律以 `/order/detail` 的 `fee` 字段为准。
3. 固化不可越权宪法规则（代码级硬编码校验，配置无法绕过）：
   - 仅允许 `/sapi/v1/equity/*`、只读 SAPI Funding 钱包接口及只读历史研究数据源；禁用一切 crypto/合约/杠杆/做空及 `/tokenized/mint`、`/tokenized/redeem`；
   - 强制 `quoteAsset="USDC"`, `walletType="CARD"`, `tokenize=False`；
   - 严格校验 `BUY LIMIT`（`price`, `quantity`, `tradingSession`）与 `SELL MARKET`（仅 `quantity`）参数矩阵；`price` 最多2位小数；`clientOrderId` 严格匹配 `^[a-zA-Z0-9-_]{32,36}$`；
   - 最大同时5个持仓（含在途买单占槽位）、8% HWM 持久化回撤锁、RTH 交易时段（15:45 ET 禁开新仓、15:55 ET 启动强平）、最小经济可行仓位 `min_viable_notional_usdc=120`、全局默认 `live_trade_enabled=False`。
4. 编写完整单元测试（pytest + ruff + mypy）：必须包含针对官方 `orderReport` 部分成交样例（`Q=10, fq=5, FN=927.5, tc=1856.2`）不误算佣金、越权配置拦截、错误参数组合、非法 `clientOrderId`、超2位小数价格、非RTH开仓拦截的反例测试。

仅提交阶段1代码、测试报告与架构决策记录（ADR），不启动任何真实网络交易，等待审批。
```
**验收门槛 G1：** 静态检查与单元测试 100% 通过；`orderReport` 部分成交 `tc` 陷阱及所有宪法禁令均由自动化测试覆盖。

### 阶段 2 — 三级 WebSocket 分片池、本地日内 K 线 WAL 热恢复与三维只读对账适配

**开发 Prompt 02：**

```text
阶段1已验收。现在开发 Binance Stocks 只读客户端、三级 WebSocket 实时行情采集器与本地日内 K 线 WAL 持久化层（仅使用无副作用的只读接口与流，绝不调用下单或撤单接口）。

本阶段实现要求：
1. REST 只读与三维持仓/资金对账适配器（合入 V2.2-U3, U5）：
   - 实现 `/sapi/v1/equity/market/exchangeInfo`、`/market/tokenized-assets`、`/market/quote`（处理未知或停牌标的返回 HTTP 200 空响应体的边界情况）；
   - 实现 `/sapi/v1/equity/order/open-orders`、`/order/detail`、`/order/history`（注意 `orderStatus` 过滤仅传终态枚举）、`/trade/history` 只读查询；
   - 实现 SAPI Funding 钱包（`CARD`）只读查询 + 基于 `/trade/history` 与本地订单表的**三维持仓对账器**；
   - 实现 `POST /sapi/v1/equity/listenKey`（带签名）用于创建及每 25 分钟自动续期 `listenKey`，捕获 `-1125`/`-3038` 自动重建连接。
2. 三级漏斗 WebSocket 采集与分片管理（基址 `wss://nbstream.binance.com/equity`）：
   - 统一封装 `/ws` 裸流与 `/stream?streams=...` 组合流信封解析器；
   - L1 全局流：单连接订阅 `price`（3秒全市场快照）与 `calendar`；
   - L2 重点扫描池（≤300标的）：多连接分片管理器（每条连接 100-120 streams），订阅 `{SYMBOL}@kline_5m`、`{SYMBOL}@tradingStatus`、`{SYMBOL}@tradability`；
   - L3 焦点报价池（≤20标的）：通过 RPC `SUBSCRIBE`/`UNSUBSCRIBE` 动态订阅候选或持仓标的的 `{SYMBOL}@quote`。
3. 本地日内 K 线 WAL 热恢复与区分流类型的 Staleness Gate（合入 V2.2-U2, U5）：
   - **解决币安无历史 K 线 REST 接口盲点**：每根收到的 `@kline_5m` 实时写入本地 SQLite WAL 表 `intraday_bars_wal`；编写进程盘中重启恢复逻辑，100ms 内从本地 WAL 重建当日自 09:30 ET 起的 K 线序列与累积 VWAP；若检测到 >5m 断流缺口，标记 `vwap_intact=False`（或可选用只读 Alpaca SIP 分钟线回补并打标）；
   - **区分周期流与事件跳变流**：仅对 `price`/`@kline_5m`/`@quote` 施加消息时间差 Staleness Gate；对无周期心跳包的 `calendar`/`@tradability`/`@tradingStatus` 采用初始 `/exchangeInfo` 基准 + WS 底层 PING/PONG 存活监控，严禁因盘中无停牌事件而误判陈旧断流！

在只读模式下实测并提交：各接口延迟分布、WS分片连接稳定性、模拟盘中 `kill -9` 重启后 K 线与 VWAP 恢复耗时、空值出现频率及对账测试证据。等待投资人批准。
```
**验收门槛 G2：** 300 标的 WS 分片稳定运行，盘中重启后当日 K 线与 VWAP 100% 从本地 WAL 无缝恢复，事件跳变流零误报。

### 阶段 3 — 历史数据库统一（强制 SIP 一致性）、时点股票池与防泄漏对齐

**开发 Prompt 03：**

```text
阶段2已验收。现在盘点现有 Alpaca 历史数据库，构建全新且不覆盖原库的 V2.2 标准研究数据层（时间范围：`2024-11-01` 至 `2026-10-30`，周期：`1Min, 5Min, 1Hour, 1Day, 1Week`），最终同步至 Windows D 盘独立目录及云端研究目录。

本阶段实现要求：
1. 时点有效（Point-in-Time）股票池构建：
   - 读取约480只候选 CSV，与 Binance Stocks `/market/exchangeInfo` 真实可交易标的取交集；
   - 保留 `TSLA`，硬性排除 `SpaceX`/`SPCX` 及在美上市中国企业；
   - 个股按历史时点市值 ≥ 50亿美元过滤，ETF 按 ETF 自身流动性/规模规则独立过滤；结合 `listingTime`、`delistingTime` 及 Alpaca `asof` 改名映射，确保存储的每一天样本只包含当时已上市且符合条件的标的，杜绝幸存者偏差。
2. 强制 `feed=sip` 一致性与质量清洗：
   - 核验所有历史 Bar 的数据源标签：RVOL 与 VWAP 特征必须建立在统一的 `feed=sip` 全市场合并数据之上；若旧库部分数据为 `IEX`，必须重新拉取 `SIP` 或明确隔离，严禁混算；
   - 结合 NYSE 官方日历（含 DST 夏令时切换、提前收盘半日市、节假日）精确标记每根 K 线的 `session`（`RTH` / `EXTENDED` / `OVERNIGHT`）；检查并修复时间戳缺失、重复 Bar、零成交量、异常毛刺及拆股/分红映射。
3. 预计算无泄漏基准特征与跨域偏移报告（Domain Shift Report）：
   - 计算严格按日内 RTH 重置的累积 VWAP 及基于“过去 N 个交易日同一分钟时间窗”的历史成交量分布基准；
   - 对比同一交易时段内 Binance Stocks 录制的实时 `@kline_5m` / `@quote` 与 Alpaca `SIP` 历史 Bar 在价格、成交量、VWAP 上的分布差异，输出误差预算报告。

生成不可变数据版本清单（含 SHA256 校验与数据质量报告），不做策略调参，等待批准。
```
**验收门槛 G3：** 历史库 100% 时点一致、无未来信息泄漏、`SIP` 口径统一，且完成 Binance 实时流与 Alpaca 历史流的误差对标。

### 阶段 4 — 保守穿透撮合 Backtrader 引擎与事件驱动离线重放器

**开发 Prompt 04：**

```text
阶段3已验收。现在构建独立的 Backtrader 研究回测引擎与事件驱动离线订单重放器（Order Replay Engine），专门解决默认回测过度乐观与限价单逆向选择问题。

本阶段实现要求：
1. 杜绝同根偷看与理想化成交：
   - 信号只能在 K 线 $t$ 完全闭合（`close` 确认）后生成，委托最早在 $t+1$ 周期进入撮合；
   - 同时保留两套撮合模式作为对照：(a) Backtrader 默认宽松撮合（仅作乐观上限参考）；(b) **V2.2 保守穿透撮合模式（作为实际上线判据）**。
2. V2.2 保守穿透撮合与微观结构约束：
   - 针对 `BUY LIMIT` 限价单 $L$：若下根 K 线仅触及 `Low == L`，不得按 100% 成交计，必须满足穿透条件 `Low <= L - delta`（如穿透至少 `$0.02` 或指定点差/ATR比例）才视为确定成交；在临界区间 `[L - delta, L]` 内按保守队列模型模拟未成交或部分成交；
   - 针对同一根 5m K 线内同时触及止盈（TP）与止损（SL）的情况：自动下钻读取同期的 `1Min` 数据判定真实先后顺序；若 `1Min` 同一分钟内仍同时触及，一律按**悲观原则（SL 止损优先触发）**结算并打上 `ambiguous_bar_sl_assumed=True` 标记；
   - 完整模拟 `exchangeInfo` 的 `stepSize`、`minNotional`、2位小数限价、分数股限价拒单回退整数股（`486442` 规则）、最小经济可行名义仓位（`120 USDC`）、双边佣金、买卖点差、网络延迟、TTL 超时撤单及 **15:55:00 ET（半日市 12:55:00 ET）日终强制平仓**。
3. 全样本候选日志库与反例测试套件：
   - 将所有生成的 `TradeIntent`（含风控拒绝、未成交踏空、部分成交、已成交单）落库，计算每笔信号的 MFE/MAE 与未成交机会成本；
   - 编写专门单元测试证明：未闭合 K 线不可访问、未来 MFE 不可进入策略、休市与 15:45 ET 后禁止新开仓、同柱 TP/SL 冲突严格走 1m 复核或悲观止损。

提交回测/重放引擎代码、反例测试结果及人工逐笔复核样例，等待批准。
```
**验收门槛 G4：** 反例测试全部通过，保守撮合与宽松撮合差异量化清晰，每笔交易损益与费用可人工逐笔精确复核。

### 阶段 5 — 严格因果的市场状态分类器与共同特征层

**开发 Prompt 05：**

```text
阶段4已验收。现在基于 `SPY`、`QQQ` 及 11 大行业板块 ETF 的价格、成交量、ATR、日内同时间窗相对波动率、VWAP 斜率与市场广度，开发严格因果的市场状态分类器（Market Regime Classifier）与共享特征快照层（`FeatureSnapshot`）。

本阶段实现要求：
1. 五状态因果分类：
   - 定义状态集合：`TREND_UP`, `SIDEWAYS`, `TREND_DOWN`, `HIGH_VOL_PANIC`, `UNKNOWN`；
   - 仅使用截至决策时点 $t$ 已闭合的 K 线与实时状态，引入日内时间槽归一化（Time-of-Day Normalization）与状态切换防抖滞后保护（Hysteresis）；任何数据缺失、过期、`vwap_intact=False` 或置信度过低时强制落入 `UNKNOWN`，而 `UNKNOWN` 与 `HIGH_VOL_PANIC` 状态下严禁任何做多新开仓。
2. 策略路由矩阵与流批一致性契约：
   - 明确各状态下的策略启用门禁（如 `TREND_UP` 启用 S1/S2、开盘窗口结合 `TREND_UP`/`SIDEWAYS` 启用 S3、`SIDEWAYS` 供 S4 离线研究）；
   - 实现带 `data_asof_utc` 与 `features_hash` 的不可变 `FeatureSnapshot`，通过自动化测试证明离线批处理与实盘流式递推（含盘中从 SQLite WAL 热恢复后）计算出的特征值精确相等（误差 < 1e-9）。

仅交付市场状态分类器、共享特征层及 2024-2026 各状态分布诊断报告，不优化策略收益，等待批准。
```
**验收门槛 G5：** 流批特征计算（含 WAL 热恢复场景）100% 一致，严格因果无泄漏，`UNKNOWN` 状态 100% 阻断新开仓。

### 阶段 6 — S1 日内动量 + 相对强弱 + 日内波动率归一化因果 Kalman/RLS

**开发 Prompt 06：**

```text
阶段5已验收。现在独立实现策略 S1：日内动量 + 相对强弱（Relative Strength）+ 动态多因子残差模型。只输出结构化 `TradeIntent` 并接入阶段4回测引擎评估，不连接真实下单网关。

本阶段实现要求：
1. 解决共线性与日内异方差的数学实现：
   - 对板块 ETF 收益率相对 `SPY` 做正交化处理，消除市场因子与板块因子的多重共线性；
   - 对 5m 收益率按历史同时间窗波动率 $\sigma_{	ext{tod}}$ 进行标准化后再输入状态空间模型；
   - 实现纯因果 Kalman Filter（严禁使用 Kalman Smoother）实时更新特异截距 $lpha_t$ 与动态 $eta_{m,t}, eta_{	ext{sec},t}$，并同步实现带指数遗忘因子 $\lambda$ 的递推最小二乘（RLS）与固定窗口滚动 OLS 作为消融基线。
2. 信号触发、退出与消融对比：
   - 当正残差动量 $lpha_t$ 及其信噪比超过训练阈值、板块相对强弱为正、处于允许的市场状态、且未过度偏离 VWAP 时生成做多候选；
   - 在训练集与验证集上严格对比：固定 $eta$ 基线 vs. RLS vs. 因果 Kalman 的信号数量、保守撮合净收益、夏普/PF、最大回撤与计算耗时；若因果 Kalman 未能显著超越 RLS/固定 $eta$，如实报告并推荐更简模型。

提交 S1 代码、单元测试及消融对比报告，等待批准。
```
**验收门槛 G6：** S1 因果无泄漏，正交化与波动率归一化有效，完成与简单基线的样本外消融验证。

### 阶段 7 — S2 VWAP/RSI(6) 趋势回调 + 层级 Bayesian 与多分类单纯形校准

**开发 Prompt 07：**

```text
阶段6已验收。现在独立实现策略 S2：5分钟 RTH VWAP + Wilder RSI(6) 趋势回调策略，结合层级 Bayesian / 广义线性概率模型与多分类联合校准。

本阶段实现要求：
1. 精确指标定义：
   - RTH VWAP 每日 09:30 ET 归零重置；Wilder RSI(6) 仅在 5m K 线闭合时更新，固化跨日预热（Warmup）规则并在文档与测试中锁定；
   - 基线触发条件：收盘价 > VWAP、VWAP 斜率 > 0、RSI(6) 在近期窗口内曾低于 40 后重新上穿 50、回调未跌破关键结构支撑、量能配合且未过度拉升。
2. 完备三分类标签与单纯形概率校准：
   - 构造未来 $H$ 根 K 线（如最高持有 90 分钟）内 `{TP先触达, SL先触达, 超时Timeout}` 的互斥完备三分类标签；
   - 建立按板块/波动率分层的 Bayesian Logistic 或正则化多分类模型，使用 Multinomial Logistic Calibration 或 Temperature Scaling 进行样本外联合概率校准，确保输出的 $(p_{	ext{TP}}, p_{	ext{SL}}, p_{	ext{TO}})$ 严格满足 $\sum p_i = 1$；
   - 对同一标的短时间内连续触发的重叠信号实施去重或样本降权，输出 Brier Score、可靠性曲线及 Bootstrap 置信区间。

对比“纯规则 VWAP+RSI(6) 基线”与“+多分类校准概率门禁”的样本外表现，等待批准。
```
**验收门槛 G7：** 三分类校准概率严格和为 1，可靠性曲线良好，无未来标签泄漏，消融证据完整。

### 阶段 8 — S3 ORB (5/15/30m) + 同时间窗 RVOL + 低延迟 CUSUM/SPRT

**开发 Prompt 08：**

```text
阶段7已验收。现在独立实现策略 S3：开盘区间突破（ORB 5/15/30分钟）+ 日内同时间窗 RVOL + VWAP + 序贯突破检验，并彻底修复历史系统中“ORB 有信号但未进入下单链路”的断链问题。

本阶段实现要求：
1. 严格因果的 ORB 与 RVOL：
   - 5m/15m/30m 开盘区间（ORH/ORL）必须在该时间窗完全闭合后才可锁定；RVOL 必须使用过去 N 个交易日同一分钟时间窗的历史成交量基准计算。
2. 消除 5m 多步等待滞后的 CUSUM / SPRT 设计：
   - CUSUM（参数 $k, h$ 由训练集固定）与 Wald SPRT / 贝叶斯对数似然比（LLR）支持在 **1分钟闭合数据或5m截面标准化价量创新** 上运行，规定严格的最大观察步数（如不超过3步）与未决拒绝状态（`UNDECIDED -> REJECT`）；
   - 入场限价优先设定在突破后回踩 ORH 上沿的可控区间，TTL 设为 1–2 根 5m K 线，超时未成交立即撤销，严禁反复抬价追高。
3. 端到端全链路打通验证：
   - 针对阶段0定位的历史 ORB 断链点，编写从 `S3 Signal -> Arbitrator -> LimitOptimizer -> PortfolioRisk -> MockExecutionGateway -> Guardian Exit` 的端到端集成测试，断言每一个环节的状态流转与日志落盘。

分别评估 5m/15m/30m ORB 以及“纯 ORB+RVOL 基线 vs. +CUSUM/SPRT”的样本外净表现与踏空率，等待批准。
```
**验收门槛 G8：** ORB 全链路端到端集成测试 100% 畅通，无滞后追高，完成三种开盘周期与基线的样本外对比。

### 阶段 9 — S4 VWAP/RSI(6) 超跌反弹 + 无量纲化 OU 均值回归（实验隔离）

**开发 Prompt 09：**

```text
阶段8已验收。现在独立实现实验策略 S4：VWAP + Wilder RSI(6) 超跌反弹 + 无量纲化 Ornstein-Uhlenbeck (OU) 均值回归模型。本策略全程强制锁定 `live_enabled = False`，仅用于离线研究。

本阶段实现要求：
1. 解决日内 VWAP 异方差的无量纲残差构造：
   - 使用经日内滚动 ATR(14) 归一化后的无量纲偏离 $z_t = rac{P_t - 	ext{VWAP}_t}{	ext{ATR}_t}$ 拟合离散 AR(1)/OU：$z_t = a + \phi z_{t-1} + \eta_t$；
   - 仅当滚动窗口内满足平稳性诊断、$0 < \phi < 1$ 显著、且半衰期 $	au_{1/2} = -rac{\ln 2}{\ln \phi}\Delta t \le 25$ 分钟时才允许生成候选。
2. 暴跌保护与反接飞刀门禁：
   - 在 `TREND_DOWN`、大盘急跌、个股伴随极端高 RVOL 放量破位或 $\phi \ge 1$ 时 100% 拒绝开仓。

提交 S4 代码、与纯 VWAP+RSI(6) 超跌基线的对比报告及明确的不适用市场环境清单，等待批准。
```
**验收门槛 G9：** S4 无量纲 OU 数学诊断严谨，暴跌过滤门禁有效，配置硬编码隔离不进入实盘生产。

### 阶段 10 — 统一信号仲裁器与交易资格数学门禁

**开发 Prompt 10：**

```text
阶段9已验收。现在开发 S1–S4 共享的统一信号仲裁器（`SignalArbitrator`）与净期望资格门禁。

本阶段实现要求：
1. 同标的冲突消解与去重：
   - 以 `(symbol, bar_close_time)` 及相邻时间冷却窗作为去重键；当同一标的同时被多个策略触发时，按各策略样本外校准后的**风险调整净期望（$\mathbb{E}[	ext{net\_bps}] / 	ext{RiskUnit}$）**及置信区间下界择优保留唯一 `TradeIntent`。
2. 跨标的组合排序与槽位分配：
   - 当可用仓位槽位（最大 5 仓减去当前持仓数及在途买单数）不足时，综合净期望、板块分散度、点差流动性与所需资金占用进行优先级排序。
3. 严格拒绝码（`rejection_reasons`）体系：
   - 任何信号若存在：行情陈旧、`vwap_intact == False`、`tradability != "BUY_SELL"`、停牌、处于 15:45 ET 之后、止盈空间不足以覆盖双边费用与滑点（双边总摩擦占比 > 20%）、或校准后净期望 $\le 0$，一律拦截并记录结构化 `reason_code`。

提交仲裁器代码、多信号并发冲突单元测试及仲裁审计日志样例，等待批准。
```
**验收门槛 G10：** 并发冲突与同标的重复信号 100% 消解，槽位预占逻辑严密，所有拒单原因可追溯。

### 阶段 11 — 限价优化器、分数股/整数股自适应与最小经济仓位门禁

**开发 Prompt 11：**

```text
阶段10已验收。现在实现可插拔的限价执行数学优化模块（`LimitOptimizer`），将通过仲裁的 `TradeIntent` 转化为精确符合 Binance Stocks 交易规则的委托参数。

本阶段实现要求：
1. 候选限价效用最大化：
   - 针对每个信号构建三档候选买价：`passive`（买一价 `bp` 或微幅回踩）、`retest`（关键结构位/VWAP/ORH回踩价）、`aggressive`（卖一价 `ap` 或受控可成交限价，但不超过 `entry_price_ceiling`）；
   - 在指定等待窗口 $T$（TTL）下估计 $U(L,T) = P(	ext{fill} \le T \mid X, L) \cdot \mathbb{E}[	ext{netPnL} \mid 	ext{fill}, L, X] - C_{	ext{missed}}(L, T) - \lambda \cdot 	ext{CVaR}_{lpha}$。
2. 交易所精度、分数股规则（`486442`）与最小经济可行仓位（合入 V2.2-U6, U7）：
   - 价格严格使用 `Decimal` 截断至最多 2 位小数，校验 `multiplierUp`/`multiplierDown` 价格笼子；
   - 若该标的不支持分数股 `LIMIT` 委托，强制将计算出的份额向下取整为整数股（`stepSize=1`）；若取整后 `quantity < 1`，拒绝下单并返回 `WHOLE_SHARE_EXCEEDS_RISK_OR_CAP`；
   - **最小经济可行仓位门禁**：若最终名义金额 `quantity * price < min_viable_notional_usdc`（默认 `120 USDC`），或双边摩擦成本 > 目标止盈毛利的 `20%`，拒绝下单并返回 `NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY`；
   - **下单超时幂等恢复（应对 `-1006`/`-1007`）**：发送 `POST /order/place` 前先将 `clientOrderId` 写入本地 WAL；若遇网络超时或 `-1006`/`-1007`，严禁更换 `clientOrderId` 盲目重发，必须先调 `GET /sapi/v1/equity/order/detail?clientOrderId=...` 核实是否已落地。

在离线重放器与 Mock Gateway 中全面测试三档限价策略、分数股/最小经济仓位拦截及 `-1007` 超时幂等恢复，提交实证报告，等待批准。
```
**验收门槛 G11：** 限价精度、最小经济仓位、分数股边界及 `-1006`/`-1007` 超时幂等恢复 100% 合规。

### 阶段 12 — 独立 24/7 Guardian 守护进程、撤单竞态（`486446`/`486444`）与阶梯降级退出

**开发 Prompt 12：**

```text
阶段11已验收。现在实现完全独立于策略引擎的 24/7 止盈止损守护服务（`guardian`）与三维对账服务（`reconciler`），并在 Mock Broker 与故障注入环境中完成极限验证。

本阶段实现要求：
1. 基于真实成交份额的动态保护与 15:55:00 ET 日终前置强平：
   - 只要买单发生任何部分成交（`PARTIALLY_FILLED` 或 `fq > 0`），Guardian 立即以真实已成交净份额建立 SL、TP 与最大持仓时间保护记录；
   - 每日 **15:55:00 ET（半日市 12:55:00 ET）** 自动触发日终清仓流程，严禁拖延至 15:59:55 ET 导致撤单跨过 16:00 ET 边界。
2. 彻底解决“TP 挂单中触发 SL”的撤单竞态与错误码 `486446`/`486437`/`486444`（合入 V2.2-U6）：
   - 当持仓存在在途 `SELL LIMIT` 止盈单而触发 SL 或 15:55 ET 强平时，Guardian 进入 `CANCEL_PENDING_FOR_SL`：
     (a) 先锁定该持仓禁止产生新卖单，调用 `POST /sapi/v1/equity/order/cancel`；
     (b) 若返回 `486444`（Order not found or already filled），立即查询 `/order/detail`：若原 TP 单已 `FILLED`，直接转入 `FLAT_RECONCILED`，绝不重发卖单！
     (c) 若返回 `status="S"` 或 `486446`（撤单进行中，份额仍冻结），必须等待 `orderReport` 推送 `x="ORDER_TERMINAL"` 或轮询 `/order/detail` 确认终态，再对 `remaining_sellable_qty = position_qty - tp_order_filled_qty > 0` 执行退出。
3. 阶梯降级退出器（`EXIT_EXECUTION_LADDER`，合入 V2.2-U4）：
   - RTH 内首选发送 `SELL MARKET`（仅传 `quantity`）；
   - 若遇 `-1112 NO_DEPTH` 或 `486413` 拒绝，立即降级为按当前 `max(multiplierDown_floor, bidPrice * 0.997)` 发送可立即成交的 Marketable `SELL LIMIT`（`tradingSession="RTH"`）；
   - 若因极端网延跨入 16:00 ET 盘后触发 `486441`，整数股部分立即改发 `EXTENDED SELL LIMIT` 平仓，零头分数股进入 `EXIT_REQUIRED` 并告警。

执行 15 项故障注入测试（含买单部分成交后撤单、TP与SL同秒触发、`486446` 撤单延迟、`486444` 撤单时已止盈成交、`-1112` 市价无深度自动降级 Marketable Limit、15:55 ET 强平、盘中停牌、策略进程 `kill -9` 崩溃、Guardian进程重启三维对账恢复），提交全部测试通过证据，等待批准。
```
**验收门槛 G12：** 15 项故障注入测试 100% 通过，策略崩溃不影响 Guardian 独立平仓，具备 `-1112`/`486441`/`486444` 完整闭环。

### 阶段 13 — 账户级总风控 `PortfolioRisk`（8% HWM 锁、防 `tc-FN` 误锁、PDT 防护与三维对账）

**开发 Prompt 13：**

```text
阶段12已验收。现在实现账户级唯一资金与风险审批权威 `PortfolioRisk`，并在 Mock Broker 下执行多进程并发与极端行情压力测试。

本阶段实现要求：
1. 真实净值（Equity）与槽位核算（合入 V2.2-U1, U3）：
   - 基于 SAPI `CARD` 真实可用 `USDC` + 在途买单冻结 `USDC` + 当前持仓按实时有效 Bid 价估算的净清算市值，实时计算账户真实总权益（Equity）；
   - **强制校验**：在买单 `PARTIALLY_FILLED` 期间，严禁用 `orderReport` 的 `tc - FN` 扣减净值，彻底杜绝把未成交本金误算为亏损而假触发 8% HWM 锁；
   - 严格执行：最多同时 5 个独立标的槽位（在途未终态买单强制占槽位）、单笔风险预算默认 `0.25%` Equity、单标的名义仓位上限默认 `20%` Equity、单笔最小经济仓位 `120 USDC`。
2. 持久化 8% 高水位（HWM）回撤锁与 PDT（`486440`）熔断门禁：
   - 账户历史最高净值（HWM）与回撤锁定状态使用数据库事务 + 本地校验文件双重持久化落盘；
   - 一旦 `Current_Equity <= HWM * 0.92`，立即原子性切换至 `HALT_NEW_ENTRIES`：自动取消所有在途未成交 `BUY` 单、拒绝一切新开仓请求、仅保留 Guardian 的平仓/止损卖出权限，并发送最高级别告警；解锁唯一途径为投资人人工签名指令；
   - 内置滚动 5 个交易日 Day-Trade 计数器与错误码 `486440`/`486439` 实时熔断器。

在 Mock 环境下完成极限压力测试：5仓满额并发申报、320 USDC 买单刚成交 1 股（检验不会因 `tc - FN` 误触发 8% HWM 锁）、真实现金回撤达 8% 立即触发 HWM 锁、进程重启后 HWM 锁保持有效。提交形式化风控不变量验证报告，等待批准。
```
**验收门槛 G13：** 形式化风控不变量 100% 满足，部分成交绝不误触发 HWM 锁，真实 8% 回撤 100% 锁定且跨重启不可绕过。

### 阶段 14 — 全样本科学回测、四阶消融、DSR/邻域稳健性检验与 Go/No-Go 决策包

**开发 Prompt 14：**

```text
阶段13已验收。现在冻结所有策略代码与超参数候选，在 `2024-11-01` 至 `2026-10-30` 标准历史库上执行完整的训练（2024.11-2025.10）、滚动验证（2025.11-2026.04）与最终锁定测试（2026.05-2026.10，仅运行一次），并完成上线前全面安全审计。

本阶段实现要求：
1. 四阶消融、DSR 与参数邻域稳健性检验（合入 V2.2-U8）：
   - 针对 S1、S2、S3、S4 分别输出：Stage A（简单基线）→ Stage B（+数学模型）→ Stage C（+限价优化与穿透撮合）→ Stage D（+动态止盈止损）在验证集与最终锁定测试集上的独立表现；
   - 必报指标：总交易笔数、未成交踏空笔数、成交率、扣除真实费用与穿透滑点后的净收益、单笔净期望（bps 与 USDC）、Profit Factor、胜率、盈亏比、最大回撤、CVaR(95%)、Bootstrap 95% 置信区间、**Deflated Sharpe Ratio (DSR)** 以及**核心超参上下浮动 ±15% 的邻域净期望矩阵**；
   - 任何在样本外锁定测试集上净期望 $\le 0$、DSR 不显著或参数邻域脆弱的策略，必须标记为 `NO_GO / RESEARCH_ONLY`。
2. 压力测试与全面安全审计：
   - 测试佣金/滑点翻倍、限价穿透门槛加严、行情延迟 1–5 秒、开盘剧烈跳空场景下的净值表现；
   - 逐项核查：零明文密钥、仅允许接口白名单、`walletType="CARD"`、`tokenize=False`、分数股 `486442` 保护、最小经济仓位 `120 USDC`、WS `EQ_` 前缀与小写归一化、防 `tc-FN` 误算、本地日内 K 线 WAL 热恢复、Guardian `486446`/`486444`/`-1112` 阶梯退出、15:55 ET 强平缓冲、8% HWM 锁。

提交完整可复现审计包、P0/P1/P2 问题清零证明、分策略上线推荐清单及最终 Go/No-Go 决策报告，不触碰实盘，等待投资人最终裁定。
```
**验收门槛 G14：** P0/P1 缺陷清零，锁定测试集通过 DSR 与参数邻域稳健性检验；否则判定 No-Go。

### 阶段 15 — 日本云端受控上线（必须获得投资人针对本阶段的专门书面授权）

**开发 Prompt 15：**

```text
阶段14已验收，且投资人已明确书面授权启动阶段15受控实盘切换。

上线执行标准流程（SOP）：
1. 上线前完整备份与回滚点制作：
   - 完整备份日本服务器现有生产代码、配置、数据库、systemd 单元与历史日志，记录 Git Commit SHA 与文件 SHA256，生成一键回滚脚本并验证可用性；严禁新旧两套交易执行进程同时运行。
2. 生产前置核验清单（Pre-flight Check）：
   - 核验 `POST /sapi/v1/equity/account/disclaimer` 状态；
   - 核验 SAPI `CARD` 钱包实际可用 `USDC` 与三维对账器初始状态、无遗留未知在途单或未受保护持仓；
   - 核验服务器 NTP 时间同步（误差 < 100ms）、NYSE 交易日历（含 15:45 ET 禁入与 15:55 ET 强平定时）、8% HWM 初始基准值落盘、告警通知通道畅通。
3. 严格按序启动与灰度放行：
   - 第一步：启动 `market_ingestor`（含本地日内 K 线 WAL 落盘）与 `reconciler`，确认 L1/L2/L3 WebSocket 流、`POST /sapi/v1/equity/listenKey` 续期及三维对账 100% 正常；
   - 第二步：启动独立 `guardian` 服务并验证心跳；
   - 第三步：启动 `execution_gateway` 与 `signal_engine`，**初次仅启用在 G14 锁定测试中正式通过审批的策略（S4 强制保持禁用）**；
   - 首周采用保守风险预算（如单笔风险 0.15%–0.25%、最多 2–3 仓）观察真实限价成交率、滑点与接口稳定性，任何 P0 异常立即触发 `HALT_NEW_ENTRIES` 并告警。

提交上线检查表、进程状态证据、首日逐笔交易对账审计报告与日终总结。
```
**验收门槛 G15：** 生产环境唯一网关与独立 Guardian 稳定运行，三维持仓与 `CARD` 钱包账实完全相符，零越权调用。

### 阶段 16 — 周期复盘与受控数学模型迭代

**开发 Prompt 16：**

```text
阶段15已稳定运行。现在建立常态化的日终交易审计、每 5 个交易日周度复盘及月度/季度科学评估机制。

本阶段实现要求：
1. 自动化复盘与归因报告（`reporter`）：
   - 每个美股交易日 RTH 收盘对账后自动生成日报，每 5 个交易日生成周报：统计分策略真实信号数、限价单成交率、部分成交比例、平均价格改善 vs. 逆向选择损耗、实现滑点、双边佣金占比、止盈/止损/15:55 ET 强平分布、真实胜率与模型预测概率的校准偏差（ECE / Brier Score）以及特征分布漂移（PSI）。
2. 不可变审计轨迹与严控迭代纪律：
   - 每笔真实委托完整关联 `intent_id`、`clientOrderId`、`orderId`、`features_hash`、模型版本号及当时行情快照；
   - 任何模型参数或限价优化器的修改建议，只能在隔离研究分支中走“离线训练 → 样本外验证 → G14 审计（含 DSR 与邻域稳健性） → 投资人书面批准”流程；严禁线上进程自学习、自动改参或放宽 8% HWM 锁与 5 仓上限。

提交自动化日报/周报生成模块、模型漂移监控脚本与版本回滚规范。
```
**验收门槛 G16：** 每笔实盘交易可 100% 追溯还原，实盘成交概率与回测偏差量化清晰，迭代审批闭环稳固。

## 8. 每阶段必须交付的统一报告格式

```text
[阶段编号]：[阶段名称]
结论：PASS / FAIL / CONDITIONAL
1. 修改文件：路径、Git commit SHA、配置差异；只读阶段写「无改动」
2. 测试证据：执行命令、时间戳、覆盖率、正例/反例结果、故障注入记录
3. 接口与数据实证：Binance Stocks / SAPI 钱包 / Alpaca 实测与文档对照发现
4. 模型与执行表现：基线 vs. 增强（四阶消融 + DSR + 邻域稳健性），样本内/样本外净期望、成交率、成本损耗及 95% 置信区间
5. 风控不变量核查：8% HWM 锁（含防 tc-FN 误算核查）、≤5仓（含在途买单）、仅股票/ETF、walletType=CARD、tokenize=false、分数股486442保护、最小经济仓位120 USDC、15:55 ET强平、Guardian独立状态
6. 风险台账：P0/P1/P2 待修复项与外部不可抗风险
7. 是否满足本阶段验收门槛（G0–G16）：逐条提供明确证据
8. 对当前生产系统的影响：零影响 / 需投资人批准事项
9. 下一阶段建议：仅提出建议，绝不自行跨阶段执行
停在当前阶段，等待投资人明确批准。
```

## 9. 第一批上线范围与硬性 Go/No-Go 铁律

- **策略准入**：优先从 **S1、S2** 中挑选在保守穿透撮合下真正通过样本外锁定测试、DSR 检验与参数邻域稳健性检验的策略；**S3** 必须同时通过端到端链路测试与样本外净期望检验方可加入；**S4** 保持实验隔离默认关闭。
- **拒绝虚假胜率承诺**：不设定脱离盈亏比的机械胜率数字；硬性要求每一上线模型在扣除真实佣金、点差与逆向选择穿透成本后，样本外净期望显著为正，且尾部亏损（CVaR）与最大回撤严格受控于 8% 账户锁定阈值之内。
- **一票否决项（No-Go）**：凡存在任何未解决 P0 缺陷、三维持仓或 `CARD` 钱包余额对账不一致、`orderReport` 部分成交误用 `tc - FN` 算佣金、盘中重启无法恢复当日 5m K 线与 VWAP、分数股/整数股规则未闭环、Guardian 在 `486446`/`486444`/`-1112` 下可能发重复卖单或卡死、未签署免责声明（`486410`）、`tokenize=false` 未强制锁定或 8% HWM 回撤锁可被重置，一律判定 **No-Go**。
