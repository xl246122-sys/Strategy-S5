# S5-P2 时段数据质量

```text
S5 Phase / Gate: S5-P2 / G-S5-2
基线 Git SHA / 本阶段 Git SHA:
  基线 main = e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee
  本阶段实现 feature/s5-eod-reversal = 024fb0abcefddb0450be2ce2d77c799b9397c7f6
  本报告在其后的单独提交里。分支没有合并。
实际修改的文件及行号: 无既有文件被修改。
实际新增的文件:
  src/bstocks_v2/research_data/s5_audit.py
  src/bstocks_v2/research_data/s5_recorder.py
  tests/s5/test_audit.py
  docs/s5/s5_02_data_quality_report.md
  派生数据（不进 Git）: data_v2_2/s5/coverage.json、symbol_coverage.json、manifest.json
原 S1-S4 回归测试（总数、通过、失败、跳过）:
  全量 pytest 211 passed，0 failed，0 skipped。S5 之前的 198 个都在这次里面。
S5 新测试（总数、通过、失败、跳过）:
  本阶段新增 4 个，连同 P1 的 9 个，S5 共 13 个，全部通过。
是否发生生产文件、进程、订单、资金变更（必须为否）: 否。没有写 SIP 原库，没有下单，没有改日本机进程配置，没有新开套接字。
API 官方契约核验与只读实测差异:
  本阶段没有请求币安。历史重叠根数仍是 0。20:00 ET 之后没有可配对的币安 K 线，不插值。
研究数据版本、数据范围、源和 session 覆盖率: 见下方矩阵。
P0/P1/P2 余项: 夜盘不能做历史可交易回测。盘后只有 OHLCV，没有买卖盘。币安重叠仍是 0。密钥轮换仍开放。G14 仍是 NO_GO。
本阶段 GATE: PASS
回滚命令与恢复验证:
  git switch feature/s5-eod-reversal
  git revert 024fb0abcefddb0450be2ce2d77c799b9397c7f6
  派生目录 data_v2_2/s5 可整目录删除，SIP 原库不受影响。
下阶段的前置条件: 投资人接受 G-S5-2。未接受不得开始 S5-P3。
投资人需要批准的具体变更: 无。本阶段不请求把影子记录器开到日本机上。
```

采集模式是 `prospective_forward`。历史库可以做常规时段 OHLCV 研究。它不能证明盘后成交，也不能证明夜盘成交。

## 哪部分能用

源是只读的 `D:\Alpaca_Research_DB\FRESH_2Y_5TF_20261101`。扫描了 1 分钟和 5 分钟全部 11,526 个分片、481 个代码。`feed` 只有 `sip`，`adjustment` 只有 `raw`。行数与阶段 3 一致：1 分钟 99,579,568，5 分钟 24,104,805。文件里没有 `bid` / `ask`。已有行的成交量没有 0；缺的时段是文件里没有这根 K 线，不是一根空 K 线。

| 1 分钟 | 库存标签 | 时钟标签 | 有成交的时钟根 |
| --- | ---: | ---: | ---: |
| 盘前 04:00–09:30 | pre 7,492,404 | PRE 7,492,404 | 7,492,404 |
| 常规 09:30–16:00 | rth 86,893,936 | RTH_CLOCK 86,925,824 | 86,925,824 |
| 盘后至 20:00 | post 5,193,228 | POST 5,160,532 | 5,160,532 |
| 20:00–04:00 | 含在 post 里 | OVERNIGHT 808 | 808 |

5 分钟同一关系：pre 3,191,499，rth 18,481,654，post 2,431,652；时钟 OVERNIGHT 420，且这 420 根都有成交。

库存 `rth` 比固定 09:30–16:00 时钟少 31,888 根 1 分钟线（5 分钟少 16,069 根）。差额等于库存 `post` 比“16:00–20:00 加夜盘”多出来的根数。那是半日市 13:00 之后被库存标成 `post`、被固定时钟算进下午的部分。研究口径以库存 `session` 和 XNYS 收盘为准。

| 用途 | 结论 |
| --- | --- |
| 常规时段 OHLCV 筛选 | 可用。1 分钟满网格 194,100 根的代码只有 31 个。5 分钟满网格 38,820 根的有 281 个。不满的是缺打印，没有补造。 |
| 盘前 / 盘后 OHLCV | 481 个代码都有一些 `post` 根。这是价格带，不是盘口，不能当成可成交回测。 |
| 夜盘 | 1 分钟 20:00 之后一共 808 根，分布在 259 个代码。222 个代码是 0。最多的 MSFT、BAC、CSCO、GE 也只有 60 根，两年合计约一小时，不是每个交易日的夜盘。5 分钟最多是 SPY 的 14 根。实验臂 C 必须前瞻采集。 |
| 币安对 SIP | 对齐根数 0。没有双边都有的 close / volume / VWAP / bid / ask 可以比。20:00 之后币安侧没有录到的根，标 `NO_SIP_REFERENCE` 的配对，不插值。 |

清单：

| 文件 | SHA-256 |
| --- | --- |
| `data_v2_2/s5/coverage.json` | `23d24626f433aaf709590752c66f3300e9d582cb716b2e1408ee3bac6fd27c25` |
| `data_v2_2/s5/symbol_coverage.json` | `75cf1607d14a9ccb4cb6f77e7621f14e4a12bcd85d32bb4b05dc007d4f1833dd` |

`manifest.json` 写于 `2026-11-03T03:04:04Z`，时区 `America/New_York`，`source_opened_for_write` 为 false。许可说明是 Alpaca SIP 历史线只读，没有合成币安 K 线。事件时点晚于决策时点的记录会被影子日志标成 `LEAKED_FUTURE`，不当成当时可见。

## 影子记录器

`ShadowRecorder` 只往调用方指定的 sqlite 追加。路径落在 SIP 原库里面会拒绝。它不连接 websocket，也不调用下单。焦点池上限 20 个代码，和现有 L3 报价上限一致，不另开 300 个代码的全量订阅。

测试覆盖了无买卖盘、休市、0 成交、重复事件、超过一根 5 分钟线的断档、决策时点之后的事件，以及夏令时两侧 10:00 美东仍是 RTH。这些是反例，不是实盘采集。

日本机仍是 2 vCPU、1.9 GiB 内存，扫描当时已用约 550 MiB、可用约 1.3 GiB，磁盘可用 40 GB。`overlap_capture`（PID 350312）还在等 `2026-11-03T09:00:00Z`，也就是美东 04:00，RSS 约 41 MB。本阶段没有向那台机器同步代码，也没有启动第二路订阅。

## 测试

```text
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q --tb=line
```

ruff 通过。mypy 158 个源文件通过。pytest **211 passed**，0 failed，0 skipped。

停在 G-S5-2。
