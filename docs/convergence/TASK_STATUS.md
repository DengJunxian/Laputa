# Laputa MCRP 阶段状态

| 阶段 | 优先级 | 状态 | 基线 / 证据 | 下一动作 |
| --- | --- | --- | --- | --- |
| 01 基线与 PPT | P0 | PASS | `e40f1dd49f415685bb05d6ca9d3632fe340cab45`；[基线实测](BASELINE_TESTS.md)、[能力事实](EXISTING_CAPABILITIES.md)、[六页机制卡](PPT_MECHANISM_CARDS.md) | 本阶段完成。 |
| 02 联合世界 | P0 | PASS | [合成配对、独立对账与回放](STAGE_02.md) | 本阶段完成。 |
| 03 Agent 策略 | P1 | PASS | [三臂对照、记忆、预算与回放](STAGE_03.md) | 真实付费模型 E2E 为 NOT_EXECUTED。 |
| 04 PPT 机制 | P1 | PASS | [M1 登记、消融及阴性结果](STAGE_04.md) | 本阶段完成。 |
| 05 实验 | P1 | NOT_STARTED | 合成配对 pilot 上限 8 根 | 04 通过后按阶段提示词启动。 |
| 06 Workbench/CI | P1 | NOT_STARTED | 现有 Workbench 与本地项目安装 | 05 通过后按阶段提示词启动。 |
| 07 终验 | P1 | NOT_STARTED | 独立执行与可检查证据 | 06 通过后按阶段提示词启动。 |
