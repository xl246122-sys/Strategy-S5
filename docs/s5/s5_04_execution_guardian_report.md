# S5-P4 执行模拟、跨收盘保护与账户风控

```text
S5 Phase / Gate: S5-P4 / G-S5-4
基线 Git SHA / 本阶段 Git SHA:
  基线 main = e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee
  上一阶段 feature/s5-eod-reversal = 4ba3aa4
  本阶段实现 feature/s5-eod-reversal = 0429682cf364bd4cde324a901768dee0aceb9020
  本报告在其后的单独提交里。分支没有合并。
实际修改的文件及行号:
  src/bstocks_v2/constitution/guard.py 第 24–26 行，第 117–124 行，第 145 行，第 186–188 行
  src/bstocks_v2/contracts/models.py 第 199 行
  src/bstocks_v2/portfolio_risk/authority.py 第 67 行，第 205 行起的 approve，第 269 行 cancel_unfilled，第 844 行起的 _draft
  src/bstocks_v2/strategies/s5/__init__.py 导出研究账本
  src/bstocks_v2/strategies/s5/policy.py 第 66–73 行 open_research
  tests/s5/test_signals.py 第 249–257 行，拒绝下单的那一行不再被当成路由
实际新增的文件:
  src/bstocks_v2/strategies/s5/execution.py
  src/bstocks_v2/strategies/s5/book.py
  tests/s5/test_execution.py
  docs/s5/s5_04_execution_guardian_report.md
原 S1-S4 回归测试（总数、通过、失败、跳过）:
  全量 pytest 247 passed，0 failed，0 skipped。其中阶段 12 的 15 个和阶段 13 的 9 个都在这次里面。
S5 新测试（总数、通过、失败、跳过）:
  本阶段新增 24 个，连同此前的 25 个，S5 共 49 个，全部通过。
是否发生生产文件、进程、订单、资金变更（必须为否）: 否。没有下单，没有撤单，没有改日本机，没有把 S5 放进仲裁器。
API 官方契约核验与只读实测差异:
  本阶段没有请求币安。DAY 与 GTC 是否能跨过 16:00 或 20:00，没有交易所回执。两边都按不可成交处理。
研究数据版本、数据范围、源和 session 覆盖率:
  没有新的行情。撮合只用测试里的合成报价和 K 线。锁箱没有读。
P0/P1/P2 余项: 这些成交是模拟。盘后可执行报价仍然没有。G14 仍沿用此前的 NO_GO，本阶段没有重开。密钥轮换仍开放。
本阶段 GATE: PASS
回滚命令与恢复验证:
  git switch feature/s5-eod-reversal
  git revert 0429682cf364bd4cde324a901768dee0aceb9020
  全局 Guardian 和 LimitOptimizer 在这个提交里没有 diff。
下阶段的前置条件: 投资人接受 G-S5-4。未接受不得开始 S5-P5。
投资人需要批准的具体变更: 章程新增研究用开仓时钟。默认路径不变。本阶段不请求实盘、不请求仲裁器准入、不请求影子采集。
```

S5 的持仓在自己的 sqlite 里。`Guardian.on_clock` 仍然不认识 S5。15:55（半日市 12:55）照旧只平掉 Guardian 账本里的仓。`S5GuardianPolicy.governs` 仍是 false，`on_clock` 仍拒绝。要跑模拟，必须显式调用 `open_research`。`live_place` 和 `live_cancel` 仍会拒绝。

`LimitOptimizer` 没有改。S1–S4 的限价单仍是常规时段。S5 的研究适配在 `execution.py`：两位价格、整数股回退、价差和深度、穿透队列。价格只碰到限价、没有再穿过 1 美分，不成交。跨过会话边界的 K 线整根丢弃。

## 公共代码

`check_order` 和 `PortfolioRisk.approve` 增加了可选参数 `session_policy`，默认 `GLOBAL`。不传它时，15:45 和半日市 12:45 的拒绝与原来一样。

`S5_RESEARCH` 只做一件不同的事：开仓买单可以落在研究窗口里，包括 15:45 之后和盘后。调用方必须把 `OrderDraft.research_only` 设为 true。5 个仓位、8% 高水位、120 USDC、摩擦占比、价格两位小数、禁止买市价、禁止卖空，都还在。实盘锁如果被打开，这条路径会拒绝。外部配置改不了这个锁。

`EntryRequest.trading_session` 默认仍是 `RTH`。`cancel_unfilled` 只撤销一笔从未成交的研究买单，有成交就不能用它抹掉。

## 跨收盘

实验臂 A 在收盘前进入，止盈是限价。15:55 不把它改成市价。16:00 之后，原来的 DAY 或 GTC 都记成 `EXPIRED_UNVERIFIED`，已成交数量保持 0。这不是在声称交易所会撤单，只是拒绝把下一时段的价格算成上一张单的成交。

盘后退出只用 `EXTENDED` 限价。20:00 之后，`EXTENDED` 不会被当成 `24H`。`overnightSupported` 不是 true 时，状态写成 `EXIT_REQUIRED`，新开仓冻结，并发出 CRITICAL 告警。支持夜盘时，新单是 `24H` 限价，旧单的成交数量仍是 0。

盘后和夜盘的卖出市价记为拒绝，不记成成交。常规时段里 S5 也不用市价，只允许限价。

同一根 K 线同时穿过止盈和止损时，按止损价记账。跳空穿过止损、但没有再穿透止损限价时，不编造成交价，状态是 `EXIT_REQUIRED`，告警 `STOP_UNFILLED`。

## 账户边界与竞态

研究买单仍先过 `PortfolioRisk`。第 6 个名字被 `MAX_POSITIONS` 拒绝。权益落到 8% 高水位以下后，下一笔 S5 买单被拒绝，锁的原因仍是 `HWM_LOCK`。经纪商代码 `486440` 当天禁止再买。

部分成交可以记下原始 `total_cost`。实现佣金在终态之前是空的，`declare_fee` 会抛出 `FeeTrapError`。差额不从权益里扣。

`486444` 且明细已经 `FILLED` 时，仓位平掉，不再发第二张卖单。代码到了但状态还不是 `FILLED` 时，进入 `EXIT_REQUIRED`，也不补卖。`486446` 或份额冻结时停在 `CANCEL_PENDING_FOR_SL`，卖单仍是原来那一张。卖出数量大于持仓会被拒绝。同一张退出单再提交，客户订单号不变。

进程关掉再打开，`EXIT_REQUIRED` 和冻结标志还在。

## 故障记录

24 个测试都在 `tests/s5/test_execution.py`。每个都是合成事件。

| 测试 | 结果 |
| --- | --- |
| 15:50、半日 12:50、16:10 的全局审批 | `NON_RTH`。研究路径可以开仓。摩擦超限和未知策略参数仍拒绝 |
| 15:55 与半日 12:55 | Guardian 里的 AAPL 平掉。S5 的 AIQ 仍是限价保护，没有市价单 |
| DAY 与 GTC 过 16:00 | 成交数量 0，状态 `EXPIRED_UNVERIFIED`，资金槽位释放 |
| 15:55–16:00 混合 K 线 | 不成交，原因 `MIXED_BAR` |
| 19:59 | 在途卖单是 `EXTENDED`，没有 `24H` |
| 20:01 且夜盘标志为 false | `EXIT_REQUIRED`，冻结新开仓，没有市价，没有 `24H` |
| 20:01 且夜盘标志为 true | 旧单未成交并过期，新单是 `24H` 限价 |
| 只触价 | `TOUCH_NO_PENETRATION`，成交数量 0 |
| 整数股与价格 | 数量 2，带 `486442`。三位小数和价格笼子拒绝 |
| 新闻、状态、价差、缺字段 | 不建仓。`AAPL` 不在宇宙里 |
| 交易所拒绝 | 不成交，槽位释放 |
| 停牌、无买价 | `EXIT_REQUIRED` 加 CRITICAL |
| 部分成交 | 终态前佣金为空，`tc - FN` 不入账 |
| 486444 | 已成交则平仓且只有一张卖单；未确认则不补卖 |
| 486446 | 冻结，卖单不增加 |
| 超卖与市价 | `SHORT`；盘后 `MARKET_AFTER_HOURS`；常规时段 `S5_LIMIT_ONLY` |
| 重复止损 | 客户订单号不变，在途卖单仍是一张 |
| 同柱止盈止损 | 按止损价成交 |
| 跳空穿过止损 | 成交数量 0，不编造市价 |
| 重启 | 冻结和 `EXIT_REQUIRED` 还在 |
| 周末 | `MARKET_CLOSED`，没有市价 |
| 五仓、高水位、PDT | 第六笔、回撤锁和 `486440` 都拒绝 |

2026-11-03，本地 Python：

```text
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q --tb=line
```

ruff 通过。mypy 168 个源文件通过。pytest **247 passed**，0 failed，0 skipped。

停在 G-S5-4。上面任何一笔成交都不是交易所成交。
