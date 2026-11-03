# 阶段 3 三项 P0/P1 补救

```text
[阶段 3 补项]：密钥暴露、时点市值、币安与 SIP 重叠
结论：时点市值已补进研究池；密钥轮换和跨源重叠仍未关闭
1. 修改文件：research_data\sec_caps.py、sec_fetch.py、refresh_membership.py、universe.py，tests\test_pit_caps.py，stage14\spec.py 只改了注释。研究输出在 D:\Strategy\data_v2_2\universe\。旧成员表已另存，没有重跑特征构建，没有重跑锁箱。无 Git 仓库，因此没有 commit SHA。密钥没有写入工程或本报告。
2. 测试证据：2026-11-03。本地 Python 3.11.5，ruff 全过，mypy 148 个源文件全过，pytest 189 passed。
3. 接口与数据实证：没有调用下单或撤单。没有请求现货 /api/v3/klines。SEC companyfacts 只用于股本，SIP 原库只读。
4. 模型与执行表现：没有新回测，没有调参，没有第二次锁箱。
5. 风控不变量核查：live_trade_enabled 仍固定为 false。S4 的 live_enabled 仍固定为 false。8% HWM、5 仓和 120 USDC 门槛没有改。
6. 风险台账：密钥轮换仍是 P0。币安与 SIP 重叠仍是 P1。时点市值序列不再是空的；没有可用披露的代码继续留在 market_cap_unverified。Funding 无 USDC 和两项 P2 不在本次范围。
7. G14：不因此改为通过。封存结论仍是 NO_GO / RESEARCH_ONLY。DSR 和邻域检验没有变，P0 也还在。
8. 对当前生产系统的影响：没有下单。日本机代码和 data_v2_2 没有同步。服务器环境文件只核对了权限和变量名长度，没有打印内容。
9. 下一步：由投资人在币安和 Alpaca 作废旧密钥，把新值只写入服务器那份权限 600 的环境文件。跨源重叠不能用窗口结束后的行情回补。不要进入阶段 15。
停在当前补项，等待投资人明确批准。
```

封存文件 `D:\Strategy\data_v2_2\reports\stage14_lockbox_seal.json` 没有改。它记录的锁箱正文 SHA-256 仍是 `82474de9f00ada7edd975bef5d103bb31db662fb5dcc7f37f9d5449e7332821e`。`OPEN_P0_P1` 仍是锁箱当时的三项，注释写明后来的补丁不改这份清单，也不再跑锁箱。

## 1. P0：密钥暴露，未关闭

服务器 `/home/ubuntu/.config/bstocks_v2/stage2.env` 权限是 `600`。文件里有四个变量：`BINANCE_API_KEY`、`BINANCE_API_SECRET`、`ALPACA_API_KEY_ID`、`ALPACA_API_SECRET_KEY`。值的长度分别是 64、64、26、44。内容没有打印，也没有写进代码。

用这四个当前值在本地工程、`docs`、对话记录、终端记录、临时目录和 Cursor 日志里做了原文检索，检索过程只输出命中次数。对话记录和工程里没有原文。临时目录里 4 份安装日志含有当时进程环境中的 Alpaca 两条，已替换成 `REDACTED`。币安两条不在那些安装日志里。替换后的复扫，加上 Cursor 日志，命中 0。用来读取服务器环境文件的临时脚本已经从日本机和本地删掉。

这只清掉了已找到的副本。服务器上的仍是原来那两对。币安和 Alpaca 都没有让当前密钥自行作废的接口，作废要在账户网页里用双重验证完成。本机没有这两个网站的登录会话。新值只放入上述权限 600 的环境文件，不要写入代码、Git、脚本、报告或对话。在作废完成之前，这项 P0 仍然开放。

## 2. P1：时点市值，已补上

口径：SEC 定期报告里已经公开的普通股股本，乘以当天 SIP 原始收盘价。`asof` 是收盘日。交易日 T 只用 `asof < T` 的最近一笔，和原来的 `pit_cap` 一致。披露日晚于 T 的股本不能让 T 过门槛。

使用的标签，按文件里实际有数的记录合并，再取披露日最新的一笔：

- `dei:EntityCommonStockSharesOutstanding`
- `us-gaap:CommonStockSharesOutstanding`
- `us-gaap:SharesOutstanding`
- `ifrs-full:NumberOfSharesOutstanding`

只收 10-K、10-K/A、10-Q、10-Q/A、20-F、20-F/A、40-F、40-F/A、6-K、6-K/A。股本低于 10 万股的记录丢掉，避免封面误填的个位数变成接近 0 的市值。披露日早于交易日超过 548 天的股本不再使用，避免 2010 年前后的旧封面去给 2024–2026 定价。除权日在计量日之后、收盘日之前（含收盘日）的拆股，股本乘 `new_rate / old_rate`，与当天原始价格同一单位。优先股股本、加权平均股本和“已发行但未扣库存股”没有拿来充数。

CIK 来自 SEC `company_tickers.json`。代码里的 `.` 会再试一次 `-`，所以 `BRK.B` 能对上 `BRK-B`。公司事实缓存在 `D:\Strategy\data_v2_2\reference\sec\`，没有下载整包 companyfacts.zip。双类别用“实体股本 × 该代码原始收盘价”，两类价格接近时，两类都可能显示成整家公司的量级，门槛只要求至少 50 亿美元，这个近似会偏大而不是偷看未来。ADR 的股本可能只是 ADR 那一行。没有对上 CIK、或没有上述标签的近期记录，保持 `market_cap_unverified`。

数量级抽查，交易日 `2025-11-03` 看到的前一日市值：NVDA 约 4.92 万亿美元，AAPL 约 4.00 万亿美元，MSFT 约 3.85 万亿美元。同一天低于 50 亿美元的只有 FMC 约 19 亿美元、SAIC 约 43 亿美元、MTDR 约 49 亿美元。`XOM` 在当前 CIK 下只有 `2026-08-03` 和 `2026-11-02` 两笔可用披露，股本约 41 亿股；这之前的交易日没有时点市值。

`pit_caps.csv`：368 个代码，183,218 行。SHA-256 `fa47a1fdad59dcbb76a5b64e1206625eda8f24d284ec9533bbe0944c73730c11`。没有可用序列的 46 个代码：

`META NYT BRK.B V MA VIRT QRVO DDOG NET OKTA TEAM WDAY APPF DBX ACN RDDT TTD EA CHTR CMCSA VOD FOXA ABNB DASH H NKE F LEN HOOD IBKR CME REGN AZN HEI MOG.A UPS HSY MKC EL TSN STZ TAP SPG EQR AVB MSTR`

成员表只改原来已经是 `market_cap_unverified` 的行。ETF 行和更早被别的门槛拒绝的行没有重算。5 分钟特征库没有重建。

| 原因 | 补之前 | 补之后 |
| --- | ---: | ---: |
| `ok` | 26,780 | 205,358 |
| `market_cap_unverified` | 200,174 | 20,632 |
| `market_cap_below` | 0 | 964 |
| 其余原因 | 13,546 | 13,546 |
| 合计 | 240,500 | 240,500 |

个股可研究会话 178,578。ETF 可研究会话仍是 26,780。合计可研究 205,358。416 只个股里，366 只至少有一个交易日进入可研究池，50 只一个交易日都没有。旧表已另存，SHA-256 与阶段 3 报告一致：

| 表 | 行数 | SHA-256 |
| --- | ---: | --- |
| `membership_before_caps.parquet` | 240,500 | `77182701c1a7164dd36fcb76f5c6f20456064ed566b34a3f630cbd35de1ac2e5` |
| `pit_before_caps.parquet` | 26,780 | `5e35365c3a1e733cb600cb4b03d5d5ff84de1c2f55f564846bd97b825a69000c` |
| `membership.parquet` | 240,500 | `963930f45f78f0b044230b4f0d7c9904a43e7f7ce5579753ac017d11bd2b5b4a` |
| `pit.parquet` | 205,358 | `0e69c65a6d36cada287b5ee9bbd07ecfc2f49e1f07f08d5153f40e1b7627af40` |

## 3. P1：币安与 SIP 重叠，未关闭

研究窗口止于 `2026-10-30`。股票行情 REST 只有 `exchangeInfo`、`quote`、`tokenized-assets`，没有历史 K 线。现货 K 线不是这批股票，没有调用。

`2026-11-03` 复核了日本机 `/home/ubuntu`、`/tmp`、`/var/tmp`。pytest 临时库以外没有 `intraday_bars_wal`。本地 `data_v2_2` 没有 SQLite 行情库。窗口已经结束，历史 K 线接口仍然不存在。

当天盘后，SIP 在美东 20:00 前有 `SPY` 成交，20:05 之后为 0 根。公开 5 分钟流在隔夜有更新，但没有已收盘根，也没有周一常规时段的币安录制可以对上。对齐根数仍是 0。日本机上的采集进程在等 `2026-11-03` 04:00 ET，收满 30 根后再写偏差。锁箱没有重跑。说明写在 `D:\Strategy\data_v2_2\reports\domain_shift.md`。

## 测试命令

```text
D:\Strategy\binance_stocks_v2_2\.venv\Scripts\python.exe -m ruff check src tests
D:\Strategy\binance_stocks_v2_2\.venv\Scripts\python.exe -m mypy src
D:\Strategy\binance_stocks_v2_2\.venv\Scripts\python.exe -m pytest -q --tb=line
```
