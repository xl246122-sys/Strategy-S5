# 阶段 11 验收报告

```text
[阶段 11]：限价优化器、分数股/整数股自适应与最小经济仓位门禁
结论：G11 的工程门槛通过。优化器只生成 CARD 限价草案。幂等网关只对注入的 Mock 经纪商说话。实盘下单入口保持关闭
1. 修改文件：D:\Strategy\binance_stocks_v2_2\src\bstocks_v2\limit_optimizer\（spec.py、book.py、fill_model.py、utility.py、stage11.py，包入口；stage8.py 只改了说明，bind_limit 行为未改）。执行网关新增 execution_gateway\idempotent.py，包入口导出 IdempotentMockGateway。测试在 tests\test_stage11.py。同一份研究代码已同步到日本机 /home/ubuntu/binance_stocks_v2_2。无 Git 仓库，因此没有 commit SHA。密钥没有写入工程。
2. 测试证据：2026-11-02。本地 Python 3.11.5 与日本机虚拟环境 Python 3.12.16 都是 ruff 全过、mypy 116 个源文件全过、pytest 154 passed。阶段 11 新增 13 个测试。没有行情扫描，也没有调用真实下单或撤单。
3. 接口与数据实证：本阶段不连交易所，不读 5 分钟库，不进入 2026-05-01 之后的锁箱。成交概率用构造出来的 6 根 5 分钟线训练。价格笼子、2 位小数和 clientOrderId 按宪法与阶段 4 的保守穿透规则核对，没有新的交易所回包。
4. 模型与执行表现：本阶段没有新的成交回放。样例截面上三档效用分别是 passive −5.3310524、retest 11.34742、aggressive 7.76979。胜出是 148.50 的 retest，数量 2，带 486442 取整记录。固定算例的条件净期望是 4.2778 USDC，CVaR 是 2.119，效用是 0.44768。
5. 风控不变量核查：live_trade_enabled 与 S4 的 LIVE_ENABLED 都固定为 false。POST /sapi/v1/equity/order/place 在 live_place 上抛 LiveTradingDisabledError。整数股安全模式默认开启。名义金额低于 120 USDC，或双边摩擦严格大于目标毛利的 20%，都记 NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY。高价股取整后不足 1 股记 WHOLE_SHARE_EXCEEDS_RISK_OR_CAP。单仓上限仍是权益的 20%。
6. 风险台账：见下方。阶段 3 的密钥、时点市值和跨源重叠仍然开着。本阶段没有再查 Funding 钱包。阶段 7 的概率门禁没有接进来。成交概率没有在历史库上拟合。486442 的真实拒单仍然没有交易所样本，所以整数股安全模式保持开启。
7. G11：限价向下截断到 0.01，且不超过 entry_price_ceiling。参考价乘上下沿之后，落在笼子外的价格记 PRICE_CAGE。120 USDC 与 20% 摩擦门禁有正例和反例。分数股开关打开时，安全模式仍把高价股打成 WHOLE_SHARE_EXCEEDS_RISK_OR_CAP。-1007 落单后只查 detail，clientOrderId 不变，不会再发第二张新单。-1007 未落单时用同一 id 重试，经纪商里只有一张单。-1006 走同一条查询。进程停在 SUBMITTING 时，恢复只查 detail，place 次数为 0。
8. 对当前生产系统的影响：没有下单。日本机只更新了研究代码。环境文件、腾讯云代理、原始行情库和 5 分钟特征库未改。
9. 下一阶段建议：不要打开 S4 的实盘开关。不要把这次选出的 retest 当成可部署策略。不要为了多放出委托去关闭整数股安全模式、放宽 120 USDC 或把 20% 摩擦改成大于等于才拒绝。不要用 2026-05-01 之后的锁箱去拟合成交概率。阶段 7 的概率门禁仍然不进入后续阶段。等投资人批准后再进入阶段 12。
停在当前阶段，等待投资人明确批准。
```

## 冻结口径

这些常数写在本阶段测试之前，没有用成交样本回改。

| 项目 | 取值 |
| --- | --- |
| 三档 | `passive`，`retest`，`aggressive`。同一价格打平时，较低的价格优先；价格也相同，则 passive 优先 |
| passive | 买一减去 1 个 tick（0.01），再向 0 截断到分 |
| retest | 有命名结构位就用结构位，否则用 VWAP，否则用 ORH。只取一个，再向 0 截断 |
| aggressive | `min(卖一, entry_price_ceiling)`，再向 0 截断。卖一高于天花板时，委托价停在天花板 |
| 几何 | `stop_price < L <= entry_price_ceiling < take_profit_price` |
| 价格笼子 | `reference * multiplierDown <= L <= reference * multiplierUp`。笼子边界不向外取整 |
| 等待窗 T | 默认 2 根 5 分钟线，即 600 秒。只允许 1、2、3 根。意图剩余寿命短于 T，记 `TTL_SHORTER_THAN_HORIZON` |
| 成交概率 | 阶段 4 保守全成：后续 T 根里出现 `low <= L - 0.02`。训练标签只用截止时刻之前已经收完的窗口。决策当根的 low 不进标签。空样本直接拒绝，不发明先验 |
| 平滑 | `(命中数 + 1) / (样本数 + 2)` |
| 条件净期望 | 赢走到止盈，亏走到止损，超时按限价平出。佣金 0.05%，点差用两边各 0.01 |
| 踏空成本 | `(1 - P) * max(条件净期望, 0)`。正期望时，`P * 净期望 - 踏空成本 = (2P - 1) * 净期望` |
| CVaR | 亏损分布最差 5% 的期望。未成交和止盈的亏损都记 0。权重 λ = 1 |
| 效用 | `P * 条件净期望 - 踏空成本 - λ * CVaR` |
| 份额 | `Q = min(风险预算 / (L - 止损), 权益 * 20% / L)`。默认权益 1,600 USDC，单仓上限 320 USDC |
| 整数股 | 安全模式默认开启。交易所未声明分数股限价时也同样取整。`Q` 先向 0 取到整数股，再按不小于 1 的 `stepSize` 对齐。取整后小于 `max(1, minQty)`，记 `WHOLE_SHARE_EXCEEDS_RISK_OR_CAP`。取整本身记 `486442`，不因此拒单 |
| 经济仓位 | `Q * L < 120`，或双边摩擦占目标毛利严格大于 20%，都记 `NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY`。等于 20% 仍可进入效用比较 |
| 幂等 | 发送前把 35 位 `clientOrderId` 写入 SQLite `order_prepare_wal`，状态 `WAL_PREPARED`，`journal_mode=WAL`。`-1006` / `-1007` 先把状态写成 `UNKNOWN_TIMEOUT`，再 `GET /sapi/v1/equity/order/detail`。确认到的单记 `DETAIL_CONFIRMED`。查无此单才允许用同一个 id 再发。换新 id 记 `RESEND_NEW_ID_FORBIDDEN` |

## 三档效用

样例书：买一 149，卖一 150，结构位 148.50，天花板 150，止损 148，止盈 165。风险预算 4 USDC，权益 1,600。三档数量都是 2。

注入的成交概率是 passive 0.40、retest 0.90、aggressive 0.95。这组概率用来锁定公式，不是从历史库估出来的。retest 的价格改进盖过了 aggressive 更高的成交概率，效用 11.34742 对 7.76979。passive 的成交概率低于一半，`(2P - 1)` 为负，效用 −5.3310524。胜出草案是 `BUY LIMIT`，`walletType=CARD`，`tokenize=false`，`tradingSession=RTH`，不带 `notional`。Mock 网关收下了这张草案。

另一组手算把价格钉在 100、止损 98、止盈 110、数量 1、成交概率 0.8。条件净期望 4.2778，踏空成本 0.85556，CVaR 2.119，效用 0.44768。把成交概率降到 0.10、止损概率 0.20、超时概率 0.50 时，最差 5% 会跨过止损桶吃进一部分超时亏损，CVaR 为 0.9196。

## 成交概率不看这笔单的未来

6 根构造 K 线里，决策当根的 low 是 1，后面窗口的 low 是 100。截止时刻取第 4 根收盘。两条训练回撤都是 0。按拉普拉斯，限价 100.02 的概率是 0.75，限价 90 的概率是 0.25。在截止时刻之后再拼上一根 low 为 1 的 K 线，回撤和最后选出的价格都不变。那根未来低点用阶段 4 的规则单独复核，确实会穿过 100 减去 0.02；优化器的入参里没有 K 线，选价时读不到它。

这只证明截止规则。它不是全历史库上的成交率，也不能拿去调高某个策略的期望。

## 整数股、120 USDC 和摩擦

价格在 500 附近时，4 USDC 风险预算和 320 USDC 仓位上限都买不满 1 股。即使过滤器写着允许分数股，默认安全模式仍向下取整并记 `WHOLE_SHARE_EXCEEDS_RISK_OR_CAP`。

安全模式关闭、步长 0.01、价格在 50 附近时，份额可以是小数，但名义金额约 5 USDC，记 `NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY`，明细以 `notional<120` 开头。

150 对 150.50 的摩擦比例高于 20%，150 对 165 低于 20%。比例公式与阶段 10 相同。天花板 150、止盈 150.10、三档都贴着 150 时，名义金额已经超过 120，仍因摩擦比例高于 20% 被同一原因码拒绝。

## 超时幂等

`clientOrderId` 由意图编号和档位做 SHA-256，前缀 `v22`，长度 35，符合 `^[a-zA-Z0-9-_]{32,36}$`。同一意图再准备一次，返回原来的 id。

`-1007` 且经纪商已经收下：库里先是 `WAL_PREPARED`，调用瞬间是 `SUBMITTING`，随后只发一次 `POST /sapi/v1/equity/order/place` 和一次 `GET /sapi/v1/equity/order/detail`，状态 `DETAIL_CONFIRMED`。再提交会得到 `ALREADY_CONFIRMED`，不会出现第二个 id。

`-1007` 且经纪商没有收下：第一次 `DETAIL_ABSENT`，第二次用同一个 id 得到 `ACKNOWLEDGED`。经纪商字典里只有这一张单。`-1006` 且已经收下的路径与第一种 `-1007` 相同。

把已提交的行改成 `SUBMITTING` 再打开一个新进程，恢复时 `place` 次数是 0，只查 detail。`live_place` 对真实 `POST /sapi/v1/equity/order/place` 抛出 `LiveTradingDisabledError`。

## 风险台账

- P0：阶段 3 已经记录的密钥暴露仍未轮换。本阶段没有读取或写回密钥。
- P0：个股时点市值仍然空着。本阶段没有把个股放进委托样本。
- P1：币安与 SIP 的重叠样本仍然空着。本阶段没有读价格库。
- 本阶段新增：成交概率的训练函数只在构造 K 线上验证。没有用 2024-11-01 至 2026-04-30 的 5 分钟库估计真实成交率，样例里的 0.40 / 0.90 / 0.95 是注入值。
- 本阶段新增：整数股安全模式默认开启。阶段 0 的缓存不能证明 RTH 分数股限价会被接受，本阶段也没有真实 `486442` 回包。
- 本阶段新增：踏空成本用的是同一限价自己的条件净期望。它不估计“如果去打卖一会少赚多少”。
- 本阶段新增：幂等记录在本地 SQLite。`WAL_PREPARED` 提交之前进程消失，就没有可恢复的 id。提交之后，恢复路径不更换 id。
- 阶段 10 仍然有效：冷却锚点依赖调用方把分到的标的记成在途；S3 分到槽位也不构成可部署结论。
