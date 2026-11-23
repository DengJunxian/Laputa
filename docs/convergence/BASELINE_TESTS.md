# 阶段 01 实测基线

2026-11-23（Asia/Shanghai）。本地分支 `feature/laputa-convergence` 从干净的 `origin/main` 克隆，审阅和运行起点均为 `e40f1dd49f415685bb05d6ca9d3632fe340cab45`。现有 EcoSimNew 研究树单独保留。六页据会话附件原图整理，执行包的三个入口文件与 ZIP 成员逐字节匹配。

## 环境与命令

Darwin arm64，Homebrew CPython 3.12.13，uv 0.12.13。新建独立 `.venv`，以 editable 安装该 SHA 的 `ecosim-research==0.4.0.dev1`。安装用时 12.06 秒，`uv pip check` 通过。核心依赖 Pydantic 2.13.4、PyYAML 6.0.3；研发/人口/数据可选项已装，含 NumPy 2.3.5、pandas 2.2.3、SciPy 1.18.1、statsmodels 0.15.0、ipfn 1.4.4、PyArrow 26.0.1。pytest 9.2.0。此环境未装 `llm` extra。未运行真实模型、跨平台安装或正式规模门。

以下命令从仓库根执行。每次重跑使用新的输出目录。完整测试日志、JUnit、依赖清单、时间和仿真归档保存在本地 Git 忽略的 `build/convergence/01/`。

```sh
mkdir -p build/convergence/01
uv venv --python python3.12 .venv
uv pip install --python .venv/bin/python -e '.[research,population,data]' pytest
uv pip check --python .venv/bin/python
uv pip freeze --python .venv/bin/python > build/convergence/01/dependencies.txt
/usr/bin/time -p .venv/bin/python -m pytest -ra -q --junitxml=build/convergence/01/pytest.xml tests

evidence=build/convergence/01
.venv/bin/ecosim run --config examples/closed-economy-credit-offer.json --output "$evidence/credit" --until 2
.venv/bin/ecosim resume "$evidence/credit"
.venv/bin/ecosim replay "$evidence/credit" --output "$evidence/credit-replay"
.venv/bin/ecosim run --config examples/closed-economy-credit-offer.json --output "$evidence/credit-continuous"
```

## pytest

已收集 354 个测试节点。合计 **351 通过、3 跳过、0 失败、0 错误，另有 150 个子测试通过**。跳过项均为 `tests/test_closed_economy_live_control.py` 的 SDK + mock HTTP 测试，该环境缺少 `llm` extra。

串行段墙钟 488.99 秒，完成 87 个节点后主动中断以缩短剩余耗时（进程退出码 2，未完成用例计入后续批次）。其余 267 个节点分六组并行，以同一解释器执行，墙钟 224.33 秒，六组退出码均为 0。按 `--collect-only` 清单与七份 JUnit 中有名的节点键核对，每个测试节点恰有一份终态。同目录 `collected.txt`、`pytest-shard-plan.json`、`pytest-shard-*.{log,xml}` 与 `pytest-receipt.json` 保存选择、命令及实测结果。

## 5 期规则运行

| 命令 | 结果 | 进程墙钟 |
| --- | --- | --- |
| `run --until 2` | `checkpointed`，0→2 | 0.74 秒 |
| `resume` | `completed`，2→5 | 0.71 秒 |
| `replay` | `matched`，独立重新执行并核对 5 期，`applied_deltas_to_execution=false`，无分歧 | 0.64 秒 |
| 完整不中断 `run` | `completed`，0→5，与续跑的初始状态及 5 个提交状态 hash 逐期一致 | 0.75 秒 |

规则命令的 `external_api_calls=0`。配置 hash `73b5fbe95f797a882e243833a1e8e4cff147d0c8e17930f205667d45238fc623`。运行身份中的代码 hash `f0b13b86ef92f99a81b5d07fdf448e4a44a4ee46385a5b0010daca40d1b23b1d`。期末业务状态 hash `e0157af8d0ebb0b4979af3dbe8204aed4717a535756e525b59edb2671b7700f2`。

该例有 4 户、2 企业和 2 银行；两银行在第 1 期登记资本，家庭在第 2 期申请 12 个最小货币单位，第 3 期放款，第 4、5 期还款，财政于第 4、5 期各支付 1 单位转移。期末 journal 含 20 次工资支付、10 次生产、20 次采购和 20 次消费。对应本地 `run.json`、`resume.json`、`replay.json`、`state-comparison.json` 与 `credit/`。

## 配置差距探针

`build/convergence/01/configuration-probe.json` 保存以下实测。通过的两种配置各自创建一个 `economic_runtime`。

| 输入 | 结果 |
| --- | --- |
| `examples/closed-economy-credit-offer.json` | `ClosedEconomy`，唯一商品 `good`，含 2 银行及财政账户。 |
| 5 期、4 户、2 企业，`securities.shock_tick=2` | `SecurityMarket`，唯一商品槽为 `security:A`，主体 ID 为投资者与做市商。 |
| 信用/财政原配置 + `securities.shock_tick=2` | 拒绝。原文 `Security sessions require a pre-shock and later window and an exclusive funded asset`。 |
| 信用/财政原配置 + `demand.peer_uplift_units=1` | 拒绝。原文 `Consumption demand requires a fixed disclosed network and known offline labor inputs`。 |

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from pydantic import ValidationError
from civitas.application.closed_economy import build_closed_economy

credit = json.loads(Path("examples/closed-economy-credit-offer.json").read_text())
security = {"ticks": 5, "households": 4, "firms": 2, "fault_tick": None,
            "securities": {"shock_tick": 2}}
for raw in (credit, security, {**credit, "securities": security["securities"]},
            {**credit, "demand": {"peer_uplift_units": 1}}):
    try:
        world = build_closed_economy(raw)
    except ValidationError as error:
        print(error.errors()[0]["msg"])
    else:
        print(type(world.scenario).__name__, world.scenario.economy.good_id,
              world.scenario.economy.actor_ids, [p.participant_id for p in world.participants])
PY
```
