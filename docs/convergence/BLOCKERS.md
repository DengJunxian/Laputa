# Laputa 收敛阻塞

## 阶段 01

阶段 01 验收通过，无阻塞。三项可选 SDK mock 测试因未装 `llm` extra 跳过，见 [基线记录](BASELINE_TESTS.md)。

## 阶段 02 的基线缺口

- `ClosedEconomyConfig.compatible_configuration()` 拒绝已有银行/财政配置与现有证券配置合用。单独传入两套配置都可构建。联合需求配置的现有银行互斥也在探针中复现。
- `build_closed_economy()` 根据 `securities` 选择 `SecurityMarket`，该场景把 `security:A` 放入唯一商品数量/成本槽。`EconomicRuntime._plan()` 拒绝有银行或财政的证券成交。阶段 02 优先补齐联合场景的多资源账本合同，并保留独立 A 场景。
- 阶段 02 必须验证价格、消费、销售、劳动和财政的逐期实值。配置可构建、旧专业场景通过及基线会计验收均不足以关闭此缺口。
