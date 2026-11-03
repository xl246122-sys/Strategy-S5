# S5-P5 历史筛选与 Go/No-Go 预评估

```text
S5 Phase / Gate: S5-P5 / G-S5-5
基线 Git SHA / 本阶段 Git SHA:
  基线 main = e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee
  上一阶段 feature/s5-eod-reversal = c393f680b0f84b7383ef3227896c8c50c4dfb4de
  本阶段文件还没有提交。评估当时工作区是脏的，SHA 仍是上面这一枚。
实际修改的文件及行号: 无既有文件被修改。
实际新增的文件:
  src/bstocks_v2/strategies/s5/assess.py
  src/bstocks_v2/research_data/s5_backtest.py
  tests/s5/test_assess.py
  docs/s5/s5_05_backtest_report.md
  派生数据（不进 Git）: data_v2_2/s5/s5_05_assessment.json、s5_05_experiment.json、s5_05_ledger.json、s5_05_manifest.json
原 S1-S4 回归测试（总数、通过、失败、跳过）:
  全量 pytest 259 passed，0 failed，0 skipped。其中阶段 12 的 15 个和阶段 13 的 9 个都在这次里面。S5 之前的 198 个也在这次里面。
S5 新测试（总数、通过、失败、跳过）:
  本阶段新增 12 个，连同此前的 49 个，S5 共 61 个，全部通过。
是否发生生产文件、进程、订单、资金变更（必须为否）: 否。没有下单，没有撤单，没有改日本机，没有把 S5 放进仲裁器。
API 官方契约核验与只读实测差异:
  本阶段没有请求币安。SIP 只读了一个 5 分钟文件的 schema，没有加载行。列里没有 bid / ask。
研究数据版本、数据范围、源和 session 覆盖率: 见下方。锁箱 2026-05-01 起没有读。
P0/P1/P2 余项: 可成交样本为 0。盘后和夜盘没有可执行报价。G14 仍是 NO_GO。密钥轮换仍开放。币安与 SIP 对齐根数仍是 0。
本阶段 GATE: PASS
策略结论: NO_GO / RESEARCH_ONLY。shadow_authorized = false。
回滚命令与恢复验证:
  删除本阶段新增的三个源文件、本报告，以及 data_v2_2/s5/s5_05_*.json。
  SIP 原库、screen_summary.json、coverage.json 都没有写入。
下阶段的前置条件: 投资人接受 G-S5-5。本结论没有允许 SHADOW ONLY，未另行书面改判前不得开始 S5-P6。
投资人需要批准的具体变更: 无。不请求实盘，不请求影子采集，不请求把前瞻封存窗口打开。
```

研究口径是常规时段候选筛选。历史库能给出价格屏，不能给出有报价证据的成交。本阶段没有输出夜盘可交易盈利。

## 数据核查

输入是两份已经封存的派生文件，哈希与 S5-P2、S5-P3 报告一致：

| 文件 | SHA-256 |
| --- | --- |
| `data_v2_2/s5/coverage.json` | `23d24626f433aaf709590752c66f3300e9d582cb716b2e1408ee3bac6fd27c25` |
| `data_v2_2/s5/screen_summary.json` | `5cf2657931700d1ab58dc74b823e352e4788e850e0efc03a71f18fd899ad23a6` |

2026-11-03 又读了 `FRESH_2Y_5TF_20261101/parquet/5Min/SPY/SPY_5Min_2024-11.parquet` 的 schema。`rows_loaded` 为 0。列是 OHLCV、`vw`、`session`、`feed`，没有 `bid`，没有 `ask`。1 分钟覆盖里的夜盘时钟根仍是 808，5 分钟仍是 420。评估模式记为 `rth_candidate_screen`。

屏上 38,935 条决策全部是 `UNFILLABLE`，`routable` 为 0，`lockbox_read` 为 false。报价数为 0，新闻快照数为 0。有报价证据的成交数是 0。净期望、Profit Factor、最大回撤、CVaR、bootstrap 区间都没有估计。空样本不记成期望 0。

## 消融

B0 到 B3 沿用预注册价格屏。B4 是送到研究账本的准入数。B5 是该实验臂的全部决策，三臂不相加。每一行的路径都是未成交，因为全体决策都是未成交，子集也是未成交。

| 实验臂 | 层 | 信号 | 成交 | 先止盈 | 先止损 | 超时 | 未成交 | 净期望 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A_RTH_TO_POST | B0 | 579 | 0 | 0 | 0 | 0 | 579 | 未估计 |
| A_RTH_TO_POST | B1 | 145 | 0 | 0 | 0 | 0 | 145 | 未估计 |
| A_RTH_TO_POST | B2 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| A_RTH_TO_POST | B3 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| A_RTH_TO_POST | B4 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| A_RTH_TO_POST | B5 | 13,020 | 0 | 0 | 0 | 0 | 13,020 | 未估计 |
| B_POST_TO_POST | B0 | 572 | 0 | 0 | 0 | 0 | 572 | 未估计 |
| B_POST_TO_POST | B1 | 81 | 0 | 0 | 0 | 0 | 81 | 未估计 |
| B_POST_TO_POST | B2 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| B_POST_TO_POST | B3 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| B_POST_TO_POST | B4 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| B_POST_TO_POST | B5 | 12,954 | 0 | 0 | 0 | 0 | 12,954 | 未估计 |
| C_POST_TO_OVERNIGHT | B0 | 589 | 0 | 0 | 0 | 0 | 589 | 未估计 |
| C_POST_TO_OVERNIGHT | B1 | 56 | 0 | 0 | 0 | 0 | 56 | 未估计 |
| C_POST_TO_OVERNIGHT | B2 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| C_POST_TO_OVERNIGHT | B3 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| C_POST_TO_OVERNIGHT | B4 | 0 | 0 | 0 | 0 | 0 | 0 | 未估计 |
| C_POST_TO_OVERNIGHT | B5 | 12,961 | 0 | 0 | 0 | 0 | 12,961 | 未估计 |

封存屏里的分段是：A 训练 8,715 / 验证 4,305，B 训练 8,657 / 验证 4,297，C 训练 8,664 / 验证 4,297。两段的成交都是 0。35 只预注册行业 ETF 各自成交 0。按行业的筛选计数没有落在屏摘要里，本阶段没有回读行情库再切行业。新闻快照为 0，`NEWS_UNKNOWN` 不记成非事件日，事件日和非事件日都空着。

样本下限沿用路径模型的 30 笔已了结交易。这里是 0，样本不足。

## 敏感性

费用加倍、点差加倍、穿透从 0.01 加到 0.02、决策后移 1 秒和 5 秒、训练/验证分段、事件簇留出、去掉一只代码，都作用在 0 笔成交上。结果仍是未估计，没有一条被采纳成新样本。

夜盘买价为 0、跳空或停牌这两项要求不得编造成交。历史样本上它们的账面盈亏是空的。

参数 ±15% 只写下邻域，没有重扫行情，也没有改冻结值：简单收益门槛仍是 -0.03（邻域 -0.0255 与 -0.0345），残差 z 仍是 -2（邻域 -1.70 与 -2.30），计划净目标仍是 1.0%（邻域 0.85% 与 1.15%），计划止损仍是 1.5%。

预注册宇宙里没有 INTC。去掉它少 0 笔，标题样本仍是全样本。

单元测试里的合成报价只证明记账规则：没有报价的撮合不入账；部分成交把剩余数量留在信号里；费用或延迟可以把薄边缘打掉；跳空不记成盈利；单名留出即使让剩余样本变正，也不替换全样本。这些数字不是市场结果。

## 锁箱与前瞻封存

2026-05-01 至 2026-10-30 已被阶段 14 跑过一次。封存正文 SHA-256 仍是 `82474de9f00ada7edd975bef5d103bb31db662fb5dcc7f37f9d5449e7332821e`。S5 的屏没有读这段。它也不能再叫做 S5 的未触碰测试集。

S5 已经用过的计数窗口是训练 2024-11-01 至 2025-10-31、验证 2025-11-01 至 2026-04-30。阈值没有在这两段上重拟合。

新的前瞻封存还没有打开。规则先写死：冻结现在这套筛选、训练和验证划分；冻结之后的第一个 XNYS 交易日才开始只追加的采集；最终评估只允许 1 次；评估前不得改参。本阶段没有采集，也没有消耗这一次评估。

## 判定

`NO_GO / RESEARCH_ONLY`。原因是没有可执行报价、成交样本不足、三臂净期望都未估计、滑点无法测量、出场在无买价的带上不能证实，以及上面的费用、延迟和分段应力都给不出正期望。安全项没有把跳空或零买价记成盈利。

账本 `data_v2_2/s5/s5_05_ledger.json` 的种类是 `count_ledger`，`quote_backed_fills` 是空数组。清单写于 `2026-11-03T04:20:57Z`：

| 文件 | SHA-256 |
| --- | --- |
| `s5_05_assessment.json` | `8d549b7d43d1c378b03479ffad8e9a3a53f9d46c38d5f938a5feb175bde08a39` |
| `s5_05_experiment.json` | `92f61c63c8d9c169bec91d0094a3b004cec73790db371710672e14594b6f314d` |
| `s5_05_ledger.json` | `dee12d90476e8ee6cd63754935f687c5e021cb8f5986800f4bafec81f7e9f231` |

2026-11-03，本地 Python：

```text
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q --tb=line
.venv\Scripts\python.exe -m bstocks_v2.research_data.s5_backtest
```

ruff 通过。mypy 170 个源文件通过。pytest **259 passed**，0 failed，0 skipped。评估入口打印 `NO_GO PASS`。

停在 G-S5-5。上面没有一笔交易所成交。
