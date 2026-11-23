# 阶段 05 · 联合冲击合成 pilot

**执行验收 PASS，科研推断 INCONCLUSIVE。** 8 个预登记根均通过四臂配对和操纵检查。第 3 期消费在每个根的两条无冲击臂均为 16 件，两条冲击臂均为 10 件。加倍损失权重提高了持仓家庭的预算预留，两种权重下预算都触及零下限，主交互差为零。

## 冻结与执行

- [协议](../../examples/joint-shock-protocol.v1.json) 在试跑前提交为 `4215ebd39c156065b75e78c34a422882d4ca2c70`，协议 SHA-256 为 `2afa31dcd74622031e0c7238f252022f9646353f5572a091609aaf36902b98cd`。执行源码提交为 `e5975a9dd75e6f76325cb29bb517a7486786ee5b`。运行计划登记了源码文件、环境与四臂各自的配置哈希。
- 固定 8 户、2 家企业、2 家银行、1 个财政主体和 1 类证券。独立单位是一个世界随机根。每根的 4 个世界共用期初、预热状态、配对随机地址和有资金约束的第 2 期证券场次，第 3 期是首个观察已提交标价的决策期，也是唯一主终点。
- 四臂一律开启同伴消费披露。第 3 期同标价的两条权重臂逐根核对了完整观察哈希，四臂也逐根核对了每个行动者按渠道划分的观察条数。该期每臂共收到 16 条消费披露，第 4 期仍均为 16 条，第 5 期无冲击臂 16 条、冲击臂 10 条。后期披露量来自先前已结算的消费，作为过程结果保留。
- 32 个分配均完成，合计 160 个运行期，墙钟 39.77 秒。失败、审计失败、未启动和自然停机均为 0。第 101 根四臂在第 2 期生成 checkpoint，续跑到第 5 期，再独立执行回放。4 份回放各核对 5 期并匹配终态，无执行态增量注入。外部 API 调用、模型尝试及模型支出均为 0。

## 预登记配对结果

单位为第 3 期结算消费件数。每行使用相同的 8 个世界根，家庭、交易和期次均归入所属世界。配对差从逐笔消费回执独立复算。

| 对照 | 完整根 / 分配根 | 平均配对差 | 样本 SD | MCSE | 观测范围 | 95% 区间 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| 价格冲击 × 损失权重（主对照） | 8 / 8 | 0 | 0 | 0 | [0, 0] | 缺失 |
| 权重 1 下的冲击差 | 8 / 8 | −6 | 0 | 0 | [−6, −6] | 缺失 |
| 权重 2 下的冲击差 | 8 / 8 | −6 | 0 | 0 | [−6, −6] | 缺失 |
| 冲击下权重 2 减权重 1 | 8 / 8 | 0 | 0 | 0 | [0, 0] | 缺失 |

四行的观测样本方差均为零，依冻结规则将区间记为 `null`，原因 `zero_observed_sd`。观测范围只描述这 8 根。结果用于检验流程和记录观察到的方差，推断状态为 `INCONCLUSIVE`。种子、终点和分析规则均沿用冻结协议。

## 源证据链与失效记录

以第 101 根的无冲击线性、冲击线性和冲击加权臂为例。货币量为仿真最小货币单位，交易 ID 在每个世界内独立解释。

| 环节 | 从已提交归档复核的结果 |
| --- | --- |
| 资产与市价 | 第 2 期各臂均成交 1 单位证券，成交回执对应 `econ:security-fill:security:A-trade-00000001`，实际收盘 100。冲击臂另有 `econ:security:mark` 将合成标价设为 10，无现金或资产移转分录。 |
| 家庭判断 | 第 3 期 8 户中有 3 户收到持仓估值。`household:0002` 的 `security-wealth:3:household:0002` 由 `decision:3:household:0002` 引用。其估值为 100 / 10 / 10，预算预留增量为 0 / 90 / 180。 |
| 购买意图 | 同一家庭的预算为 51 / 0 / 0。无冲击臂发出 `intent:3:household:0002:purchase` 采购 2 件，冲击臂各留下 `command:intent:3:household:0002:hold` 回执。 |
| 实际交易与消费 | 无冲击臂该家庭有独立采购和消费分录，后者为 `econ:command:command:intent:3:household:0002:consume-purchase`。从全体 8 户核对到 8 / 5 / 5 笔消费回执，合计 16 / 10 / 10 件。 |
| 企业与劳动 | 2 家企业的销售与结算采购对账，`firm:00` 第 3 期收入为 176 / 110 / 110。其第 4 期劳动报价要求上期至少销售 13 件。第 5 期 `household:0001` 的雇主为 `firm:00` / `firm:01` / `firm:01`，工资均为 12，分录为 `econ:labor:5:household:0001:payment`。 |
| 银行与财政 | 单一财政主体的 `econ:fiscal:4` 引用第 3 期所有消费分录。消费 16 时不触发，实际转移 0。消费 10 时触发，实际转移 2。两家银行均参加对账，`bank:a` 第 3 期发放 12，随后两期各收回本金 6，四臂一致。 |

32 个世界均通过独立经济、银行及财政对账。160 个世界期中，128 个当期无实际收盘，32 个有实际收盘但相邻上一期无收盘，故可计算的邻期证券收益为 0 个。保留 96 个 `security_unfilled` 回执及 32 条满足评估条件但未触发的财政记录。主终点保留无消费结算的家庭，结算量按零计。

## 有效性与 RP06 校准范围

- **内部执行有效性。** 从提交状态重读观察、决策和回执，并将购买、消费、卖方收入、财政阈值及分录对账。核对 32 份逐臂证据的文件和终态哈希，复算 8 根的主终点和配对差。独立执行回放覆盖预登记的第 101 根四臂。
- **机制识别有效性。** 在固定规则世界中，合成标价处理改变了下一期的授权估值、采购及实际结算，并传到劳动和财政。权重处理改变了预留，预算下限令两臂行动相同。披露策略固定开启，pilot 的配对估计针对冲击与权重，未单独估计关闭传播的效应。
- **经验效度。** 联合冲击证据来自固定人口和网络的合成规则世界。现有 [RP06 校准脚本](../../scripts/rp06_calibrate.py) 与 [冻结规范](../../examples/rp06-calibration.json) 仅支持原公共品游戏里可观测合作选择的预测校准。按冻结哈希复算已有数据，14,620 行、1,462 个节点，按场分为 48/16/16 个拟合、开发和留出场。原始缺失选择 1,060 行，按顺序排除第一轮 1,462 行、当前选择缺失 1,040 行、前一期选择缺失 8 行。选中 `own_previous`，留出场平均 Brier 较常数模型低 0.082530，按场 bootstrap 的 95% 差值区间为 [−0.097862, −0.066116]。联合冲击缺乏匹配经验数据，资产传染、宏观金融危机和真实家庭行为均无该实验的验证。

## 复现与归档

从仓库根、在已有 Python 3.12 研究环境运行。使用新输出目录。

```sh
out="build/convergence/05/reproduction-$(date +%Y%m%d-%H%M%S)"
env -u OPENAI_API_KEY -u DEEPSEEK_API_KEY .venv/bin/python -m scripts.joint_shock_pilot \
  --protocol examples/joint-shock-protocol.v1.json --output "$out"
```

- `build/convergence/05/pilot/plan.json` 和 `requirements.lock` 保存协议、执行 SHA、配置、源码哈希和实测环境。使用 Python 3.12.13 和 SciPy 1.18.1。依赖锁哈希为 `2dfb15511245586c147b7a88dea7f3a3fd870f9c4b5e090a98071b19177156b8`。
- 同目录 `execution.json` 保存全部分配和终态，`report.json` 保存操纵检查、分母、四个对照及缺失原因。`worlds/<root>/<arm>/pilot-trace.json` 包含逐期状态哈希、授权观察、意图、回执及账本引用，同目录保留提交快照和观察段。`worlds/101/<arm>-replay/report.json` 保存回放收据。
- `build/convergence/05/audit-check.log` 保存对主终点、分母、哈希、缺失以及银行和财政结果的独立核对。`rp06-calibration-audit.json` 保存本地已有数据的校准复算，沿用冻结规范中的 SHA-256、来源和许可。复算命令如下，`RP06_CSV` 指向已有的 `individual_data.csv`。

```sh
.venv/bin/python scripts/rp06_calibrate.py --spec examples/rp06-calibration.json \
  --raw "$RP06_CSV" --output build/convergence/05/rp06-calibration-reproduction.json
```

定向回归 **84 通过、0 失败、0 跳过**，另有 37 个子测试通过，墙钟 74.45 秒。覆盖冻结协议、配对统计、归档证据链、RP06、旧 C/D、证券专场、异质策略、撤边和 v1/v3 golden hash。记录位于 `build/convergence/05/pytest-gate.log` 和 `pytest-gate.xml`。

```sh
.venv/bin/python -m pytest -q -ra tests/test_joint_shock_pilot.py tests/test_rp06_calibration.py \
  tests/test_reference_loss.py tests/test_wp10_demand.py tests/test_joint_policy.py \
  tests/test_joint_world.py tests/test_network_withdrawal.py tests/test_closed_economy.py \
  tests/test_wp10_security.py tests/test_v4_runtime.py tests/test_economic_runner.py \
  tests/test_fiscal_policy.py tests/test_wp10_catalog.py
```
