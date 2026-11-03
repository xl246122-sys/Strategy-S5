# S5-P7 独立 Go/No-Go 与冻结

```text
S5 Phase / Gate: S5-P7 / G-S5-7
基线 Git SHA / 本阶段 Git SHA:
  基线 main = e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee
  分支 HEAD = c393f680b0f84b7383ef3227896c8c50c4dfb4de
  本阶段文件还没有提交。工作区是脏的，SHA 仍是上面这一枚。
实际修改的文件及行号: 无既有文件被修改。
实际新增的文件:
  src/bstocks_v2/strategies/s5/decision.py
  src/bstocks_v2/research_data/s5_freeze.py
  tests/s5/test_decision.py
  docs/s5/s5_07_go_nogo.md
  派生数据（不进 Git）: data_v2_2/s5/s5_07_verdict.json、s5_07_whitelist.json、s5_07_manifest.json
原 S1-S4 回归测试（总数、通过、失败、跳过）:
  全量 pytest 284 passed，0 failed，0 skipped。其中阶段 12 的 15 个和阶段 13 的 9 个都在这次里面。S5 之前的 198 个也在这次里面。
S5 新测试（总数、通过、失败、跳过）:
  本阶段新增 10 个，连同此前的 76 个，S5 共 86 个，全部通过。
是否发生生产文件、进程、订单、资金变更（必须为否）: 否。没有下单，没有撤单，没有解锁高水位，没有打开阶段 15，没有改日本机。
API 官方契约核验与只读实测差异:
  本阶段没有请求币安。SIP 只读了一个 5 分钟文件的 schema，没有加载行。列里没有 bid / ask。
研究数据版本、数据范围、源和 session 覆盖率:
  沿用已封存的 S5-P5 与 S5-P6。实时只读样本是 0 个交易日。锁箱没有重跑。前瞻窗口没有打开。
P0/P1/P2 余项:
  三臂有报价成交为 0。盘后和夜盘没有可执行报价。影子工程下限 20 日没有达到。DSR 未估计。尾部 CVaR 未测量。
  G14 封存已核对，部署结论仍是 NO_GO。密钥轮换仍开放。币安与 SIP 对齐根数仍是 0。
本阶段 GATE: PASS
策略结论: 三臂都是 NO_GO。总状态 RESEARCH_ONLY。live_authorized = false。collection_authorized = false。
回滚命令与恢复验证:
  删除 decision.py、s5_freeze.py、tests/s5/test_decision.py、本报告，以及 data_v2_2/s5/s5_07_verdict.json、s5_07_whitelist.json、s5_07_manifest.json。
  s5_05、s5_06、coverage.json、screen_summary.json、SIP 原库和阶段 14 封存都留下。
下阶段的前置条件: 投资人接受 G-S5-7。本手册在这一阶段结束。未另行书面决定前，采集授权保持 false，实盘锁保持 false。
投资人需要批准的具体变更: 无。不请求实盘，不请求只读采集，不请求打开前瞻封存窗口，不请求进入阶段 15。
```

投资人指令是进入 S5-P7。这一阶段汇总已经封存的证据并作出独立裁定。研究代码已经写完。统计没有通过。影子没有通过。实盘授权没有授予。这四件事各自成立，后一件不从前面一件推导出来。

## 四层结果

| 层 | 结果 | 证据 |
| --- | --- | --- |
| 研究工程 | DONE | S5 包、资格门、研究账本和冻结入口都在。`s5_live_enabled()` 返回 false，`MODE` 是 `RESEARCH_ONLY`。 |
| 统计 | FAIL | 三臂已了结成交都是 0。净期望、DSR、±15% 邻域、滑点和出场都没有可引用的正结果。 |
| 影子 | INCOMPLETE | 实时只读交易日是 0，工程下限是 20。归因是 `NO_LIVE_TAPE`。交易所成交是 0。 |
| 实盘授权 | NOT_GRANTED | `live_trade_enabled`、`s4_live_enabled`、`s5_live_enabled` 都是 false。高水位没有解锁。阶段 15 没有打开。 |

单元测试里的合成样本可以走出另外两个标签，用来证明标签彼此独立。统计失败时，即便影子天数凑满 20，裁定仍是 `NO_GO`。统计通过而影子天数是 19 时，裁定是 `SHADOW_MORE`，`collection_authorized` 仍是 false。三臂都达到审查条件时，裁定可以是 `GO_FOR_REVIEW`，`live_authorized` 仍是 false。G14 仍是 `NO_GO` 时，实盘变更单是空的。这些合成数字不是市场结果。

当前封存带上的正式裁定如下。

| 实验臂 | 裁定 | 统计 | 影子 | 已了结 | 净期望 |
| --- | --- | --- | --- | ---: | --- |
| A_RTH_TO_POST | NO_GO | FAIL | INCOMPLETE | 0 | 未估计 |
| B_POST_TO_POST | NO_GO | FAIL | INCOMPLETE | 0 | 未估计 |
| C_POST_TO_OVERNIGHT | NO_GO | FAIL | INCOMPLETE | 0 | 未估计 |

三臂没有合成一个期望。正式入口在封存结论离开 `NO_GO` 时会拒绝落盘。历史屏上 A 有 13,020 条决策，B 有 12,954 条，C 有 12,961 条，全部未成交。空样本不记成期望 0，DSR 也不记成 0。`dsr_estimated` 是 false。CVaR 状态是 `UNMEASURED`。

费用加倍、点差加倍、穿透加倍、延迟、分段、跳空和零买价，仍是 S5-P5 封存里的记录。这次没有重扫行情，也没有改冻结参数。冻结 SHA-256 仍是 `00f5a0c250b1c0fac2688fb64881b5ccf2a56f13a6d47b914300d1735c59f963`。最终评估次数没有消耗。前瞻封存窗口没有打开。

因此不出具实盘启用变更单。`live_enablement_reason` 是 `STRATEGY_NO_GO`。

## G14 与账户锁

阶段 14 封存正文的文本 SHA-256 仍是 `82474de9f00ada7edd975bef5d103bb31db662fb5dcc7f37f9d5449e7332821e`，与 `stage14_lockbox_seal.json` 一致。S1、S2、S3、S4 的部署栏都是 `NO_GO`。`p0_p1_clear` 是 false。封存原文里的未清项仍是密钥轮换、时点市值和币安/SIP 重叠。后来的时点市值补丁没有改写这份封存，G14 也没有改成通过。

封存里的 `safety_audit.passed` 是 true。那是代码清单核对，不是部署通过。

本次读到的运行时锁：准入集合只有 S1–S4，`S5GuardianPolicy.governs()` 是 false，`guardian/machine.py` 没有引用 S5。8% 高水位保留比例是 0.92，同时持仓上限是 5，最小名义是 120 USDC。`PortfolioRisk.unlock` 没有被调用。

## 版本锁定与补丁白名单

冻结清单写于 `2026-11-03T05:03:24Z`。当时相对 main 的路径是 43 条，白名单外路径是 0 条。本报告在清单之后写成，不回写那份 JSON。

相对 main 被改过的公共文件只有三份。S1、S2、S3、S4、Guardian、LimitOptimizer、仲裁器和阶段 14 的路径都不在差异里。

| 文件 | 行 | 变更 |
| --- | --- | --- |
| `constitution/guard.py` | 25–26，32，121，188 | 增加默认关闭的 `S5_RESEARCH`。`s5_live_enabled` 固定 false。不传策略时仍走原来的 15:45 时钟。 |
| `contracts/models.py` | 101–108，199 | 研究字段有默认值。`research_only` 默认 false。旧的 S1–S4 载荷仍能通过校验。 |
| `portfolio_risk/authority.py` | 205，232，269，844 | `approve` 的时段参数默认 `GLOBAL`。`cancel_unfilled` 只由 S5 研究账本调用，部分成交不能消掉。 |

`S5_RESEARCH` 只在研究账本显式传入、并且 `research_only` 为 true、实盘锁为 false 时，才允许研究窗口里的买单。15:55 强平仍由全局 Guardian 执行。S5 的 `on_clock` 固定拒绝。

生产库没有迁移。`intraday.sqlite`、`guardian.sqlite`、`portfolio.sqlite`、`orders.sqlite` 都没有新表。研究侧的表是记录器的 `s5_shadow_event`、研究账本的 `s5_flag` / `s5_alert` / `s5_refusal` / `s5_order` / `s5_position`，以及 `s5_06_shadow.sqlite` 里的 `s5_06_observation`。回滚是删除研究文件，不对这些生产库做反向迁移。

封存输入在落盘前后各核了一遍，哈希没有变：

| 文件 | SHA-256 |
| --- | --- |
| `s5_05_assessment.json` | `8d549b7d43d1c378b03479ffad8e9a3a53f9d46c38d5f938a5feb175bde08a39` |
| `s5_05_ledger.json` | `dee12d90476e8ee6cd63754935f687c5e021cb8f5986800f4bafec81f7e9f231` |
| `s5_06_summary.json` | `b688c050ce253d2f2f5bbdbeca51a5991e55c2bf854070ae2d7c0fcd8dbaf7cf` |
| `s5_06_shadow.sqlite` | `f4f9afd2e6cf56daeee61f52e92b6ad7123822b7fd81c4eb04f9530fa1484391` |
| `coverage.json` | `23d24626f433aaf709590752c66f3300e9d582cb716b2e1408ee3bac6fd27c25` |
| `screen_summary.json` | `5cf2657931700d1ab58dc74b823e352e4788e850e0efc03a71f18fd899ad23a6` |

本阶段产出：

| 文件 | SHA-256 |
| --- | --- |
| `s5_07_verdict.json` | `805637f5bd2076f1ee8f24518882287c3a088313e1167b97b7474c31c94993b1` |
| `s5_07_whitelist.json` | `dd3dd234b23687733f9aa8dd96fb7d6631bc4e5edecae6af2a6aac22e5065a24` |
| `s5_07_manifest.json` | `1ff82a42e1b3bcb3347b8a8b1bbc0c1349abb2a34ddbd5e636aa7fb8747025c9` |

## 测试

2026-11-03，本地 Python：

```text
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q --tb=line
.venv\Scripts\python.exe -m bstocks_v2.research_data.s5_freeze
```

ruff 通过。mypy 174 个源文件通过。pytest **284 passed**，0 failed，0 skipped。冻结入口打印 `NO_GO PASS RESEARCH_ONLY`。

## 投资人可以另作的决定

这三项都没有在本阶段被授予。

1. 接受 `RESEARCH_ONLY`。S5 留在研究状态。V2.2 仍按它自己的阶段 15/16 路线由投资人另行决定，本裁定不替它申请，也不把它关掉。
2. 另行书面授权只读采集。那一次决定之前，`collection_authorized` 保持 false。
3. 另行书面授权实盘。那还要先通过既有阶段 15 的前置检查。本阶段不能把 `live_enabled` 设为 true，也不能解锁高水位。

停在 G-S5-7。三臂都是 `NO_GO`。
