# S5-P6 只读影子跟踪

```text
S5 Phase / Gate: S5-P6 / G-S5-6
基线 Git SHA / 本阶段 Git SHA:
  基线 main = e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee
  上一阶段 feature/s5-eod-reversal = c393f680b0f84b7383ef3227896c8c50c4dfb4de
  本阶段文件还没有提交。工作区是脏的，SHA 仍是上面这一枚。
实际修改的文件及行号: 无既有文件被修改。
实际新增的文件:
  src/bstocks_v2/strategies/s5/observe.py
  src/bstocks_v2/research_data/s5_shadow.py
  tests/s5/test_shadow.py
  docs/s5/s5_06_shadow_report.md
  派生数据（不进 Git）: data_v2_2/s5/s5_06_summary.json、s5_06_observations.json、s5_06_manifest.json、s5_06_shadow.sqlite
原 S1-S4 回归测试（总数、通过、失败、跳过）:
  全量 pytest 274 passed，0 failed，0 skipped。其中阶段 12 的 15 个和阶段 13 的 9 个都在这次里面。S5 之前的 198 个也在这次里面。
S5 新测试（总数、通过、失败、跳过）:
  本阶段新增 15 个，连同此前的 61 个，S5 共 76 个，全部通过。
是否发生生产文件、进程、订单、资金变更（必须为否）: 否。没有下单，没有撤单，没有启动影子进程，没有改日本机，没有把 S5 放进仲裁器。
API 官方契约核验与只读实测差异:
  本阶段没有请求币安。SIP 只读了一个 5 分钟文件的 schema，没有加载行。列里没有 bid / ask。
研究数据版本、数据范围、源和 session 覆盖率: 见下方。实时只读样本是 0 个交易日。锁箱没有读。前瞻窗口没有打开。
P0/P1/P2 余项: 交易所成交为 0。主机 CPU、内存、磁盘和 WS 延迟没有测量。G14 仍是 NO_GO。密钥轮换仍开放。币安与 SIP 对齐根数仍是 0。
本阶段 GATE: PASS
观察状态: INCOMPLETE。策略结论仍是 NO_GO。collection_authorized = false。
回滚命令与恢复验证:
  删除本阶段新增的两个源文件、本测试、本报告，以及 data_v2_2/s5/s5_06_summary.json、s5_06_observations.json、s5_06_manifest.json、s5_06_shadow.sqlite。
  s5_05 四件、coverage.json、screen_summary.json、SIP 原库都没有写入。
下阶段的前置条件: 投资人接受 G-S5-6。实时样本仍是 0，20 个交易日的工程下限没有达到。未另行书面决定前不得开始 S5-P7，也不得把采集授权改成 true。
投资人需要批准的具体变更: 无。不请求实盘，不请求在日本机启动进程，不请求打开前瞻封存窗口。
```

投资人指令是进入 S5-P6。冻结的 G-S5-5 仍是 `NO_GO`，`shadow_authorized` 仍是 false。本阶段按手册的回退交付部署方案和本地回放，不改判，不启动进程。

## 冻结与回放

参数冻结沿用 P3 注册值，本阶段没有重拟合。冻结 SHA-256 是 `00f5a0c250b1c0fac2688fb64881b5ccf2a56f13a6d47b914300d1735c59f963`。简单收益门槛仍是 -0.03，残差 z 仍是 -2，计划净目标仍是 1.0%，计划止损仍是 1.5%，TTL 仍是 300 秒。

入口先核对两份已经封存的 P5 文件，哈希与 S5-P5 报告一致：

| 文件 | SHA-256 |
| --- | --- |
| `data_v2_2/s5/s5_05_assessment.json` | `8d549b7d43d1c378b03479ffad8e9a3a53f9d46c38d5f938a5feb175bde08a39` |
| `data_v2_2/s5/s5_05_ledger.json` | `dee12d90476e8ee6cd63754935f687c5e021cb8f5986800f4bafec81f7e9f231` |
| `data_v2_2/s5/coverage.json` | `23d24626f433aaf709590752c66f3300e9d582cb716b2e1408ee3bac6fd27c25` |
| `data_v2_2/s5/screen_summary.json` | `5cf2657931700d1ab58dc74b823e352e4788e850e0efc03a71f18fd899ad23a6` |

2026-11-03 又读了 `FRESH_2Y_5TF_20261101/parquet/5Min/SPY/SPY_5Min_2024-11.parquet` 的 schema。`rows_loaded` 为 0。列里没有 `bid`，没有 `ask`。正式回放因此是空带。空带的估计成交率、区间和交易所成交率都空着。

| 实验臂 | 信号 | 有报价 | 估计成交 | 交易所成交 | 估计成交率 | 区间 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| A_RTH_TO_POST | 0 | 0 | 0 | 0 | 空 | 空 |
| B_POST_TO_POST | 0 | 0 | 0 | 0 | 空 | 空 |
| C_POST_TO_OVERNIGHT | 0 | 0 | 0 | 0 | 空 | 空 |

实时只读会话数是 0，工程下限是 20。样本也低于路径模型的 30 笔门槛，区间不下发。夹具行即使凑满 20 个交易日，也不计入这个下限。

离线账本的有报价成交是 0，影子账本的交易所成交是 0。归因是 `NO_LIVE_TAPE`。两边都没有交易所成交，这不是一份成交对照。

清单写于 `2026-11-03T04:37:37Z`：

| 文件 | SHA-256 |
| --- | --- |
| `s5_06_summary.json` | `b688c050ce253d2f2f5bbdbeca51a5991e55c2bf854070ae2d7c0fcd8dbaf7cf` |
| `s5_06_observations.json` | `a5338d955b09046ec0b16f3a9625b7955c763aae07dc722e474e6078745f932f` |
| `s5_06_shadow.sqlite` | `f4f9afd2e6cf56daeee61f52e92b6ad7123822b7fd81c4eb04f9530fa1484391` |
| `s5_06_manifest.json` | `9d1f61e2a7527980431e6dd59da8975a26e700d7ce6163626490a7d2c7e30e21` |

观察文件是空列表。SQLite 只有 `s5_06_observation`。表约束要求 `books_profit = 0`，并且 `exchange_fill_id` 为空。库里没有 `intraday_bars_wal`。

## 记账规则

一条带要带决策时、交易所时、捕获时和评分时。交易所时晚于捕获时记 `CLOCK`。捕获时晚于决策时记 `NOT_YET_VISIBLE`。报价晚于 5 秒记 `STALE`。K 线开始早于决策、结束晚于评分、或结束不晚于决策，记 `LOOKAHEAD`，收盘价不写入路径。

没有 bid/ask、价差过宽、深度不足、队列未知，都保持未成交，种类是 `unobserved`。队列已知且低点穿透 0.01 时，状态可以是 `FILLED_ESTIMATE` 或 `PARTIAL_ESTIMATE`，种类是 `estimate`。估计数量留下余量。退出买价缺失或为 0 时路径是 `EXIT_FAILED`。可见的退出买价只记 `PATH_RECORDED`。两条路径的 `books_profit` 都是 false，账本里没有盈亏字段。

单元测试里的合成报价只证明这些规则。它们不是市场结果，也不计入 20 日下限。30 笔以上才给出 Wilson 区间，区间种类仍是 `estimate`。交易所成交率在任何样本上都空着。

`ShadowTracker.place` 与 `cancel` 调用研究锁，分别拒绝 `/sapi/v1/equity/order/place` 和 `/sapi/v1/equity/order/cancel`。`decide_launch` 在评估已经授权、主机也已测量时仍然返回 `start_process = false`，原因里有 `NO_UNATTENDED_SPAWN`。本次正式运行的原因还有 `G_S5_5_SHADOW_NOT_AUTHORIZED` 和 `HOST_UNMEASURED`。

最终评估次数没有消耗。前瞻封存窗口没有打开。

## 部署、停止、回滚

本次没有执行启动。主机峰值 CPU、可用内存、磁盘写入、WS 延迟和断流率都没有测量。

启动，留到另有书面决定并且冻结评估改判之后：

1. 冻结评估的 `shadow_authorized` 仍是 false 时，不启动 worker。
2. `python -m bstocks_v2.research_data.s5_shadow` 只在 `data_v2_2/s5` 下写回放文件。它不打开套接字，也不调用下单或撤单。
3. 即使评估改判，这个模块也不会自行拉起进程。

停止：没有进程号。不要向无关的 Python 进程发信号。

回滚：删除上面四份 `s5_06` 派生文件。留下 `s5_05` 文件、`coverage.json`、`screen_summary.json` 和 SIP 库。

## 测试

2026-11-03，本地 Python：

```text
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q --tb=line
.venv\Scripts\python.exe -m bstocks_v2.research_data.s5_shadow
```

ruff 通过。mypy 172 个源文件通过。pytest **274 passed**，0 failed，0 skipped。回放入口打印 `INCOMPLETE PASS NO_LIVE_TAPE`。

停在 G-S5-6。上面没有一笔交易所成交，也没有一个实时只读交易日。
