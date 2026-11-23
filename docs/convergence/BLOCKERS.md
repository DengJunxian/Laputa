# Laputa 收敛阻塞

## 阶段 01

阶段 01 验收通过，无阻塞。三项可选 SDK mock 测试因未装 `llm` extra 跳过，见 [基线记录](BASELINE_TESTS.md)。

## 阶段 02 已关闭的基线缺口

- `ClosedEconomyConfig.compatible_configuration()` 拒绝已有银行/财政配置与现有证券配置合用。单独传入两套配置都可构建。联合需求配置的现有银行互斥也在探针中复现。
- 基线 `build_closed_economy()` 根据 `securities` 选择 `SecurityMarket`，该场景把 `security:A` 放入唯一商品数量/成本槽。显式联合配置现已接入 `ClosedEconomy` 和格式 3 的单一经济簿，独立 A 场景通过回归。
- 价格、消费、销量、劳动与财政逐期通过验收。证据包含无成交、故障回滚、checkpoint 续跑和独立执行回放。阶段 02 无待修阻塞。[验收记录](STAGE_02.md)
