# 基线能力与差距

**代码基线** `DengJunxian/Laputa@e40f1dd49f415685bb05d6ca9d3632fe340cab45`。执行包审阅 SHA、远端 `main` 与本地起点一致。相邻研究树 `EcoSimNew@2770174c989e8f8bfa736761b8c7c89a28a00f0b` 有独立历史与未跟踪文档。以下证据以 Laputa 的独立克隆为准。

## 能力事实表

源码位置以 `reproduction/runtime/source/src/civitas/` 为根。测试列对应 [本次运行的测试集及其结果](BASELINE_TESTS.md)，单项通过只支持该文件覆盖的行为。

| 能力 | 源码真值 | 基线检查 | 剩余差距、优先级及修改 |
| --- | --- | --- | --- |
| 世界时钟、权限、提交 | `core/world.py::WorldKernel.step/restore_snapshot`；冻结观察、证据子集核验、类型化执行、FEEDBACK/COMMIT 及事务恢复。 | `test_v4_runtime.py` 的时间边界、故障回滚、并发、v1/v3 golden 与版本检查。 | **P0 保留。** 联合世界复用同一时钟，不增加总调度器。 |
| 账本、生产、消费 | `economy/runtime.py::EconomicRuntime` 按主体记分录，提供可用现金、工资预占、单 `good_id` 及库存成本。`scenarios/closed_economy.py::boundary/execute_batch/feedback` 执行工资、商品、退款、消费并汇总实际销量。 | `test_economic_book.py`、`test_closed_economy.py`；5 期样例含 20 次工资支付、20 次采购、20 次消费。 | **P0 修改联合分支。** 商品与证券须各有单位、成本、预占和对账。证券出售价款不得进入经营销售。 |
| 银行、财政及共存 | `economy/banking.py::BankPlan, verify_claims, bank_facts`；`economy/fiscal.py::fiscal_plan/fiscal_facts`；`EconomicRuntime.banking_period/fiscal_period`。合约、存贷、跨行准备金、提款队列、税收与财政反馈已有本地切片。银行和财政在同一本经济簿，基线决策席位为家庭与企业。 | `test_banking.py`、`test_fiscal_policy.py`、`test_joint_world.py`、`test_joint_liquidation.py`、`test_resident_credit.py` 和 5 期样例。 | **P0 复用。** 两家银行及财政共同结算，直接以已有财政消费触发作为响应门。银行/财政新增自主政策与跨行证券支付为 LATER。 |
| 证券撮合与回执 | `scenarios/security_market.py::SecurityPolicy.decide, SecurityMarket.execute_batch, validate_security_receipts` 使用 `markets/multiasset.py::MatchingVenue` 和同一个 v4 `EconomicRuntime`，在证券专场做预付撮合；无成交时当期收盘价为缺失。 | `test_wp10_security.py` 检查资金/数量、双边回执、空成交、恢复与回放；`test_wp10_catalog.py` 检查 A 组处理。 | **P0 修改联合分支。** 现有证券分支把证券作为唯一商品。`_plan` 拒绝银行/财政并存；证券专场的回执验证限定场景只含证券。保留原 A 分支。 |
| 财富、商品需求和劳动 | `scenarios/closed_economy_policy.py::ClosedEconomyPolicy.decide` 以可用现金、保留额和目标库存构造购买；`ClosedEconomy._offer_visible` 用上期真实销量判断劳动报价，合约 t+1 生效。 `closed_economy_demand.py::demand_comparison` 有授权的消费回执比较。 | `test_wp10_demand.py` 覆盖销量阈值、合约生效、消费消息来源；`test_dynamic_labor.py`、`test_labor_contract_outcomes.py` 验证劳动。组合探针重现 `demand + banking` 校验拒绝。 | **P0 修改。** 缺证券市值→购买的可见输入。阶段 02 复用 `labor_transfer`，不依赖完整 `demand`；阶段 04 只适配需要的回执披露/比较。 |
| 社会观察、关系与反馈 | `scenarios/closed_economy_relations.py::SocialActorState, ClosedEconomyObserver.select_messages` 有有限注意、通信边、曝光/消费根记录及履约状态；`relations/store.py::RelationStore`、`relations/types.py::CommunicationEdge/RelationState` 管理类型化关系。劳动、信用等经济边源于合约/账本。 | `test_closed_economy.py` 的可见性、转发同根和实际消费；`test_network_proposal.py`、`test_network_withdrawal.py` 的生效和恢复；`test_cooperation_feedback.py` 的履约链。 | **P0 保留现有通信，P1 机制 04。** 保留观测和行动证据。财富及持仓归经济观察，社交状态保存授权曝光/信任。 |
| 策略与模型 | `contracts/ports.py::Policy` 已有统一 `decide` 形状；`application/closed_economy.py::build_closed_economy` 注册供应商、劳动、提款和有限离线社会选择。`scenarios/closed_economy_cognition.py` 复用观察与执行合同。`application/economic_run.py::create_run` 约束响应归档、运行预算与 live 配置。 | `test_closed_economy_cognition.py`、`test_closed_economy_offline_cognition.py`、`test_goal03_hybrid.py` 的许可、归档、回退与回放。SDK mock 覆盖情况见基线。 | **P1 修改。** 可用的选择席位有限，联合证券世界仍缺可组合的行为/归档策略。真实服务与计费单独验收。 |
| 持久化和重放 | `application/economic_run.py::create_run/resume_run/replay_run`；`storage/checkpoint.py::PARTICIPANT_VERSIONS` 与 `contracts/codecs.py::WORLD_CODECS`。记录配置/源码/依赖，独立重跑取状态。 | `test_economic_runner.py`、`test_v4_runtime.py`、`test_rp05_storage.py`；命令行从第 2 期续到第 5 期，与重新执行的 5 期匹配。 | **P0 修改联合状态读取与验证。** 保留旧 schema 和源身份；新增资产字段纳入版本、初始化、导入、hash、回滚和验证。 |
| A–E 实验与 D1 | `experiments/scenario_catalog.py::ScenarioSuite.validate_domains, run_suite` 检查各域并要求 rule；`scripts/goal05_research.py` 有计划冻结、执行、审计。 | `test_wp10_catalog.py` 的零成交、分母、丢失回放；`test_goal05_research_matrix.py`、`test_rp06_d1.py`。README 保留原 D1 未达登记的有效根门槛。 | **P1 修改。** 用单独联合配对入口表达新切片，保留 A–E 与 D1 登记、分母和比较口径。 |
| 研究工作台、发布 | `application/workbench.py::config/inspect/research/pair_export`；普通运行固定期限且 rule，另有登记研发预览和只读观察。`pyproject.toml` 提供 3.12 安装；当前树没有 Actions workflow，第三方 notices 可用。 | `test_local_workbench.py`、`test_readonly_comparison.py`、`test_rp05_products.py` 与独立 3.12 安装。 | **P1 修改。** 接上联合证据、账户/关系、对照和来源，建立 CI。项目级许可尚未分配，保留既有第三方归属。 |

## 四批证据与 Git 差异

| 来源 | 用于本轮的证据及规范 | 当前代码校正 |
| --- | --- | --- |
| Batch 1 `03_Reuse_Report.md`、`04_Reusable_Component_Inventory.json` | 优先保留 EcoSim 内核和既有 `MatchingVenue`。原 ABIDES 固定源码是撮合语义的对照候选；它另有调度、账本语义和历史依赖。现有第三方提取按 notices 保留归属。 | Laputa 已有专场撮合。阶段 02 优先复用仓内 plan/match/cancel 和固定点 notional，无需求引入 ABIDES 的运行栈。 |
| Batch 2 `01_Unified_Formal_Model.md`、`05_Experiment_Protocol.md`、机制与实验登记册 | E01/02/05/06/07、SM02/04/05；区分名义持仓估值、实际购买力、工资与产出。世界是配对单位，根与共同随机流固定；记录未成交、未触发、失败及自然停止；经验效度需要单独检验。 | 原 A/C/E 库继续保留。只有本轮联合配置及明确定义的消融进入 pilot。 |
| Batch 3 `01_System_Architecture.md`、`02_Economic_Contracts.md`、`05_Component_Selection_and_Reuse.md`、`06_Work_Packages.md` | WP01–05/07/08/10 的状态权威、账户平衡、时序、观察屏障、预占及回执门。WP06/09/11/13 约束模型、实验和检查。 | 当前 v4 已有多个限定场景。WP 原完整门保留，未以适配存在替代完整能力。 |
| Batch 4 `06_WP_Completion.md`、`07_Updated_Codex_Prompts.md`、`04_Mechanism_Walkthrough.md`、`final_acceptance.json` | 历史 C/D 账本与独立回放、履约故障→本人→同伴→采购路径、8 根×4 臂保存证据。RP01 证券/劳动，RP02 权限/关系，RP04 账本制度。seed 211 是事后诊断。 | Batch 4 记录的 62 项属于其冻结提交。当前已出现劳动合同、银行、财政、有界重连、归档与证券专场。应增量复用，历史测试数和合成结果按原范围引用。 |

## 阶段 02 精确开发入口

在本分支从阶段 01 本地提交进入。读取执行包 `prompts/02_JOINT_WORLD.md`。**首个编辑**为 `tests/test_joint_world.py` 新增 `test_security_shock_reaches_joint_economy`。先断言联合配置能构建 `ClosedEconomy` 并同时拥有一份参与者经济簿、一类证券和商品、家庭/企业/银行/财政账户。当前旧配置直接组合会报错，若新合同暂未定义则以同一构建入口复现红测。现有证券专场互斥保持其回归语义。

```sh
git status --short --branch
git rev-parse HEAD
.venv/bin/python -m pytest -q tests/test_joint_world.py -k test_security_shock_reaches_joint_economy
```

1. **组合根** · `scenarios/closed_economy_config.py::ClosedEconomyConfig.compatible_configuration` 和 `application/closed_economy.py::build_closed_economy` 注册明确的联合配置，进入同一个 `ClosedEconomy`、同一个 `WorldKernel`。既有 A 配置走相同的旧专场。固定 8 家庭、2 企业、2 银行、财政、单商品及单证券。5 期、root 7、零调用。
2. **核算与撮合** · `economy/runtime.py::settle_security/_plan/_apply/_rebuild`、`contracts/economic.py`及必要的 `storage/checkpoint.py` 适配独立证券数量、成本和预占字段。复用 `SecurityOrderIntent`、`MatchingVenue.plan/match/cancel`、`validate_security_receipts` 的双边校验语义。预付做市现金出自同账本既有一方。统一处理混合执行顺序、预算、编号和回执；逐笔对账并保留空成交。成交实现损益与商品销售分别记账。兼容格式仍可读取，新增不兼容格式显式版本化。
3. **唯一时序** · `scenarios/closed_economy.py::apply_event/project_observation/execute_batch/feedback` 和 `closed_economy_policy.py::ClosedEconomyPolicy.decide`。第 2 期用合成且可审计的价格标记下降，标记、实际成交价和缺成交独立记录；市值只是观察。第 3 期只向持有者投影按统一价格和当前数量计算的私人证券财富变化。可计算的线性预算收紧仍受可用现金约束；观察 ID → 采购命令 → 交付/消费回执 → 企业已结算销售。第 4 期复用 `_offer_visible`，用 firm:00 的前期真实销量触发对原 firm:01 工人的换岗报价，接受后在第 5 期核对劳动与工资对手。第 4 期财政复用 `consumption_floor_units` 的前期消费响应，引用 journal；两家银行维持现有结算语义。社会联系人保留授权观察边界。
4. **固定对照** · 一个新样例 `examples/closed-economy-security-feedback.json`。比较冲击/无冲击及财富输入接通/断开。各臂共享初态、持仓、随机根、资金额度、撮合和制度；断开改变的是策略使用该私人事实的通道。选定并登记资产初值、冲击及预算参数、企业销量阈值、财政消费阈值后再运行成对验收。记录无交易、拒绝、未触发。实际跨域差异需由对应证据和回执验证。
5. **验收入口** · `application/economic_run.py` 的联合运行身份、恢复和回放，按需更新混合状态投影。执行 `pytest -q tests/test_joint_world.py tests/test_wp10_security.py tests/test_wp10_demand.py tests/test_closed_economy.py tests/test_economic_runner.py tests/test_v4_runtime.py`。用同一新样例保存完整 5 期、在中间提交位置停下再恢复、独立重放，核对业务 hash、证券/商品/现金/债务、前后故障和旧回归。Workbench 扩展留至阶段 06。
