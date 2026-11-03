# 阶段 1 验收报告

```text
[阶段 1]：V2.2 隔离工程骨架、契约归一化层与风险宪法固化
结论：PASS
1. 修改文件：新建 D:\Strategy\binance_stocks_v2_2\ 与服务器 /home/ubuntu/binance_stocks_v2_2\。无 Git 仓库，因此没有 commit SHA。根目录 .gitignore 增加了虚拟环境、缓存和 API Key 文件名。密钥文件没有被复制进工程。
2. 测试证据：2026-11-02 13:30 左右（Asia/Shanghai）。本地 Python 3.11.5 与日本机 Python 3.12.16 均为 ruff 全过、mypy 全过、pytest 15 passed。语句覆盖率 89%。
3. 接口与数据实证：本阶段没有发起任何币安或 Alpaca 网络请求，也没有读取密钥内容。授权路径只确认文件存在。
4. 模型与执行表现：本阶段没有策略回测。
5. 风控不变量核查：见下方测试清单。live_trade_enabled 在代码里固定返回 false。
6. 风险台账：无新的 P0。P2：部分异常分支尚未覆盖，不影响本阶段门槛。
7. G1：满足。部分成交 tc-FN 陷阱和宪法禁令都有失败用例。
8. 对当前生产系统的影响：日本机原来没有交易进程。新增的是独立目录、uv 和 Python 3.12 虚拟环境。腾讯云代理和系统 Python 3.14 未替换。
9. 下一阶段建议：阶段 2 只做只读行情、listenKey 和三维对账，仍然不调用下单接口。
停在当前阶段，等待投资人明确批准。
```

## 工程位置

- 本地：`D:\Strategy\binance_stocks_v2_2`，虚拟环境 Python 3.11.5
- 日本：`/home/ubuntu/binance_stocks_v2_2`，`uv` 安装的 CPython 3.12.16
- 包名：`bstocks_v2`
- 策略、行情、Guardian、回测等目录已建好，里面还没有实盘逻辑

## 测试命令与结果

本地：

```text
ruff check .            All checks passed
mypy src                Success: no issues found in 28 source files
pytest --cov=bstocks_v2 15 passed，覆盖率 89%
```

日本机同一套命令，Python 3.12.16，同样是 15 passed、ruff 通过、mypy 通过、覆盖率 89%。

官方部分成交样例 `Q=10, p=185.5, fq=5, FN=927.5, tc=1856.2`：

- `tc - FN` 等于 928.7。归一化器给出的已实现佣金是 0，不是 928.7。
- 整单预留费另记为 `tc - Q * p = 1.2`，不拿来扣净值。
- 1,600 USDC 扣掉已实现佣金后仍是 1,600，8% 锁线是 1,472，不会误锁。
- 若错误地扣掉 928.7，净值变成 671.3，测试证明那会触发锁。
- 全部成交后，`tc - FN` 只作为终态初值；REST `fee` 一旦给出，就改以 `fee` 为准。

宪法反例均失败于预期原因：外部配置打开实盘或把仓位上限改成 50、下单和撤单、mint/redeem、合约与现货加密接口、非法 `clientOrderId`、三位小数价格、float 价格、`BUY MARKET`、限价单携带 `notional`、`tokenize=true`、无持仓卖出、周日和 15:45 ET 开仓、100 USDC 名义金额、5 个槽位已满、净值正好回撤 8%、摩擦占比超过 20%。

## 密钥

两份授权文件都在 `D:\Key Store` 下，没有抄进仓库，也没有用于联网。阶段 2 需要时再从原路径读取。

## 仍未做的事

钱包对账、WebSocket、日内 K 线 WAL、真实下单都不在本阶段。`portfolio_risk` 目录还是占位；5 仓、120 USDC 和 8% 开仓锁已经由 `ConstitutionGuard` 强制执行，配置文件改不掉。
