# S5-P1 市场数据契约

```text
S5 Phase / Gate: S5-P1 / G-S5-1
基线 Git SHA / 本阶段 Git SHA:
  基线 main = e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee
  本阶段实现 feature/s5-eod-reversal = fea3b8344d9fc54fc8b1796ea40c58a1a10b097d
  本报告在其后的单独提交里。分支没有合并，也没有远程。
实际修改的文件及行号:
  constitution/guard.py 第 29 行，增加 s5_live_enabled=False
  contracts/models.py 第 101–108 行、第 121–122 行
实际新增的文件:
  src/bstocks_v2/sessions/__init__.py
  src/bstocks_v2/sessions/calendar.py
  src/bstocks_v2/sessions/eligibility.py
  src/bstocks_v2/strategies/s5/__init__.py
  src/bstocks_v2/strategies/s5/lock.py
  src/bstocks_v2/strategies/s5/policy.py
  src/bstocks_v2/strategies/s5/research_config.json
  tests/s5/test_eligibility.py
  tests/s5/test_policy.py
  docs/s5/s5_01_market_data_contract.md
原 S1-S4 回归测试（总数、通过、失败、跳过）:
  全量 pytest 207 passed，0 failed，0 skipped。其中 198 个是本阶段之前已有的测试。
S5 新测试（总数、通过、失败、跳过）: 9 passed，0 failed，0 skipped。
是否发生生产文件、进程、订单、资金变更（必须为否）: 否。没有下单、撤单、部署，也没有碰日本机进程。
API 官方契约核验与只读实测差异:
  本阶段没有请求 exchangeInfo。解析器只接受调用方已经拿到的行。
  缺字段、非布尔标志、float 精度都拒绝。本地 exchange_slim.json 没有资格值，因此整份快照会 fail closed。
研究数据版本、数据范围、源和 session 覆盖率:
  沿用 S5-P0。夜盘历史带不存在。日历可以把 20:00–04:00 标成 OVERNIGHT，这不是成交证据。
P0/P1/P2 余项: 见下方。G14 仍是 NO_GO。密钥轮换仍开放。币安与 SIP 对齐根数仍是 0。
本阶段 GATE: PASS
回滚命令与恢复验证:
  git switch main
  git branch -D feature/s5-eod-reversal
  main 停在 e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee，S5 代码随之消失。
下阶段的前置条件: 投资人接受 G-S5-1。未接受不得开始 S5-P2。
投资人需要批准的具体变更: 无。本阶段不请求采集、不请求实盘、不请求把 S5 放进仲裁器。
```

仓库根目录 `D:\Strategy`。分支 `feature/s5-eod-reversal`。`data_v2_2/`、虚拟环境和密钥文件不在 Git 里。

## 契约

`TradingCalendar`（`sessions/calendar.py`）用已有的 XNYS 收盘表。它不改原来的 `XnysCalendar.classify`。

| 标签 | 规则 |
| --- | --- |
| PRE | 交易日 04:00–09:30 |
| RTH | 09:30 到当日收盘。半日市收到 13:00 |
| POST | 收盘到 20:00 |
| OVERNIGHT | 交易日 20:00 到次日 04:00。次日若不是交易日，04:00 之后是 `NON_SESSION` |
| NON_SESSION | 周末、休市，以及不接在交易日晚上的凌晨 |

`SessionEligibility` 不访问网络。一行 `exchangeInfo` 必须同时给出 `tradability`、`extendedSession`、`overnightSupported`、`fractionableEh`、`stepSize`、`minQty`、`minNotional`、`multiplierUp`、`multiplierDown`。缺任何一项是 `UNKNOWN_ELIGIBILITY`。float 是 `INVALID_FILTER`。

订单时段对应关系：

- RTH 限价单只能带 `tradingSession=RTH`。
- PRE 和 POST 限价单只能带 `EXTENDED`，并且 `extendedSession` 必须是 JSON 布尔 true。
- 20:00 之后的限价单只能带 `24H`，并且 `overnightSupported` 必须是 true。`EXTENDED` 不能当成 `24H`。
- 盘后、盘前、夜盘的市价单一律 `MARKET_AFTER_HOURS`。
- 买单市价在任何时段都是 `BUY_MARKET_FORBIDDEN`。
- 休市是 `MARKET_CLOSED`。

`TradeIntent` 新增字段都有默认值。旧的 S1–S4 载荷不带这些键仍能通过校验。缺省 `news_event_state` 是 `UNKNOWN`。`strategy_id` 原本就有。

S5 代码锁在 `strategies/s5/lock.py`：`LIVE_ENABLED = False`，`MODE = "RESEARCH_ONLY"`。配置文件写成 true 会被章程拒绝，函数仍然返回 false。`refuse_live_route` 总是抛出 `LiveTradingDisabledError`。

`PositionPolicy` 允许在研究契约里写下 `allow_cross_rth_close=True`，但 `active`、`live_enabled`、`session_eligibility_verified` 都是 false。`S5GuardianPolicy.governs` 固定返回 false。`Guardian.on_clock` 没有引用它。仲裁器准入集合仍然只有 S1–S4。15:45 与 15:55 的函数体没有改。

## 测试

2026-11-03，本地 Python：

```text
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q --tb=line
```

ruff 通过。mypy 156 个源文件通过。pytest **207 passed**，0 failed，0 skipped。

新测试覆盖：未知资格、休市、周末凌晨、半日 13:00 之后不再是 RTH、盘后市价单、`EXTENDED` 冒充 `24H`、`overnightSupported=false`、缺字段、float 步长、旧意图反序列化、S5 无下单入口、Guardian 源码未挂上 S5。

## 白名单与未做的事

本阶段只碰了上面列出的文件。没有做信号、特征、新闻拉取、回测或影子进程。S5 没有进入 `ADMITTED`。没有改 S1–S4 的参数和信号。没有放宽 8% HWM、五仓、0.25% 风险或 120 USDC。

仍开放：密钥轮换；币安与 SIP 重叠为 0；没有历史夜盘带；没有历史盘口；标的级 24H 资格未做只读实测，所以现在任何真实快照在这套解析器里都应被拒绝，直到字段被证明存在。

停在 G-S5-1。
