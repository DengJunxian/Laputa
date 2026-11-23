# 阶段 03 联合世界的异质策略

基线 `729b2e24f95bcd51e037244b5b20c18a9c674008`。沿用阶段 02 的 5 期、root 7 和同一开仓经济簿。两户和一家企业参与策略演示。银行与财政继续执行既有制度规则。

## 策略路径

- `JointPolicyAdapter` 实现现有 Policy 形状。`joint_policy.seats` 按登记主体与期次派发，合法角色由家庭和企业名录确定。未分派期次使用规则。行为策略通过同一预算函数提高预留额，参数为固定谨慎额加上上一期已结算采购金额的一半。
- 回执在下一期进入私有观察，唯一 `MemoryBook` 记录当前可见证券参考估值和采购反馈。参考估值来自阶段 02 的已登记标价基准。新损失权重留在阶段 04。记忆和经济期次共同提交与回滚，调用与费用事实由既有调用账保存。
- 结构化响应在 `baseline` 与 `cautious` 之间选择，并引用一个有权限的证据 ID。谨慎选项让家庭延期采购，或让企业按原报价步长降价。原策略生成类型化意图，编译、现金、库存、证券、劳动和财政结算共用原接口。模型输入只包含本席可见账户、相关市场事实和授权回执。复用旧 SupplierModelPolicy 的证据投影与检索，以及 SocialChoicePolicy 使用的结构化选择、拒绝回退和调用归档。保留旧研究入口。
- `synthetic_test` 和 `archived_response` 共用非货币调用预算。离线归档按完整请求语义匹配，可回放已记录响应。新 `live` 槽用既有 Provider、累计项目费用账、每次调用预留、期次调用槽和凭据门。可选 SDK 沿用现有 extra。
- 新联合 `live` 配置需要 `model_manifest`，其 `entry` 包含 `provider`、`model`、`revision`、`price`、`pricing_reference`、`verified_at`，`entry_hash` 为条目的规范内容哈希。条目与显式 Provider 配置逐项相等，响应必须报告固定 revision。合法新条目可通过配置接入，旧 WP05 规则依旧校验原有无清单调用。模型条目及价格随运行身份冻结，编辑条目或价格会破坏提交链身份。清单的核验来源由提交方提供。

## 实测

| 配置 | household:0000 第 1 期采购意图 | 固定调用数 | 外部模型调用 | 独立重执行 |
| --- | ---: | ---: | ---: | --- |
| 纯规则 | 2 件 | 0 | 0 | 5 期匹配 |
| 行为偏好 | 1 件 | 0 | 0 | 5 期匹配 |
| 行为与固定归档混合 | 1 件 | 3 | 0 | 5 期匹配 |

归档家庭 `household:0001` 在第 3、4 期延期采购，生成两张持有回执。`firm:00` 第 3 期以类型化命令将报价从 10 降至 9。行为家庭 `household:0000` 第 2 期读到第 1 期采购回执，实际支出为 10，谨慎预留从 49 调整为 54。该家庭第 3–5 期读取参考估值 100，并继续按期记录已结算反馈。

三臂期初经济簿相同，期初及期末现金均为 1322，证券总量为 4。归档臂和生成响应的合成臂期末经济簿相同。故障注入验证经济状态和记忆回滚，已记调用在续跑时复用。跨主体证据和被修改的归档均被拒绝。

回归 **82 通过、0 失败、0 跳过**，另有 48 个子测试通过，墙钟 67.37 秒。真实 Provider 路径由 SDK mock HTTP 验证了预算、准确 revision、错误 revision 的失败账和冻结后更换价格的拒绝。**真实付费模型 E2E 为 NOT_EXECUTED。** 红测和回归日志位于 `build/convergence/03/red.log`、`build/convergence/03/pytest-gate.log`。三臂证据及响应带位于 `build/convergence/03/verified.95qOmo/`，固定响应带含 3 条，哈希为 `32552ee6963d9101ebd47d10ff45d1926e4341967b20780092c6ce6109d83650`。

## 复现

从仓库根使用已安装的 Python 3.12 环境和新的输出目录。

```sh
mkdir -p build/convergence/03
out=$(mktemp -d "$(pwd)/build/convergence/03/verified.XXXXXX")
base=examples/closed-economy-security-feedback.json
seats=examples/joint-policy-seats.json
jq --slurpfile policy "$seats" '.joint_policy = $policy[0]' "$base" > "$out/synthetic.json"
jq '.joint_policy.seats |= map(select(.strategy == "behavioral"))' "$out/synthetic.json" > "$out/behavior.json"
jq '.joint_policy.source = "archived_response"' "$out/synthetic.json" > "$out/archived.json"
.venv/bin/ecosim run --config "$base" --output "$out/rule"
.venv/bin/ecosim run --config "$out/behavior.json" --output "$out/behavior"
.venv/bin/ecosim run --config "$out/synthetic.json" --output "$out/synthetic"
.venv/bin/ecosim freeze-offline-responses "$out/synthetic" --output "$out/tape.json"
.venv/bin/ecosim run --config "$out/archived.json" --output "$out/archived" --until 2 --response-archive "$out/tape.json"
.venv/bin/ecosim resume "$out/archived"
for arm in rule behavior archived; do
  .venv/bin/ecosim replay "$out/$arm" --output "$out/$arm-replay"
done
```

定向回归覆盖 `test_joint_policy.py`、`test_joint_world.py`、`test_closed_economy_cognition.py`、`test_closed_economy_live_control.py`、`test_closed_economy_offline_cognition.py`、`test_goal03_hybrid.py`、`test_economic_runner.py`、`test_v4_runtime.py`、`test_wp10_security.py` 和 `test_closed_economy.py`，使用 pytest 和现有 `openai==2.26.0` 可选依赖。在独立本地依赖目录安装 SDK，并将其附加至模块搜索路径，保持主环境 Pydantic 2.13.4。
