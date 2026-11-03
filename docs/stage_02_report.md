# 阶段 2 验收报告

```text
[阶段 2]：三级 WebSocket 分片池、本地日内 K 线 WAL 热恢复与三维只读对账适配
结论：CONDITIONAL
1. 修改文件：D:\Strategy\binance_stocks_v2_2\ 下新增行情、分片、WAL、对账与只读探测代码，并同步到 /home/ubuntu/binance_stocks_v2_2\。无 Git 仓库，因此没有 commit SHA。密钥只放在服务器 /home/ubuntu/.config/bstocks_v2/stage2.env，权限 600，没有进入工程、日志或本报告。
2. 测试证据：2026-11-02。本地 Python 3.11.5 与日本机 Python 3.12.16 均为 ruff 全过、mypy 全过、pytest 29 passed。本地语句覆盖率 73%。覆盖率低于阶段 1，是因为只读探测脚本只在服务器实跑，没有进单元测试。
3. 接口与数据实证：日本机 2026-11-02 06:13:57 UTC（美东 01:13，常规交易时段未开）完成只读探测。明细见下方。未调用 /order/place 与 /order/cancel。
4. 模型与执行表现：本阶段没有策略回测。
5. 风控不变量核查：live_trade_enabled 仍固定为 false。阶段 1 的部分成交 tc-FN 用例仍在 29 项测试内。NEW 与 ACCEPTED 在发往网络之前被拒绝。账户里已有的 6 笔未完成订单保持原样。
6. 风险台账：见下方 P1/P2。无新的 P0。
7. G2：WAL 热恢复与状态流误报门禁满足。300 标的分片在休市窗口 8/8 连接并完成 PING/PONG，持续约 15 秒。常规交易时段的连续推流尚未观察，因此本阶段为 CONDITIONAL。
8. 对当前生产系统的影响：没有下单。日本机新增的是行情只读代码和一份权限 600 的环境文件。腾讯云代理与系统 Python 3.14 未改动。账户里原有的未完成订单和持仓未被动。
9. 下一阶段建议：阶段 3 只做 SIP 历史库、时点股票池和防泄漏。开做之前先确认资金快照为什么在几分钟内少了 USDC。
停在当前阶段，等待投资人明确批准。
```

## 实测时间与环境

- 日本机：`VM-0-13-ubuntu`，Python 3.12.16
- 只读探测：`2026-11-02T06:13:57Z`，美东 01:13，常规交易时段未开盘
- `kill -9` 恢复：同一轮服务器测试，约 `2026-11-02T06:06Z`

## REST 只读

| 接口 | 结果 |
| --- | --- |
| `GET /sapi/v1/equity/market/exchangeInfo` | HTTP 200，5 次耗时 59.23 / 74.31 / 61.65 / 59.23 / 80.32 ms，响应约 3.05 MB |
| `GET /market/tokenized-assets` | HTTP 200，31.19 ms，106 条 |
| `GET /market/quote?symbol=AAPL` | HTTP 200，30.24 ms，含买卖价字段 |
| `GET /market/quote?symbol=NOTAREALSYMBOLZZZ` | HTTP 200，空响应体 `b""`，187.30 ms，解析器没有调用 `json.loads("")` |
| `GET /order/open-orders` | HTTP 200，33.11 ms，6 笔未完成订单 |
| `GET /order/history?orderStatus=FILLED` | 带 7 日 `startTime`/`endTime` 后 HTTP 200，40.99 ms，分页对象 |
| `GET /trade/history` | 同样带 7 日窗口，HTTP 200，34.89 ms，分页对象 |
| `POST /sapi/v1/asset/get-funding-asset` | HTTP 200，29.94 ms |
| `POST /sapi/v1/equity/listenKey` | 创建成功，只记录长度为 36，密钥本身未打印 |
| Alpaca `data` SIP 1 分钟线 | AAPL，`2026-10-30T13:30:00Z` 至 `13:40:00Z`，11 根，`feed=sip` |

订单历史和成交历史在不带时间窗口时返回 `-1102`：必须有 `startTime` 或 `endTime`。客户端已改为由调用方传入窗口，单元测试锁住了终态过滤和时间参数。

## WebSocket

- L1：`price` + `calendar` 单连接打开，收到 1 条 `price`，应用层 PING/PONG 成功。快照字段是 `e` 与 `rates`。`mp` 在每条 rate 上。这次快照解析出 19 个阶段，取值同时有 `C` 和 `ON`，因此没有合成单一市场阶段。
- L2：从 `exchangeInfo` 选出 300 个标的，3 条流，900 个 stream，8 个分片，每片最多 120。8 个连接全部打开，15 秒后 8 个都完成 PING/PONG，错误列表为空。
- L3：对 `wss://nbstream.binance.com/equity/ws` 发送 `SUBSCRIBE` 与 `UNSUBSCRIBE`，应答 `id=1` 与 `id=4`。

休市期间第一片分片在 8 秒内没有等到 K 线。`vw` 为空时回退到成交额/成交量或 `(h+l+c)/3`，`T` 为空时回退到事件时间 `E`，这两项由单元测试锁住。

## kill -9 与 VWAP

写入 78 根 5 分钟 K 线后，进程打印 `READY`，随后 `kill -9`。新进程打开 SQLite 并恢复：

```text
elapsed_ms=0.551 match=True bars=78 vwap=10.385
```

0.551 ms，数值与写入前的累积 VWAP 一致。缺口测试另证：缺一根 K 线时 `vwap_intact=False` 且不计算 RTH VWAP；补上该根后恢复，Wilder RSI(6) 在样例 `10,11,12,11,10,9,10` 上等于 50。

状态流 `calendar`、`@tradability`、`@tradingStatus` 在 24 小时无消息、连接仍存活时 `stale=False`。周期流 `price` 超过 10 秒无消息则判为陈旧。

## 资金快照

06:07 UTC 的 Funding 钱包资产为 `BNB`、`EQ_AMDL`、`EQ_BRKU`、`EQ_INTC`、`EQ_SPCX`、`USDC`。06:11 与 06:13 的两次读取没有 `USDC`，其余四个 `EQ_` 与 `BNB` 仍在。本阶段没有下单。三维对账器在本地账本、成交回报和钱包数量不一致时把 `halt_new_entries` 置为真。当前进程的本地账本是空的，不能把这次快照当成已经对齐。

## 风险台账

- P1：几分钟内的只读资金快照不一致，USDC 从有到无。阶段 3 之前需要再做一次只读确认，查清是余额变为零后接口省略了该资产，还是账户外有其他程序在动资金。
- P2：账户已有 6 笔未完成订单，以及 `EQ_AMDL`、`EQ_BRKU`、`EQ_INTC`、`EQ_SPCX`。新账本尚未纳入这些存量。
- P2：Funding 钱包里有 `BNB`。策略报价资产是 USDC。本阶段没有交易该资产。
- P2：探测发生在美东 01:13，未见 `@kline_5m` 实盘推送。空值回退目前由单元测试证明。

## 测试命令

本地与日本机：

```text
ruff check .
mypy src
pytest --cov=bstocks_v2
```

日本机恢复：

```text
python -m bstocks_v2.market_ingestor.wal_restart write <db>
kill -9 <pid>
python -m bstocks_v2.market_ingestor.wal_restart read <db>
```
