# 阶段 0 验收报告 — 只读审计（G0）

- 审计时间：2026-11-02 13:13–13:20 Asia/Shanghai（服务器 UTC 05:13–05:16）
- 范围：日本服务器 `43.163.203.127` 与本地 `D:\Strategy`。同盘上已存在、但位于工作区之外的历史库只做了只读盘点，没有复制、移动或改写。
- 变更：服务器业务文件、进程、cron、systemd 均未修改。未调用下单、撤单或免责声明签署接口。未读取 `D:\Key Store` 中的任何密钥文件。

## A. 现状架构与资源盘点

```text
本地 D:\
  Strategy\                         本项目工作区。只有方案、宪法、.gitignore。无代码、无数据库、无 Git。
  Alpaca_Research_DB\FRESH_2Y_...   已完成的 SIP 五周期库（2024-11-01 .. 2026-10-30）
  AlpacaData\                       更早的 Gate3 库：1 分钟仅 2026-07-23 .. 2026-10-23
  Stock-Quant\stock-q6-orb-lab\     旧 ORB 研究/虚拟盘实验。不下真单。
  Key Store\                        凭据目录。本次未打开。

日本 VM-0-13-ubuntu  43.163.203.127
  Ubuntu 26.04.1 LTS / 内核 7.0.0-30-generic
  2 vCPU（AMD EPYC 9754 切片）/ 内存 1.9 GiB（可用约 1.4 GiB）/ 磁盘 49 G，已用 6.1 G，剩余 41 G
  启动约 30 分钟。负载 0.11。chrony 偏差 +0.035 ms。
  监听端口只有 SSH 22 与本机 DNS/NTP。
  /home/ubuntu、/opt、/srv 没有交易代码、虚拟环境、SQLite 或 CSV。
  运行中的非系统进程是腾讯云代理：tat_agent、barad_agent、YDService、YDLive。
  用户 crontab 为空。root crontab 只有腾讯 stargate，每 5 分钟一次。无失败 systemd 单元。
```

本地 `D:` 剩余约 614 GB，`C:` 剩余约 600 GB。全量历史库应继续放在本地，不要整库搬上这台 41 GB 剩余空间的云主机。

Python：`python3 --version` 为 3.14.4；`dpkg` 中 `python3` 为 `3.14.3-0ubuntu2`。没有 `pip`、`uv`、`conda`、`docker`。有 `git`。这与阶段 1 要求的 Python 3.11/3.12 独立环境不一致，阶段 1 需要单独安装，不能改系统 Python。

东京到币安的 TCP 连接（各 7 次）：

| 目标 | 最小 | 中位 | 最大 | 平均 |
|---|---:|---:|---:|---:|
| `api.binance.com:443` | 2.77 ms | 3.50 ms | 3.58 ms | 3.26 ms |
| `nbstream.binance.com:443` | 3.35 ms | 4.00 ms | 4.89 ms | 4.08 ms |

`GET /api/v3/time` 返回 HTTP 200，总耗时 16.5 ms，`serverTime` 与当前日期一致。股票接口走 CloudFront 东京节点（`NRT12-P8` / `NRT20-P1`）。

## B. 七项核查结论

### 1. 钱包、持仓与三维对账

服务器和 `D:\Strategy` 都没有 API Key、没有订单日志、没有持仓快照。因此本次不能回答“`tokenize=false` 买入后，Funding 钱包是否直接返回 `EQ_*`”。也没有免责声明签署记录，无法证明账户已避免 `486410`。

旧实验 `collector_vps/binance_exchange_probe.py` 明确写着行情访问不能证明下单或账户权限，并把每个标的的 `eligible_to_trade` 固定写成 `False`。没有 `get-funding-asset` 的调用实现，也没有三维对账器。

### 2. 分数股与限价单

2026-11-02 从日本服务器重放只读请求：

- 不带 Key：`GET /sapi/v1/equity/market/exchangeInfo`、`/quote`、`/tokenized-assets` 都是 HTTP 400，正文 `{"code":-2014,"msg":"API-key format invalid."}`。耗时约 20–26 ms。
- 只带一个无效 Key 头：同一 `exchangeInfo` 变为 HTTP 400，`{"code":-2008,"msg":"Invalid Api-Key ID."}`。
- 方案里写的“无需签名的公开接口”，在这次实测中至少需要合法的 `X-MBX-APIKEY`。签名是否另外必需，要等真实只读 Key 才能判断，因为请求在 Key 校验阶段就结束了。

因此今天没有拿到新的 `exchangeInfo` 正文。可用的是旧实验缓存：

`D:\Stock-Quant\stock-q6-orb-lab\reports\binance-exchange-wire.json`  
文件时间 2026-10-30 07:34:20 UTC，7,957 个标的。样例（`A`）：

```json
{
  "symbol": "A",
  "tradability": "BUY_SELL",
  "fractionable": true,
  "fractionableEh": true,
  "stepSize": "0.000000001",
  "minNotional": "5.00000000",
  "multiplierUp": "1.1000",
  "multiplierDown": "0.9000",
  "maxNumOrders": 200
}
```

缓存统计：`BUY_SELL` 7,837，`OFFMARKET` 99，`NONE` 21。`fractionable=true` 6,369，`false` 1,588。`fractionableEh=true` 6,342。`stepSize` 全部是 `0.000000001`。`minNotional` 全部是 `5.00000000`，不是蓝图里写的 1 美元。7,956 个标的没有 `minQty` 字段；只有 `MU` 带了 `minQty=0.005`。

这份缓存不能证明 RTH 分数股 `LIMIT` 会被 `486442` 或 `486443` 拒绝。旧代码没有下过真单，也没有这类拒单日志。`fractionable=true` 只说明交易所声明可以拆碎，不说明限价单接受小数数量。阶段 2 必须用只读 Key 重拉 `exchangeInfo`；`486442` 的行为只能在阶段 11 的 Mock 和后续受控验证里闭环，不能靠这次缓存下结论。

`TSLA`、`AAPL`、`SPY`、`QQQ` 都在这份清单里。`SPCX` 也在清单里，但 479 只研究股票池 CSV 不包含 `SPCX`。

### 3. PDT 与交割

没有账户成交历史，没有 `486440` 或 `486439` 日志，无法判断 1,600 USDC 规模下每天 3–5 笔日内回转是否已经触发过 PDT 或 T+1 冻结。旧虚拟盘用的是本地现金，不经过币安交割。这项在拿到只读账户权限之前保持未验证。阶段 0–14 本来也不真实下单。

### 4. WebSocket 与 `tc` 字段

`nbstream.binance.com:443` 能完成 TCP 连接，对端 `52.68.34.6`。没有 `listenKey`，所以没有订阅 `{listenKey}@orderReport`，也就没有今天的实盘部分成交报文。

工作区和服务器上都没有归一化代码，不存在“现有代码算错 `tc - FN`”的实现。蓝图引用的官方危险样例仍然有效，留给阶段 1 的单元测试：

`Q=10, p=185.5, fq=5, FN=927.5, tc=1856.2`。若误用 `tc - FN`，会得到 928.7，而不是真实佣金。

`listenKey` 续期代码不存在。旧实验只连公共组合流，不创建用户流。

### 5. 无历史 K 线接口时的恢复

实测 `GET /sapi/v1/equity/market/klines?symbol=AAPL&interval=5m` 返回 HTTP 404：

```json
{"error":"Not Found","message":"No message available","status":404,"path":"/sapi/v1/equity/market/klines"}
```

服务器上没有 `intraday_bars_wal`，没有断流回补，没有 VWAP/ORB 热恢复。盘中重启目前会丢失当天 09:30 ET 以来的全部币安 K 线。这是空系统，不是一个坏掉的恢复器。

### 6. 旧 ORB 为什么没有进入正式下单

`D:\Strategy` 和日本服务器都没有 ORB 代码。相关实现在工作区之外的 `D:\Stock-Quant\stock-q6-orb-lab`，而且它被写成虚拟盘。

断点有两层：

1. 正式下单链路不存在。`research_pc/paper_live.py` 第 1 行写明本地前向虚拟会话，从不向券商发单。`PaperEngine.quote()` 在内存里用报价加减滑点模拟成交，没有 `POST /sapi/v1/equity/order/place`。
2. 即使在虚拟盘内部，信号也会在进入可成交状态前被丢掉。`research_pc/paper_engine.py` 的 `bar()` 先发出 `SIGNAL` 并分配本地订单号；若 `entries_paused`、`risk_halted`、`emergency_halted` 或 `allowed()` 不通过，立刻以 `risk_or_market_gate` 拒绝并返回。另外，开盘前 15 分钟、收盘前 20 分钟、K 线不完整、价格没有突破开盘区间高点时，函数在发出信号之前就返回。
3. 行情探针把交易资格写死为不可交易。`collector_vps/probe.py` 的 `symbol_rows()` 对每个标的设置 `eligible_to_trade: False`。`binance_exchange_probe.py` 同样写死。`tests/test_p0.py` 把这个 `False` 当成预期结果。

所以旧 ORB 可以产生研究信号和虚拟订单，但没有任何一条代码把信号送到币安正式委托。这次不修改该实验室。

### 7. 数据源

`D:\Strategy` 内没有候选 CSV，也没有 Alpaca 文件。数据在工作区外面，且本次没有改动。

| 位置 | 内容 | 口径 |
|---|---|---|
| `D:\Alpaca_Research_DB\股池数据\GrokBot_美股ETF股票池479_五周期_全新采集_20261101.csv` | 479 只候选 | 文件声明采集计划为 SIP；`SPCX` 不在池内 |
| `D:\Alpaca_Research_DB\FRESH_2Y_5TF_20261101` | 1Min / 5Min / 1Hour / 1Day / 1Week | `feed=sip`，`adjustment=raw`，纽约日期 2024-11-01 至 2026-10-30。481 个代码（479 只股票池 + `SGOV` + `BIL`）。57,629 个分片，126,780,498 行，约 5.99 GB。HTTP 200 共 31,822 次 |
| `D:\AlpacaData` Gate3 | 476 只 | 同样标记 `feed=sip`。1 分钟只有 2026-07-23 至 2026-10-23（66 个交易日）；日线从 2023-09-25 到 2026-10-23 |

FRESH 与 2026-10-30 的币安缓存相交 474 只。不在缓存中的 5 只是 `EA`、`EQR`、`IBUY`、`MPLX`、`PARA`。下载报告已把 `EA`、`PARA` 标成退市或历史实体，不能直接当可交易标的。

FRESH 的质量报告写明：五个周期都没有重复时间戳，没有 OHLC 违规；研究窗口内没有缺失交易日。日线有 203 次绝对对数收益超过 20% 的跳跃，其中 23 次对得上公司行为，180 次未匹配。`NFLX`、`XLK` 等拆股日仍是 raw 价格。阶段 3 必须单独做复权与时点过滤，不能把这套库直接当成已防泄漏的特征库。

未发现这套 FRESH 库使用 `feed=iex`。旧 Gate3 也标记为 SIP，但 1 分钟窗口更短，不能和 FRESH 的 1 分钟混在同一个 RVOL 里。

## C. 风险清单

| 级别 | 事项 |
|---|---|
| P0 | 没有可用的币安只读 Key。`exchangeInfo` 今天无法匿名拉取，钱包、免责声明、PDT、分数股限价拒单都还没实证。 |
| P0 | 没有日内 K 线 WAL。币安 K 线 REST 返回 404，盘中重启会失去当天 VWAP 和 ORB。 |
| P0 | 旧 ORB 没有正式下单实现。不能把虚拟盘引擎接进实盘。 |
| P1 | 云主机只有 2 vCPU 和 1.9 GiB 内存。300 标的行情流、Guardian 和回测不能同时堆在这台机器上。全量 Parquet 留在本地。 |
| P1 | 系统 Python 是 3.14，且没有 pip。阶段 1 必须另建 3.11 或 3.12 环境。 |
| P1 | 2026-10-30 缓存里的 `minNotional` 是 5，不是蓝图中的 1。阶段 1 的 120 USDC 经济门槛仍然有效，但交易所最小值要以重拉结果为准。 |
| P1 | FRESH 是 raw 价格。拆股日会出现巨大跳跃。直接用于回测会把复权错误当成收益。 |
| P2 | 缓存里 7,956 个标的没有 `minQty`，只有 `MU` 有。字段契约不稳定，归一化不能假设 `minQty` 总是存在。 |
| P2 | 日本机刚启动不久，bash 历史里只有 `apt update/upgrade/autoremove`。没有交易进程可保护，也不要把腾讯云代理关掉。 |

## D. 现有材料与 V2.2 的差异

| 项目 | 现在 | V2.2 要求 |
|---|---|---|
| 运行位置 | 日本机为空；旧实验只在本地 | 本地研发目录 + 日本 `/home/ubuntu/binance_stocks_v2_2` |
| 下单 | 不存在。虚拟盘本地撮合 | 单一网关，默认 `live_trade_enabled=False` |
| `orderReport` | 无用户流、无 `EQ_` / `tc` 处理 | `EquityPayloadNormalizer`，禁止未完成时用 `tc-FN` |
| 日内 K 线 | 无 WAL；K 线 REST 404 | 每根 5 分钟 K 线落 SQLite，100 ms 热恢复 |
| 对账 | 无 | 本地账本 + 挂单/成交 + CARD 钱包 |
| 风控 | 虚拟盘有暂停开仓和日内亏损开关 | 5 槽、120 USDC、8% HWM 永久锁、PDT |
| 研究数据 | FRESH SIP 五周期已在 `D:\Alpaca_Research_DB` | 独立 `D:\Strategy\data_v2_2`，时点股票池，不覆盖旧库 |
| 密钥 | 在 `D:\Key Store`，本次未读 | 环境变量或服务器本地文件，不进 Git |

## E. 建议复用与不要复用

可以只读引用：

- FRESH 的 SIP 五周期 Parquet、清单和公司行为文件，作为阶段 3 的来源。复制或建立新清单，不改原目录。
- 2026-10-30 的 `exchangeInfo` 缓存，只作为字段样例。阶段 2 要用新的只读 Key 重拉。
- 股票池 CSV 的 479 只代码，作为候选起点。还要再排除不可交易、退市和 `SPCX`，并做时点市值过滤。
- 服务器上已经正常的 chrony。

不要复用为实盘模块：

- `PaperEngine` 的内存撮合。它会发虚拟成交，也会在信号之后拒绝，而且没有币安订单状态机。
- `eligible_to_trade = False` 这段探针逻辑。它是安全占位，不是交易资格判断。
- Gate3 的短窗口 1 分钟数据。不能和 FRESH 混算 RVOL。
- 腾讯云代理和 stargate cron。

## F. 目录与资源预算

```text
D:\Strategy\binance_stocks_v2_2\          阶段 1 起的代码
D:\Strategy\data_v2_2\                    阶段 3 的独立研究层
D:\Strategy\docs\                         各阶段报告
/home/ubuntu/binance_stocks_v2_2\         日本侧代码、SQLite WAL、精简研究集
```

日本机适合跑行情入库、对账和 Guardian。1.9 GiB 内存下，重点扫描池要按连接数分片，回测放在本地。FRESH 约 6 GB，加上派生特征后本地仍有约 614 GB 空闲。云盘 41 GB 只保留代码、WAL 和校验清单。

阶段 0–14 不真实下单。阶段 15 另需书面授权。

## G. 需要拍板的事项

1. 阶段 1 是否按空环境新建，并继续不改动 `D:\Stock-Quant` 和两套 Alpaca 旧库。
2. 是否提供币安只读 API Key（行情 + Funding 钱包只读）。交易权限留到阶段 15 的单独授权。Key 不进 Git。
3. 研究股票池是否就以这份 479 只 CSV 为起点，并确认 `SGOV`/`BIL` 继续排除在回测主池之外。
4. 阶段 3 是否只读引用 FRESH，再写出独立的 `data_v2_2` 清单，而不是把 Parquet 再下载一遍。
5. 云主机维持 2 vCPU / 1.9 GiB，只跑实盘进程；重型回测留在本地。

## 验收

G0 的“生产零变更、零交易”已满足。七项里，网络、K 线 404、旧 ORB 断点、SIP 数据盘点有直接证据。钱包、免责声明、PDT、分数股限价拒单和当天的 `orderReport` 报文，因为没有只读 Key 和没有历史账户日志，明确标为未验证，没有用缓存代替今天的账户事实。

阶段 0 到此停止。未进入阶段 1。
