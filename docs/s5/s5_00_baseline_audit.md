# S5-P0 基线审计

```text
S5 Phase / Gate: S5-P0 / G-S5-0
基线 Git SHA / 本阶段 Git SHA: 无。D:\Strategy 与 D:\Strategy\binance_stocks_v2_2 都不是 Git 仓库。
实际修改的文件及行号: 无既有文件被修改。
实际新增的文件: docs/s5/s5_00_baseline_audit.md（本报告）
原 S1-S4 回归测试（总数、通过、失败、跳过）: 本阶段没有重跑全量。最近一次全量记录是 2026-11-03，pytest 189 passed、0 failed。见下方测试基线。
S5 新测试（总数、通过、失败、跳过）: 0。S5 包尚未建立。本阶段只复跑了与数据缺口直接相关的 9 个既有只读测试，9 passed。
是否发生生产文件、进程、订单、资金变更（必须为否）: 否。没有下单、撤单、转账、重启或部署。日本机上已有的 overlap_capture 进程未被碰。
API 官方契约核验与只读实测差异: 见第 4 节。overnightSupported 等资格值没有留在本地快照里，标的级 24H 资格为未验证。
研究数据版本、数据范围、源和 session 覆盖率: Alpaca SIP FRESH_2Y_5TF_20261101，约 2024-11-01 至 2026-10-30。四只高流动性标的有 pre/rth/post，没有可回测的连续夜盘。币安与 SIP 对齐根数仍是 0。
P0/P1/P2 余项: 见第 6 节。
本阶段 GATE: PASS（审计本身完成）。G14 不是 PASS。S5 不是 GO。
回滚命令与恢复验证: 删除 docs/s5/s5_00_baseline_audit.md。没有代码、配置、数据库或进程可回滚。
下阶段的前置条件: 投资人书面批准 G-S5-0，并单独决定是否初始化 Git、建立 feature/s5-eod-reversal。未批准不得进入 S5-P1。
投资人需要批准的具体变更: 见第 8 节。本阶段不请求实盘，也不请求改 15:45/15:55。
```

审计日期：2026-11-03。范围只读。研发根目录是 `D:\Strategy\binance_stocks_v2_2`，包名 `bstocks_v2`，版本 `2.2.0`（`pyproject.toml`）。历史库在 `D:\Strategy\data_v2_2`，SIP 原库在 `D:\Alpaca_Research_DB\FRESH_2Y_5TF_20261101`。日本机 `43.163.203.127`，用户 `ubuntu`。

## 1. G14 真实进度

G14 **没有通过**。进入阶段 14 只说明锁箱跑过一次，不表示可以部署。

依据 `docs/stage_14_go_nogo_report.md`：

- S1、S2、S3、S4 的部署结论都是 `NO_GO / RESEARCH_ONLY`。
- 锁箱已封存，`stage14_lockbox_seal.json` 的正文 SHA-256 是 `82474de9f00ada7edd975bef5d103bb31db662fb5dcc7f37f9d5449e7332821e`。第二次 `execute()` 会拒绝重跑。
- `p0_p1_clear` 为 false。阶段 3 的密钥轮换仍是 P0。币安与 SIP 重叠仍是 P1，对齐根数 0（`data_v2_2/reports/domain_shift.md`，状态 `insufficient_overlap`）。
- 时点市值已在 `docs/stage_03_p0_p1_remediation.md` 补上，该补项写明 **G14 不因此改为通过**。
- `live_trade_enabled` 与 S4 `s4_live_enabled` 都是代码里的 false，不是环境开关。
- 原 Lockbox（2026-05-01 至 2026-10-30）已被阶段 14 使用。它不能再被叫作 S5 的未触碰测试集。

## 2. 代码锚点

策略没有插件注册表。准入集合是硬编码的。

| 对象 | 路径 | 行号 | 现状 |
| --- | --- | --- | --- |
| S1 信号 | `strategies/s1/signal.py` `decide` | 32，`strategy_id="S1"` 在 111 | 独立包 |
| S2 信号 | `strategies/s2/signal.py` `decide` | 86，`strategy_id="S2"` 在 140 | 独立包 |
| S3 信号 | `strategies/s3/signal.py` `decide` | 55，`strategy_id="S3"` 在 134 | 独立包 |
| S4 信号与实盘锁 | `strategies/s4/signal.py`；`strategies/s4/lock.py` | 信号 70，id 在 151；`LIVE_ENABLED = False` 在 lock.py 第 8 行 | 双重锁定 |
| 准入集合 | `signal_arbitrator/spec.py` `ADMITTED` | 13 | 只有 `S1`–`S4` |
| SignalArbitrator | `signal_arbitrator/stage10.py` | 类 131；`UNKNOWN`/`HIGH_VOL_PANIC` 在 253–254；15:45 在 283–294 | 超时即 `PAST_ENTRY_CUTOFF_TIME`，盘前即 `NON_RTH` |
| TradeIntent | `contracts/models.py` | 65–113 | `extra="forbid"`，金额是 Decimal。现有字段没有 session、research_arm、news |
| TradingSession | `contracts/models.py` | 40–43 | `RTH` / `EXTENDED` / `24H` |
| ConstitutionGuard | `constitution/guard.py` | 类 54；`live_trade_enabled()` 48–51 固定返回 False | 配置不能改 `_IMMUTABLE`（25–36） |
| 15:45 停开仓 | `constitution/guard.py` `entry_window_open` | 191–200 | 半日市 12:45。周末直接关。开仓违反则 `NON_RTH`（174–175） |
| 15:55 强平 | `constitution/guard.py` `flatten_deadline` | 203–206 | 半日市 12:55。Guardian `on_clock` 在 `guardian/machine.py` 286–287 调用 `_flatten_all` |
| 8% HWM | `constitution/guard.py` | `HWM_RETAINED = 0.92` 第 22 行；回撤 0.08 在第 30 行 | 账户层再判一次，`portfolio_risk/authority.py` 421、481 |
| 钱包 / tokenize | `constitution/guard.py` | 34–35，139–142 | `CARD`，`tokenize` 必须 false。mint/redeem 在 38–39 被禁 |
| 分数股 | `research_backtest/sizing.py`；`limit_optimizer/stage11.py` | 60；290–297 | `486442` 向下取整。整数股安全模式默认开 |
| PDT | `portfolio_risk/spec.py` | 12–16 | 5 个交易日、最多 3 次，代码 `486440` |
| 单笔风险 / 单仓 | `portfolio_risk/spec.py` | 7–9 | 规划净值 1600；风险 0.25%；单仓 20%；最低名义 120 来自章程 |
| PortfolioRisk | `portfolio_risk/authority.py` | 类 131 | 单一账户权威。Guardian 减仓不因 halt 被挡（约 320 行） |
| LimitOptimizer | `limit_optimizer/stage11.py` | 类 104 | 只产出 CARD 限价。对象上没有真实 place |
| WAL | `market_ingestor/wal.py` | 表 `intraday_bars_wal` 在 55–68 | 5 分钟柱。恢复函数从 182 行起，注释写明重建一个 RTH 会话 |
| 日历 | `research_data/calendar.py` | 8–11，35–67 | `America/New_York`，XNYS。标签是 `RTH` / `EXTENDED` / `OVERNIGHT` / `NON_SESSION` / `MIXED`。04:00–20:00 里非 RTH 都叫 `EXTENDED`，没有把 PRE 和 POST 拆开 |
| 回测入口 | `stage14/execute.py` | 文件头第 1 行 | 一次锁箱。封存后拒绝再跑。重放强平在 `research_backtest/replay.py` 176–192 |
| Guardian 持仓 | `guardian/journal.py` `Position` | 150–173 | 没有 `strategy_id`，没有跨收盘策略。主键是 symbol |

`Position` 与 `entry_window_open` 都是全局的。S5 若要跨过 16:00，不能改这两处的默认行为，只能在旁边加策略级策略，且默认仍是 fail closed。

## 3. 测试、依赖、回滚

最近一次**全量**记录写在 `docs/stage_03_p0_p1_remediation.md`（2026-11-03）：

```text
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q --tb=line
```

结果：ruff 通过，mypy 148 个源文件通过，pytest **189 passed**。该 189 等于阶段 14 报告的 185，再加上 `tests/test_pit_caps.py` 的 4 个。

当前 `tests/` 有 19 个文件、198 个 `test_` 函数。多出来的 9 个是 `test_tape_audit.py`（3）和 `test_overlap_capture.py`（6）。本阶段只跑了这两个文件：

```text
pytest tests/test_overlap_capture.py tests/test_tape_audit.py -q --tb=line
```

结果：**9 passed，0 failed，0 skipped**。没有网络，没有读密钥。全量 198 没有在本阶段重跑，因此不能把 198 写成新的全绿记录。

依赖：`pyproject.toml` 只有范围约束（Python `>=3.11,<3.13`，pydantic、tzdata、websockets；研究可选 pyarrow、exchange-calendars、backtrader）。仓库里没有 `uv.lock` 或 `requirements.txt`。

回滚：没有 commit 可复位。本阶段的回滚就是删掉本报告。以后要做 S5-P1 的分支，必须先有投资人批准的 Git 初始化；不能把“无 SHA”说成已冻结基线。抽查的两份文件，本地与日本机 SHA-256 一致：

- `research_data/overlap_capture.py`：`9ed6d668cdc91ef0023a432785ed83d92e01896905781a2fa2019477b3e5bcdc`
- `constitution/guard.py`：`c9bd685a078b23e96a7f661d2c56975b25a19a6298e0740cb73351269e84be5e`

这只说明这两个文件目前相同，不是整树已冻结。

## 4. 数据与接口

### 4.1 SIP 原库（只读实测）

目录只有 `config`、`corporate_actions`、`logs`、`manifests`、`parquet`、`reports`。`parquet` 只有 `1Min`、`5Min`、`1Hour`、`1Day`、`1Week`。没有报价或深度目录。K 线列有 `o/h/l/c/v/vw/n`、`session`、`feed`，没有 bid/ask。

`feed` 在抽查的 SPY 上全部是 `sip`。阶段 3 已记录全库约：1Min 99,579,568 行，5Min 24,104,805 行，起点 `2024-11-01T08:00:00Z`。特征层 `data_v2_2/features/5m` 是 RTH，约 762 MB，不能当盘后带。

本阶段读了 SPY、QQQ、AAPL、INTC 的全部分月文件，只取 `t/session/v`。四只的 RTH 根数与 XNYS 网格一致：1 分钟 194,100（495 个完整日 × 390 + 5 个半日 × 210），5 分钟 38,820（495 × 78 + 5 × 42）。存储的 `session` 只有 `pre`、`rth`、`post`。

| 标的 | 1Min pre | 1Min post | 1Min 20:00 后有量的根 | 5Min post |
| --- | ---: | ---: | ---: | ---: |
| SPY | 138,605 | 99,957 | 15 | 23,772 |
| QQQ | 153,675 | 102,466 | 14 | 23,779 |
| AAPL | 108,996 | 81,314 | 7 | 23,223 |
| INTC | 142,263 | 106,989 | 18 | 23,961 |

20:00 ET 之后的根仍被标成 `post`，不是独立夜盘，而且两年里只有个位数到十几根。INTC、AAPL、QQQ 的这些根集中在 `2026-10-30` 23:00 之后。这不是 20:00–04:00 的连续可交易带。

因此：

- 实验臂 A/B 有盘后 OHLCV，可以做**候选筛选研究**。
- 同一批文件不能证明盘后成交、排队或止盈。阶段 14 的报价替代（收盘价 ±0.01，笼子 0.80/1.20）仍然是替代，不是盘口。
- 实验臂 C 没有历史夜盘带。不能用 RTH 或 POST 的 K 线宣布 24H 收益。
- 全市场 11,526 个分片没有在本阶段逐片重数。上面四只是高流动性样本，不是全库比率。

本地 `data_v2_2` 没有 SQLite 行情库。日本机 `/home/ubuntu` 下的 sqlite 都在 pytest 临时目录，不是生产 WAL。

### 4.2 币安重叠（未验证的部分保持未验证）

`domain_shift.md`：对齐根数 0。股票 REST 没有历史 K 线。现货 `/api/v3/klines` 不能当作股票 K 线，章程也会拒绝 `/api/v3/`。

日本机进程：`python -m bstocks_v2.research_data.overlap_capture`，PID 350312，启动 `2026-11-03 10:05:58`（服务器本地时间，日本时区）。日志只有 20 行。早先一次启动因当时代码 `import exchange_calendars` 失败；当前磁盘文件已与本地哈希一致，当前代码不在那一行导入。当前日志是 `overlap_waiting 2026-11-03T09:00:00Z`，即美东 `2026-11-03 04:00`。进程在等 SIP 窗口，**还没有写下重叠样本**。本审计没有重启它。

`overlap_capture.py` 只在 04:00–20:00 ET 配对，并且自己写明 SIP 在 20:00 之后没有打印。即便这轮采集成功，也补不上夜盘对照。

### 4.3 资格字段与 bStocks

`data_v2_2/reference/exchange_slim.json` 声明过这些键：`extendedSession`、`fractionable`、`fractionableEh`、`overnightSupported`、`stepSize`、`minNotional`、`multiplierUp/Down`、`tradability`。7,957 行实际只留下 `symbol`、`tradability`、`listingTime`、`delistingTime`。`overnightSupported` 和 `extendedSession` 的值不在快照里。`BUY_SELL` 7,844，`OFFMARKET` 98，`NONE` 15。标的级 24H / 延长时段资格：**未验证**。代码里没有解析这些字段（全库检索无 `overnightSupported`）。

订单枚举已有 `EXTENDED` 和 `24H`，但开仓时钟不接受 RTH 以外的买单。枚举存在不等于允许盘后开仓。

Funding 只读快照（阶段 3，未在本阶段重打）含 `EQ_AMDL`、`EQ_BRKU`、`EQ_INTC`、`EQ_SPCX`，没有 USDC。`EQ_` 是钱包符号，账本会去掉前缀。研究价格来自 SIP 与 equity 行情，不是 bStocks 代币价格。`tokenize` 在章程、网关和优化器里被固定为 false。S5 不得把 `EQ_INTC` 持仓或 bStocks 报价当成 equity `tokenize=false` 的可成交样本。这一点是代码边界，不是本阶段新的只读实测。

官方文档链接以开发手册第 10 节为准（2026-11-03 编制时核对）。本阶段没有再请求 `exchangeInfo`，所以标的资格仍标为未验证，而不是“文档上有字段所以为真”。

## 5. 服务器资源（只读）

日本机：2 vCPU，内存 1.9 GiB（可用约 1.3 GiB），磁盘 `/` 49G 中已用 6.5G、可用 40G。代码树 136 MB，`/home/ubuntu/data_v2_2` 908 KB。环境文件权限 `600`，本阶段没有打开它。

这台机器就是手册里要先测量的 2 vCPU / 2 GB 级别。上面已经有一个在等窗口的采集进程。S5 录音不能再复制一套全量订阅。本地 D 盘可用约 614 GB，C 盘可用约 500 GB。

## 6. 风险

**P0**

- 没有 Git。无法冻结 SHA，也无法开 `feature/s5-eod-reversal`，直到投资人批准建库。
- 密钥轮换仍未完成（既有 P0）。S5 不得读取或打印环境文件。
- 没有连续历史夜盘。实验臂 C 不能做历史可交易回测。
- 盘后只有 OHLCV，没有历史 bid/ask/深度。不能把 POST 收益写成可成交净收益。
- 标的级 `overnightSupported` / `extendedSession` 值未留存。未验证必须 fail closed。
- 15:45 与 15:55 是全局的，Guardian 持仓没有策略标签。改错一处就会让 S1–S4 跨过收盘。

**P1**

- G14 未通过，重叠样本仍是 0。采集进程在等 2026-11-03 04:00 ET，尚未产出。
- `TradeIntent` 拒绝未知字段。会话与新闻字段必须是可选、有默认值的扩展，否则 S1–S4 反序列化会断。
- 日历三套词表不一致：Parquet 是 `pre/rth/post`，研究日历是 `RTH/EXTENDED/OVERNIGHT`，订单是 `RTH/EXTENDED/24H`。
- 服务器内存余量小。S5 影子进程要单独论证，不能默认开启。
- 2026-05 至 2026-10 锁箱已被 V2.2 使用。

**P2**

- 没有依赖锁文件。
- 本阶段没有重扫全部 11,526 个 SIP 分片的 session。
- 全量 pytest / mypy / ruff 没有在本阶段重跑。

## 7. 以后若批准，最小触及面

现在不改这些文件。下列只是下一阶段的白名单草稿：

- 新增 `strategies/s5/`、`tests/s5/`、`data_v2_2/s5/`、`docs/s5/`。
- `signal_arbitrator/spec.py` 的 `ADMITTED` 增加一个 id，默认仍不放行实盘。
- `contracts/models.py` 只加可选字段，默认值必须让旧意图继续合法。
- 新的 `PositionPolicy` / `S5GuardianPolicy` 接口，先不接入 `Guardian.on_clock`。
- **不要**改 `entry_window_open`、`flatten_deadline`、`HWM_RETAINED`、五仓、0.25% 风险、120 USDC、`live_trade_enabled`。
- 不改 S1–S4 的信号、参数和锁箱封存文件。

## 8. 请投资人决定

1. 接受本审计：G14 仍是 NO_GO，S5 只作为旁边的研究，不进入阶段 15。
2. 是否批准初始化 Git 并记录基线。未批准则 S5-P1 无法按手册开分支。
3. 接受实验臂 C 在现有历史上没有夜盘带；A/B 只有 OHLCV，没有盘口成交证据。
4. 不批准实盘、不批准关掉 15:55、不批准读取或轮换密钥以外的凭证操作。密钥轮换仍按阶段 3 补项，由投资人在交易所页面作废，不由本阶段代做。

停在 G-S5-0。
