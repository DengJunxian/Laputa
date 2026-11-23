# 阶段 02 联合证券与实体经济

基线 `2efd7749ae515e3603abfceba45378a100ff6c3f`。验收例固定 root 7、5 期、8 户、2 企业、2 银行、财政、单消费品和单证券 `security:A`。数据来源为 synthetic。两组配对共用冲击且可见的实验臂。初态与第 1 期业务状态在三臂相同。

| 同根配置 | 第 2 期合成标价 | 第 3 期获授权财富观察的家庭 | 第 3 期 firm:00 销量 / 销售收入 | 第 3 期总消费 | 第 4 期财政转移 | 第 5 期 household:0001 的工资支付方 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 冲击、可见 | 10 | 3 | 10 / 110 | 10 | 2 | firm:01 |
| 无冲击、可见 | 100 | 3 | 16 / 176 | 16 | 0 | firm:00 |
| 冲击、隐藏 | 10 | 0 | 16 / 176 | 16 | 0 | firm:00 |

金额采用最小货币单位，商品按件，两个支付方均支付 12。销量低于 13 时原有劳动规则不开放换岗，前期消费低于 13 时财政按既有预算支付。第 3 期银行记录 12 的贷款资产与家庭贷款负债，以及同额的存款负债与家庭存款资产。

## 时序与对账

- 四户在期初各配置一单位证券。第 2 期按既有撮合进行一次预付会话，`firm:01` 用自己账户中的现金买入一单位。正常验收例的成交价为 100，未成交卖单到期。现金不足的测试保留拒绝回执、零成交及空收盘价。会话清算先检查冻结资金及可用证券；现金、单位和当期预算随成交在经济簿更新，未成交的预占随会话结束释放。卖方到账不可资助同批商品指令。
- 标价事件作为同一 journal 的无分录记录提交。第 3 期有权限的剩余持有人看到每单位 90 的市值下降；其策略按同额增加现金保留目标，商品支出仍由预算、报价和现货决定。观测事实链接标价交易 ID。证券配置的开仓成本、处置成本及价款有独立科目，家庭现金与商品库存均可从同一 journal 复算。
- 两个参与者格式 1、2 继续读取，联合经济采用 `economic_runtime` 参与者格式 3，世界合同保持 v4。命令回执按原时钟核对，成交具有买卖双方回执和共同交易 ID。故障注入在第 2 期写入提交标记之前回滚标价、成交和工资等全部状态。改写 checkpoint 的证券数量会被 journal 重建验证拒绝。
- 第 4 期财政事件按期引用第 3 期已结算消费的 journal ID，第 5 期劳动记录引用工资分录。三臂均从第 2 期 checkpoint 续跑，独立重执行匹配全部 5 期，外部模型调用为 0。效果是这个冻结合成世界内的行为差异。

## 复现

从仓库根运行。每次使用新目录。

```sh
mkdir -p build/convergence/02
out=$(mktemp -d "$(pwd)/build/convergence/02/acceptance.XXXXXX")
fixture=examples/closed-economy-security-feedback.json
cp "$fixture" "$out/shock.json"
jq '.security_feedback.shock_enabled = false' "$fixture" > "$out/flat.json"
jq '.security_feedback.wealth_observed = false' "$fixture" > "$out/hidden.json"
for arm in shock flat hidden; do
  .venv/bin/ecosim run --config "$out/$arm.json" --output "$out/$arm" --until 2
  .venv/bin/ecosim resume "$out/$arm"
  .venv/bin/ecosim replay "$out/$arm" --output "$out/$arm-replay"
done
.venv/bin/python -m pytest -q tests/test_joint_world.py tests/test_wp10_security.py \
  tests/test_wp10_demand.py tests/test_closed_economy.py tests/test_economic_book.py \
  tests/test_economic_runner.py tests/test_v4_runtime.py tests/test_banking.py \
  tests/test_fiscal_policy.py tests/test_wp10_catalog.py
```

阶段 02 复用本仓 `MatchingVenue.plan/match/cancel` 及旧预付会话的订单与回执合同。现有撮合可以直接产出同一经济簿需要的成交，因此外部撮合框架、额外调度器和新增依赖均留在范围外。此切片只包含一次现金预付会话、外生合成标价和规则决策。后续阶段负责通用异质策略、PPT 机制消融、跨根实验和工作台增量。

## 验收记录

- 基线红测经过原证券/机构互斥断言，随后在未注册的 `security_feedback` 字段处失败。最终回归为 124 通过、0 失败、0 跳过，另有 72 个子测试通过，墙钟 125.83 秒。覆盖原证券专场、C/D、银行、财政及 v1/v3 golden hash。参与者版本注册后的未知版本测试使用 4，版本 3 经过合法读取和重建。
- 三臂初始状态 hash 为 `7a37d7f579bb30ddcf004b29d4ee8d9379b5f151dac231711cd96abc14ab66b4`。冲击臂期末状态 hash 为 `d68b8c030589c56cd8c6f80acba7e47f1f911d206d3201264ebaae07d6eaf939`。三臂均保存独立对账，初末现金合计均为 1322，证券合计均为 4，各成交 1 单位。CLI 单个启动/续跑/回放命令墙钟为 0.50–0.92 秒。源码 hash 为 `cfe58deb667d07a10352bbf445080e76f3e4ff59acfcdf972bca98f27ad6e2ca`。
- 本地原始日志位于 `build/convergence/02/red.log`、`build/convergence/02/pytest-gate.log`、`build/convergence/02/pytest-gate.xml`，三臂观察、对账和运行收据位于 `build/convergence/02/verified.JBoOOF/trace.json` 及同目录的逐臂归档。
