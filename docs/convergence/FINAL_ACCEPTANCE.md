# Laputa MCRP 最终验收

**阶段 07 PASS。本轮最小完整社会经济共同演化平台（MCRP）交付完成。** 验证对象为进入阶段 07 时的 `77c6143cb02a10374ef7e5a061b35c666f88ddad`。从该提交建立干净的 detached worktree，在其外部新建虚拟环境并安装 wheel。归档中的源码哈希为 `c941ddcd93fafcc6d97e60dc2528e1277bf5718c2ef5211cf25ba344e640c85b`，逐个 Python 源文件及三项 Workbench 资产的内容均与检出匹配。

环境为 Darwin arm64、CPython 3.12.13、SQLite 3.53.4。核心依赖为 Pydantic 2.13.4、PyYAML 6.0.3，分析采用 SciPy 1.18.1。完整安装清单和许可元数据分别为 `build/convergence/07/dependencies.lock` 和 `environment-and-licenses.json`。安装及资产检查用时 101.96 秒，`pip check` 无依赖冲突。验收使用规则或离线响应，外部付费模型调用为 0。

## 强制门槛

下表中的证据产物位于 `build/convergence/07/`。已有阶段文档给出对应的冻结配置和复跑命令。

| 目标 | 状态 | 独立终验结果与证据 |
| --- | --- | --- |
| 1 · 干净安装、核心回归及 CI 本地等价 | **PASS** | 执行 `.github/workflows/quality.yml` 中安装、资产与 notice 检查以及 pytest 的 shell 步骤，**97 项与 42 个子测试通过**，pytest 用时 159.61 秒。另跑账本、银行、联合清算、离线认知、自然停止和只读对照，**68 项与 43 个子测试通过**，用时 117.34 秒。两组均 0 失败、0 跳过。`install-and-assets.log`、`ci-local-tests.log`、`core-and-scenarios.log`。 |
| 2 · 单世界、单账本和跨域约束 | **PASS** | 从零执行 [阶段 02](STAGE_02.md) 的三臂。每臂 8 户、2 企业、2 银行、1 财政主体、1 类证券，仅有一个 `economic_runtime` 结算参与者。复算现金、商品与证券数量、银行债权债务和财政收支，验证未成交回执与来源 ID。`stage02/independent-audit.json`。 |
| 混合策略、记忆和类型化动作（总控 B） | **PASS** | 干净环境回归 `tests/test_joint_policy.py` 复现同世界规则、行为和固定归档的混合决策。已结算采购更新同一记忆，归档调用上限为 3，输出经过类型化动作及公共账本。核对跨主体证据拒绝，故障后的归档和记忆回滚，以及续跑后 5 期独立回放。付费模型为单独的 `NOT_TESTED` 项。见 [阶段 03](STAGE_03.md)。 |
| 3 · 冻结配对协议、完整分母、逐期重放 | **PASS** | 原协议 SHA-256 `2afa31dcd74622031e0c7238f252022f9646353f5572a091609aaf36902b98cd`。8 根的 32 条轨迹全部完成，所有初态及期次哈希均与阶段 05 归档相同。第 101 根四臂均从第 2 期 checkpoint 续跑，独立执行回放各匹配 5 期。另对 `101/shock_weighted` 从配置重建并重执行，逐项比对初态和第 1–5 期哈希。`pilot/report.json`、`pilot/execution.json`、`pilot-audit.json`、`pilot-replay-hashes.json`。 |
| 4 · PPT 机制的更新、观察、行动、回执与消融 | **PASS** | 重新执行 [阶段 04](STAGE_04.md) 的权重 1、权重 2 与披露切断三臂。唯一新增行为机制 M1 可从私人估值追到预算、意图、回执及同伴曝光，三臂各匹配 5 期独立回放。`stage04/independent-audit.json`。 |
| 5 · Workbench、权限、对照及导出 | **PASS** | 由安装的 wheel 在 `127.0.0.1` 启动，浏览器真实冻结模板、运行、查看 5 期、浏览四层关系及对账。第 3 期显示通信 16、雇佣 8、贷款 1、持仓 4 条关系及 16 个账本账户。观察者实际请求研究者时间轴、对照导出和他人观察均获 403，第 0 期返回 `unrecorded_observe`。阶段 05 两条加权臂的来源、随机根、提交、源码及状态可见，下载 JSON 与独立重算逐字节一致。`workbench-browser-receipt.json`、`workbench-comparison.json`。 |
| 6 · 第三方声明、依赖许可与发布边界 | **PASS** | 检查源码 [THIRD_PARTY_NOTICES](../../THIRD_PARTY_NOTICES.md)、wheel 中的四份原始许可及各依赖的安装许可文件与 SHA-256。Pydantic、PyYAML、ipfn 的元数据为 MIT，NumPy、pandas、SciPy 为 BSD 系许可，statsmodels 为 BSD-3-Clause，PyArrow 为 Apache-2.0。可选 OpenAI SDK 的原包许可为 Apache-2.0；终验未安装此 extra。根目录无项目级 `LICENSE`，公开预览未授予自有代码的通用开源再利用权限。本轮交付为本地提交。 |
| 7 · 终验交付及停止 | **PASS** | 本文件与 [三项非阻塞后续事项](NEXT_TASK.md) 随本地分支提交。无阻塞修复，无新机制、依赖或生产代码变更。 |

## 跨域证据与机制追溯

- 同一 journal 的期初持仓分录给 4 户各配置 1 份证券。第 2 期 `econ:security-fill:security:A-trade-00000001` 成交 1 份，双方现金回执为 +100 和 −100。`econ:security:mark` 将冲击臂标价设为 10，实际成交价为 100，标价事件的现金和商品分录为空。三臂期初与期末现金均为 1322，期末证券均为 4。每个提交期均核对产权与余额。
- 冲击可见时，第 3 期消费 10 件、`firm:00` 销售额 110。无冲击或隐藏私人财富事实时分别为 16 件、176。第 4 期 `econ:fiscal:4` 的税基引用当期工资分录，三臂实缴税均为 16。其转移判断引用第 3 期真实消费分录，冲击可见时支付 2，其余为 0。第 5 期 `econ:labor:5:household:0001:payment` 的支付方由 `firm:00` 变为 `firm:01`，两者均实付 12。`joint-credit` 第 3 期放款 12，银行与家庭存贷双方镜像，后两期各还 6。金额均为仿真最小货币单位。
- M1 更新由 `EconomicRuntime.security_wealth`、`ClosedEconomy.project_observation` 和 `ClosedEconomyPolicy.decide` 串接。先提交标价，下一期才向持仓户投影私人事实。策略引用事实计算损失预留并通过类型化意图和合法预算边界。三臂各有 9 次私人估值，权重 1 与 2 的额外预留分别为 90 与 180，两臂持仓户采购预算均为零，已保存无购买结算的回执结果。
- `ClosedEconomyObserver.select_messages` 对照消费回执和通信约束，`demand_comparison` 将授权消费用于下一次决策。开启披露时，两条权重臂各观察到 42 条消息，其中 4 条进入决策证据，出现 3 次消费目标上调。切断后这些计数都为 0，三臂第 3–5 期实际消费仍为 10／10／10。M2 属于已有角色的联合账本整合。M3 退休机制缺少年龄队列、生命周期合同及养老金负债，概率扭曲、其他扩展和现实拟合亦有各自的后续证据要求，归档于 [PPT 机制卡](PPT_MECHANISM_CARDS.md) 和 [积压表](BACKLOG.md)。

## 科研效度与尚缺

内部执行和固定模型内的机制操纵均通过。8 个独立单位为随机根，同一根的四个世界为配对条件。第 3 期消费在每根的两条无冲击臂均为 16 件，两条冲击臂均为 10 件。冲击在两种权重下均为 −6 件，权重与冲击的交互差为 0。样本 SD、MCSE 都为 0，按冻结规则将 95% 区间记为缺失。结论仍为 `INCONCLUSIVE`。四臂第 3 期的观察量核对一致，同标价权重臂的完整观察哈希相同。

保留 96 个未成交证券订单回执和 32 条未触发的财政响应，所有 160 个世界期均缺少相邻两期的实际成交收盘价，收益记为 `null`。固定期限内实际失败、自然停止及未启动数均为 0，分母仍为 32。终验期间未修改冻结种子、终点或分析规则。现有 RP06 的证据只覆盖原公共品实验中合作选择的预测校准。联合冲击的经验效度为 **NOT_TESTED**，有待匹配的估值、消费和制度数据。

| 独立待验项 | 状态 | 当前边界 |
| --- | --- | --- |
| GitHub 托管 CI | **NOT_TESTED** | 本地等价工作流已通过，分支未推送。 |
| 双根 1,000 户 × 100 期正式性能和回放 | **NOT_TESTED** | 阶段 06 仅核验 32 户与 100 户的 20 期规则粗测。 |
| 真实付费 Provider 端到端与账单 | **NOT_TESTED** | 本次只验证规则及离线策略链和预算边界。 |

## 重跑入口与停止记录

从干净的 `77c6143` 检出，用仓库外 Python 3.12 虚拟环境执行 `python -m pip install .`、`python -m pip install '.[research,population,data]' 'pytest==9.2.0'` 和 [CI 工作流](../../.github/workflows/quality.yml) 的检查与测试。阶段 02 和 04 的命令分别见[联合结算](STAGE_02.md)和[机制消融](STAGE_04.md)。冻结试验和浏览器入口如下，每次使用新目录。

```sh
python -m scripts.joint_shock_pilot --protocol examples/joint-shock-protocol.v1.json --output /path/to/pilot
ecosim workbench --workspace /path/to/workbench --research-preview examples --joint-pilot /path/to/pilot --port 0
```

无本地强制验收阻塞。未推送、未发布、未更改项目许可。阶段 07 停止，七阶段结束。
