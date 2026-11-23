# SCOPE_LOCK / Laputa MCRP

**Stage 02 base** `2efd7749ae515e3603abfceba45378a100ff6c3f` · **Current stage** 02 · **Branch** `feature/laputa-convergence`

**One target behavior**

一个规则世界运行 8 户、2 家企业、2 家银行、一个财政主体、一种商品和一类证券。证券标价变化经下一期的私人财富观察和预算规则传到真实采购与消费，随后由已结算销量触发劳动合约取舍，并由财政读取真实消费回执。现有的社交关系继续提供受权限约束的信息。

| 优先级 | 阶段 | 冻结验收 |
| --- | --- | --- |
| P0 | 01 | 实际 Git 与 Python 3.12 基线、3–5 期规则例及续跑/执行回放、配置反例、四批证据对照与六页机制卡。改动范围为审计文档和项目指令。 |
| P0 | 02 | 一个联合世界、一个 5 期合成验收例。冻结同根配对，核验账本、冲击与反馈、失败回滚、checkpoint/resume/replay 及旧证券与 C/D 回归。 |
| P1 | 03–04 | 复用现有策略端口完成两种主体选择的规则/行为/归档混合，保留显式授权与预算约束下的可选真实模型端口。阶段 04 只接入 M1「参考点损失权重」，利用现有已验证同伴披露路径。 |
| P1 | 05–06 | 一个冻结的合成配对试验，最多 8 个独立随机根。增量扩展既有 Workbench，验证授权、干净安装、CI 与许可记录。 |
| P1 | 07 | 从干净工作区独立执行被锁定的端到端验收，记录剩余缺口并停止。 |

**Files/code surfaces expected**

阶段 02 的红测、组合根、证券结算和状态合同、规则/观察边界、持久化验证入口见 [能力与精确入口](EXISTING_CAPABILITIES.md#阶段-02-精确开发入口)。

**Existing upstream code to use**

本轮首选现有 `MatchingVenue`、`EconomicRuntime`、`ClosedEconomy` 和既有存续/观察/回放协议。阶段 02 预期外部依赖增量为零。Batch 1 中的 ABIDES 留作同语义撮合对照参考。

**Acceptance and stop**

[测试基线](BASELINE_TESTS.md) · [机制登记](PPT_MECHANISM_CARDS.md) · [状态](TASK_STATUS.md)。各阶段通过后立即停；下一阶段以用户指示启动。一轮实现和一轮定向 QA/修复，最多两次针对已知阻塞的尝试，剩余问题进入 [阻塞表](BLOCKERS.md)。

**Out of scope**

[积压项](BACKLOG.md) 为 LATER。本轮范围包括本地提交；远端推送、公开发布、修改许可和付费模型调用需另行授权。

**Stage 02 slice** · root 7、5 期、两组同初态配对。第 2 期记合成标价，第 3 期按可见财富调整采购，第 4 期读取真实销量和消费，第 5 期核对工资。沿用现有撮合、同一本经济簿及权限制约。验收包含证券数量/成本、预算预占、双方回执、零成交、故障回滚和续跑/执行重放。

**Result** · PASS，见 [阶段 02 实测](STAGE_02.md)。
