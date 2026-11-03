# S5-P3 信号、新闻过滤和路径概率

```text
S5 Phase / Gate: S5-P3 / G-S5-3
基线 Git SHA / 本阶段 Git SHA:
  基线 main = e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee
  本阶段实现 feature/s5-eod-reversal = 6ac4aa978f9fad8083a6687ccc7bd5cbe425d6a0
  本报告在其后的单独提交里。分支没有合并。
实际修改的文件及行号:
  src/bstocks_v2/strategies/s5/__init__.py 第 4–13 行，导出研究入口
实际新增的文件:
  src/bstocks_v2/strategies/s5/spec.py
  src/bstocks_v2/strategies/s5/universe.py
  src/bstocks_v2/strategies/s5/event_gate.py
  src/bstocks_v2/strategies/s5/features.py
  src/bstocks_v2/strategies/s5/model.py
  src/bstocks_v2/strategies/s5/signals.py
  src/bstocks_v2/strategies/s5/diagnose.py
  src/bstocks_v2/research_data/s5_screen.py
  tests/s5/test_signals.py
  tests/s5/test_screen.py
  docs/s5/s5_03_model_research_report.md
原 S1-S4 回归测试（总数、通过、失败、跳过）:
  全量 pytest 223 passed，0 failed，0 skipped。S5 之前的 198 个都在这次里面。
S5 新测试（总数、通过、失败、跳过）:
  本阶段新增 12 个，连同 P1 的 9 个和 P2 的 4 个，S5 共 25 个，全部通过。
是否发生生产文件、进程、订单、资金变更（必须为否）: 否。没有下单，没有改仲裁器准入，没有改 15:45/15:55，没有碰日本机。
API 官方契约核验与只读实测差异:
  本阶段没有请求币安。历史屏没有买卖盘，也没有新闻时点。夜盘资格仍是未知，实验臂 C 全部 OVERNIGHT_UNSUPPORTED。
研究数据版本、数据范围、源和 session 覆盖率: 见下方。锁箱 2026-05-01 起没有读。
P0/P1/P2 余项: 可成交样本为 0，路径概率不足。市场状态快照没有接到这次历史屏上。G14 仍是 NO_GO。密钥轮换仍开放。
本阶段 GATE: PASS
回滚命令与恢复验证:
  git switch feature/s5-eod-reversal
  git revert 6ac4aa978f9fad8083a6687ccc7bd5cbe425d6a0
  派生文件 data_v2_2/s5/screen_summary.json 可删除。SIP 原库没有写入。
下阶段的前置条件: 投资人接受 G-S5-3。未接受不得开始 S5-P4。
投资人需要批准的具体变更: 无。本阶段不请求把 S5 放进仲裁器，也不请求影子采集。
```

研究入口是 `evaluate`。它只产出研究记录和可选的 `TradeIntent`。`routable` 固定为 false。`refuse_live_route` 仍会拒绝 `/sapi/v1/equity/order/place`。准入集合仍只有 S1–S4。

阈值在读训练集之前写死：简单收益 `<= -3%`，残差 z `<= -2`，beta 用决策日之前最多 60 个、至少 40 个已完成会话。净目标 1.0% / 1.5% / 2.0% 都记账，研究单的价格只用 1.0%。1.5% 止损是计划价，不是成交承诺。没有按某一只代码或近期赢家改过这些数。

## 四层筛选

每条记录同时留下统计屏和硬拒绝。硬拒绝把 `admitted_level` 清空，统计屏仍保留。

| 层 | 在上一层之上再要求 |
| --- | --- |
| B0 | 相对前一常规收盘的简单收益达到预注册跌幅 |
| B1 | 用决策前的市场、行业 beta 算出的残差 z 达到预注册极端值 |
| B2 | 窗口内卖压衰竭或修复、VWAP 偏离减轻、同时段成交量回落，并且买卖盘较先前收窄 |
| B3 | 训练好的路径概率给出正的计划净期望 |

硬拒绝始终包括：不在预注册的 35 只行业 ETF 里、窗口外、`UNKNOWN` / `HIGH_VOL_PANIC`、新闻不是当时可见的 `CLEAR`、没有买卖盘、价差过宽、深度过薄、仍在创新低、常规时段 K 线有缺口。没有新闻时点就记 `NEWS_UNKNOWN`，不当成“没有利空”。没有买卖盘就记 `SPREAD_UNKNOWN` 和 `DEPTH_UNKNOWN`，路径标签记 `UNFILLABLE`。这些行留在样本里。

实验臂 A 看收盘前 30 分钟到收盘前 6 分钟，B 看收盘后 5 分钟到 60 分钟，C 看收盘后 5 分钟到 19:30。半日市随 XNYS 收盘时间移动。三臂分开计数。C 还要求 `overnightSupported` 为真，未知则拒绝。

特征哈希只覆盖决策时点及之前的 K 线、先前会话和当时的新闻、报价。决策之后的价格不进入哈希。批处理和逐根推送的哈希一致。同一根 K 线里止盈和止损都穿过时记 `SL_FIRST`。低点只碰到限价、没有再穿过 1 美分，不算成交。

## 历史屏

源是只读的 `D:\Alpaca_Research_DB\FRESH_2Y_5TF_20261101`，5 分钟 SIP，训练段 2024-11-01 至 2025-10-31，验证段 2025-11-01 至 2026-04-30。宇宙是基金说明书映射里的 35 只行业 ETF。SPY 和行业 SPDR 只提供因子。每个代码、每个交易日、每个实验臂用窗口内最后一根已闭合 5 分钟线做一次决策。价格按该验证段结束前的拆股因子调整。清单 `data_v2_2/s5/screen_summary.json`，SHA-256 `5cf2657931700d1ab58dc74b823e352e4788e850e0efc03a71f18fd899ad23a6`，写于 `2026-11-03T03:34:56Z`。

这次没有接上阶段 5 的市场状态快照，所以 38,935 条全部带 `REGIME_NOT_ATTACHED`，准入数为 0。代码在真正拿到 `UNKNOWN` 或 `HIGH_VOL_PANIC` 时会另记 `REGIME_BLOCKED`。下表的 B0、B1 是价格屏，不是准入。

| 实验臂 | 决策 | 训练 / 验证 | B0 | B1 | B2 | B3 | 准入 | 路径 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A_RTH_TO_POST | 13,020 | 8,715 / 4,305 | 579 | 145 | 0 | 0 | 0 | 全部 UNFILLABLE |
| B_POST_TO_POST | 12,954 | 8,657 / 4,297 | 572 | 81 | 0 | 0 | 0 | 全部 UNFILLABLE |
| C_POST_TO_OVERNIGHT | 12,961 | 8,664 / 4,297 | 589 | 56 | 0 | 0 | 0 | 全部 UNFILLABLE |

新闻快照 0，买卖盘 0。因此每条都有 `NEWS_UNKNOWN`、`SPREAD_UNKNOWN`、`DEPTH_UNKNOWN`。B2 要有当时的买卖盘才能通过，所以历史 B2 是 0。实验臂 C 全部 `OVERNIGHT_UNSUPPORTED`。另有 10,910 条在决策时点对不上 SPY 或行业同步收盘，残差 z 留空，行仍保留。超过项目既有跳变阈值的决策是 0 条。A 臂有 2,641 条 `FALLING_KNIFE`，B 臂 243 条，C 臂 357 条。A 臂另有 2,984 条常规时段缺口。

## 概率

市场上没有一条带买卖盘的可执行路径，训练标签不足，模型种类是 `insufficient`。市场样本的 Brier 和 ECE 没有估计。四等分概率只是占位，不作为反弹概率，B3 不开放。

选择器本身在合成样本上按预注册规则工作，这些数字不是市场边缘：

| 合成验证 | 种类 | Brier | ECE |
| --- | --- | ---: | ---: |
| 类别不均衡、特征相同 | frequency | 0.8350340136054421768707483003 | 0.2380952380952380952380952388 |
| 四类可分 | logistic | 0.3498410198950730243022053085 | 0.4694419291419839846156037417 |

逻辑回归只在验证集 Brier 至少改善 0.01 时留下。上表第二行因此留下，但它的 ECE 并没有更好，也没有套到历史屏上。训练函数拒绝验证段和 2026-05-01 之后的锁箱日期。

## 测试

2026-11-03，本地 Python：

```text
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q --tb=line
```

ruff 通过。mypy 166 个源文件通过。pytest **223 passed**，0 failed，0 skipped。

泄漏反例在 `tests/s5/test_signals.py`：决策之后的 K 线、新闻、报价和“先前会话”会拒绝；不可执行的后续价格不改变特征哈希；触价不穿 1 美分记 `UNFILLABLE`；同柱止盈和止损记 `SL_FIRST`。

停在 G-S5-3。
