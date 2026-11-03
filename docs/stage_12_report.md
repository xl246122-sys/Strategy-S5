# 阶段 12 验收报告

```text
[阶段 12]：独立 24/7 Guardian 守护进程、撤单竞态（486446/486444）与阶梯降级退出
结论：G12 的工程门槛通过。Guardian 只对注入的 Mock 经纪商说话。实盘下单和撤单入口保持关闭
1. 修改文件：D:\Strategy\binance_stocks_v2_2\src\bstocks_v2\guardian\（spec.py、ids.py、ladder.py、journal.py、broker.py、machine.py、worker.py、strategy_stub.py、__main__.py，包入口；stage8.py 未改行为）。对账新增 reconciler\audit.py，包入口导出 LiveReconciler。ledger.py 只加了 SQLite busy_timeout=5000。测试在 tests\test_stage12.py。同一份研究代码已同步到日本机 /home/ubuntu/binance_stocks_v2_2。无 Git 仓库，因此没有 commit SHA。密钥没有写入工程。
2. 测试证据：2026-11-02。本地 Python 3.11.5 与日本机虚拟环境 Python 3.12.16 都是 ruff 全过、mypy 126 个源文件全过、pytest 169 passed。阶段 12 新增 15 个故障注入测试。本地全量 8.99 秒，日本机全量 3.27 秒。没有行情扫描，也没有调用真实下单或撤单。
3. 接口与数据实证：本阶段不连交易所，不读 5 分钟库，不进入 2026-05-01 之后的锁箱。486444、486446、486437、486441、-1112、486413、-1007 都是 Mock 脚本返回的代码。价格笼子、2 位小数和 clientOrderId 按宪法核对，没有新的交易所回包。
4. 模型与执行表现：本阶段没有新的成交回放，也没有新的期望或 DSR。样例截面上，买一 100.01 乘 0.997 得到 99.70997，向 0 截断到 99.70。地板价 99.75 高于这个截断价时，可成交限价停在 99.75。10.5 股遇到 486441 后，扩展限价只发 10 股，0.5 股留在 EXIT_REQUIRED。0.4 股没有整数部分，不发扩展限价。
5. 风控不变量核查：live_trade_enabled 与 S4 的实盘开关都固定为 false。live_place 与 live_cancel 对真实 POST /sapi/v1/equity/order/place 和 /order/cancel 抛 LiveTradingDisabledError。卖单带 walletType=CARD、tokenize=false。市价卖单只有 quantity。部分成交的 1856.2 与 927.5 只作原文留存，已实现佣金保持为空。120 USDC 仍是开仓门禁；退出按已成交份额处理，不把碎股退出再套一次开仓名义金额。8% HWM 锁属于阶段 13，本阶段没有实现，也没有解锁开关。
6. 风险台账：见下方。阶段 3 的密钥、时点市值和跨源重叠仍然开着。本阶段没有再查 Funding 钱包，三维对账用的是注入快照。阶段 7 的概率门禁没有接进来。15 项闭环都在 Mock 里完成，不能代替交易所真实回包。
7. G12：15 项故障注入全部通过。策略进程被强杀后，独立 Guardian 进程仍把持仓走到 FLAT_RECONCILED。Guardian 自己被强杀后，重启时从 SQLite WAL 恢复的计时低于 100 毫秒，止盈单仍在，没有新的委托。-1112、486413、486441、486444、486446 都有对应终态，486446 等待期间 486437 的次数是 0。
8. 对当前生产系统的影响：没有下单。日本机只更新了研究代码。环境文件、腾讯云代理、原始行情库和 5 分钟特征库未改。没有重启任何服务。
9. 下一阶段建议：不要打开 S4 的实盘开关。不要把这次 Mock 守护进程接到 api.binance.com。不要为了把 0.4 股发出去而改用扩展时段分数股。不要提前做阶段 13 的 8% HWM 锁。不要用 2026-05-01 之后的锁箱去补成交样本。阶段 7 的概率门禁仍然不进入后续阶段。等投资人批准后再进入阶段 13。
停在当前阶段，等待投资人明确批准。
```

## 状态机

持久化在 SQLite，`journal_mode=WAL`。一条标的一行。

`BUY_PENDING` 收到部分成交后进入 `PARTIAL_POSITION`，止损价、止盈价和最大持仓时间随已成交份额写下。买单撤成终态且仍有成交时进入 `PROTECTED_POSITION`，再挂 `SELL LIMIT` 进入 `TP_PENDING`。止损、最长持有或 15:55 强平碰到在途止盈单时进入 `CANCEL_PENDING_FOR_SL`，卖出锁先落下。撤单终态确认后，剩余可卖份额进入 `EXIT_EXECUTION_LADDER`。平仓确认后是 `FLAT_RECONCILED`。

对账不平进入 `UNCERTAIN`，同时把 `halt_new_entries` 写成 1。撤单超过 5 秒仍无终态，或 `486444` 查不到已成交明细，进入 `FROZEN_ESCALATED` 并同样停止新开仓。这两条升级路径写在状态机里；15 项脚本没有把等待拖过 5 秒。

`clientOrderId` 由标的和角色做 SHA-256，前缀 `g12`，长度 35。同一角色再次准备会得到原来的 id。

## 十五项故障注入

1. 买单 10 股部分成交 5 股，原文总成本 1856.2、已成交名义 927.5。状态是 `PARTIAL_POSITION`，已实现佣金为空。TTL 撤单后只为 5 股挂止盈限价，买单终态是 `CANCELED`，成交 5。
2. 撤止盈返回受理状态 `S`。可见终态放在 800 毫秒之后。这段时间第二次止损请求不再撤单、也不发卖单，`486437` 次数为 0。终态出现后只发一张 `SELL MARKET`，数量 10，不带价格和 `tradingSession`。
3. 撤单返回 `486446` 并且份额保持冻结。轮询期间没有卖单，`486437` 为 0。释放冻结并确认 `CANCELED` 之后，才发数量 10 的市价卖单并到达 `FLAT_RECONCILED`。
4. 止盈单已经 `FILLED`。撤单返回 `486444`，查明细确认后进入 `FLAT_RECONCILED`，卖单条数不变，重复下单次数为 0。
5. 10 股止盈成交了 4 股后撤成。随后的市价卖单数量是 6。
6. `SELL MARKET` 收到 `-1112`。下一张是 `SELL LIMIT`，`tradingSession=RTH`，价格 99.70，数量 8，`walletType=CARD`，`tokenize=false`。
7. `SELL MARKET` 收到 `486413`。地板价 99.75 高于截断后的 99.70，限价用 99.75，会话仍是 `RTH`。
8. 时钟放在 16:00:01 ET。`SELL MARKET` 收到 `486441`。10.5 股改发数量 10 的 `EXTENDED SELL LIMIT`，0.5 股记 `EXIT_REQUIRED`。0.4 股没有整数部分，只留下 `EXIT_REQUIRED`，扩展限价张数为 0。
9. 全日 12:55 和 15:54:59 都不撤、不卖。15:55:00 撤销未成交买单并市价卖掉持仓。半日市 12:54:59 不动，12:55:00 清空。截止时刻来自宪法里的 `flatten_deadline`，全日 15:55:00，半日 12:55:00。
10. `TRADING_HALTED` 时止损进入 `EXIT_REQUIRED`，撤单次数和卖单次数都不变。状态改回 `TRADING` 后，先确认止盈撤单终态，再市价平仓到 `FLAT_RECONCILED`。
11. 止盈单在没有回报的情况下被标成 `FILLED`。`recover` 读 `/order/detail` 的 Mock 明细，把卖出记入本地账本，再与成交历史和 Funding 快照对账。本地、成交、钱包都是平仓，状态 `FLAT_RECONCILED`，新卖单张数为 0，恢复计时低于 100 毫秒。
12. 策略替身进程和 Guardian 进程各开一个。强杀策略进程后，Guardian 的 pid 还在，并在收到退出命令后把 AAPL 做到 `FLAT_RECONCILED`。日本机上 `Popen.kill()` 是 `SIGKILL`。本地 Windows 上同一测试走 `TerminateProcess`。两边都通过。
13. Guardian 被强杀后再次拉起。两次恢复的 `recover_ms` 都小于 100，状态保持 `TP_PENDING`，委托日志条数不变。计时只包住从 WAL 读回和对账的那一段，不把解释器启动算进去。
14. 挂止盈时注入 `-1007`，单已经落在经纪商里。下单当下守卫状态是 `SUBMITTING`，随后是 `DETAIL_CONFIRMED`。`clientOrderId` 不变，委托日志只有 1 条，重复下单次数为 0。再把守卫改回 `SUBMITTING` 后调用 `recover`，只查明细，下单次数仍是 1。`live_place` 和 `live_cancel` 抛出 `LiveTradingDisabledError`。
15. 本地与成交历史都是 AAPL 10 股，Funding 快照里的 `EQ_AAPL` 是 7。对账结果 `matched=false`，`halt_new_entries=true`，持仓进入 `UNCERTAIN`。新买单被 `HALT_NEW_ENTRIES` 拒绝。关掉进程再打开，锁定和 `UNCERTAIN` 仍在。

## 风险台账

- P0：阶段 3 已经记录的密钥暴露仍未轮换。本阶段没有读取或写回密钥。
- P0：个股时点市值仍然空着。本阶段没有把个股放进委托样本。
- P1：币安与 SIP 的重叠样本仍然空着。本阶段没有读价格库。
- 本阶段新增：十五项异常都来自 SQLite 里的脚本经纪商。没有真实的 `486444`、`486446`、`-1112` 或 `486441` 回包，因此不能据此打开实盘守护进程。
- 本阶段新增：0.5 股和 0.4 股停在 `EXIT_REQUIRED`。下一 RTH 开盘再卖出的分支写在 `poll` 里，这 15 项没有把时钟拨到下一个 09:30 去执行它。
- 本阶段新增：撤单确认的升级时限是 5 秒，到点进入 `FROZEN_ESCALATED` 并停止新开仓。15 项脚本的等待都短于这个时限。
- 阶段 11 仍然有效：成交概率没有在历史库上拟合；整数股安全模式默认开启；幂等记录在提交前消失就没有可恢复的 id。
- 阶段 10 仍然有效：冷却锚点依赖调用方把分到的标的记成在途；S3 分到槽位也不构成可部署结论。
