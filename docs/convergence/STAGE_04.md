# 阶段 04 · M1 固定参考点的损失权重

基线 `dae266ca5e6d47613b40ba08b0d75c533582d9c4`。本阶段只有一项登记机制。预冻结主验证保留阶段 02 的 8 户、5 期、root 7、原证券标价冲击 90 和同一期初经济簿。

## 机制登记

| 字段 | M1 · `reference_loss_v1` |
| --- | --- |
| Theory / source | 参考照片 1 的 Pro-SF 页可见参考点依赖和损失厌恶。Batch 2 E06 登记分段价值与独立损失系数，SM05 登记可见参照消费。此处采用整数化的线性损失侧预算响应。[机制卡](PPT_MECHANISM_CARDS.md) |
| State | 期初每证券单位参考价 `P_0`、当前持仓 `q_t`、观察时已提交且可见的有效合成标价 `P_t`，以及原有授权消费交易回执和通信边。 |
| Update | 参考估值 `R_t = q_t * P_0`，当前估值 `V_t = q_t * P_t`，损失 `L_t = max(0, R_t - V_t)`。在原预算预留中加入 `L_t * lambda_bps // 10000`。`lambda_bps=10000` 精确复现原有线性响应，收益侧的额外预留为零。 |
| Owner | `EconomicRuntime` 继续拥有标价和持仓事实。`ClosedEconomyPolicy` 计算损失、预留及证据 ID，预算和库存使用原有命令与回执合同。沿用 `ClosedEconomyObserver`、`SocialActorState`、`RelationStore` 及其去重、边和权限记录。 |
| Exposure | 持有人在第 3 期收到第 2 期标价对应的私人观察。消费结算后第 1 期由回执所属家庭披露，第 2 期才可能经在用通信边进入接收者观察。披露字段为消费单位与回执身份。 |
| Decision effect | 持有人按新预留额预算采购。接收者按既有比较器处理同伴的实际消费，回执支持的同伴消费高于本人同期消费时提高目标 1 件。预算、报价及库存继续限制实际采购。 |
| Metrics | 每个持有人期次的参考值、可用现金、加权预留、采购意图和真实结算。每个接收者的消息、曝光、证据使用、消费目标和实际消费，以回执 ID 追溯。 |
| Ablation | 同根配对在披露开启时比较 `lambda_bps=10000` 与 `20000`。再在 `20000` 下切断消费披露，保留原通信结构及其他信息路径。 |

复用本仓既有消费比较和消息授权，无新增依赖或外部实现。验证只随现有一个世界时钟推进。

## 冻结对照结果

| 条件 | 每个受冲击持有者的额外预留 | 已曝光的消费消息 | 作为决策证据的消费消息 | 抬高消费目标的家庭数 | 第 3–5 期实际消费 |
| --- | ---: | ---: | ---: | ---: | --- |
| 权重 1，披露开启 | 90 | 42 | 4 | 3 | 10 / 10 / 10 件 |
| 权重 2，披露开启 | 180 | 42 | 4 | 3 | 10 / 10 / 10 件 |
| 权重 2，披露关闭 | 180 | 0 | 0 | 0 | 10 / 10 / 10 件 |

三个持有家庭在第 3–5 期共形成 9 个有授权的市值观察。两种权重下它们的采购预算均为 0，采购意图和实际消费均相同，权重配对的完整经济簿相同。披露切断消除了消费曝光和目标上调，实际消费序列也相同。两个对照的经济行动结果均为零差异。

第 3 期 `household:0002` 的已结算消费在第 4 期沿通信边披露给 `household:0004`，后者第 5 期才收到并引用这条回执作为比较证据。主验证逐条确认接收者和根组合只曝光一次，且消息的实际结算回执、出现时点和消费单位相符。

另有两项定向工程测试。固定假设的可用现金 120 验证同一决策规则随权重从 1 升为 2 时，类型化采购意图从 2 件降至 0 件。使用现有撤边机制的边界测试验证，第 7 期生效的撤边阻断在途消费消息，银行的合同继续有效，贷款本金仅按既有还款规则从 6 降至 4，未撤边对照也为 4。这两项属于接口和时序测试。

三臂从第 3 期 checkpoint 续跑，独立执行回放各匹配 5 期，外部模型调用为 0。对账通过，三臂期末现金均为 1322、证券均为 4。记录位于 `build/convergence/04/verified.fV96ME/trace.json`。

回归 **78 通过、0 失败、0 跳过**，另有 37 个子测试通过，墙钟 81.64 秒。包括旧 C/D、证券专场、联合世界、异质策略、撤边和 v1/v3 golden hash。红测和最终 pytest 记录分别见 `build/convergence/04/red.log` 与 `build/convergence/04/pytest-gate.log`。

## 复现

从仓库根运行，每次使用新目录。

```sh
mkdir -p build/convergence/04
out=$(mktemp -d "$(pwd)/build/convergence/04/verified.XXXXXX")
base=examples/closed-economy-security-feedback.json
settings=examples/reference-loss-settings.json
jq --slurpfile settings "$settings" '.reference_loss = $settings[0]' "$base" > "$out/weighted.json"
jq '.reference_loss.lambda_bps = 10000' "$out/weighted.json" > "$out/linear.json"
jq '.reference_loss.peer_disclosure = false' "$out/weighted.json" > "$out/channel_cut.json"
for arm in linear weighted channel_cut; do
  .venv/bin/ecosim run --config "$out/$arm.json" --output "$out/$arm" --until 3
  .venv/bin/ecosim resume "$out/$arm"
  .venv/bin/ecosim replay "$out/$arm" --output "$out/$arm-replay"
done
.venv/bin/python -m pytest -q tests/test_reference_loss.py tests/test_wp10_demand.py \
  tests/test_joint_policy.py tests/test_joint_world.py tests/test_network_withdrawal.py \
  tests/test_closed_economy.py tests/test_wp10_security.py tests/test_v4_runtime.py \
  tests/test_economic_runner.py tests/test_fiscal_policy.py tests/test_wp10_catalog.py
```
